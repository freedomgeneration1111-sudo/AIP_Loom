"""Reconcile planner — turns a validated ParsedUpdateBlock into a ReconcilePlan.

This module is the **single authority** for planning reconcile operations.
No other module may independently decide what changes to apply from model
output.  The planner consumes the validated :class:`ParsedUpdateBlock` from
:mod:`aip_loom.update_parser` and the loaded :class:`ProjectState` from
:mod:`aip_loom.project`, and produces a :class:`ReconcilePlan` that is the
sole input to the apply step (Chunk 15).

Design principles (BuildSpec §14, §3A, and Chunk 14 description):

- **Single planner**: :func:`build_reconcile_plan` is the only function
  that decides what changes to make.  Preview and Apply share the exact
  same plan shape.  No re-parsing, no re-building, no plan divergence.
- **Zero canonical writes during preview**: The planner is **pure** — it
  reads project state and the parsed update block, but it never writes
  to canonical files, transaction workspaces, or any other persistent
  storage.  Preview is idempotent and safe to run multiple times.
- **Provisional ID resolution**: The planner resolves provisional IDs
  (``new-1``, ``new-2``) to their eventual canonical IDs (``D-0005``,
  ``T-0003``) using :func:`aip_loom.ids.allocate_next_id`.  This
  resolution is deterministic given the same project state and update
  block.  The resolution map is part of the plan so that apply can use
  it directly without re-resolving.
- **Semantic validation**: The planner detects conflicts and problems
  that the syntactic parser (Chunk 13) cannot catch because they
  require project context:

  - Closing a thread that does not exist in the threads ledger
  - Updating a ledger entry that does not exist
  - Closing a thread that is also being updated (conflict)
  - Attempting to auto-approve items that require human review

- **Honest failure**: Every conflict produces a :class:`LoomError` with
  a stable error code and machine-readable detail.  The planner never
  silently drops or ignores a conflict.
- **Frozen result**: :class:`ReconcilePlan` is frozen after construction.
  Downstream code cannot mutate the plan.
- **Serializable**: The plan can be converted to a JSON-serializable
  dictionary via :meth:`ReconcilePlan.to_dict`.  This is the contract
  between preview and apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import (
    AUTO_APPROVAL_BLOCKED,
    CHUNK_NOT_FOUND,
    FIELD_INVALID,
    MODEL_ASSIGNED_ID,
    RECONCILE_PRE_VALIDATION_FAILED,
    VALIDATION_BROKEN_REFERENCE,
    LoomError,
    LoomWarning,
)
from .ids import allocate_next_id, extract_id_number, InvalidIdError
from .project import ProjectState
from .schemas import (
    UpdateBlock,
    UpdateExistingEntry,
    UpdateLedgerItemNew,
    UpdateThreadItemNew,
    _LEDGER_ID_RE,
)
from .update_parser import ParsedUpdateBlock

__all__ = [
    "ReconcilePlan",
    "PlannedLedgerChange",
    "PlannedFileChange",
    "ProvisionalIdMapping",
    "build_reconcile_plan",
]


# ---------------------------------------------------------------------------
# Planned ledger change
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvisionalIdMapping:
    """Maps a model-proposed provisional ID to a canonical ID.

    Attributes
    ----------
    provisional_id:
        The provisional ID from the model output (e.g. ``"new-1"``).
    canonical_id:
        The canonical ID that will be assigned during apply
        (e.g. ``"D-0005"``).
    item_type:
        The type of ledger item (``"decision"`` or ``"thread"``).
    summary:
        A short summary of the item, for human review.
    """

    provisional_id: str
    canonical_id: str
    item_type: str
    summary: str


@dataclass(frozen=True)
class PlannedLedgerChange:
    """A single planned change to a ledger.

    Attributes
    ----------
    change_type:
        The type of change: ``"new_decision"``, ``"new_thread"``,
        ``"close_thread"``, or ``"update_existing"``.
    item_id:
        The canonical ID of the affected item.  For new items, this
        is the resolved canonical ID.  For updates and closures, this
        is the existing canonical ID.
    provisional_id:
        For new items, the original provisional ID.  Empty string
        for other change types.
    detail:
        A dictionary with change-specific details (summary, changes,
        etc.).
    requires_human_review:
        Whether this change requires human review before it can be
        auto-applied.
    """

    change_type: str
    item_id: str
    provisional_id: str
    detail: dict[str, Any]
    requires_human_review: bool


@dataclass(frozen=True)
class PlannedFileChange:
    """A single planned file modification.

    Attributes
    ----------
    file_path:
        The absolute path to the file that will be modified.
    change_description:
        A human-readable description of the change.
    change_type:
        The type of change: ``"prose_replacement"``,
        ``"frontmatter_update"``, ``"ledger_update"``, etc.
    """

    file_path: str
    change_description: str
    change_type: str


# ---------------------------------------------------------------------------
# ReconcilePlan — the contract between preview and apply
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcilePlan:
    """The complete plan for a reconcile operation.

    This is the **sole contract** between the planner (Chunk 14) and the
    applier (Chunk 15).  The applier must not re-derive, re-parse, or
    re-resolve anything — it consumes this plan directly.

    Attributes
    ----------
    target_chunk:
        The chunk ID being updated (e.g. ``"C-0001"``).
    mode:
        The update mode (currently always ``"full_replacement"``).
    revised_prose:
        The revised prose text that will replace the current prose body.
        Empty string if only ledger changes are proposed.
    ledger_changes:
        Tuple of :class:`PlannedLedgerChange` instances representing
        all planned ledger modifications.
    id_mappings:
        Tuple of :class:`ProvisionalIdMapping` instances mapping
        provisional IDs to their resolved canonical IDs.
    file_changes:
        Tuple of :class:`PlannedFileChange` instances representing
        all planned file modifications.
    conflicts:
        Tuple of :class:`LoomError` instances representing semantic
        conflicts or problems that prevent clean application.
    warnings:
        Tuple of :class:`LoomWarning` instances representing non-fatal
        issues that the user should be aware of.
    requires_human_review:
        Whether any part of this plan requires human review before
        it can be safely applied.
    plan_ok:
        Whether the plan has no conflicts and can be applied cleanly.
        If ``False``, the apply step must refuse to proceed.
    """

    target_chunk: str
    mode: str
    revised_prose: str
    ledger_changes: tuple[PlannedLedgerChange, ...]
    id_mappings: tuple[ProvisionalIdMapping, ...]
    file_changes: tuple[PlannedFileChange, ...]
    conflicts: tuple[LoomError, ...]
    warnings: tuple[LoomWarning, ...]
    requires_human_review: bool
    plan_ok: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary of the plan.

        This is the canonical serialization format that the apply step
        (Chunk 15) will consume.  It must be stable and complete.
        The revised prose text is included so that apply can write it
        to the chunk file without re-parsing model output.
        """
        return {
            "target_chunk": self.target_chunk,
            "mode": self.mode,
            "revised_prose": self.revised_prose,
            "revised_prose_length": len(self.revised_prose),
            "ledger_changes": [
                {
                    "change_type": lc.change_type,
                    "item_id": lc.item_id,
                    "provisional_id": lc.provisional_id,
                    "detail": lc.detail,
                    "requires_human_review": lc.requires_human_review,
                }
                for lc in self.ledger_changes
            ],
            "id_mappings": [
                {
                    "provisional_id": m.provisional_id,
                    "canonical_id": m.canonical_id,
                    "item_type": m.item_type,
                    "summary": m.summary,
                }
                for m in self.id_mappings
            ],
            "file_changes": [
                {
                    "file_path": fc.file_path,
                    "change_description": fc.change_description,
                    "change_type": fc.change_type,
                }
                for fc in self.file_changes
            ],
            "conflicts": [
                {"code": c.code, "message": c.message, "detail": c.detail}
                for c in self.conflicts
            ],
            "warnings": [
                {"code": w.code, "message": w.message, "detail": w.detail}
                for w in self.warnings
            ],
            "requires_human_review": self.requires_human_review,
            "plan_ok": self.plan_ok,
        }


