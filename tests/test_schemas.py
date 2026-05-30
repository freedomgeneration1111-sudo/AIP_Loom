"""Tests for aip_loom.schemas — Pydantic model validation.

These tests prove that the schema module enforces the BuildSpec rules:
- extra="forbid" rejects unknown fields
- required fields cannot be omitted
- semver schema version is validated
- model-proposed canonical IDs are rejected
- enum values are enforced
- update block constraints hold
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aip_loom.schemas import (
    SUPPORTED_SCHEMA_VERSION,
    ChunkFrontmatter,
    ChunkOrder,
    ChunkStatus,
    CommentEntry,
    CommentLog,
    DecisionEntry,
    DecisionLedger,
    Distillate,
    DistillateNode,
    ProjectManifest,
    ProjectType,
    QuestionEntry,
    QuestionLedger,
    ReviewState,
    SchemaVersionCheck,
    SessionEntry,
    SessionLog,
    ThreadEntry,
    ThreadLedger,
    ThreadState,
    UpdateBlock,
    UpdateExistingEntry,
    UpdateLedgerItemNew,
    UpdateMode,
    UpdateThreadItemNew,
    validate_schema_version,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_V = SUPPORTED_SCHEMA_VERSION  # shorthand


def _frontmatter(**overrides: object) -> dict:
    """Return a minimal valid frontmatter dict, with overrides applied."""
    base = {
        "schema_version": _V,
        "id": "C-0001",
        "title": "Chapter One",
        "word_count": 500,
        "prose_checksum": "abc123",
        "created_at": "2026-05-28T12:00:00Z",
        "updated_at": "2026-05-28T12:00:00Z",
    }
    base.update(overrides)
    return base


def _manifest(**overrides: object) -> dict:
    base = {
        "schema_version": _V,
        "name": "my-novel",
        "project_type": "novel",
        "created_at": "2026-05-28T12:00:00Z",
        "updated_at": "2026-05-28T12:00:00Z",
    }
    base.update(overrides)
    return base


def _update_block(**overrides: object) -> dict:
    base = {
        "schema_version": _V,
        "fence_type": "loom-update",
        "mode": "full_replacement",
        "target_chunk": "C-0001",
        "revised_prose": "The quick brown fox.",
        "change_summary": "Revised opening.",
        "requires_human_review": True,
    }
    base.update(overrides)
    return base


# ===========================================================================
# Schema version validation
# ===========================================================================


class TestSchemaVersion:
    """Tests for semver schema version validation (BuildSpec §4.1)."""

    def test_valid_supported_version(self) -> None:
        assert validate_schema_version("0.1.0") == "0.1.0"

    def test_valid_patch_bump_accepted(self) -> None:
        """Patch version changes must not break loading."""
        assert validate_schema_version("0.1.1") == "0.1.1"

    def test_valid_minor_bump_accepted(self) -> None:
        """Unknown minor version is accepted (caller emits warning)."""
        assert validate_schema_version("0.2.0") == "0.2.0"

    def test_unknown_major_rejected(self) -> None:
        """Unknown major version is a hard error (BuildSpec §4.1)."""
        with pytest.raises(ValueError, match="Unsupported schema major version"):
            validate_schema_version("1.0.0")

    def test_invalid_semver_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid schema_version"):
            validate_schema_version("not-semver")

    def test_schema_version_check_model_valid(self) -> None:
        m = SchemaVersionCheck(schema_version="0.1.0")
        assert m.schema_version == "0.1.0"

    def test_schema_version_check_model_bad_major(self) -> None:
        with pytest.raises(ValidationError):
            SchemaVersionCheck(schema_version="2.0.0")


# ===========================================================================
# extra="forbid" enforcement
# ===========================================================================


class TestExtraForbid:
    """Every model must reject unknown fields (BuildSpec anti-pattern #2)."""

    def test_manifest_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ProjectManifest(**_manifest(unknown_field="bad"))

    def test_frontmatter_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ChunkFrontmatter(**_frontmatter(bogus="nope"))

    def test_decision_entry_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            DecisionEntry(
                id="D-0001",
                review_state="approved",
                created_at="2026-05-28T12:00:00Z",
                summary="Decided X",
                surprise="bad",
            )

    def test_update_block_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            UpdateBlock(**_update_block(extra_key="nope"))

    def test_distillate_node_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            DistillateNode(chunk_id="C-0001", title="Ch1", extra="bad")

    def test_session_entry_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            SessionEntry(
                id="S-0001",
                chunk_id="C-0001",
                created_at="2026-05-28T12:00:00Z",
                rogue="nope",
            )


# ===========================================================================
# Required field enforcement
# ===========================================================================


class TestRequiredFields:
    """Models must not allow None to hide missing required data."""

    def test_manifest_requires_name(self) -> None:
        data = _manifest()
        del data["name"]
        with pytest.raises(ValidationError, match="name"):
            ProjectManifest(**data)

    def test_frontmatter_requires_id(self) -> None:
        data = _frontmatter()
        del data["id"]
        with pytest.raises(ValidationError, match="id"):
            ChunkFrontmatter(**data)

    def test_decision_entry_requires_summary(self) -> None:
        with pytest.raises(ValidationError, match="summary"):
            DecisionEntry(
                id="D-0001",
                review_state="approved",
                created_at="2026-05-28T12:00:00Z",
                summary="",
            )

    def test_update_block_requires_target_chunk(self) -> None:
        data = _update_block()
        del data["target_chunk"]
        with pytest.raises(ValidationError, match="target_chunk"):
            UpdateBlock(**data)


# ===========================================================================
# Enum validation
# ===========================================================================


class TestEnumValidation:
    """Invalid enum values must be rejected."""

    def test_invalid_review_state(self) -> None:
        with pytest.raises(ValidationError):
            DecisionEntry(
                id="D-0001",
                review_state="not_a_state",
                created_at="2026-05-28T12:00:00Z",
                summary="X",
            )

    def test_invalid_project_type(self) -> None:
        with pytest.raises(ValidationError):
            ProjectManifest(**_manifest(project_type="nonexistent"))

    def test_invalid_chunk_status(self) -> None:
        with pytest.raises(ValidationError):
            ChunkFrontmatter(**_frontmatter(status="nonexistent"))

    def test_invalid_thread_state(self) -> None:
        with pytest.raises(ValidationError):
            ThreadEntry(
                id="T-0001",
                review_state="approved",
                created_at="2026-05-28T12:00:00Z",
                summary="X",
                state="nonexistent",
            )

    def test_valid_enum_values(self) -> None:
        """All valid enum values should be accepted."""
        assert ProjectManifest(**_manifest(project_type="novel")).project_type == ProjectType.NOVEL
        assert ProjectManifest(**_manifest(project_type="technical")).project_type == ProjectType.TECHNICAL
        assert ProjectManifest(**_manifest(project_type="academic")).project_type == ProjectType.ACADEMIC
        assert ProjectManifest(**_manifest(project_type="general")).project_type == ProjectType.GENERAL


# ===========================================================================
# ID validation
# ===========================================================================


class TestIdValidation:
    """IDs must follow canonical patterns."""

    def test_valid_chunk_id(self) -> None:
        fm = ChunkFrontmatter(**_frontmatter(id="C-0001"))
        assert fm.id == "C-0001"

    def test_valid_chunk_id_longer_prefix(self) -> None:
        fm = ChunkFrontmatter(**_frontmatter(id="CH-0012"))
        assert fm.id == "CH-0012"

    def test_invalid_chunk_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Invalid chunk ID"):
            ChunkFrontmatter(**_frontmatter(id="bad-id"))

    def test_decision_id_must_have_d_prefix(self) -> None:
        with pytest.raises(ValidationError):
            DecisionEntry(
                id="X-0001",
                review_state="approved",
                created_at="2026-05-28T12:00:00Z",
                summary="X",
            )

    def test_thread_id_must_have_t_prefix(self) -> None:
        with pytest.raises(ValidationError):
            ThreadEntry(
                id="D-0001",
                review_state="approved",
                created_at="2026-05-28T12:00:00Z",
                summary="X",
            )

    def test_question_id_must_have_q_prefix(self) -> None:
        with pytest.raises(ValidationError):
            QuestionEntry(
                id="D-0001",
                review_state="approved",
                created_at="2026-05-28T12:00:00Z",
                question="Why?",
            )


# ===========================================================================
# Model-proposed canonical ID rejection
# ===========================================================================


class TestModelAssignedIdRejection:
    """Model-proposed canonical IDs in new ledger items are forbidden (§7)."""

    def test_new_decision_with_canonical_id_rejected(self) -> None:
        """Canonical IDs are rejected — they fail the provisional_id pattern
        and would also be caught by the model_validator as defense-in-depth."""
        with pytest.raises(ValidationError):
            UpdateLedgerItemNew(
                provisional_id="D-0001",
                summary="New decision",
            )

    def test_new_thread_with_canonical_id_rejected(self) -> None:
        """Canonical IDs are rejected — they fail the provisional_id pattern."""
        with pytest.raises(ValidationError):
            UpdateThreadItemNew(
                provisional_id="T-0001",
                summary="New thread",
            )

    def test_new_decision_with_provisional_id_accepted(self) -> None:
        item = UpdateLedgerItemNew(
            provisional_id="new-1",
            summary="New decision",
        )
        assert item.provisional_id == "new-1"
        assert item.review_state == ReviewState.PENDING

    def test_new_thread_with_provisional_id_accepted(self) -> None:
        item = UpdateThreadItemNew(
            provisional_id="new-2",
            summary="New thread",
        )
        assert item.provisional_id == "new-2"
        assert item.review_state == ReviewState.PENDING

    def test_new_item_cannot_force_approved(self) -> None:
        """Model output may not force review_state=approved on new items."""
        with pytest.raises(ValidationError):
            UpdateLedgerItemNew(
                provisional_id="new-1",
                summary="Sneaky approval",
                review_state="approved",
            )

    def test_provisional_id_must_match_pattern(self) -> None:
        with pytest.raises(ValidationError, match="provisional_id"):
            UpdateLedgerItemNew(
                provisional_id="invalid-id",
                summary="Bad provisional",
            )


# ===========================================================================
# Update block constraints
# ===========================================================================


class TestUpdateBlockConstraints:
    """Update block must enforce Phase 1 rules (§7)."""

    def test_valid_full_replacement(self) -> None:
        ub = UpdateBlock(**_update_block())
        assert ub.mode == UpdateMode.FULL_REPLACEMENT
        assert ub.fence_type == "loom-update"

    def test_patch_mode_rejected(self) -> None:
        with pytest.raises(ValidationError, match="PATCH mode is unsupported"):
            UpdateBlock(**_update_block(mode="patch"))

    def test_thread_update_fence_rejected(self) -> None:
        """thread-update is not accepted in Phase 1 (§7)."""
        with pytest.raises(ValidationError):
            UpdateBlock(**_update_block(fence_type="thread-update"))

    def test_update_block_with_new_items(self) -> None:
        ub = UpdateBlock(
            **_update_block(
                new_decisions=[
                    {"provisional_id": "new-1", "summary": "New decision"},
                ],
                new_threads=[
                    {"provisional_id": "new-2", "summary": "New thread"},
                ],
                close_threads=["T-0001"],
            )
        )
        assert len(ub.new_decisions) == 1
        assert len(ub.new_threads) == 1
        assert ub.close_threads == ["T-0001"]

    def test_invalid_target_chunk_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Invalid target_chunk"):
            UpdateBlock(**_update_block(target_chunk="bad-chunk"))

    def test_invalid_close_thread_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Invalid thread ID"):
            UpdateBlock(**_update_block(close_threads=["bad-id"]))

    def test_update_existing_valid(self) -> None:
        ub = UpdateBlock(
            **_update_block(
                update_existing=[
                    {"id": "D-0001", "changes": {"rationale": "Updated reason"}},
                ],
            )
        )
        assert len(ub.update_existing) == 1

    def test_update_existing_invalid_id(self) -> None:
        with pytest.raises(ValidationError, match="Invalid canonical ID"):
            UpdateBlock(
                **_update_block(
                    update_existing=[
                        {"id": "bad-id", "changes": {}},
                    ],
                )
            )


# ===========================================================================
# Ledger file models
# ===========================================================================


class TestLedgerFiles:
    """Ledger files must validate correctly."""

    def test_decision_ledger_valid(self) -> None:
        dl = DecisionLedger(
            schema_version=_V,
            entries=[
                {
                    "id": "D-0001",
                    "review_state": "approved",
                    "created_at": "2026-05-28T12:00:00Z",
                    "summary": "Use present tense",
                },
            ],
        )
        assert len(dl.entries) == 1

    def test_thread_ledger_valid(self) -> None:
        tl = ThreadLedger(
            schema_version=_V,
            entries=[
                {
                    "id": "T-0001",
                    "review_state": "approved",
                    "created_at": "2026-05-28T12:00:00Z",
                    "summary": "Resolve character arc",
                    "state": "open",
                },
            ],
        )
        assert len(tl.entries) == 1

    def test_question_ledger_valid(self) -> None:
        ql = QuestionLedger(
            schema_version=_V,
            entries=[
                {
                    "id": "Q-0001",
                    "review_state": "approved",
                    "created_at": "2026-05-28T12:00:00Z",
                    "question": "What POV?",
                },
            ],
        )
        assert len(ql.entries) == 1

    def test_empty_ledger_valid(self) -> None:
        dl = DecisionLedger(schema_version=_V)
        assert dl.entries == []

    def test_ledger_bad_schema_version(self) -> None:
        with pytest.raises(ValidationError):
            DecisionLedger(schema_version="99.0.0")


# ===========================================================================
# Distillate
# ===========================================================================


class TestDistillate:
    def test_valid_distillate_node(self) -> None:
        dn = DistillateNode(
            chunk_id="C-0001",
            title="Chapter One",
            summary="Opening chapter",
            key_decisions=["D-0001"],
            open_threads=["T-0001"],
            word_count=500,
        )
        assert dn.chunk_id == "C-0001"

    def test_distillate_node_invalid_chunk_id(self) -> None:
        with pytest.raises(ValidationError, match="Invalid chunk_id"):
            DistillateNode(chunk_id="bad", title="X")

    def test_distillate_node_invalid_decision_id(self) -> None:
        with pytest.raises(ValidationError, match="Invalid ID in list"):
            DistillateNode(chunk_id="C-0001", title="X", key_decisions=["bad-id"])

    def test_distillate_file_valid(self) -> None:
        d = Distillate(
            schema_version=_V,
            nodes=[
                {"chunk_id": "C-0001", "title": "Ch1"},
            ],
        )
        assert len(d.nodes) == 1


# ===========================================================================
# Session and comment logs
# ===========================================================================


class TestSessionLog:
    def test_valid_session_entry(self) -> None:
        se = SessionEntry(
            id="S-0001",
            chunk_id="C-0001",
            created_at="2026-05-28T12:00:00Z",
        )
        assert se.id == "S-0001"

    def test_session_entry_invalid_id(self) -> None:
        with pytest.raises(ValidationError):
            SessionEntry(
                id="X-0001",
                chunk_id="C-0001",
                created_at="2026-05-28T12:00:00Z",
            )

    def test_session_log_valid(self) -> None:
        sl = SessionLog(schema_version=_V, entries=[])
        assert sl.entries == []


class TestCommentLog:
    def test_valid_comment_entry(self) -> None:
        ce = CommentEntry(
            id="CM-0001",
            target_id="C-0001",
            content="Needs revision",
            created_at="2026-05-28T12:00:00Z",
        )
        assert ce.id == "CM-0001"

    def test_comment_entry_empty_content_rejected(self) -> None:
        with pytest.raises(ValidationError, match="content"):
            CommentEntry(
                id="CM-0001",
                target_id="C-0001",
                content="",
                created_at="2026-05-28T12:00:00Z",
            )


# ===========================================================================
# Project manifest
# ===========================================================================


class TestProjectManifest:
    def test_valid_manifest(self) -> None:
        pm = ProjectManifest(**_manifest())
        assert pm.name == "my-novel"
        assert pm.project_type == ProjectType.NOVEL

    def test_manifest_with_chunk_order(self) -> None:
        pm = ProjectManifest(
            **_manifest(chunks={"order": ["C-0001", "C-0002"]})
        )
        assert pm.chunks.order == ["C-0001", "C-0002"]

    def test_manifest_invalid_chunk_in_order(self) -> None:
        with pytest.raises(ValidationError, match="Invalid chunk ID in order"):
            ProjectManifest(**_manifest(chunks={"order": ["bad-id"]}))

    def test_manifest_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="name"):
            ProjectManifest(**_manifest(name=""))

    def test_manifest_bad_schema_version(self) -> None:
        with pytest.raises(ValidationError):
            ProjectManifest(**_manifest(schema_version="5.0.0"))


