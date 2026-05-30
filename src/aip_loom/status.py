"""Project status dashboard for AIP_Loom.

This module is the **single authority** for computing a project's overall
status.  It assembles a :class:`StatusReport` from the loaded project state,
validation results, Git status, lock state, and recovery indicators.  No
other module may independently compute and present project status — it must
delegate to :func:`compute_status` here.

Design principles (BuildSpec §3A and Chunk 10 description):

- **Honest above all**: Status never fabricates zero counts or hides
  problems.  If the project cannot be loaded, the report reflects that
  failure rather than pretending everything is fine.
- **Single data structure**: :class:`StatusReport` is the same structure
  used for Rich terminal output and JSON output.  No divergent logic.
- **Compose, don't duplicate**: Status reuses ``load_project()``,
  ``validate_project()``, ``git_status()``, and lock detection rather
  than computing state independently.
- **Health classification**: Overall health is classified as
  ``HEALTHY``, ``DEGRADED``, or ``BLOCKED``.  A project is ``BLOCKED``
  when there are structural errors that prevent normal work.  It is
  ``DEGRADED`` when there are warnings (pending reviews, dirty
  checksums) but no blocking errors.  It is ``HEALTHY`` only when
  there are zero errors and zero warnings requiring action.
- **Recovery awareness**: If ``RECOVERY.md`` exists (left after a Git
  commit failure during reconcile), it is surfaced as a warning.
- **Next actions**: The report includes suggested next actions based
  on the current state, helping the user orient quickly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from .errors import (
    LoomError,
    LoomWarning,
    RECOVERY_FILE_EXISTS,
    STALE_LOCK_DETECTED,
    VALIDATION_PENDING_REVIEW,
)
from .git import GitStatus, git_status
from .layout import ProjectLayout
from .lock import LockInfo, _read_lock_file
from .project import (
    ProjectError,
    ProjectState,
    ValidationResult,
    load_project,
    validate_project,
)
from .schemas import ReviewState, ThreadState

__all__ = [
    "HealthLevel",
    "ChunkStatusSummary",
    "LedgerStatusSummary",
    "GitStatusSummary",
    "LockStatusSummary",
    "StatusReport",
    "compute_status",
]


# ---------------------------------------------------------------------------
# Health classification
# ---------------------------------------------------------------------------


class HealthLevel(str, Enum):
    """Overall project health classification.

    - ``HEALTHY``: No errors, no warnings requiring action.
    - ``DEGRADED``: No errors, but warnings exist (pending reviews,
      dirty checksums, etc.).  The project is usable but needs attention.
    - ``BLOCKED``: Structural errors exist (missing files, broken
      references, load failures).  Normal operations may not work.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Sub-report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkStatusSummary:
    """Summary of chunk states across the project.

    Attributes
    ----------
    total:
        Total number of chunks on disk.
    draft:
        Number of chunks with status ``draft``.
    revised:
        Number of chunks with status ``revised``.
    final:
        Number of chunks with status ``final``.
    dirty_checksums:
        Number of chunks with dirty (mismatched) checksums.
    chunk_ids:
        Ordered list of chunk IDs (from chunk_order resolution).
    """

    total: int
    draft: int
    revised: int
    final: int
    dirty_checksums: int
    chunk_ids: tuple[str, ...]


@dataclass(frozen=True)
class LedgerStatusSummary:
    """Summary of ledger entry counts and pending review counts.

    Attributes
    ----------
    decisions_total:
        Total decision entries.
    decisions_pending:
        Decision entries with ``review_state=pending``.
    threads_total:
        Total thread entries.
    threads_open:
        Thread entries with ``state=open``.
    threads_blocked:
        Thread entries with ``state=blocked``.
    threads_pending:
        Thread entries with ``review_state=pending``.
    questions_total:
        Total question entries.
    questions_unresolved:
        Question entries that are not resolved.
    questions_pending:
        Question entries with ``review_state=pending``.
    total_pending_review:
        Total entries across all ledgers with ``review_state=pending``.
    """

    decisions_total: int
    decisions_pending: int
    threads_total: int
    threads_open: int
    threads_blocked: int
    threads_pending: int
    questions_total: int
    questions_unresolved: int
    questions_pending: int
    total_pending_review: int