# ---------------------------------------------------------------------------
# Internal helpers — semantic validation
# ---------------------------------------------------------------------------


def _validate_target_chunk_exists(
    update_block: UpdateBlock,
    state: ProjectState,
) -> LoomError | None:
    """Verify the target chunk exists in the project.

    The update parser validates the chunk ID format, but cannot verify
    that the chunk actually exists in the project.  That requires
    project context, which is only available here.
    """
    target = update_block.target_chunk
    if target not in state.chunks:
        return LoomError(
            code=CHUNK_NOT_FOUND,
            message=(
                f"Target chunk {target!r} does not exist in the project.  "
                f"The model is proposing changes to a chunk that is not "
                f"part of the current project state."
            ),
            detail={
                "target_chunk": target,
                "available_chunks": sorted(state.chunks.keys())[:20],
            },
        )
    return None


def _validate_close_threads(
    close_thread_ids: list[str],
    state: ProjectState,
) -> list[LoomError]:
    """Validate that threads being closed exist and are open.

    Returns a list of errors for:
    - Thread IDs that do not exist in the threads ledger
    - Threads that are already closed
    """
    errors: list[LoomError] = []

    if state.threads_ledger is None:
        # No threads ledger at all — all close operations are invalid
        for tid in close_thread_ids:
            errors.append(
                LoomError(
                    code=VALIDATION_BROKEN_REFERENCE,
                    message=(
                        f"Cannot close thread {tid!r}: the threads ledger "
                        f"could not be loaded or does not exist."
                    ),
                    detail={"thread_id": tid, "reason": "no_threads_ledger"},
                )
            )
        return errors

    # Build lookup of existing threads by ID
    thread_map: dict[str, Any] = {}
    for entry in state.threads_ledger.entries:
        thread_map[entry.id] = entry

    for tid in close_thread_ids:
        if tid not in thread_map:
            errors.append(
                LoomError(
                    code=VALIDATION_BROKEN_REFERENCE,
                    message=(
                        f"Cannot close thread {tid!r}: this thread ID does "
                        f"not exist in the threads ledger."
                    ),
                    detail={"thread_id": tid, "reason": "not_found"},
                )
            )
        elif thread_map[tid].state.value == "closed":
            errors.append(
                LoomError(
                    code=VALIDATION_BROKEN_REFERENCE,
                    message=(
                        f"Cannot close thread {tid!r}: this thread is "
                        f"already closed."
                    ),
                    detail={
                        "thread_id": tid,
                        "current_state": "closed",
                        "reason": "already_closed",
                    },
                )
            )

    return errors


