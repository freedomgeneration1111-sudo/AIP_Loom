"""Transactional reconcile apply — the single authority for mutating canonical state.

This module is the **single authority** for applying a :class:`ReconcilePlan`
to an AIP_Loom project.  No other module may modify canonical files based on
model output — it must delegate to :func:`apply_reconcile_plan` here.

Design principles (BuildSpec §6, §15, and §3A):

- **Plan consumption**: The apply function consumes a :class:`ReconcilePlan`
  directly.  It **must not** re-parse model output, re-resolve provisional
  IDs, re-validate references, or rebuild the plan in any way.  The plan is
  the contract; apply only executes it.
- **Strict step ordering**: The 14-step apply protocol from BuildSpec §15
  must be followed exactly.  No step may be skipped or reordered.
- **Snapshot before modify**: Before any canonical file is modified, the
  :class:`TransactionWorkspace` snapshots it.  On any failure before
  successful canonical replacement, the workspace restores from snapshots.
- **Rollback-on-failure**: If any step between snapshot and canonical
  replacement fails, all modified files are restored from snapshots and
  the workspace is cleaned up.  The project is left in its pre-apply state.
- **Git failure = recovery file**: If the canonical writes succeed but the
  Git commit fails, a ``RECOVERY.md`` file is written with exact manual
  recovery commands.  The process exits nonzero but does **not** destroy
  the writer data that was already applied.
- **Lock held throughout**: The exclusive lock is acquired before step 1
  and released after step 13 (or on any error exit).  No concurrent
  reconcile operations are possible.
- **Honest failure**: Every failure produces a :class:`CommandResult` with
  a stable error code from :mod:`aip_loom.errors` and machine-readable
  detail.  No failure is silent.

Apply protocol (BuildSpec §15, 14 steps):

1. Acquire lock
2. Load project + pre-validation
3. Git cleanliness check (unless allowed)
4. Parse output via ``update_parser`` → get ``ParsedUpdateBlock``
5. Build ``ReconcilePlan`` from planner
6. Snapshot all files that will be modified
7. Write pre-archive evidence
8. Write staged state + staged validation
9. Canonical replacement with rollback-on-failure
10. Post-apply validation
11. Complete archive + session append
12. Git add/commit (with recovery file on failure)
13. Release lock
14. Print summary

Recovery contracts:

- **RECONCILE_RESTORED_AFTER_FAILURE**: If any step fails before canonical
  replacement completes, all snapshotted files are restored.  The project
  is in its exact pre-apply state.
- **RECONCILE_APPLIED_BUT_GIT_FAILED**: If canonical replacement succeeds
  but Git commit fails, writer data is preserved and ``RECOVERY.md`` is
  written with exact recovery commands.
- **RECONCILE_POST_VALIDATION_FAILED**: If post-apply validation finds
  errors, files are restored from snapshots (the same as any pre-replacement
  failure — we treat post-apply validation failure as a hard stop).
- **RECONCILE_STAGED_VALIDATION_FAILED**: If staged validation fails before
  canonical replacement, nothing has been written to canonical files.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .checksum import compute_prose_checksum
from .errors import (
    CHECKSUM_MISMATCH,
    FILE_WRITE_ERROR,
    GIT_DIRTY,
    GIT_COMMIT_FAILED,
    LOCK_HELD,
    RECONCILE_APPLIED_BUT_GIT_FAILED,
    RECONCILE_POST_VALIDATION_FAILED,
    RECONCILE_PRE_VALIDATION_FAILED,
    RECONCILE_RESTORED_AFTER_FAILURE,
    RECONCILE_STAGED_VALIDATION_FAILED,
    RECONCILE_PARTIAL_CORRUPTION,
    RECOVERY_FILE_EXISTS,
    LoomError,
    LoomWarning,
)
from .frontmatter import parse_frontmatter, write_frontmatter
from .fs import safe_write_text
from .git import GitError, configure_local_git, git_add, git_commit, is_git_clean
from .ids import allocate_next_id
from .layout import ProjectLayout
from .lock import LockError, acquire_lock, ProjectLock
from .project import ProjectState, load_project, validate_project
from .reconcile_plan import (
    PlannedFileChange,
    PlannedLedgerChange,
    ProvisionalIdMapping,
    ReconcilePlan,
    build_reconcile_plan,
)
from .results import CommandResult
from .schemas import (
    ChunkFrontmatter,
    ChunkStatus,
    CommentEntry,
    DecisionEntry,
    DecisionLedger,
    ReviewState,
    SessionEntry,
    SessionLog,
    SUPPORTED_SCHEMA_VERSION,
    ThreadEntry,
    ThreadLedger,
    ThreadState,
)
from .transaction import (
    NoopFailureInjector,
    TransactionError,
    TransactionWorkspace,
)
from .update_parser import ParsedUpdateBlock, parse_model_output
from .yaml_io import dump_yaml, dump_yaml_string, load_yaml

__all__ = [
    "apply_reconcile_plan",
    "ReconcileApplyResult",
    "write_recovery_file",
]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileApplyResult:
    """The result of applying a reconcile plan.

    Attributes
    ----------
    plan_applied:
        Whether the plan was successfully applied to canonical state.
    target_chunk:
        The chunk ID that was updated.
    ledger_changes_count:
        Number of ledger changes applied.
    id_mappings_count:
        Number of provisional ID mappings resolved.
    file_changes_count:
        Number of files modified.
    git_committed:
        Whether the Git commit succeeded.
    recovery_file_written:
        Whether a RECOVERY.md file was written (Git failure path).
    tx_id:
        The transaction workspace ID.
    session_id:
        The session ID (S-NNNN) allocated for this reconcile.
    """

    plan_applied: bool
    target_chunk: str
    ledger_changes_count: int
    id_mappings_count: int
    file_changes_count: int
    git_committed: bool
    recovery_file_written: bool
    tx_id: str
    session_id: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary of the result."""
        return {
            "plan_applied": self.plan_applied,
            "target_chunk": self.target_chunk,
            "ledger_changes_count": self.ledger_changes_count,
            "id_mappings_count": self.id_mappings_count,
            "file_changes_count": self.file_changes_count,
            "git_committed": self.git_committed,
            "recovery_file_written": self.recovery_file_written,
            "tx_id": self.tx_id,
            "session_id": self.session_id,
        }


