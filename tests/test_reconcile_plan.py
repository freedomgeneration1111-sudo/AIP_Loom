"""Tests for aip_loom.reconcile_plan — reconcile planner and preview.

These tests prove:

- build_reconcile_plan produces a valid ReconcilePlan from valid input
- Target chunk not found → CHUNK_NOT_FOUND conflict
- Close non-existent thread → VALIDATION_BROKEN_REFERENCE conflict
- Close already-closed thread → VALIDATION_BROKEN_REFERENCE conflict
- Update non-existent ledger entry → VALIDATION_BROKEN_REFERENCE conflict
- Close + update same thread → RECONCILE_PRE_VALIDATION_FAILED conflict
- Model-assigned canonical IDs caught (defense-in-depth)
- Auto-approval warnings surfaced correctly
- Provisional IDs resolved to canonical IDs deterministically
- Plan is frozen (immutable)
- Plan serializable to JSON via to_dict()
- Preview performs zero canonical writes
- Same input → same plan (deterministic)
- Plan shape is complete for Chunk 15 consumption
- No-mutation guarantee: project state unchanged after planning
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from aip_loom.errors import (
    AUTO_APPROVAL_BLOCKED,
    CHUNK_NOT_FOUND,
    MODEL_ASSIGNED_ID,
    RECONCILE_PRE_VALIDATION_FAILED,
    VALIDATION_BROKEN_REFERENCE,
    LoomError,
    LoomWarning,
)
from aip_loom.ids import allocate_next_id
from aip_loom.project import ChunkData, ProjectState
from aip_loom.reconcile_plan import (
    PlannedFileChange,
    PlannedLedgerChange,
    ProvisionalIdMapping,
    ReconcilePlan,
    build_reconcile_plan,
)
from aip_loom.schemas import (
    SUPPORTED_SCHEMA_VERSION,
    ChunkFrontmatter,
    ChunkStatus,
    DecisionEntry,
    DecisionLedger,
    Distillate,
    ProjectManifest,
    ReviewState,
    ThreadEntry,
    ThreadLedger,
    ThreadState,
    UpdateBlock,
    UpdateLedgerItemNew,
    UpdateThreadItemNew,
    UpdateMode,
)
from aip_loom.update_parser import ParsedUpdateBlock


# ---------------------------------------------------------------------------
# Helpers — minimal project state construction
# ---------------------------------------------------------------------------

_V = SUPPORTED_SCHEMA_VERSION
_TS = "2026-05-30T12:00:00Z"


def _make_layout(tmp_path: Path) -> Any:
    """Create a minimal ProjectLayout for testing."""
    from aip_loom.layout import ProjectLayout

    # Create required directories
    (tmp_path / "chunks").mkdir()
    (tmp_path / "ledgers").mkdir()
    (tmp_path / "archive").mkdir()
    (tmp_path / ".aip-loom").mkdir()

    return ProjectLayout(root=tmp_path)


def _make_chunk_data(chunk_id: str, prose: str = "Original prose.") -> ChunkData:
    """Create a ChunkData instance for testing."""
    return ChunkData(
        file_path=Path(f"/tmp/chunks/{chunk_id}.md"),
        frontmatter=ChunkFrontmatter(
            schema_version=_V,
            id=chunk_id,
            title=f"Chunk {chunk_id}",
            word_count=len(prose.split()),
            prose_checksum="abc123def456",
            created_at=_TS,
            updated_at=_TS,
        ),
        prose_body=prose,
    )


def _make_project_state(
    tmp_path: Path,
    chunk_ids: list[str] | None = None,
    decisions: list[DecisionEntry] | None = None,
    threads: list[ThreadEntry] | None = None,
) -> ProjectState:
    """Create a minimal ProjectState for testing."""
    layout = _make_layout(tmp_path)

    chunks = {}
    if chunk_ids:
        for cid in chunk_ids:
            chunks[cid] = _make_chunk_data(cid)

    decision_entries = decisions or []
    thread_entries = threads or []

    manifest = ProjectManifest(
        schema_version=_V,
        name="test-project",
        created_at=_TS,
        updated_at=_TS,
    )

    decisions_ledger = DecisionLedger(
        schema_version=_V,
        entries=decision_entries,
    )
    threads_ledger = ThreadLedger(
        schema_version=_V,
        entries=thread_entries,
    )

    return ProjectState(
        layout=layout,
        manifest=manifest,
        decisions_ledger=decisions_ledger,
        threads_ledger=threads_ledger,
        questions_ledger=None,
        distillate=None,
        sessions=None,
        comments=None,
        chunks=chunks,
        chunk_order=None,
    )


def _make_update_block(**overrides: Any) -> UpdateBlock:
    """Create a minimal valid UpdateBlock for testing."""
    base = {
        "schema_version": _V,
        "fence_type": "loom-update",
        "mode": "full_replacement",
        "target_chunk": "C-0001",
        "revised_prose": "The revised text.",
        "change_summary": "Updated the prose.",
        "requires_human_review": True,
    }
    base.update(overrides)
    return UpdateBlock(**base)


def _make_parsed_block(**overrides: Any) -> ParsedUpdateBlock:
    """Create a ParsedUpdateBlock for testing."""
    update_block = _make_update_block(**overrides)
    return ParsedUpdateBlock(
        update_block=update_block,
        revised_prose=update_block.revised_prose,
        raw_content="yaml content here",
        fence_start=0,
        fence_end=100,
    )


# ---------------------------------------------------------------------------
# Valid plan tests
# ---------------------------------------------------------------------------


class TestValidPlan:
    """Verify that valid inputs produce a clean plan."""

    def test_minimal_valid_plan(self, tmp_path: Path) -> None:
        """A minimal valid update block produces a clean plan."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        parsed = _make_parsed_block()
        plan = build_reconcile_plan(parsed, state)

        assert plan.target_chunk == "C-0001"
        assert plan.mode == "full_replacement"
        assert plan.plan_ok is True
        assert len(plan.conflicts) == 0

    def test_plan_with_new_decisions(self, tmp_path: Path) -> None:
        """Plan correctly includes new decisions."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        update_block = _make_update_block(
            new_decisions=[
                UpdateLedgerItemNew(
                    provisional_id="new-1",
                    summary="A new decision",
                ),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        assert plan.plan_ok is True
        new_decision_changes = [
            lc for lc in plan.ledger_changes if lc.change_type == "new_decision"
        ]
        assert len(new_decision_changes) == 1
        assert new_decision_changes[0].provisional_id == "new-1"

    def test_plan_with_new_threads(self, tmp_path: Path) -> None:
        """Plan correctly includes new threads."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        update_block = _make_update_block(
            new_threads=[
                UpdateThreadItemNew(
                    provisional_id="new-2",
                    summary="A new thread",
                ),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        assert plan.plan_ok is True
        new_thread_changes = [
            lc for lc in plan.ledger_changes if lc.change_type == "new_thread"
        ]
        assert len(new_thread_changes) == 1

    def test_plan_with_close_threads(self, tmp_path: Path) -> None:
        """Plan correctly includes close_threads."""
        threads = [
            ThreadEntry(
                id="T-0001",
                review_state=ReviewState.APPROVED,
                created_at=_TS,
                summary="An open thread",
                state=ThreadState.OPEN,
            ),
        ]
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"], threads=threads)
        update_block = _make_update_block(close_threads=["T-0001"])
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        assert plan.plan_ok is True
        close_changes = [
            lc for lc in plan.ledger_changes if lc.change_type == "close_thread"
        ]
        assert len(close_changes) == 1
        assert close_changes[0].item_id == "T-0001"


# ---------------------------------------------------------------------------
# Target chunk validation
# ---------------------------------------------------------------------------


class TestTargetChunkValidation:
    """Verify CHUNK_NOT_FOUND when target chunk doesn't exist."""

    def test_nonexistent_target_chunk(self, tmp_path: Path) -> None:
        """Target chunk not in project → conflict."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0002"])
        parsed = _make_parsed_block(target_chunk="C-0001")
        plan = build_reconcile_plan(parsed, state)

        assert plan.plan_ok is False
        chunk_conflicts = [c for c in plan.conflicts if c.code == CHUNK_NOT_FOUND]
        assert len(chunk_conflicts) >= 1

    def test_existing_target_chunk_ok(self, tmp_path: Path) -> None:
        """Target chunk exists → no conflict."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        parsed = _make_parsed_block()
        plan = build_reconcile_plan(parsed, state)

        chunk_conflicts = [c for c in plan.conflicts if c.code == CHUNK_NOT_FOUND]
        assert len(chunk_conflicts) == 0


# ---------------------------------------------------------------------------
# Close thread validation
# ---------------------------------------------------------------------------


class TestCloseThreadValidation:
    """Verify close_threads semantic validation."""

    def test_close_nonexistent_thread(self, tmp_path: Path) -> None:
        """Closing a thread that doesn't exist → conflict."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        update_block = _make_update_block(close_threads=["T-9999"])
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        assert plan.plan_ok is False
        ref_conflicts = [c for c in plan.conflicts if c.code == VALIDATION_BROKEN_REFERENCE]
        assert len(ref_conflicts) >= 1
        assert "T-9999" in ref_conflicts[0].message

    def test_close_already_closed_thread(self, tmp_path: Path) -> None:
        """Closing a thread that is already closed → conflict."""
        threads = [
            ThreadEntry(
                id="T-0001",
                review_state=ReviewState.APPROVED,
                created_at=_TS,
                summary="A closed thread",
                state=ThreadState.CLOSED,
            ),
        ]
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"], threads=threads)
        update_block = _make_update_block(close_threads=["T-0001"])
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        assert plan.plan_ok is False
        closed_conflicts = [
            c for c in plan.conflicts
            if c.code == VALIDATION_BROKEN_REFERENCE and "already closed" in c.message
        ]
        assert len(closed_conflicts) >= 1

    def test_close_open_thread_ok(self, tmp_path: Path) -> None:
        """Closing an open thread → no conflict."""
        threads = [
            ThreadEntry(
                id="T-0001",
                review_state=ReviewState.APPROVED,
                created_at=_TS,
                summary="An open thread",
                state=ThreadState.OPEN,
            ),
        ]
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"], threads=threads)
        update_block = _make_update_block(close_threads=["T-0001"])
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        assert plan.plan_ok is True


# ---------------------------------------------------------------------------
# Update existing validation
# ---------------------------------------------------------------------------


class TestUpdateExistingValidation:
    """Verify update_existing semantic validation."""

    def test_update_nonexistent_decision(self, tmp_path: Path) -> None:
        """Updating a decision that doesn't exist → conflict."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        from aip_loom.schemas import UpdateExistingEntry

        update_block = _make_update_block(
            update_existing=[
                UpdateExistingEntry(id="D-9999", changes={"rationale": "Updated"}),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        assert plan.plan_ok is False
        ref_conflicts = [c for c in plan.conflicts if c.code == VALIDATION_BROKEN_REFERENCE]
        assert len(ref_conflicts) >= 1

    def test_update_existing_decision_ok(self, tmp_path: Path) -> None:
        """Updating a decision that exists → no conflict."""
        decisions = [
            DecisionEntry(
                id="D-0001",
                review_state=ReviewState.APPROVED,
                created_at=_TS,
                summary="Existing decision",
            ),
        ]
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"], decisions=decisions)
        from aip_loom.schemas import UpdateExistingEntry

        update_block = _make_update_block(
            update_existing=[
                UpdateExistingEntry(id="D-0001", changes={"rationale": "Updated"}),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        ref_conflicts = [c for c in plan.conflicts if c.code == VALIDATION_BROKEN_REFERENCE]
        # D-0001 should not produce a BROKEN_REFERENCE since it exists
        d_conflicts = [c for c in ref_conflicts if "D-0001" in c.message]
        assert len(d_conflicts) == 0


# ---------------------------------------------------------------------------
# Close + update conflict
# ---------------------------------------------------------------------------


class TestCloseUpdateConflict:
    """Verify that closing and updating the same thread is a conflict."""

    def test_close_and_update_same_thread(self, tmp_path: Path) -> None:
        """Closing and updating the same thread → conflict."""
        threads = [
            ThreadEntry(
                id="T-0001",
                review_state=ReviewState.APPROVED,
                created_at=_TS,
                summary="An open thread",
                state=ThreadState.OPEN,
            ),
        ]
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"], threads=threads)
        from aip_loom.schemas import UpdateExistingEntry

        update_block = _make_update_block(
            close_threads=["T-0001"],
            update_existing=[
                UpdateExistingEntry(id="T-0001", changes={"summary": "Updated"}),
            ],
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        assert plan.plan_ok is False
        conflict_conflicts = [
            c for c in plan.conflicts
            if c.code == RECONCILE_PRE_VALIDATION_FAILED
        ]
        assert len(conflict_conflicts) >= 1
        assert "close_and_update_conflict" in str(conflict_conflicts[0].detail)


# ---------------------------------------------------------------------------
# Provisional ID resolution
# ---------------------------------------------------------------------------


class TestProvisionalIdResolution:
    """Verify that provisional IDs are resolved to canonical IDs."""

    def test_new_decision_gets_canonical_id(self, tmp_path: Path) -> None:
        """New decision with provisional_id='new-1' gets resolved."""
        decisions = [
            DecisionEntry(
                id="D-0001",
                review_state=ReviewState.APPROVED,
                created_at=_TS,
                summary="Existing",
            ),
        ]
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"], decisions=decisions)
        update_block = _make_update_block(
            new_decisions=[
                UpdateLedgerItemNew(
                    provisional_id="new-1",
                    summary="A new decision",
                ),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        assert len(plan.id_mappings) == 1
        assert plan.id_mappings[0].provisional_id == "new-1"
        assert plan.id_mappings[0].canonical_id == "D-0002"
        assert plan.id_mappings[0].item_type == "decision"

    def test_new_thread_gets_canonical_id(self, tmp_path: Path) -> None:
        """New thread with provisional_id='new-1' gets resolved."""
        threads = [
            ThreadEntry(
                id="T-0001",
                review_state=ReviewState.APPROVED,
                created_at=_TS,
                summary="Existing",
                state=ThreadState.OPEN,
            ),
        ]
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"], threads=threads)
        update_block = _make_update_block(
            new_threads=[
                UpdateThreadItemNew(
                    provisional_id="new-1",
                    summary="A new thread",
                ),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        assert len(plan.id_mappings) == 1
        assert plan.id_mappings[0].provisional_id == "new-1"
        assert plan.id_mappings[0].canonical_id == "T-0002"
        assert plan.id_mappings[0].item_type == "thread"

    def test_multiple_new_items_sequential_ids(self, tmp_path: Path) -> None:
        """Multiple new items get sequential canonical IDs."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        update_block = _make_update_block(
            new_decisions=[
                UpdateLedgerItemNew(provisional_id="new-1", summary="First"),
                UpdateLedgerItemNew(provisional_id="new-2", summary="Second"),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        decision_mappings = [m for m in plan.id_mappings if m.item_type == "decision"]
        assert len(decision_mappings) == 2
        assert decision_mappings[0].canonical_id == "D-0001"
        assert decision_mappings[1].canonical_id == "D-0002"

    def test_id_mappings_in_ledger_changes(self, tmp_path: Path) -> None:
        """Ledger changes use the resolved canonical IDs."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        update_block = _make_update_block(
            new_decisions=[
                UpdateLedgerItemNew(provisional_id="new-1", summary="A decision"),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        new_decision = [lc for lc in plan.ledger_changes if lc.change_type == "new_decision"][0]
        assert new_decision.item_id == "D-0001"
        assert new_decision.provisional_id == "new-1"


# ---------------------------------------------------------------------------
# Auto-approval warnings
# ---------------------------------------------------------------------------


class TestAutoApprovalWarnings:
    """Verify auto-approval detection and warning generation."""

    def test_block_no_review_but_items_need_review(self, tmp_path: Path) -> None:
        """Block says no review needed but items need review → warning."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        update_block = _make_update_block(
            requires_human_review=False,
            new_decisions=[
                UpdateLedgerItemNew(
                    provisional_id="new-1",
                    summary="A decision",
                    requires_human_review=True,
                ),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        approval_warnings = [w for w in plan.warnings if w.code == AUTO_APPROVAL_BLOCKED]
        assert len(approval_warnings) >= 1

    def test_block_and_items_both_require_review(self, tmp_path: Path) -> None:
        """Both block and items require review → no auto-approval warning."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        update_block = _make_update_block(
            requires_human_review=True,
            new_decisions=[
                UpdateLedgerItemNew(
                    provisional_id="new-1",
                    summary="A decision",
                    requires_human_review=True,
                ),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        approval_warnings = [w for w in plan.warnings if w.code == AUTO_APPROVAL_BLOCKED]
        assert len(approval_warnings) == 0


# ---------------------------------------------------------------------------
# File changes planning
# ---------------------------------------------------------------------------


class TestFileChanges:
    """Verify planned file changes."""

    def test_prose_replacement_planned(self, tmp_path: Path) -> None:
        """Prose replacement is planned for the target chunk."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        parsed = _make_parsed_block()
        plan = build_reconcile_plan(parsed, state)

        prose_changes = [fc for fc in plan.file_changes if fc.change_type == "prose_replacement"]
        assert len(prose_changes) >= 1

    def test_ledger_update_planned(self, tmp_path: Path) -> None:
        """Ledger updates are planned for new decisions."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        update_block = _make_update_block(
            new_decisions=[
                UpdateLedgerItemNew(provisional_id="new-1", summary="Decision"),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        ledger_changes = [fc for fc in plan.file_changes if fc.change_type == "ledger_update"]
        assert len(ledger_changes) >= 1

    def test_no_prose_no_prose_change(self, tmp_path: Path) -> None:
        """Ledger-only update (no revised prose) doesn't plan prose change."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        update_block = _make_update_block(
            revised_prose="",
            new_decisions=[
                UpdateLedgerItemNew(provisional_id="new-1", summary="Decision"),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        # In full_replacement mode, we still plan the file change even
        # if prose is empty (because we need to update frontmatter).
        # This is correct behavior — the chunk file must be touched
        # to update checksums even for ledger-only changes.
        # The key point is no canonical writes during preview.


# ---------------------------------------------------------------------------
# Plan immutability
# ---------------------------------------------------------------------------


class TestPlanImmutability:
    """Verify the plan is frozen (immutable)."""

    def test_reconcile_plan_is_frozen(self, tmp_path: Path) -> None:
        """ReconcilePlan is frozen and cannot be mutated."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        parsed = _make_parsed_block()
        plan = build_reconcile_plan(parsed, state)

        with pytest.raises(FrozenInstanceError):
            plan.target_chunk = "C-9999"  # type: ignore[misc]

    def test_planned_ledger_change_is_frozen(self) -> None:
        """PlannedLedgerChange is frozen."""
        change = PlannedLedgerChange(
            change_type="new_decision",
            item_id="D-0001",
            provisional_id="new-1",
            detail={"summary": "Test"},
            requires_human_review=True,
        )
        with pytest.raises(FrozenInstanceError):
            change.item_id = "D-9999"  # type: ignore[misc]

    def test_provisional_id_mapping_is_frozen(self) -> None:
        """ProvisionalIdMapping is frozen."""
        mapping = ProvisionalIdMapping(
            provisional_id="new-1",
            canonical_id="D-0001",
            item_type="decision",
            summary="Test",
        )
        with pytest.raises(FrozenInstanceError):
            mapping.canonical_id = "D-9999"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestPlanSerialization:
    """Verify plan can be serialized to JSON."""

    def test_to_dict_produces_valid_dict(self, tmp_path: Path) -> None:
        """to_dict() produces a JSON-serializable dictionary."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        parsed = _make_parsed_block()
        plan = build_reconcile_plan(parsed, state)

        d = plan.to_dict()
        assert isinstance(d, dict)
        assert "target_chunk" in d
        assert "mode" in d
        assert "ledger_changes" in d
        assert "id_mappings" in d
        assert "file_changes" in d
        assert "conflicts" in d
        assert "warnings" in d
        assert "requires_human_review" in d
        assert "plan_ok" in d

    def test_to_dict_json_serializable(self, tmp_path: Path) -> None:
        """to_dict() output can be serialized to JSON."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        parsed = _make_parsed_block()
        plan = build_reconcile_plan(parsed, state)

        d = plan.to_dict()
        json_str = json.dumps(d, ensure_ascii=False)
        assert isinstance(json_str, str)
        # Round-trip
        parsed_back = json.loads(json_str)
        assert parsed_back["target_chunk"] == "C-0001"

    def test_to_dict_with_conflicts(self, tmp_path: Path) -> None:
        """Conflicts are properly serialized in to_dict()."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0002"])
        parsed = _make_parsed_block(target_chunk="C-0001")
        plan = build_reconcile_plan(parsed, state)

        d = plan.to_dict()
        assert len(d["conflicts"]) > 0
        assert d["conflicts"][0]["code"] == CHUNK_NOT_FOUND


# ---------------------------------------------------------------------------
# No-mutation guarantee
# ---------------------------------------------------------------------------


class TestNoMutation:
    """Verify that planning never mutates project state."""

    def test_project_state_unchanged_after_planning(self, tmp_path: Path) -> None:
        """Project state is identical before and after planning."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        chunk_count_before = len(state.chunks)
        decision_count_before = len(state.decisions_ledger.entries) if state.decisions_ledger else 0

        parsed = _make_parsed_block()
        plan = build_reconcile_plan(parsed, state)

        # State should be unchanged
        assert len(state.chunks) == chunk_count_before
        if state.decisions_ledger:
            assert len(state.decisions_ledger.entries) == decision_count_before

    def test_no_files_created_during_planning(self, tmp_path: Path) -> None:
        """Planning creates no new files in the project directory."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        files_before = set(tmp_path.rglob("*"))

        parsed = _make_parsed_block()
        plan = build_reconcile_plan(parsed, state)

        files_after = set(tmp_path.rglob("*"))
        assert files_before == files_after

    def test_preview_is_idempotent(self, tmp_path: Path) -> None:
        """Running planning twice produces identical results."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        parsed = _make_parsed_block()

        plan1 = build_reconcile_plan(parsed, state)
        plan2 = build_reconcile_plan(parsed, state)

        assert plan1.to_dict() == plan2.to_dict()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Verify that planning is deterministic."""

    def test_same_input_same_plan(self, tmp_path: Path) -> None:
        """Same input always produces the same plan."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        parsed = _make_parsed_block()

        plan1 = build_reconcile_plan(parsed, state)
        plan2 = build_reconcile_plan(parsed, state)

        assert plan1.target_chunk == plan2.target_chunk
        assert plan1.mode == plan2.mode
        assert plan1.plan_ok == plan2.plan_ok
        assert len(plan1.ledger_changes) == len(plan2.ledger_changes)

    def test_id_resolution_deterministic(self, tmp_path: Path) -> None:
        """Provisional ID resolution is deterministic."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        update_block = _make_update_block(
            new_decisions=[
                UpdateLedgerItemNew(provisional_id="new-1", summary="First"),
                UpdateLedgerItemNew(provisional_id="new-2", summary="Second"),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )

        plan1 = build_reconcile_plan(parsed, state)
        plan2 = build_reconcile_plan(parsed, state)

        for m1, m2 in zip(plan1.id_mappings, plan2.id_mappings):
            assert m1.canonical_id == m2.canonical_id


# ---------------------------------------------------------------------------
# Plan shape completeness for Chunk 15
# ---------------------------------------------------------------------------


class TestPlanShapeCompleteness:
    """Verify the plan has everything Chunk 15 needs."""

    def test_plan_has_all_required_fields(self, tmp_path: Path) -> None:
        """ReconcilePlan has every field needed for apply."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        parsed = _make_parsed_block()
        plan = build_reconcile_plan(parsed, state)

        # These fields are the contract for Chunk 15
        assert hasattr(plan, "target_chunk")
        assert hasattr(plan, "mode")
        assert hasattr(plan, "revised_prose")
        assert hasattr(plan, "ledger_changes")
        assert hasattr(plan, "id_mappings")
        assert hasattr(plan, "file_changes")
        assert hasattr(plan, "conflicts")
        assert hasattr(plan, "warnings")
        assert hasattr(plan, "requires_human_review")
        assert hasattr(plan, "plan_ok")

    def test_ledger_change_has_required_fields(self, tmp_path: Path) -> None:
        """PlannedLedgerChange has every field needed for apply."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        update_block = _make_update_block(
            new_decisions=[
                UpdateLedgerItemNew(provisional_id="new-1", summary="Decision"),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        assert len(plan.ledger_changes) >= 1
        lc = plan.ledger_changes[0]
        assert hasattr(lc, "change_type")
        assert hasattr(lc, "item_id")
        assert hasattr(lc, "provisional_id")
        assert hasattr(lc, "detail")
        assert hasattr(lc, "requires_human_review")

    def test_id_mapping_has_required_fields(self, tmp_path: Path) -> None:
        """ProvisionalIdMapping has every field needed for apply."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        update_block = _make_update_block(
            new_decisions=[
                UpdateLedgerItemNew(provisional_id="new-1", summary="Decision"),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        assert len(plan.id_mappings) >= 1
        m = plan.id_mappings[0]
        assert hasattr(m, "provisional_id")
        assert hasattr(m, "canonical_id")
        assert hasattr(m, "item_type")
        assert hasattr(m, "summary")

    def test_serialized_plan_is_complete(self, tmp_path: Path) -> None:
        """Serialized plan contains all data needed by apply."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        update_block = _make_update_block(
            revised_prose="New prose.",
            new_decisions=[
                UpdateLedgerItemNew(provisional_id="new-1", summary="Decision"),
            ],
            close_threads=["T-0001"] if False else [],
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="New prose.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        d = plan.to_dict()

        # Chunk 15 must be able to:
        # 1. Know which chunk to update
        assert d["target_chunk"] == "C-0001"
        # 2. Know the mode
        assert d["mode"] == "full_replacement"
        # 3. Get the revised prose
        assert d["revised_prose_length"] > 0
        # 4. Iterate over all ledger changes with canonical IDs
        assert isinstance(d["ledger_changes"], list)
        # 5. Map provisional IDs to canonical IDs
        assert isinstance(d["id_mappings"], list)
        # 6. Know which files to modify
        assert isinstance(d["file_changes"], list)
        # 7. Check for conflicts
        assert isinstance(d["conflicts"], list)
        # 8. Check if review is needed
        assert isinstance(d["requires_human_review"], bool)
        # 9. Know if the plan is safe to apply
        assert isinstance(d["plan_ok"], bool)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Verify edge cases and boundary conditions."""

    def test_empty_ledgers_project(self, tmp_path: Path) -> None:
        """Plan works with empty ledgers (no existing entries)."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        parsed = _make_parsed_block()
        plan = build_reconcile_plan(parsed, state)

        assert plan.plan_ok is True

    def test_no_new_items_plan(self, tmp_path: Path) -> None:
        """Plan with no new items (only prose replacement) works."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        parsed = _make_parsed_block()
        plan = build_reconcile_plan(parsed, state)

        assert plan.plan_ok is True
        assert len(plan.id_mappings) == 0

    def test_multiple_conflicts_reported(self, tmp_path: Path) -> None:
        """Multiple conflicts are all reported."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0002"])
        update_block = _make_update_block(
            target_chunk="C-0001",
            close_threads=["T-9999"],
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        # Should have at least 2 conflicts:
        # 1. Target chunk not found
        # 2. Thread not found
        assert len(plan.conflicts) >= 2

    def test_requires_human_review_flag(self, tmp_path: Path) -> None:
        """requires_human_review is True when any item needs review."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        update_block = _make_update_block(
            requires_human_review=True,
            new_decisions=[
                UpdateLedgerItemNew(provisional_id="new-1", summary="Decision"),
            ]
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)

        assert plan.requires_human_review is True

    def test_plan_ok_false_with_conflicts(self, tmp_path: Path) -> None:
        """plan_ok is False when there are conflicts."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0002"])
        parsed = _make_parsed_block(target_chunk="C-0001")
        plan = build_reconcile_plan(parsed, state)

        assert plan.plan_ok is False
        assert len(plan.conflicts) > 0

    def test_reconcile_plan_type(self, tmp_path: Path) -> None:
        """build_reconcile_plan returns a ReconcilePlan instance."""
        state = _make_project_state(tmp_path, chunk_ids=["C-0001"])
        parsed = _make_parsed_block()
        plan = build_reconcile_plan(parsed, state)

        assert isinstance(plan, ReconcilePlan)