def _validate_update_existing(
    update_entries: list[UpdateExistingEntry],
    state: ProjectState,
    close_thread_ids: list[str],
) -> list[LoomError]:
    """Validate updates to existing ledger entries.

    Checks:
    - The referenced ID exists in the appropriate ledger
    - The update is not conflicting with a close operation on the
      same thread (close + update = conflict)
    - If a ledger could not be loaded (None), updates to it are
      rejected with a specific error message
    """
    errors: list[LoomError] = []

    # Build sets of known IDs per ledger
    decision_ids: set[str] = set()
    thread_ids: set[str] = set()
    question_ids: set[str] = set()
    decisions_ledger_loaded = state.decisions_ledger is not None
    threads_ledger_loaded = state.threads_ledger is not None
    questions_ledger_loaded = state.questions_ledger is not None

    if decisions_ledger_loaded:
        decision_ids = {e.id for e in state.decisions_ledger.entries}
    if threads_ledger_loaded:
        thread_ids = {e.id for e in state.threads_ledger.entries}
    if questions_ledger_loaded:
        question_ids = {e.id for e in state.questions_ledger.entries}

    close_set = set(close_thread_ids)

    for entry in update_entries:
        eid = entry.id
        # Determine which ledger this ID belongs to
        if eid.startswith("D-"):
            if not decisions_ledger_loaded:
                errors.append(
                    LoomError(
                        code=VALIDATION_BROKEN_REFERENCE,
                        message=(
                            f"Cannot update decision {eid!r}: the decisions "
                            f"ledger could not be loaded."
                        ),
                        detail={"id": eid, "ledger": "decisions", "reason": "ledger_unavailable"},
                    )
                )
            elif eid not in decision_ids:
                errors.append(
                    LoomError(
                        code=VALIDATION_BROKEN_REFERENCE,
                        message=(
                            f"Cannot update decision {eid!r}: this ID does "
                            f"not exist in the decisions ledger."
                        ),
                        detail={"id": eid, "ledger": "decisions", "reason": "not_found"},
                    )
                )
        elif eid.startswith("T-"):
            if not threads_ledger_loaded:
                errors.append(
                    LoomError(
                        code=VALIDATION_BROKEN_REFERENCE,
                        message=(
                            f"Cannot update thread {eid!r}: the threads "
                            f"ledger could not be loaded."
                        ),
                        detail={"id": eid, "ledger": "threads", "reason": "ledger_unavailable"},
                    )
                )
            elif eid not in thread_ids:
                errors.append(
                    LoomError(
                        code=VALIDATION_BROKEN_REFERENCE,
                        message=(
                            f"Cannot update thread {eid!r}: this ID does "
                            f"not exist in the threads ledger."
                        ),
                        detail={"id": eid, "ledger": "threads", "reason": "not_found"},
                    )
                )
            elif eid in close_set:
                errors.append(
                    LoomError(
                        code=RECONCILE_PRE_VALIDATION_FAILED,
                        message=(
                            f"Conflict: thread {eid!r} is both being closed "
                            f"and updated.  A thread cannot be closed and "
                            f"updated in the same reconcile operation."
                        ),
                        detail={
                            "id": eid,
                            "reason": "close_and_update_conflict",
                        },
                    )
                )
        elif eid.startswith("Q-"):
            if not questions_ledger_loaded:
                errors.append(
                    LoomError(
                        code=VALIDATION_BROKEN_REFERENCE,
                        message=(
                            f"Cannot update question {eid!r}: the questions "
                            f"ledger could not be loaded."
                        ),
                        detail={"id": eid, "ledger": "questions", "reason": "ledger_unavailable"},
                    )
                )
            elif eid not in question_ids:
                errors.append(
                    LoomError(
                        code=VALIDATION_BROKEN_REFERENCE,
                        message=(
                            f"Cannot update question {eid!r}: this ID does "
                            f"not exist in the questions ledger."
                        ),
                        detail={"id": eid, "ledger": "questions", "reason": "not_found"},
                    )
                )
        else:
            # Unknown prefix — this should have been caught by the parser,
            # but we check here as defense-in-depth
            errors.append(
                LoomError(
                    code=FIELD_INVALID,
                    message=(
                        f"Cannot update entry {eid!r}: unknown ID prefix.  "
                        f"Expected D-, T-, or Q- prefix."
                    ),
                    detail={"id": eid, "reason": "unknown_prefix"},
                )
            )

    return errors