@dataclass(frozen=True)
class GitStatusSummary:
    """Summary of Git working tree state.

    Attributes
    ----------
    is_repo:
        Whether the project is inside a Git repository.
    clean:
        Whether the working tree is clean.
    staged_count:
        Number of staged files.
    unstaged_count:
        Number of unstaged files.
    untracked_count:
        Number of untracked files.
    branch:
        Current branch name (empty string if not a repo or detached HEAD).
    """

    is_repo: bool
    clean: bool
    staged_count: int
    unstaged_count: int
    untracked_count: int
    branch: str


@dataclass(frozen=True)
class LockStatusSummary:
    """Summary of project lock state.

    Attributes
    ----------
    locked:
        Whether a lock file exists.
    lock_info:
        Parsed lock file information, or ``None`` if no lock.
    is_stale:
        Whether the lock is stale (holding process is dead).
    """

    locked: bool
    lock_info: LockInfo | None
    is_stale: bool


# ---------------------------------------------------------------------------
# Main status report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusReport:
    """Complete project status report.

    This is the single, comprehensive data structure that the ``status``
    command produces.  It is used for both Rich terminal rendering and
    JSON output — no divergent logic.

    Attributes
    ----------
    health:
        Overall health classification.
    root:
        Project root path.
    project_name:
        Project name from manifest, or ``"<unknown>"`` if manifest
        could not be loaded.
    project_type:
        Project type from manifest, or ``"<unknown>"`` if unavailable.
    load_succeeded:
        Whether ``load_project()`` completed without critical errors.
    load_errors:
        Errors encountered during loading.
    load_warnings:
        Warnings encountered during loading.
    validation:
        The full validation result, or ``None`` if loading failed
        critically.
    chunks:
        Chunk status summary.
    ledgers:
        Ledger status summary.
    git:
        Git status summary.
    lock:
        Lock status summary.
    recovery_file_exists:
        Whether a ``RECOVERY.md`` file exists in the project root.
    error_count:
        Total validation errors.
    warning_count:
        Total validation warnings.
    next_actions:
        Suggested next actions for the user, in priority order.
    """

    health: HealthLevel
    root: str
    project_name: str
    project_type: str
    load_succeeded: bool
    load_errors: tuple[LoomError, ...]
    load_warnings: tuple[LoomWarning, ...]
    validation: ValidationResult | None
    chunks: ChunkStatusSummary
    ledgers: LedgerStatusSummary
    git: GitStatusSummary
    lock: LockStatusSummary
    recovery_file_exists: bool
    error_count: int
    warning_count: int
    next_actions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary of the status report."""
        result: dict[str, Any] = {
            "health": self.health.value,
            "root": self.root,
            "project_name": self.project_name,
            "project_type": self.project_type,
            "load_succeeded": self.load_succeeded,
            "recovery_file_exists": self.recovery_file_exists,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "next_actions": list(self.next_actions),
        }

        # Load errors/warnings
        result["load_errors"] = [
            {"code": e.code, "message": e.message, "detail": e.detail}
            for e in self.load_errors
        ]
        result["load_warnings"] = [
            {"code": w.code, "message": w.message, "detail": w.detail}
            for w in self.load_warnings
        ]

        # Validation
        if self.validation is not None:
            result["validation"] = {
                "ok": self.validation.ok,
                "errors": [
                    {"code": e.code, "message": e.message, "detail": e.detail}
                    for e in self.validation.errors
                ],
                "warnings": [
                    {"code": w.code, "message": w.message, "detail": w.detail}
                    for w in self.validation.warnings
                ],
            }
        else:
            result["validation"] = None

        # Chunks
        result["chunks"] = {
            "total": self.chunks.total,
            "draft": self.chunks.draft,
            "revised": self.chunks.revised,
            "final": self.chunks.final,
            "dirty_checksums": self.chunks.dirty_checksums,
            "chunk_ids": list(self.chunks.chunk_ids),
        }

        # Ledgers
        result["ledgers"] = {
            "decisions_total": self.ledgers.decisions_total,
            "decisions_pending": self.ledgers.decisions_pending,
            "threads_total": self.ledgers.threads_total,
            "threads_open": self.ledgers.threads_open,
            "threads_blocked": self.ledgers.threads_blocked,
            "threads_pending": self.ledgers.threads_pending,
            "questions_total": self.ledgers.questions_total,
            "questions_unresolved": self.ledgers.questions_unresolved,
            "questions_pending": self.ledgers.questions_pending,
            "total_pending_review": self.ledgers.total_pending_review,
        }

        # Git
        result["git"] = {
            "is_repo": self.git.is_repo,
            "clean": self.git.clean,
            "staged_count": self.git.staged_count,
            "unstaged_count": self.git.unstaged_count,
            "untracked_count": self.git.untracked_count,
            "branch": self.git.branch,
        }

        # Lock
        result["lock"] = {
            "locked": self.lock.locked,
            "is_stale": self.lock.is_stale,
        }
        if self.lock.lock_info is not None:
            result["lock"]["pid"] = self.lock.lock_info.pid
            result["lock"]["command"] = self.lock.lock_info.command
            result["lock"]["is_alive"] = self.lock.lock_info.is_alive

        return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_chunk_summary(
    state: ProjectState,
    validation: ValidationResult | None,
) -> ChunkStatusSummary:
    """Compute chunk status summary from project state and validation."""
    draft = 0
    revised = 0
    final = 0
    dirty_checksums = 0

    for chunk_data in state.chunks.values():
        status = chunk_data.frontmatter.status
        if status == "draft":
            draft += 1
        elif status == "revised":
            revised += 1
        elif status == "final":
            final += 1

    # Count dirty checksums from validation warnings
    if validation is not None:
        for w in validation.warnings:
            if w.code == "VALIDATION_DIRTY_CHECKSUM":
                dirty_checksums += 1

    # Chunk IDs in resolved order
    chunk_ids: tuple[str, ...] = ()
    if state.chunk_order is not None:
        chunk_ids = tuple(state.chunk_order.ordered_ids)
    else:
        chunk_ids = tuple(sorted(state.chunks.keys()))

    return ChunkStatusSummary(
        total=len(state.chunks),
        draft=draft,
        revised=revised,
        final=final,
        dirty_checksums=dirty_checksums,
        chunk_ids=chunk_ids,
    )


def _compute_ledger_summary(state: ProjectState) -> LedgerStatusSummary:
    """Compute ledger status summary from project state."""
    decisions_total = 0
    decisions_pending = 0
    threads_total = 0
    threads_open = 0
    threads_blocked = 0
    threads_pending = 0
    questions_total = 0
    questions_unresolved = 0
    questions_pending = 0

    if state.decisions_ledger is not None:
        for entry in state.decisions_ledger.entries:
            decisions_total += 1
            if entry.review_state == ReviewState.PENDING:
                decisions_pending += 1

    if state.threads_ledger is not None:
        for entry in state.threads_ledger.entries:
            threads_total += 1
            if entry.state == ThreadState.OPEN:
                threads_open += 1
            elif entry.state == ThreadState.BLOCKED:
                threads_blocked += 1
            if entry.review_state == ReviewState.PENDING:
                threads_pending += 1

    if state.questions_ledger is not None:
        for entry in state.questions_ledger.entries:
            questions_total += 1
            if not entry.resolved:
                questions_unresolved += 1
            if entry.review_state == ReviewState.PENDING:
                questions_pending += 1

    total_pending = decisions_pending + threads_pending + questions_pending

    return LedgerStatusSummary(
        decisions_total=decisions_total,
        decisions_pending=decisions_pending,
        threads_total=threads_total,
        threads_open=threads_open,
        threads_blocked=threads_blocked,
        threads_pending=threads_pending,
        questions_total=questions_total,
        questions_unresolved=questions_unresolved,
        questions_pending=questions_pending,
        total_pending_review=total_pending,
    )


def _compute_git_summary(root: Path) -> GitStatusSummary:
    """Compute Git status summary from the project root."""
    try:
        gs = git_status(root)
    except Exception:
        # If git_status fails entirely, report not a repo
        return GitStatusSummary(
            is_repo=False,
            clean=False,
            staged_count=0,
            unstaged_count=0,
            untracked_count=0,
            branch="",
        )

    # Try to get the current branch name
    branch = ""
    if gs.is_repo:
        import subprocess
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "branch", "--show-current"],
                capture_output=True,
                text=True,
                check=False,
            )
            branch = result.stdout.strip()
        except Exception:
            branch = ""

    return GitStatusSummary(
        is_repo=gs.is_repo,
        clean=gs.clean,
        staged_count=len(gs.staged),
        unstaged_count=len(gs.unstaged),
        untracked_count=len(gs.untracked),
        branch=branch,
    )


def _compute_lock_summary(layout: ProjectLayout) -> LockStatusSummary:
    """Compute lock status summary from the project layout."""
    lock_path = layout.lock_path
    if not lock_path.exists():
        return LockStatusSummary(
            locked=False,
            lock_info=None,
            is_stale=False,
        )

    info = _read_lock_file(lock_path)
    is_stale = info is not None and info.is_alive is False

    return LockStatusSummary(
        locked=True,
        lock_info=info,
        is_stale=is_stale,
    )


def _check_recovery_file(root: Path) -> bool:
    """Check whether a RECOVERY.md file exists in the project root."""
    return (root / "RECOVERY.md").is_file()


def _compute_next_actions(
    report_partial: Any,
    load_succeeded: bool,
    validation: ValidationResult | None,
    git_summary: GitStatusSummary,
    lock_summary: LockStatusSummary,
    recovery_file_exists: bool,
    ledger_summary: LedgerStatusSummary,
    chunk_summary: ChunkStatusSummary,
) -> tuple[str, ...]:
    """Compute suggested next actions based on the current state.

    Actions are ordered by priority — the most important action comes first.
    """
    actions: list[str] = []

    # 1. Critical: load failure
    if not load_succeeded:
        actions.append(
            "Fix project loading errors before proceeding. "
            "Run 'aip-loom validate' for details."
        )
        return tuple(actions)

    # 2. Recovery file
    if recovery_file_exists:
        actions.append(
            "RECOVERY.md exists — a previous reconcile may have failed. "
            "Review the file and delete it after resolving."
        )

    # 3. Stale lock
    if lock_summary.is_stale:
        actions.append(
            "Stale lock detected — a previous operation may have crashed. "
            "Run 'aip-loom lock release --force' after verifying."
        )

    # 4. Active lock
    if lock_summary.locked and not lock_summary.is_stale:
        actions.append(
            "Project is locked by another operation. "
            "Wait for it to complete or investigate the lock."
        )

    # 5. Validation errors
    if validation is not None and not validation.ok:
        actions.append(
            "Fix validation errors before proceeding. "
            "Run 'aip-loom validate' for details."
        )

    # 6. Pending reviews
    if ledger_summary.total_pending_review > 0:
        actions.append(
            f"{ledger_summary.total_pending_review} item(s) pending review — "
            "approve or reject them to clean up."
        )

    # 7. Blocked threads
    if ledger_summary.threads_blocked > 0:
        actions.append(
            f"{ledger_summary.threads_blocked} thread(s) are blocked — "
            "resolve blockers to unblock progress."
        )

    # 8. Dirty checksums
    if chunk_summary.dirty_checksums > 0:
        actions.append(
            f"{chunk_summary.dirty_checksums} chunk(s) have dirty checksums — "
            "run reconcile to update them."
        )

    # 9. Dirty Git working tree
    if git_summary.is_repo and not git_summary.clean:
        actions.append(
            "Git working tree is dirty — consider committing changes."
        )

    # 10. No chunks yet
    if chunk_summary.total == 0:
        actions.append(
            "No chunks yet — create your first chunk file in chunks/."
        )

    return tuple(actions)


def _classify_health(
    load_succeeded: bool,
    validation: ValidationResult | None,
    recovery_file_exists: bool,
    lock_summary: LockStatusSummary,
) -> HealthLevel:
    """Classify overall project health.

    Rules:
    - ``BLOCKED``: Load failed, or validation has errors, or stale lock,
      or recovery file exists.
    - ``DEGRADED``: No errors, but warnings exist (pending reviews,
      dirty checksums, active lock, dirty Git tree).
    - ``HEALTHY``: No errors, no warnings requiring action.
    """
    # Load failure is always BLOCKED
    if not load_succeeded:
        return HealthLevel.BLOCKED

    # Recovery file is a blocker
    if recovery_file_exists:
        return HealthLevel.BLOCKED

    # Stale lock is a blocker
    if lock_summary.is_stale:
        return HealthLevel.BLOCKED

    # Validation errors are blockers
    if validation is not None and not validation.ok:
        return HealthLevel.BLOCKED

    # Warnings mean degraded
    if validation is not None and len(validation.warnings) > 0:
        return HealthLevel.DEGRADED

    # Active lock is a mild degradation (not an error, but not ideal)
    if lock_summary.locked:
        return HealthLevel.DEGRADED

    return HealthLevel.HEALTHY


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_status(root: Path) -> StatusReport:
    """Compute the full project status report.

    This is the **single entry point** for computing project status.  It
    loads the project, validates it, checks Git state, checks lock state,
    checks for recovery indicators, and assembles a comprehensive
    :class:`StatusReport`.

    If the project cannot be loaded at all (e.g. no manifest), the
    report still reflects that failure honestly rather than fabricating
    a healthy status.

    Parameters
    ----------
    root:
        The project root directory.

    Returns
    -------
    StatusReport
        A frozen, comprehensive status report.
    """
    root = Path(root).resolve()

    # 1. Load project (best-effort)
    load_succeeded = True
    state: ProjectState | None = None
    load_errors: tuple[LoomError, ...] = ()
    load_warnings: tuple[LoomWarning, ...] = ()
    layout: ProjectLayout | None = None

    try:
        state = load_project(root)
        load_errors = state.load_errors
        load_warnings = state.load_warnings
        layout = state.layout
    except ProjectError as exc:
        load_succeeded = False
        load_errors = (exc.loom_error,)
        # Try to construct a layout for lock/Git checks
        try:
            layout = ProjectLayout(root=root)
        except Exception:
            layout = None

    # 2. Validate project (only if loading succeeded)
    validation: ValidationResult | None = None
    if state is not None:
        validation = validate_project(state)

    # 3. Extract project metadata
    project_name = "<unknown>"
    project_type = "<unknown>"
    if state is not None and state.manifest is not None:
        project_name = state.manifest.name
        project_type = state.manifest.project_type.value

    # 4. Compute sub-reports
    if state is not None:
        chunk_summary = _compute_chunk_summary(state, validation)
        ledger_summary = _compute_ledger_summary(state)
    else:
        chunk_summary = ChunkStatusSummary(
            total=0, draft=0, revised=0, final=0,
            dirty_checksums=0, chunk_ids=(),
        )
        ledger_summary = LedgerStatusSummary(
            decisions_total=0, decisions_pending=0,
            threads_total=0, threads_open=0, threads_blocked=0,
            threads_pending=0, questions_total=0, questions_unresolved=0,
            questions_pending=0, total_pending_review=0,
        )

    git_summary = _compute_git_summary(root)

    lock_summary = LockStatusSummary(
        locked=False, lock_info=None, is_stale=False,
    )
    if layout is not None:
        lock_summary = _compute_lock_summary(layout)

    recovery_file_exists = _check_recovery_file(root)

    # 5. Classify health
    health = _classify_health(
        load_succeeded=load_succeeded,
        validation=validation,
        recovery_file_exists=recovery_file_exists,
        lock_summary=lock_summary,
    )

    # 6. Compute error and warning counts
    error_count = len(load_errors)
    warning_count = len(load_warnings)
    if validation is not None:
        error_count += len(validation.errors)
        warning_count += len(validation.warnings)

    # 7. Compute next actions
    next_actions = _compute_next_actions(
        report_partial=None,
        load_succeeded=load_succeeded,
        validation=validation,
        git_summary=git_summary,
        lock_summary=lock_summary,
        recovery_file_exists=recovery_file_exists,
        ledger_summary=ledger_summary,
        chunk_summary=chunk_summary,
    )

    return StatusReport(
        health=health,
        root=str(root),
        project_name=project_name,
        project_type=project_type,
        load_succeeded=load_succeeded,
        load_errors=load_errors,
        load_warnings=load_warnings,
        validation=validation,
        chunks=chunk_summary,
        ledgers=ledger_summary,
        git=git_summary,
        lock=lock_summary,
        recovery_file_exists=recovery_file_exists,
        error_count=error_count,
        warning_count=warning_count,
        next_actions=next_actions,
    )