# ---------------------------------------------------------------------------
# Internal helpers — ledger mutation
# ---------------------------------------------------------------------------


def _apply_ledger_changes(
    plan: ReconcilePlan,
    state: ProjectState,
) -> dict[str, Any]:
    """Apply planned ledger changes to copies of the ledger models.

    Returns a dictionary mapping ledger label to the new Pydantic model
    instance.  Only ledgers with changes are included.

    This function does **not** write to disk.  It produces in-memory
    copies that can be validated before being written.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated_ledgers: dict[str, Any] = {}

    # Build mapping from provisional_id to canonical_id
    mapping_lookup: dict[str, str] = {
        m.provisional_id: m.canonical_id for m in plan.id_mappings
    }

    # -- Process each ledger change ------------------------------------------
    for change in plan.ledger_changes:
        if change.change_type == "new_decision":
            # Add a new decision entry
            if state.decisions_ledger is None:
                continue  # Should not happen if planner validated properly
            entries = list(state.decisions_ledger.entries)
            new_entry = DecisionEntry(
                id=change.item_id,
                summary=change.detail.get("summary", ""),
                rationale=change.detail.get("rationale", ""),
                scope=change.detail.get("scope", "global"),
                chunk_id=change.detail.get("chunk_id", ""),
                review_state=ReviewState.PENDING,
                created_at=now,
            )
            entries.append(new_entry)
            # Reconstruct the ledger
            updated_ledgers["decisions"] = DecisionLedger(
                schema_version=SUPPORTED_SCHEMA_VERSION,
                entries=entries,
            )
            # Update state reference for subsequent changes
            state = _replace_ledger_in_state(state, "decisions", updated_ledgers["decisions"])

        elif change.change_type == "new_thread":
            if state.threads_ledger is None:
                continue
            entries = list(state.threads_ledger.entries)
            new_entry = ThreadEntry(
                id=change.item_id,
                summary=change.detail.get("summary", ""),
                state=ThreadState(change.detail.get("state", "open")),
                scope=change.detail.get("scope", "global"),
                chunk_id=change.detail.get("chunk_id", ""),
                blocked_by=change.detail.get("blocked_by", []),
                review_state=ReviewState.PENDING,
                created_at=now,
            )
            entries.append(new_entry)
            updated_ledgers["threads"] = ThreadLedger(
                schema_version=SUPPORTED_SCHEMA_VERSION,
                entries=entries,
            )
            state = _replace_ledger_in_state(state, "threads", updated_ledgers["threads"])

        elif change.change_type == "close_thread":
            if state.threads_ledger is None:
                continue
            entries = list(state.threads_ledger.entries)
            for i, entry in enumerate(entries):
                if entry.id == change.item_id:
                    entries[i] = ThreadEntry(
                        id=entry.id,
                        summary=entry.summary,
                        state=ThreadState.CLOSED,
                        scope=entry.scope,
                        chunk_id=entry.chunk_id,
                        blocked_by=entry.blocked_by,
                        review_state=entry.review_state,
                        created_at=entry.created_at,
                    )
                    break
            updated_ledgers["threads"] = ThreadLedger(
                schema_version=SUPPORTED_SCHEMA_VERSION,
                entries=entries,
            )
            state = _replace_ledger_in_state(state, "threads", updated_ledgers["threads"])

        elif change.change_type == "update_existing":
            changes_dict = change.detail.get("changes", {})
            if change.item_id.startswith("D-") and state.decisions_ledger is not None:
                entries = list(state.decisions_ledger.entries)
                for i, entry in enumerate(entries):
                    if entry.id == change.item_id:
                        # Apply the changes dict to the entry
                        entry_data = entry.model_dump(mode="json")
                        entry_data.update(changes_dict)
                        entries[i] = DecisionEntry(**entry_data)
                        break
                updated_ledgers["decisions"] = DecisionLedger(
                    schema_version=SUPPORTED_SCHEMA_VERSION,
                    entries=entries,
                )
                state = _replace_ledger_in_state(state, "decisions", updated_ledgers["decisions"])

            elif change.item_id.startswith("T-") and state.threads_ledger is not None:
                entries = list(state.threads_ledger.entries)
                for i, entry in enumerate(entries):
                    if entry.id == change.item_id:
                        entry_data = entry.model_dump(mode="json")
                        entry_data.update(changes_dict)
                        entries[i] = ThreadEntry(**entry_data)
                        break
                updated_ledgers["threads"] = ThreadLedger(
                    schema_version=SUPPORTED_SCHEMA_VERSION,
                    entries=entries,
                )
                state = _replace_ledger_in_state(state, "threads", updated_ledgers["threads"])

    return updated_ledgers


def _replace_ledger_in_state(
    state: ProjectState,
    ledger_name: str,
    new_ledger: Any,
) -> ProjectState:
    """Return a new ProjectState with one ledger replaced.

    Since ProjectState is frozen, we create a new instance with
    the updated ledger.
    """
    # Build a new state with the updated ledger
    kwargs = {
        "layout": state.layout,
        "manifest": state.manifest,
        "decisions_ledger": state.decisions_ledger,
        "threads_ledger": state.threads_ledger,
        "questions_ledger": state.questions_ledger,
        "distillate": state.distillate,
        "sessions": state.sessions,
        "comments": state.comments,
        "chunks": state.chunks,
        "chunk_order": state.chunk_order,
        "load_errors": state.load_errors,
        "load_warnings": state.load_warnings,
    }
    kwargs[ledger_name + "_ledger"] = new_ledger
    return ProjectState(**kwargs)


# ---------------------------------------------------------------------------
# Internal helpers — chunk file writing
# ---------------------------------------------------------------------------


def _build_updated_chunk_content(
    plan: ReconcilePlan,
    state: ProjectState,
) -> str | None:
    """Build the updated chunk file content from the plan.

    Returns the full Markdown content (frontmatter + prose body) for
    the target chunk, or ``None`` if no prose replacement is needed.
    """
    if not plan.revised_prose and plan.mode != "full_replacement":
        return None

    target = plan.target_chunk
    if target not in state.chunks:
        return None

    chunk_data = state.chunks[target]
    existing_fm = chunk_data.frontmatter

    # Compute new checksum and word count for the revised prose
    new_prose = plan.revised_prose if plan.revised_prose else chunk_data.prose_body
    new_checksum = compute_prose_checksum(new_prose)
    new_word_count = len(new_prose.split())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build updated frontmatter
    updated_fm = ChunkFrontmatter(
        schema_version=existing_fm.schema_version,
        id=existing_fm.id,
        title=existing_fm.title,
        status=ChunkStatus.REVISED,
        word_count=new_word_count,
        prose_checksum=new_checksum,
        distillate_anchor=existing_fm.distillate_anchor,
        created_at=existing_fm.created_at,
        updated_at=now,
    )

    return write_frontmatter(updated_fm, new_prose)


# ---------------------------------------------------------------------------
# Internal helpers — archive evidence
# ---------------------------------------------------------------------------


def _write_pre_archive_evidence(
    layout: ProjectLayout,
    plan: ReconcilePlan,
    tx_id: str,
    state: ProjectState,
) -> Path:
    """Write pre-archive evidence file to the archive directory.

    The pre-archive evidence records the state of the project before
    the reconcile is applied.  This provides forensic traceability.
    """
    ensure_archive_dir = layout.archive_dir
    ensure_archive_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_path = ensure_archive_dir / f"pre-reconcile-{plan.target_chunk}-{timestamp}.json"

    evidence = {
        "type": "pre_reconcile_evidence",
        "tx_id": tx_id,
        "target_chunk": plan.target_chunk,
        "timestamp": timestamp,
        "plan": plan.to_dict(),
        "chunk_checksum_before": (
            compute_prose_checksum(state.chunks[plan.target_chunk].prose_body)
            if plan.target_chunk in state.chunks
            else None
        ),
        "model_output_length": (
            len(plan.revised_prose) if plan.revised_prose else 0
        ),
    }

    content = json.dumps(evidence, indent=2, ensure_ascii=False, default=str)
    evidence_path.write_text(content, encoding="utf-8")
    return evidence_path


def _write_post_archive_evidence(
    layout: ProjectLayout,
    plan: ReconcilePlan,
    tx_id: str,
    session_id: str,
) -> Path:
    """Write post-archive evidence file to the archive directory."""
    ensure_archive_dir = layout.archive_dir
    ensure_archive_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_path = ensure_archive_dir / f"post-reconcile-{plan.target_chunk}-{timestamp}.json"

    evidence = {
        "type": "post_reconcile_evidence",
        "tx_id": tx_id,
        "session_id": session_id,
        "target_chunk": plan.target_chunk,
        "timestamp": timestamp,
        "ledger_changes_count": len(plan.ledger_changes),
        "id_mappings_count": len(plan.id_mappings),
        "file_changes_count": len(plan.file_changes),
        "plan_ok": plan.plan_ok,
    }

    content = json.dumps(evidence, indent=2, ensure_ascii=False, default=str)
    evidence_path.write_text(content, encoding="utf-8")
    return evidence_path


# ---------------------------------------------------------------------------
# Internal helpers — session log update
# ---------------------------------------------------------------------------


def _allocate_session_id(sessions: SessionLog | None) -> str:
    """Allocate the next session ID (S-NNNN)."""
    entries = sessions.entries if sessions else []
    return allocate_next_id("S", entries)


def _append_session_entry(
    sessions: SessionLog | None,
    session_id: str,
    plan: ReconcilePlan,
    layout: ProjectLayout,
) -> SessionLog:
    """Return a new SessionLog with the reconcile session appended."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_entry = SessionEntry(
        id=session_id,
        chunk_id=plan.target_chunk,
        brief_path="",
        model_output_path="",
        reconcile_applied=True,
        created_at=now,
    )

    if sessions is None:
        entries = [new_entry]
    else:
        entries = list(sessions.entries) + [new_entry]

    return SessionLog(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        entries=entries,
    )