def _check_auto_approval(
    update_block: UpdateBlock,
) -> list[LoomWarning]:
    """Check for items that would be auto-approved despite requiring review.

    The update parser already enforces that new items have
    ``review_state=pending``.  Here we check that the block-level
    ``requires_human_review`` flag is consistent and warn if any item
    would bypass review.
    """
    warnings: list[LoomWarning] = []

    # If the block says it doesn't require human review but individual
    # items do, that's a warning (not an error — the human can still
    # catch it during review)
    if not update_block.requires_human_review:
        items_needing_review: list[str] = []
        for item in update_block.new_decisions:
            if item.requires_human_review:
                items_needing_review.append(item.provisional_id)
        for item in update_block.new_threads:
            if item.requires_human_review:
                items_needing_review.append(item.provisional_id)

        if items_needing_review:
            warnings.append(
                LoomWarning(
                    code=AUTO_APPROVAL_BLOCKED,
                    message=(
                        f"Block-level requires_human_review is False, but "
                        f"{len(items_needing_review)} item(s) have "
                        f"requires_human_review=True: "
                        f"{', '.join(items_needing_review[:5])}.  "
                        f"These items will require human review regardless."
                    ),
                    detail={
                        "block_flag": False,
                        "items_needing_review": items_needing_review[:10],
                        "count": len(items_needing_review),
                    },
                )
            )

    return warnings