# ===========================================================================
# Cross-model consistency
# ===========================================================================


class TestCrossModelConsistency:
    """Verify that schemas work together as expected."""

    def test_full_project_roundtrip(self) -> None:
        """Build a full project from schemas and verify all validate."""
        manifest = ProjectManifest(
            schema_version=_V,
            name="test-novel",
            project_type="novel",
            chunks={"order": ["C-0001"]},
        )
        fm = ChunkFrontmatter(**_frontmatter())
        dl = DecisionLedger(
            schema_version=_V,
            entries=[{
                "id": "D-0001",
                "review_state": "approved",
                "created_at": "2026-05-28T12:00:00Z",
                "summary": "Use present tense",
            }],
        )
        tl = ThreadLedger(schema_version=_V)
        dist = Distillate(
            schema_version=_V,
            nodes=[{
                "chunk_id": "C-0001",
                "title": "Chapter One",
                "key_decisions": ["D-0001"],
            }],
        )
        assert manifest.name == "test-novel"
        assert fm.id == "C-0001"
        assert len(dl.entries) == 1
        assert len(dist.nodes) == 1

    def test_update_block_to_ledger_flow(self) -> None:
        """Verify update block items can be validated and have correct
        provisional IDs (ready for ID allocation in reconcile)."""
        ub = UpdateBlock(
            **_update_block(
                new_decisions=[
                    {"provisional_id": "new-1", "summary": "New decision"},
                ],
                new_threads=[
                    {"provisional_id": "new-2", "summary": "New thread", "state": "open"},
                ],
            )
        )
        assert ub.new_decisions[0].provisional_id == "new-1"
        assert ub.new_decisions[0].review_state == ReviewState.PENDING
        assert ub.new_threads[0].review_state == ReviewState.PENDING