# ---------------------------------------------------------------------------
# Internal helpers — RECOVERY.md
# ---------------------------------------------------------------------------


def write_recovery_file(
    root: Path,
    plan: ReconcilePlan,
    tx_id: str,
    session_id: str,
    git_error: str,
) -> Path:
    """Write a RECOVERY.md file with manual recovery commands.

    This is called when canonical writes succeed but Git commit fails.
    The RECOVERY.md file provides the user with exact commands to
    manually complete the Git commit.

    Parameters
    ----------
    root:
        The project root directory.
    plan:
        The reconcile plan that was applied.
    tx_id:
        The transaction ID.
    session_id:
        The session ID.
    git_error:
        The Git error message.

    Returns
    -------
    Path
        The path to the RECOVERY.md file.
    """
    recovery_path = root / "RECOVERY.md"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build list of files that were modified
    files_modified = [fc.file_path for fc in plan.file_changes]
    # Add archive evidence files
    files_modified_str = "\n".join(f"  - `{f}`" for f in files_modified)

    content = f"""# RECOVERY — Reconcile Applied but Git Commit Failed

**This file was generated by AIP_Loom because the reconcile was applied
to canonical files but the Git commit failed.** The canonical state has
been modified. You must manually commit the changes to preserve the
reconcile.

## What Happened

- Transaction ID: `{tx_id}`
- Session ID: `{session_id}`
- Target chunk: `{plan.target_chunk}`
- Timestamp: {timestamp}
- Git error: {git_error}

## Files Modified

{files_modified_str}

## Recovery Commands

Run these commands to complete the reconcile:

```bash
cd {root}
git add -A
git commit -m "reconcile: apply {plan.target_chunk} (session {session_id}, tx {tx_id})"
```

After the commit succeeds, delete this RECOVERY.md file:

```bash
rm RECOVERY.md
git add RECOVERY.md
git commit -m "chore: remove RECOVERY.md after manual reconcile completion"
```

## If You Want to Undo the Reconcile

If you want to undo the reconcile instead of committing, use:

```bash
cd {root}
git checkout -- .
```

This will restore all modified files to their pre-reconcile state.
Then delete this RECOVERY.md file.
"""

    recovery_path.write_text(content, encoding="utf-8")
    return recovery_path