def _resolve_provisional_ids(
    update_block: UpdateBlock,
    state: ProjectState,
) -> tuple[list[ProvisionalIdMapping], list[LoomError]]:
    """Resolve provisional IDs to canonical IDs.

    Uses :func:`allocate_next_id` (the single authority for ID
    allocation) to determine the next available canonical ID for
    each new item.  This resolution is deterministic given the same
    project state and update block.

    For multiple new items of the same type, we call
    ``allocate_next_id`` once to get the first new ID, then
    increment from there (since the newly-allocated IDs won't yet
    be in the ledger entries).  This avoids duplicating the
    allocation logic from ``ids.py``.

    Returns a list of mappings and a list of errors (e.g. if the
    model somehow sneaked a canonical ID past the parser — this is
    a secondary defense-in-depth guard, as the Pydantic schema
    already rejects canonical IDs in provisional_id fields).
    """
    mappings: list[ProvisionalIdMapping] = []
    errors: list[LoomError] = []

    # Get current entries for ID allocation
    decision_entries = list(state.decisions_ledger.entries) if state.decisions_ledger else []
    thread_entries = list(state.threads_ledger.entries) if state.threads_ledger else []

    # Resolve new decisions — use the single authority allocate_next_id
    if update_block.new_decisions:
        try:
            first_new_id = allocate_next_id("D", decision_entries)
            _, first_num = extract_id_number(first_new_id)
        except (InvalidIdError, ValueError) as exc:
            # If ID allocation fails (e.g. malformed existing IDs),
            # surface it as an error
            errors.append(
                LoomError(
                    code=FIELD_INVALID,
                    message=f"Cannot allocate next decision ID: {exc}",
                    detail={"prefix": "D", "error": str(exc)},
                )
            )
            first_num = 0

        for i, item in enumerate(update_block.new_decisions):
            # Defense-in-depth: check for model-assigned canonical IDs
            # (the Pydantic schema already rejects these, but we guard here
            # in case validation is somehow bypassed)
            if _LEDGER_ID_RE.match(item.provisional_id):
                errors.append(
                    LoomError(
                        code=MODEL_ASSIGNED_ID,
                        message=(
                            f"Model-proposed canonical ID {item.provisional_id!r} "
                            f"in new_decisions.  IDs are allocated by AIP_Loom, "
                            f"never by the model."
                        ),
                        detail={"provisional_id": item.provisional_id},
                    )
                )
                continue

            canonical_id = f"D-{first_num + i:04d}"

            mappings.append(
                ProvisionalIdMapping(
                    provisional_id=item.provisional_id,
                    canonical_id=canonical_id,
                    item_type="decision",
                    summary=item.summary,
                )
            )

    # Resolve new threads — use the single authority allocate_next_id
    if update_block.new_threads:
        try:
            first_new_id = allocate_next_id("T", thread_entries)
            _, first_num = extract_id_number(first_new_id)
        except (InvalidIdError, ValueError) as exc:
            errors.append(
                LoomError(
                    code=FIELD_INVALID,
                    message=f"Cannot allocate next thread ID: {exc}",
                    detail={"prefix": "T", "error": str(exc)},
                )
            )
            first_num = 0

        for i, item in enumerate(update_block.new_threads):
            # Defense-in-depth: check for model-assigned canonical IDs
            if _LEDGER_ID_RE.match(item.provisional_id):
                errors.append(
                    LoomError(
                        code=MODEL_ASSIGNED_ID,
                        message=(
                            f"Model-proposed canonical ID {item.provisional_id!r} "
                            f"in new_threads.  IDs are allocated by AIP_Loom, "
                            f"never by the model."
                        ),
                        detail={"provisional_id": item.provisional_id},
                    )
                )
                continue

            canonical_id = f"T-{first_num + i:04d}"

            mappings.append(
                ProvisionalIdMapping(
                    provisional_id=item.provisional_id,
                    canonical_id=canonical_id,
                    item_type="thread",
                    summary=item.summary,
                )
            )

    return mappings, errors





# ---------------------------------------------------------------------------
# Internal helpers — file change planning
# ---------------------------------------------------------------------------


def _plan_file_changes(
    update_block: UpdateBlock,
    state: ProjectState,
    id_mappings: list[ProvisionalIdMapping],
) -> list[PlannedFileChange]:
    """Plan which files will be modified during apply.

    This is purely informational — it does not modify any files.
    It uses the project layout to determine the paths of files
    that will be changed.
    """
    changes: list[PlannedFileChange] = []

    # 1. Target chunk file — prose replacement
    if update_block.revised_prose or update_block.mode.value == "full_replacement":
        target = update_block.target_chunk
        if target in state.chunks:
            chunk_path = str(state.chunks[target].file_path)
            changes.append(
                PlannedFileChange(
                    file_path=chunk_path,
                    change_description=(
                        f"Replace prose body for chunk {target} "
                        f"({len(update_block.revised_prose)} chars)"
                    ),
                    change_type="prose_replacement",
                )
            )

    # 2. Decisions ledger — if new decisions or updates
    if update_block.new_decisions or any(
        e.id.startswith("D-") for e in update_block.update_existing
    ):
        changes.append(
            PlannedFileChange(
                file_path=str(state.layout.decisions_ledger_path),
                change_description=(
                    f"Add {len(update_block.new_decisions)} decision(s), "
                    f"update {sum(1 for e in update_block.update_existing if e.id.startswith('D-'))} decision(s)"
                ),
                change_type="ledger_update",
            )
        )

    # 3. Threads ledger — if new threads, close threads, or thread updates
    if (
        update_block.new_threads
        or update_block.close_threads
        or any(e.id.startswith("T-") for e in update_block.update_existing)
    ):
        changes.append(
            PlannedFileChange(
                file_path=str(state.layout.threads_ledger_path),
                change_description=(
                    f"Add {len(update_block.new_threads)} thread(s), "
                    f"close {len(update_block.close_threads)} thread(s), "
                    f"update {sum(1 for e in update_block.update_existing if e.id.startswith('T-'))} thread(s)"
                ),
                change_type="ledger_update",
            )
        )

    # 4. Questions ledger — if question updates
    if any(e.id.startswith("Q-") for e in update_block.update_existing):
        changes.append(
            PlannedFileChange(
                file_path=str(state.layout.questions_ledger_path),
                change_description=(
                    f"Update {sum(1 for e in update_block.update_existing if e.id.startswith('Q-'))} question(s)"
                ),
                change_type="ledger_update",
            )
        )

    return changes


# ---------------------------------------------------------------------------
# Internal helpers — ledger change planning
# ---------------------------------------------------------------------------