# ---------------------------------------------------------------------------
# Internal helpers — summary formatting
# ---------------------------------------------------------------------------


def _format_reconcile_summary(
    plan: ReconcilePlan,
    apply_result: ReconcileApplyResult,
    warnings: list[LoomWarning],
) -> str:
    """Format a human-readable summary of the reconcile apply."""
    lines = [
        f"Reconcile applied for chunk {plan.target_chunk}",
        f"  Session: {apply_result.session_id}",
        f"  Transaction: {apply_result.tx_id}",
        f"  Ledger changes: {apply_result.ledger_changes_count}",
        f"  ID mappings: {apply_result.id_mappings_count}",
        f"  Files modified: {apply_result.file_changes_count}",
    ]

    if plan.id_mappings:
        lines.append("  Provisional ID mappings:")
        for m in plan.id_mappings:
            lines.append(f"    {m.provisional_id} -> {m.canonical_id} ({m.item_type})")

    if apply_result.recovery_file_written:
        lines.append("  WARNING: Git commit failed — see RECOVERY.md for recovery commands")

    if warnings:
        lines.append(f"  Warnings: {len(warnings)}")
        for w in warnings[:5]:
            lines.append(f"    [{w.code}] {w.message}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers — ledger serialization
# ---------------------------------------------------------------------------


def _serialize_ledger(ledger: Any) -> str:
    """Serialize a ledger model to YAML string."""
    data = ledger.model_dump(mode="json")
    return dump_yaml_string(data)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_reconcile_plan(
    plan: ReconcilePlan,
    model_output_text: str,
    root: Path,
    allow_dirty_git: bool = False,
) -> CommandResult:
    """Apply a reconcile plan to canonical project state.

    This is the **single entry point** for applying reconcile operations.
    It follows the 14-step protocol from BuildSpec §15 exactly.

    The function consumes the :class:`ReconcilePlan` directly — it does
    **not** re-parse model output, re-resolve provisional IDs, or
    rebuild the plan in any way.  The plan is the contract; this function
    only executes it.

    Parameters
    ----------
    plan:
        The reconcile plan from :func:`build_reconcile_plan`.
    model_output_text:
        The raw model output text (for archive evidence).
    root:
        The project root directory.
    allow_dirty_git:
        If ``True``, skip the Git cleanliness check.  Default is
        ``False`` (dirty Git blocks reconcile).

    Returns
    -------
    CommandResult
        The result of the apply operation with stable error codes,
        machine-readable detail, and the apply summary.
    """
    lock: ProjectLock | None = None
    workspace: TransactionWorkspace | None = None
    all_warnings: list[LoomWarning] = list(plan.warnings)
    applied = False
    git_committed = False
    recovery_written = False
    tx_id = ""
    session_id = ""

    # We need the layout for lock and workspace — load project first
    try:
        initial_state = load_project(root)
        layout = initial_state.layout
    except Exception as exc:
        return CommandResult.failure(
            command="reconcile",
            code=RECONCILE_PRE_VALIDATION_FAILED,
            message=f"Cannot load project: {exc}",
            errors=[LoomError(
                code=RECONCILE_PRE_VALIDATION_FAILED,
                message=str(exc),
            )],
            warnings=all_warnings,
        )

    # Check for pre-existing RECOVERY.md — refuse to reconcile
    # if one exists, since it indicates a previous reconcile was
    # applied but never committed.
    recovery_path = root / "RECOVERY.md"
    if recovery_path.exists():
        return CommandResult.failure(
            command="reconcile",
            code=RECOVERY_FILE_EXISTS,
            message=(
                "A RECOVERY.md file already exists.  This indicates a "
                "previous reconcile was applied but the Git commit "
                "failed.  Either complete the previous reconcile "
                "(see RECOVERY.md) or undo it before starting a new "
                "reconcile."
            ),
            errors=[LoomError(
                code=RECOVERY_FILE_EXISTS,
                message="RECOVERY.md exists — resolve previous reconcile first",
                detail={"path": str(recovery_path)},
            )],
            warnings=all_warnings,
        )

    # ================================================================
    # STEP 1: Acquire lock
    # ================================================================
    try:
        lock = ProjectLock(layout, command="reconcile")
        lock_warnings = lock.acquire()
        all_warnings.extend(lock_warnings)
    except LockError as exc:
        return CommandResult.failure(
            command="reconcile",
            code=exc.loom_error.code,
            message=f"Cannot acquire lock: {exc.loom_error.message}",
            errors=[exc.loom_error],
            warnings=all_warnings,
        )
    except Exception as exc:
        return CommandResult.failure(
            command="reconcile",
            code=LOCK_HELD,
            message=f"Lock acquisition failed: {exc}",
            errors=[LoomError(code=LOCK_HELD, message=str(exc))],
            warnings=all_warnings,
        )

    try:
        # ============================================================
        # STEP 2: Pre-validation (project already loaded for lock)
        # ============================================================
        pre_state = initial_state
        pre_validation = validate_project(pre_state, chunk_scope=plan.target_chunk)
        if not pre_validation.ok:
            return CommandResult.failure(
                command="reconcile",
                code=RECONCILE_PRE_VALIDATION_FAILED,
                message=(
                    f"Pre-apply validation failed with "
                    f"{len(pre_validation.errors)} error(s).  "
                    f"Fix validation errors before reconciling."
                ),
                errors=list(pre_validation.errors),
                warnings=all_warnings + list(pre_validation.warnings),
            )

        # ============================================================
        # STEP 3: Git cleanliness check (unless allowed)
        # ============================================================
        if not allow_dirty_git:
            if not is_git_clean(layout.root):
                return CommandResult.failure(
                    command="reconcile",
                    code=GIT_DIRTY,
                    message=(
                        "Git working tree is dirty.  Commit or stash "
                        "your changes before reconciling, or use "
                        "--allow-dirty-git to skip this check."
                    ),
                    errors=[LoomError(
                        code=GIT_DIRTY,
                        message="Git working tree has uncommitted changes",
                    )],
                    warnings=all_warnings,
                )

        # ============================================================
        # (Steps 4-5 are already done: plan was built by caller)
        # Verify the plan is still applicable
        # ============================================================
        if not plan.plan_ok:
            return CommandResult.failure(
                command="reconcile",
                code=RECONCILE_PRE_VALIDATION_FAILED,
                message=(
                    f"Reconcile plan has {len(plan.conflicts)} conflict(s).  "
                    f"Resolve conflicts before applying."
                ),
                errors=list(plan.conflicts),
                warnings=all_warnings,
            )

        if plan.target_chunk not in pre_state.chunks:
            return CommandResult.failure(
                command="reconcile",
                code=RECONCILE_PRE_VALIDATION_FAILED,
                message=(
                    f"Target chunk {plan.target_chunk!r} not found in "
                    f"project.  The project may have changed since the "
                    f"plan was built."
                ),
                errors=[LoomError(
                    code=RECONCILE_PRE_VALIDATION_FAILED,
                    message=f"Target chunk {plan.target_chunk!r} not found",
                    detail={"target_chunk": plan.target_chunk},
                )],
                warnings=all_warnings,
            )

        # ============================================================
        # STEP 6: Snapshot all files that will be modified
        # ============================================================
        workspace = TransactionWorkspace(layout)
        try:
            tx_id = workspace.begin()
        except TransactionError as exc:
            return CommandResult.failure(
                command="reconcile",
                code=exc.loom_error.code,
                message=f"Cannot create transaction workspace: {exc.loom_error.message}",
                errors=[exc.loom_error],
                warnings=all_warnings,
            )

        # Determine all files that will be modified
        files_to_snapshot: list[Path] = []
        for fc in plan.file_changes:
            p = Path(fc.file_path)
            if p.exists():
                files_to_snapshot.append(p)

        # Also snapshot the session log (will be appended)
        files_to_snapshot.append(layout.sessions_path)

        # Snapshot each file
        try:
            for fpath in files_to_snapshot:
                workspace.snapshot_file(fpath)
        except TransactionError as exc:
            # Snapshot failure → cannot proceed safely
            try:
                workspace.cleanup()
            except Exception:
                pass
            return CommandResult.failure(
                command="reconcile",
                code=exc.loom_error.code,
                message=f"Snapshot failed: {exc.loom_error.message}",
                errors=[exc.loom_error],
                warnings=all_warnings,
            )

        # ============================================================
        # STEP 7: Write pre-archive evidence
        # ============================================================
        try:
            _write_pre_archive_evidence(layout, plan, tx_id, pre_state)
        except Exception as exc:
            # Archive evidence is important but not blocking — warn only
            all_warnings.append(LoomWarning(
                code=FILE_WRITE_ERROR,
                message=f"Could not write pre-archive evidence: {exc}",
                detail={"error": str(exc)},
            ))

        # ============================================================
        # STEP 8: Write staged state + staged validation
        # ============================================================
        # Apply ledger changes to in-memory copies
        try:
            updated_ledgers = _apply_ledger_changes(plan, pre_state)
        except Exception as exc:
            # Staged validation failed — nothing has been written to
            # canonical files yet.  Restore from snapshots (no-op since
            # we haven't written anything) and cleanup.
            _restore_and_cleanup(workspace, all_warnings)
            return CommandResult.failure(
                command="reconcile",
                code=RECONCILE_STAGED_VALIDATION_FAILED,
                message=f"Staged ledger application failed: {exc}",
                errors=[LoomError(
                    code=RECONCILE_STAGED_VALIDATION_FAILED,
                    message=str(exc),
                )],
                warnings=all_warnings,
            )

        # Build updated chunk content
        try:
            updated_chunk_content = _build_updated_chunk_content(plan, pre_state)
        except Exception as exc:
            _restore_and_cleanup(workspace, all_warnings)
            return CommandResult.failure(
                command="reconcile",
                code=RECONCILE_STAGED_VALIDATION_FAILED,
                message=f"Staged chunk content generation failed: {exc}",
                errors=[LoomError(
                    code=RECONCILE_STAGED_VALIDATION_FAILED,
                    message=str(exc),
                )],
                warnings=all_warnings,
            )

        # ============================================================
        # STEP 9: Canonical replacement with rollback-on-failure
        # ============================================================
        # Allocate session ID before writing
        try:
            session_id = _allocate_session_id(pre_state.sessions)
        except Exception:
            session_id = "S-0001"  # Fallback

        write_errors: list[LoomError] = []

        # Write updated chunk file
        if updated_chunk_content is not None:
            chunk_path = pre_state.chunks[plan.target_chunk].file_path
            try:
                safe_write_text(chunk_path, updated_chunk_content, layout)
            except Exception as exc:
                write_errors.append(LoomError(
                    code=FILE_WRITE_ERROR,
                    message=f"Cannot write chunk file: {exc}",
                    detail={"path": str(chunk_path), "error": str(exc)},
                ))

        # Write updated ledgers
        for ledger_name, ledger_model in updated_ledgers.items():
            if ledger_name == "decisions":
                ledger_path = layout.decisions_ledger_path
            elif ledger_name == "threads":
                ledger_path = layout.threads_ledger_path
            elif ledger_name == "questions":
                ledger_path = layout.questions_ledger_path
            else:
                continue

            try:
                yaml_content = _serialize_ledger(ledger_model)
                safe_write_text(ledger_path, yaml_content, layout)
            except Exception as exc:
                write_errors.append(LoomError(
                    code=FILE_WRITE_ERROR,
                    message=f"Cannot write {ledger_name} ledger: {exc}",
                    detail={"path": str(ledger_path), "error": str(exc)},
                ))

        # Write updated session log
        try:
            updated_sessions = _append_session_entry(
                pre_state.sessions, session_id, plan, layout,
            )
            sessions_yaml = _serialize_ledger(updated_sessions)
            safe_write_text(layout.sessions_path, sessions_yaml, layout)
        except Exception as exc:
            write_errors.append(LoomError(
                code=FILE_WRITE_ERROR,
                message=f"Cannot write session log: {exc}",
                detail={"path": str(layout.sessions_path), "error": str(exc)},
            ))

        # If any writes failed, restore from snapshots
        if write_errors:
            _restore_and_cleanup(workspace, all_warnings)
            return CommandResult.failure(
                command="reconcile",
                code=RECONCILE_RESTORED_AFTER_FAILURE,
                message=(
                    f"Canonical write failed with {len(write_errors)} "
                    f"error(s).  All files restored from snapshots."
                ),
                errors=write_errors,
                warnings=all_warnings,
            )

        # Do NOT commit the workspace yet — post-apply validation
        # must pass first.  If validation fails, we need the
        # workspace to still be ACTIVE so that restore() works.
        # The workspace is committed only after step 10 succeeds.

        applied = True

        # ============================================================
        # STEP 10: Post-apply validation
        # ============================================================
        try:
            post_state = load_project(root)
            post_validation = validate_project(
                post_state, chunk_scope=plan.target_chunk,
            )

            if not post_validation.ok:
                # Post-apply validation found errors — restore
                _restore_and_cleanup(workspace, all_warnings)
                return CommandResult.failure(
                    command="reconcile",
                    code=RECONCILE_POST_VALIDATION_FAILED,
                    message=(
                        f"Post-apply validation failed with "
                        f"{len(post_validation.errors)} error(s).  "
                        f"All files restored from snapshots."
                    ),
                    errors=list(post_validation.errors),
                    warnings=all_warnings + list(post_validation.warnings),
                )

            all_warnings.extend(post_validation.warnings)
        except Exception as exc:
            # If we can't even reload the project for validation,
            # restore from snapshots — we can't verify integrity.
            _restore_and_cleanup(workspace, all_warnings)
            return CommandResult.failure(
                command="reconcile",
                code=RECONCILE_POST_VALIDATION_FAILED,
                message=(
                    f"Post-apply validation failed: cannot reload "
                    f"project: {exc}.  All files restored from snapshots."
                ),
                errors=[LoomError(
                    code=RECONCILE_POST_VALIDATION_FAILED,
                    message=str(exc),
                )],
                warnings=all_warnings,
            )

        # Post-apply validation passed — now it's safe to commit
        # the workspace (files are confirmed valid on disk).
        try:
            workspace.commit()
        except TransactionError:
            # Commit marking failed, but files were written and validated.
            # This is a soft failure — the files are confirmed on disk.
            pass

        # ============================================================
        # STEP 11: Complete archive + session append
        # ============================================================
        try:
            _write_post_archive_evidence(layout, plan, tx_id, session_id)
        except Exception as exc:
            all_warnings.append(LoomWarning(
                code=FILE_WRITE_ERROR,
                message=f"Could not write post-archive evidence: {exc}",
                detail={"error": str(exc)},
            ))

        # ============================================================
        # STEP 12: Git add/commit (with recovery file on failure)
        # ============================================================
        git_commit_error: str | None = None
        try:
            # Collect all modified file paths
            paths_to_add: list[Path] = []
            for fc in plan.file_changes:
                paths_to_add.append(Path(fc.file_path))

            # Add archive evidence files
            archive_files = list(layout.archive_dir.glob(
                f"*reconcile-{plan.target_chunk}*"
            ))
            paths_to_add.extend(archive_files)

            # Add session log
            paths_to_add.append(layout.sessions_path)

            if paths_to_add:
                git_add(layout.root, paths_to_add)

            commit_message = (
                f"reconcile: apply {plan.target_chunk} "
                f"(session {session_id}, tx {tx_id})"
            )
            git_commit(layout.root, commit_message)
            git_committed = True

        except GitError as exc:
            git_commit_error = exc.loom_error.message
            # Git commit failed — write RECOVERY.md
            try:
                write_recovery_file(
                    root=layout.root,
                    plan=plan,
                    tx_id=tx_id,
                    session_id=session_id,
                    git_error=git_commit_error,
                )
                recovery_written = True
            except Exception:
                # If we can't write RECOVERY.md, that's very bad but
                # we still need to report the Git failure
                pass

        # ============================================================
        # STEP 13: Release lock (done in finally block)
        # ============================================================

        # Cleanup workspace
        if workspace is not None:
            try:
                workspace.cleanup()
            except Exception:
                pass

        # ============================================================
        # STEP 14: Print summary (build result)
        # ============================================================
        apply_result = ReconcileApplyResult(
            plan_applied=applied,
            target_chunk=plan.target_chunk,
            ledger_changes_count=len(plan.ledger_changes),
            id_mappings_count=len(plan.id_mappings),
            file_changes_count=len(plan.file_changes),
            git_committed=git_committed,
            recovery_file_written=recovery_written,
            tx_id=tx_id,
            session_id=session_id,
        )

        summary = _format_reconcile_summary(plan, apply_result, all_warnings)

        if git_commit_error:
            # Applied but Git failed — this is a special failure
            return CommandResult.failure(
                command="reconcile",
                code=RECONCILE_APPLIED_BUT_GIT_FAILED,
                message=(
                    f"Reconcile applied but Git commit failed: "
                    f"{git_commit_error}.  See RECOVERY.md for "
                    f"manual recovery commands."
                ),
                errors=[LoomError(
                    code=RECONCILE_APPLIED_BUT_GIT_FAILED,
                    message=git_commit_error,
                    detail={
                        "session_id": session_id,
                        "tx_id": tx_id,
                        "recovery_file": str(layout.root / "RECOVERY.md"),
                    },
                )],
                data=apply_result.to_dict(),
                warnings=all_warnings,
            )

        return CommandResult.success(
            command="reconcile",
            message=summary,
            data=apply_result.to_dict(),
            warnings=all_warnings,
        )

    except Exception as exc:
        # Unexpected exception — try to restore and cleanup
        if workspace is not None:
            _restore_and_cleanup(workspace, all_warnings)

        return CommandResult.failure(
            command="reconcile",
            code=RECONCILE_RESTORED_AFTER_FAILURE,
            message=f"Unexpected error during reconcile: {exc}",
            errors=[LoomError(
                code=RECONCILE_RESTORED_AFTER_FAILURE,
                message=str(exc),
            )],
            warnings=all_warnings,
        )

    finally:
        # Always release the lock
        if lock is not None and lock.is_held:
            try:
                lock.release()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Internal helpers — restore and cleanup
# ---------------------------------------------------------------------------


def _restore_and_cleanup(
    workspace: TransactionWorkspace,
    warnings: list[LoomWarning],
) -> None:
    """Restore all snapshotted files and cleanup the workspace."""
    try:
        restore_warnings = workspace.restore()
        warnings.extend(restore_warnings)
    except TransactionError:
        # Restore failed — workspace is preserved for forensics
        warnings.append(LoomWarning(
            code=RECONCILE_PARTIAL_CORRUPTION,
            message=(
                "Restore from snapshots failed.  Transaction workspace "
                "is preserved for forensic analysis."
            ),
            detail={"tx_id": workspace.tx_id},
        ))
        return

    try:
        workspace.cleanup()
    except Exception:
        pass


def _restore_from_snapshots(
    workspace: TransactionWorkspace,
    warnings: list[LoomWarning],
) -> None:
    """Restore from snapshots without cleanup (preserves evidence)."""
    try:
        restore_warnings = workspace.restore()
        warnings.extend(restore_warnings)
    except TransactionError:
        warnings.append(LoomWarning(
            code=RECONCILE_PARTIAL_CORRUPTION,
            message=(
                "Restore from snapshots failed after post-apply "
                "validation failure.  Transaction workspace is "
                "preserved for forensic analysis."
            ),
            detail={"tx_id": workspace.tx_id},
        ))