def _plan_ledger_changes(
    update_block: UpdateBlock,
    id_mappings: list[ProvisionalIdMapping],
) -> list[PlannedLedgerChange]:
    """Plan all ledger changes based on the update block.

    Uses the resolved ID mappings to assign canonical IDs to new items.
    """
    changes: list[PlannedLedgerChange] = []

    # Build lookup from provisional_id to canonical_id
    mapping_lookup: dict[str, str] = {
        m.provisional_id: m.canonical_id for m in id_mappings
    }

    # New decisions
    for item in update_block.new_decisions:
        canonical_id = mapping_lookup.get(item.provisional_id, item.provisional_id)
        changes.append(
            PlannedLedgerChange(
                change_type="new_decision",
                item_id=canonical_id,
                provisional_id=item.provisional_id,
                detail={
                    "summary": item.summary,
                    "rationale": item.rationale,
                    "scope": item.scope if hasattr(item, "scope") else "global",
                    "chunk_id": item.chunk_id if hasattr(item, "chunk_id") else "",
                    "review_state": "pending",
                },
                requires_human_review=item.requires_human_review,
            )
        )

    # New threads
    for item in update_block.new_threads:
        canonical_id = mapping_lookup.get(item.provisional_id, item.provisional_id)
        changes.append(
            PlannedLedgerChange(
                change_type="new_thread",
                item_id=canonical_id,
                provisional_id=item.provisional_id,
                detail={
                    "summary": item.summary,
                    "state": item.state.value,
                    "scope": item.scope,
                    "chunk_id": item.chunk_id if hasattr(item, "chunk_id") else "",
                    "blocked_by": item.blocked_by if hasattr(item, "blocked_by") else [],
                    "review_state": "pending",
                },
                requires_human_review=item.requires_human_review,
            )
        )

    # Close threads
    from datetime import datetime as _dt, timezone as _tz
    closed_at = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for tid in update_block.close_threads:
        changes.append(
            PlannedLedgerChange(
                change_type="close_thread",
                item_id=tid,
                provisional_id="",
                detail={"state": "closed", "closed_at": closed_at},
                requires_human_review=False,
            )
        )

    # Update existing entries
    for entry in update_block.update_existing:
        changes.append(
            PlannedLedgerChange(
                change_type="update_existing",
                item_id=entry.id,
                provisional_id="",
                detail={"changes": entry.changes},
                requires_human_review=False,
            )
        )

    return changes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_reconcile_plan(
    parsed_block: ParsedUpdateBlock,
    project_state: ProjectState,
) -> ReconcilePlan:
    """Build a reconcile plan from a parsed update block and project state.

    This is the **single entry point** for planning reconcile operations.
    Both preview and apply must use this function to produce the plan.
    No other module may independently decide what changes to make.

    The function performs these steps:

    1. **Validate target chunk**: Verify the target chunk exists in
       the project.
    2. **Validate close_threads**: Check that each thread being closed
       exists and is open.
    3. **Validate update_existing**: Check that each entry being updated
       exists, and detect close/update conflicts on threads.
    4. **Resolve provisional IDs**: Map model-proposed provisional IDs
       (``new-1``) to canonical IDs (``D-0005``) using the single
       authority ``allocate_next_id`` from ``ids.py``.
    5. **Check auto-approval**: Warn if items requiring review would
       bypass it.
    6. **Plan file changes**: Determine which files will be modified.
    7. **Plan ledger changes**: Enumerate all ledger modifications.
    8. **Determine review requirement**: Whether any part of the plan
       requires human review.
    9. **Assemble result**: Build the :class:`ReconcilePlan` with all
       changes, conflicts, and warnings.

    Parameters
    ----------
    parsed_block:
        The validated parsed update block from
        :func:`aip_loom.update_parser.parse_model_output`.
    project_state:
        The loaded project state from :func:`aip_loom.project.load_project`.

    Returns
    -------
    ReconcilePlan
        The complete plan.  Check ``plan_ok`` to determine if the plan
        can be applied cleanly.  Check ``conflicts`` for semantic problems.
        Check ``warnings`` for non-fatal issues.
    """
    update_block = parsed_block.update_block
    conflicts: list[LoomError] = []
    warnings: list[LoomWarning] = []

    # -- Step 1: Validate target chunk ----------------------------------------
    chunk_error = _validate_target_chunk_exists(update_block, project_state)
    if chunk_error is not None:
        conflicts.append(chunk_error)

    # -- Step 2: Validate close_threads ----------------------------------------
    close_errors = _validate_close_threads(
        update_block.close_threads, project_state,
    )
    conflicts.extend(close_errors)

    # -- Step 3: Validate update_existing --------------------------------------
    update_errors = _validate_update_existing(
        update_block.update_existing, project_state,
        update_block.close_threads,
    )
    conflicts.extend(update_errors)

    # -- Step 4: Resolve provisional IDs ---------------------------------------
    id_mappings, id_errors = _resolve_provisional_ids(update_block, project_state)
    conflicts.extend(id_errors)

    # -- Step 5: Check auto-approval ------------------------------------------
    approval_warnings = _check_auto_approval(update_block)
    warnings.extend(approval_warnings)

    # -- Step 6: Plan file changes ---------------------------------------------
    file_changes = _plan_file_changes(update_block, project_state, id_mappings)

    # -- Step 7: Plan ledger changes -------------------------------------------
    ledger_changes = _plan_ledger_changes(update_block, id_mappings)

    # -- Step 8: Determine review requirement ----------------------------------
    any_needs_review = (
        update_block.requires_human_review
        or any(lc.requires_human_review for lc in ledger_changes)
    )

    # -- Step 9: Assemble result -----------------------------------------------
    plan_ok = len(conflicts) == 0

    return ReconcilePlan(
        target_chunk=update_block.target_chunk,
        mode=update_block.mode.value,
        revised_prose=parsed_block.revised_prose,
        ledger_changes=tuple(ledger_changes),
        id_mappings=tuple(id_mappings),
        file_changes=tuple(file_changes),
        conflicts=tuple(conflicts),
        warnings=tuple(warnings),
        requires_human_review=any_needs_review,
        plan_ok=plan_ok,
    )
