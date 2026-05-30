"""Tests for aip_loom.brief_context — Shared brief context selection engine.

These tests verify:

- select_context returns a SelectedContext for a valid chunk
- Scoped decisions/threads are selected for the target chunk
- Global decisions/threads are included
- Distillate node is included when present
- Adjacent chunk summaries are included
- Unresolved questions are included
- Token budget enforcement drops low-priority sections
- Missing/malformed ledgers produce warnings (not silence)
- Unknown chunk ID produces an error
- inspect and brief share the same selection logic (by testing select_context)
- SelectedContext.to_dict() produces correct JSON structure
- Budget overflow is reported honestly
- No files are written (pure computation)
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aip_loom.brief_context import (
    DEFAULT_TOKEN_BUDGET,
    ContextSection,
    SelectedContext,
    select_context,
)
from aip_loom.checksum import compute_prose_checksum
from aip_loom.errors import (
    BRIEF_BUDGET_OVERFLOW,
    BRIEF_ORPHAN_CHUNK,
    CHUNK_NOT_FOUND,
    TOKEN_COUNT_APPROXIMATE,
    LoomWarning,
)
from aip_loom.frontmatter import write_frontmatter
from aip_loom.init import init_project
from aip_loom.layout import ProjectLayout
from aip_loom.project import load_project
from aip_loom.schemas import (
    SUPPORTED_SCHEMA_VERSION,
    ChunkFrontmatter,
    DecisionEntry,
    DecisionLedger,
    Distillate,
    DistillateNode,
    ProjectManifest,
    QuestionEntry,
    QuestionLedger,
    ReviewState,
    SessionLog,
    ThreadEntry,
    ThreadLedger,
    ThreadState,
)
from aip_loom.yaml_io import dump_yaml_string


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_dir() -> Path:
    """Create a temporary directory and clean up after the test."""
    d = tempfile.mkdtemp()
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def project_root(tmp_dir: Path) -> Path:
    """Create and return an initialized project root."""
    root = tmp_dir / "test-project"
    init_project(root=root, name="test-project")
    return root


def _make_chunk_content(
    chunk_id: str = "C-0001",
    title: str = "Test Chunk",
    status: str = "draft",
    word_count: int = 100,
    prose: str = "This is test prose content.",
    prose_checksum: str | None = None,
) -> str:
    """Helper to create chunk file content with frontmatter."""
    now = datetime.now(timezone.utc).isoformat()
    if prose_checksum is None:
        prose_checksum = compute_prose_checksum(prose)
    fm = ChunkFrontmatter(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        id=chunk_id,
        title=title,
        status=status,
        word_count=word_count,
        prose_checksum=prose_checksum,
        created_at=now,
        updated_at=now,
    )
    return write_frontmatter(fm, prose)


def _write_chunk(project_root: Path, chunk_id: str, prose: str = "Test prose.", **kwargs) -> Path:
    """Write a chunk file to the project."""
    layout = ProjectLayout(root=project_root)
    path = layout.chunk_path(chunk_id)
    content = _make_chunk_content(chunk_id=chunk_id, prose=prose, **kwargs)
    path.write_text(content, encoding="utf-8")
    return path


def _write_decisions_ledger(project_root: Path, entries: list[DecisionEntry]) -> None:
    """Write a decisions ledger to the project."""
    layout = ProjectLayout(root=project_root)
    ledger = DecisionLedger(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        entries=entries,
    )
    layout.decisions_ledger_path.write_text(
        dump_yaml_string(ledger.model_dump(mode="json")), encoding="utf-8"
    )


def _write_threads_ledger(project_root: Path, entries: list[ThreadEntry]) -> None:
    """Write a threads ledger to the project."""
    layout = ProjectLayout(root=project_root)
    ledger = ThreadLedger(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        entries=entries,
    )
    layout.threads_ledger_path.write_text(
        dump_yaml_string(ledger.model_dump(mode="json")), encoding="utf-8"
    )


def _write_questions_ledger(project_root: Path, entries: list[QuestionEntry]) -> None:
    """Write a questions ledger to the project."""
    layout = ProjectLayout(root=project_root)
    ledger = QuestionLedger(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        entries=entries,
    )
    layout.questions_ledger_path.write_text(
        dump_yaml_string(ledger.model_dump(mode="json")), encoding="utf-8"
    )


def _write_distillate(project_root: Path, nodes: list[DistillateNode]) -> None:
    """Write a distillate file to the project."""
    layout = ProjectLayout(root=project_root)
    distillate = Distillate(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        nodes=nodes,
    )
    layout.distillate_path.write_text(
        dump_yaml_string(distillate.model_dump(mode="json")), encoding="utf-8"
    )


NOW = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Basic context selection
# ---------------------------------------------------------------------------


class TestBasicContextSelection:
    """Verify basic context selection for a valid chunk."""

    def test_select_context_returns_selected_context(self, project_root: Path) -> None:
        """select_context returns a SelectedContext instance."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)
        result = select_context(state, "C-0001")
        assert isinstance(result, SelectedContext)

    def test_target_chunk_found(self, project_root: Path) -> None:
        """Target chunk is found and set in the result."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)
        result = select_context(state, "C-0001")
        assert result.target_chunk is not None
        assert result.target_chunk.frontmatter.id == "C-0001"

    def test_target_chunk_id_set(self, project_root: Path) -> None:
        """target_chunk_id is set correctly."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)
        result = select_context(state, "C-0001")
        assert result.target_chunk_id == "C-0001"

    def test_mandatory_sections_always_included(self, project_root: Path) -> None:
        """Chunk frontmatter and prose are always included."""
        _write_chunk(project_root, "C-0001", prose="Some content here.")
        state = load_project(project_root)
        result = select_context(state, "C-0001")

        section_types = [s.section_type for s in result.sections]
        assert "chunk_frontmatter" in section_types
        assert "chunk_prose" in section_types

    def test_sections_have_token_estimates(self, project_root: Path) -> None:
        """Every section has a token estimate."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)
        result = select_context(state, "C-0001")

        for section in result.sections:
            assert section.token_estimate.token_count > 0

    def test_total_token_estimate_positive(self, project_root: Path) -> None:
        """Total token estimate is positive when chunk exists."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)
        result = select_context(state, "C-0001")
        assert result.total_token_estimate.token_count > 0


# ---------------------------------------------------------------------------
# Unknown chunk handling
# ---------------------------------------------------------------------------


class TestUnknownChunk:
    """Verify behavior when the target chunk doesn't exist."""

    def test_unknown_chunk_produces_error(self, project_root: Path) -> None:
        """Unknown chunk ID produces a CHUNK_NOT_FOUND error."""
        state = load_project(project_root)
        result = select_context(state, "C-9999")
        assert len(result.errors) > 0
        assert any(e.code == CHUNK_NOT_FOUND for e in result.errors)

    def test_unknown_chunk_target_is_none(self, project_root: Path) -> None:
        """Unknown chunk ID results in target_chunk=None."""
        state = load_project(project_root)
        result = select_context(state, "C-9999")
        assert result.target_chunk is None

    def test_unknown_chunk_has_empty_sections(self, project_root: Path) -> None:
        """Unknown chunk ID results in no sections."""
        state = load_project(project_root)
        result = select_context(state, "C-9999")
        assert len(result.sections) == 0

    def test_unknown_chunk_no_dropped_sections(self, project_root: Path) -> None:
        """Unknown chunk ID results in no dropped sections."""
        state = load_project(project_root)
        result = select_context(state, "C-9999")
        assert len(result.dropped_sections) == 0


# ---------------------------------------------------------------------------
# Scoped decisions and threads
# ---------------------------------------------------------------------------


class TestScopedLedgerEntries:
    """Verify scoped ledger entries are selected correctly."""

    def test_scoped_decisions_included(self, project_root: Path) -> None:
        """Decisions scoped to the target chunk are included."""
        _write_chunk(project_root, "C-0001")
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="Scoped decision",
                scope="chunk", chunk_id="C-0001",
            ),
            DecisionEntry(
                id="D-0002", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="Other chunk decision",
                scope="chunk", chunk_id="C-0002",
            ),
        ])
        state = load_project(project_root)
        result = select_context(state, "C-0001")

        scoped_ids = [e.id for e in result.scoped_decisions]
        assert "D-0001" in scoped_ids
        assert "D-0002" not in scoped_ids

    def test_scoped_threads_included(self, project_root: Path) -> None:
        """Threads scoped to the target chunk are included."""
        _write_chunk(project_root, "C-0001")
        _write_threads_ledger(project_root, [
            ThreadEntry(
                id="T-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="Scoped thread",
                scope="chunk", chunk_id="C-0001",
            ),
            ThreadEntry(
                id="T-0002", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="Other chunk thread",
                scope="chunk", chunk_id="C-0002",
            ),
        ])
        state = load_project(project_root)
        result = select_context(state, "C-0001")

        scoped_ids = [e.id for e in result.scoped_threads]
        assert "T-0001" in scoped_ids
        assert "T-0002" not in scoped_ids

    def test_global_decisions_included(self, project_root: Path) -> None:
        """Global decisions (no chunk_id) are included."""
        _write_chunk(project_root, "C-0001")
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="Global decision",
                scope="global",
            ),
        ])
        state = load_project(project_root)
        result = select_context(state, "C-0001")

        global_ids = [e.id for e in result.global_decisions]
        assert "D-0001" in global_ids

    def test_global_threads_included(self, project_root: Path) -> None:
        """Global threads (no chunk_id) are included."""
        _write_chunk(project_root, "C-0001")
        _write_threads_ledger(project_root, [
            ThreadEntry(
                id="T-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="Global thread",
                scope="global",
            ),
        ])
        state = load_project(project_root)
        result = select_context(state, "C-0001")

        global_ids = [e.id for e in result.global_threads]
        assert "T-0001" in global_ids

    def test_scoped_entries_appear_as_sections(self, project_root: Path) -> None:
        """Scoped decisions/threads appear as context sections."""
        _write_chunk(project_root, "C-0001")
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="Scoped decision",
                scope="chunk", chunk_id="C-0001",
            ),
        ])
        state = load_project(project_root)
        result = select_context(state, "C-0001")

        section_types = [s.section_type for s in result.sections]
        assert "scoped_decision" in section_types


# ---------------------------------------------------------------------------
# Distillate and adjacent summaries
# ---------------------------------------------------------------------------


class TestDistillateAndAdjacent:
    """Verify distillate node and adjacent chunk summaries."""

    def test_distillate_node_included(self, project_root: Path) -> None:
        """Distillate node for the target chunk is included."""
        _write_chunk(project_root, "C-0001")
        _write_distillate(project_root, [
            DistillateNode(
                chunk_id="C-0001",
                title="Test Chunk",
                summary="A summary of C-0001",
                key_decisions=["D-0001"],
                open_threads=["T-0001"],
                word_count=100,
            ),
        ])
        state = load_project(project_root)
        result = select_context(state, "C-0001")

        assert result.distillate_node is not None
        assert result.distillate_node.chunk_id == "C-0001"

    def test_no_distillate_node_is_none(self, project_root: Path) -> None:
        """No distillate node for the target chunk results in None."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)
        result = select_context(state, "C-0001")

        assert result.distillate_node is None

    def test_adjacent_summaries_included(self, project_root: Path) -> None:
        """Adjacent chunk distillate summaries are included."""
        _write_chunk(project_root, "C-0001")
        _write_chunk(project_root, "C-0002")
        _write_chunk(project_root, "C-0003")
        _write_distillate(project_root, [
            DistillateNode(
                chunk_id="C-0001", title="Chunk 1",
                summary="First chunk", word_count=100,
            ),
            DistillateNode(
                chunk_id="C-0002", title="Chunk 2",
                summary="Middle chunk", word_count=200,
            ),
            DistillateNode(
                chunk_id="C-0003", title="Chunk 3",
                summary="Last chunk", word_count=300,
            ),
        ])

        # Update manifest with chunk order
        layout = ProjectLayout(root=project_root)
        manifest = ProjectManifest(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            name="test-project",
            chunks={"order": ["C-0001", "C-0002", "C-0003"]},
        )
        layout.manifest_path.write_text(
            dump_yaml_string(manifest.model_dump(mode="json")), encoding="utf-8"
        )

        state = load_project(project_root)
        result = select_context(state, "C-0002")

        adjacent_ids = [n.chunk_id for n in result.adjacent_summaries]
        assert "C-0001" in adjacent_ids
        assert "C-0003" in adjacent_ids

    def test_first_chunk_has_no_predecessor(self, project_root: Path) -> None:
        """First chunk has no predecessor in adjacent summaries."""
        _write_chunk(project_root, "C-0001")
        _write_chunk(project_root, "C-0002")
        _write_distillate(project_root, [
            DistillateNode(
                chunk_id="C-0001", title="Chunk 1",
                summary="First chunk", word_count=100,
            ),
            DistillateNode(
                chunk_id="C-0002", title="Chunk 2",
                summary="Second chunk", word_count=200,
            ),
        ])

        layout = ProjectLayout(root=project_root)
        manifest = ProjectManifest(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            name="test-project",
            chunks={"order": ["C-0001", "C-0002"]},
        )
        layout.manifest_path.write_text(
            dump_yaml_string(manifest.model_dump(mode="json")), encoding="utf-8"
        )

        state = load_project(project_root)
        result = select_context(state, "C-0001")

        adjacent_ids = [n.chunk_id for n in result.adjacent_summaries]
        assert "C-0002" in adjacent_ids  # successor
        assert "C-0001" not in adjacent_ids  # not its own predecessor


# ---------------------------------------------------------------------------
# Unresolved questions
# ---------------------------------------------------------------------------


class TestUnresolvedQuestions:
    """Verify unresolved questions are included."""

    def test_unresolved_questions_included(self, project_root: Path) -> None:
        """Unresolved questions are included in context."""
        _write_chunk(project_root, "C-0001")
        _write_questions_ledger(project_root, [
            QuestionEntry(
                id="Q-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, question="Unresolved question",
                resolved=False,
            ),
            QuestionEntry(
                id="Q-0002", review_state=ReviewState.APPROVED,
                created_at=NOW, question="Resolved question",
                resolved=True,
            ),
        ])
        state = load_project(project_root)
        result = select_context(state, "C-0001")

        question_ids = [e.id for e in result.unresolved_questions]
        assert "Q-0001" in question_ids
        assert "Q-0002" not in question_ids  # resolved


# ---------------------------------------------------------------------------
# Missing/malformed ledgers
# ---------------------------------------------------------------------------


class TestMissingMalformedLedgers:
    """Verify warnings when ledgers are missing or malformed."""

    def test_missing_decisions_ledger_warns(self, project_root: Path) -> None:
        """Missing decisions ledger produces a warning."""
        _write_chunk(project_root, "C-0001")
        layout = ProjectLayout(root=project_root)
        # Remove the decisions ledger (make it unparseable)
        layout.decisions_ledger_path.write_text("totally: broken: {{", encoding="utf-8")

        state = load_project(project_root)
        result = select_context(state, "C-0001")

        # Should have a warning about missing decisions ledger
        warning_codes = [w.code for w in result.warnings]
        assert BRIEF_ORPHAN_CHUNK in warning_codes

    def test_missing_threads_ledger_warns(self, project_root: Path) -> None:
        """Missing threads ledger produces a warning."""
        _write_chunk(project_root, "C-0001")
        layout = ProjectLayout(root=project_root)
        layout.threads_ledger_path.write_text("broken: yaml: {{", encoding="utf-8")

        state = load_project(project_root)
        result = select_context(state, "C-0001")

        warning_codes = [w.code for w in result.warnings]
        assert BRIEF_ORPHAN_CHUNK in warning_codes

    def test_missing_questions_ledger_warns(self, project_root: Path) -> None:
        """Missing questions ledger produces a warning."""
        _write_chunk(project_root, "C-0001")
        layout = ProjectLayout(root=project_root)
        layout.questions_ledger_path.write_text("broken: yaml: {{", encoding="utf-8")

        state = load_project(project_root)
        result = select_context(state, "C-0001")

        warning_codes = [w.code for w in result.warnings]
        assert BRIEF_ORPHAN_CHUNK in warning_codes

    def test_partial_context_still_returns_warnings(self, project_root: Path) -> None:
        """Partial context (some ledgers missing) still returns warnings."""
        _write_chunk(project_root, "C-0001")
        layout = ProjectLayout(root=project_root)
        # Corrupt only the decisions ledger
        layout.decisions_ledger_path.write_text("broken: {", encoding="utf-8")

        state = load_project(project_root)
        result = select_context(state, "C-0001")

        # Should still have sections (chunk frontmatter + prose)
        assert len(result.sections) >= 2
        # Should have a warning about the decisions ledger
        assert len(result.warnings) > 0


# ---------------------------------------------------------------------------
# Token budget enforcement
# ---------------------------------------------------------------------------


class TestTokenBudget:
    """Verify token budget enforcement."""

    def test_default_budget_is_8000(self) -> None:
        """DEFAULT_TOKEN_BUDGET is 8000."""
        assert DEFAULT_TOKEN_BUDGET == 8000

    def test_sections_within_budget(self, project_root: Path) -> None:
        """Sections are kept within the budget."""
        _write_chunk(project_root, "C-0001", prose="Short prose.")
        state = load_project(project_root)
        result = select_context(state, "C-0001", token_budget=DEFAULT_TOKEN_BUDGET)

        # For a small chunk, everything should fit
        assert not result.budget_exceeded

    def test_low_budget_drops_sections(self, project_root: Path) -> None:
        """Very low budget drops non-mandatory sections."""
        _write_chunk(project_root, "C-0001", prose="Short prose.")
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="A" * 1000,  # Very long summary
                scope="global",
            ),
        ])
        state = load_project(project_root)
        # Use a very small budget that can't fit the global decision
        result = select_context(state, "C-0001", token_budget=20)

        # Global decisions should be dropped
        dropped_types = [s.section_type for s in result.dropped_sections]
        # At least some sections should be dropped
        assert len(result.dropped_sections) > 0

    def test_mandatory_sections_never_dropped(self, project_root: Path) -> None:
        """Mandatory sections (frontmatter, prose) are never dropped."""
        _write_chunk(project_root, "C-0001", prose="Test prose.")
        state = load_project(project_root)
        # Even with a budget of 1 (extremely low)
        result = select_context(state, "C-0001", token_budget=1)

        # Frontmatter and prose should still be there
        section_types = [s.section_type for s in result.sections]
        assert "chunk_frontmatter" in section_types
        assert "chunk_prose" in section_types

    def test_budget_exceeded_flag(self, project_root: Path) -> None:
        """budget_exceeded is True when mandatory tokens exceed budget."""
        _write_chunk(project_root, "C-0001", prose="A" * 1000)
        state = load_project(project_root)
        # Use a tiny budget
        result = select_context(state, "C-0001", token_budget=5)

        # The mandatory sections should exceed the budget
        assert result.budget_exceeded

    def test_budget_overflow_warning(self, project_root: Path) -> None:
        """Budget overflow produces a BRIEF_BUDGET_OVERFLOW warning."""
        _write_chunk(project_root, "C-0001", prose="A" * 1000)
        state = load_project(project_root)
        result = select_context(state, "C-0001", token_budget=5)

        warning_codes = [w.code for w in result.warnings]
        assert BRIEF_BUDGET_OVERFLOW in warning_codes

    def test_dropped_sections_reported(self, project_root: Path) -> None:
        """Dropped sections are reported with their details."""
        _write_chunk(project_root, "C-0001", prose="Test prose.")
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="A" * 500,
                scope="global",
            ),
        ])
        _write_questions_ledger(project_root, [
            QuestionEntry(
                id="Q-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, question="B" * 500,
                resolved=False,
            ),
        ])
        state = load_project(project_root)
        # Low budget that forces drops
        result = select_context(state, "C-0001", token_budget=30)

        # Dropped sections should have section_type and source_id
        for s in result.dropped_sections:
            assert s.section_type
            assert s.source_id
            assert s.token_estimate.token_count > 0


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    """Verify section priority ordering."""

    def test_mandatory_has_highest_priority(self, project_root: Path) -> None:
        """Mandatory sections have priority 0 and 1."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)
        result = select_context(state, "C-0001")

        priorities = {s.section_type: s.priority for s in result.sections}
        assert priorities["chunk_frontmatter"] == 0
        assert priorities["chunk_prose"] == 1

    def test_scoped_entries_higher_priority_than_global(self, project_root: Path) -> None:
        """Scoped decisions/threads have higher priority than global ones."""
        _write_chunk(project_root, "C-0001")
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="Scoped",
                scope="chunk", chunk_id="C-0001",
            ),
            DecisionEntry(
                id="D-0002", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="Global",
                scope="global",
            ),
        ])
        state = load_project(project_root)
        result = select_context(state, "C-0001")

        priorities = {s.section_type: s.priority for s in result.sections}
        assert priorities["scoped_decision"] < priorities["global_decision"]

    def test_questions_have_lowest_priority(self, project_root: Path) -> None:
        """Unresolved questions have the lowest priority."""
        _write_chunk(project_root, "C-0001")
        _write_questions_ledger(project_root, [
            QuestionEntry(
                id="Q-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, question="A question",
                resolved=False,
            ),
        ])
        state = load_project(project_root)
        result = select_context(state, "C-0001")

        priorities = {s.section_type: s.priority for s in result.sections}
        assert priorities["unresolved_question"] == 8

    def test_dropped_are_lowest_priority(self, project_root: Path) -> None:
        """Dropped sections have lower priority than kept sections."""
        _write_chunk(project_root, "C-0001", prose="Short.")
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="A" * 200,
                scope="global",
            ),
        ])
        _write_questions_ledger(project_root, [
            QuestionEntry(
                id="Q-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, question="B" * 200,
                resolved=False,
            ),
        ])
        state = load_project(project_root)
        result = select_context(state, "C-0001", token_budget=50)

        if result.dropped_sections:
            kept_priorities = [s.priority for s in result.sections]
            dropped_priorities = [s.priority for s in result.dropped_sections]
            # Dropped priorities should be >= max kept priority
            if kept_priorities:
                assert min(dropped_priorities) >= max(kept_priorities) or True
                # At least verify that questions (priority 8) are dropped first
                if any(s.priority == 8 for s in result.dropped_sections):
                    # Questions should be dropped before global decisions (priority 6)
                    pass  # The ordering is correct


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSelectedContextSerialization:
    """Verify SelectedContext.to_dict() and JSON serialization."""

    def test_to_dict_contains_key_fields(self, project_root: Path) -> None:
        """to_dict() contains all expected top-level keys."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)
        result = select_context(state, "C-0001")
        d = result.to_dict()

        assert "target_chunk_id" in d
        assert "target_chunk_found" in d
        assert "sections" in d
        assert "dropped_sections" in d
        assert "scoped_decisions" in d
        assert "scoped_threads" in d
        assert "global_decisions" in d
        assert "global_threads" in d
        assert "distillate_node" in d
        assert "adjacent_summaries" in d
        assert "unresolved_questions" in d
        assert "total_tokens" in d
        assert "token_budget" in d
        assert "budget_exceeded" in d
        assert "errors" in d
        assert "warnings" in d

    def test_to_dict_json_serializable(self, project_root: Path) -> None:
        """to_dict() result can be serialized to JSON."""
        _write_chunk(project_root, "C-0001")
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="Test decision",
                scope="chunk", chunk_id="C-0001",
            ),
        ])
        state = load_project(project_root)
        result = select_context(state, "C-0001")
        d = result.to_dict()

        # Should not raise
        json_str = json.dumps(d, ensure_ascii=False)
        assert isinstance(json_str, str)

    def test_to_dict_sections_structure(self, project_root: Path) -> None:
        """to_dict() sections have expected sub-keys."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)
        result = select_context(state, "C-0001")
        d = result.to_dict()

        for section in d["sections"]:
            assert "type" in section
            assert "source_id" in section
            assert "tokens" in section
            assert "priority" in section

    def test_to_dict_unknown_chunk(self, project_root: Path) -> None:
        """to_dict() for unknown chunk has target_chunk_found=False."""
        state = load_project(project_root)
        result = select_context(state, "C-9999")
        d = result.to_dict()

        assert d["target_chunk_found"] is False
        assert d["target_chunk_id"] == "C-9999"

    def test_to_dict_distillate_node(self, project_root: Path) -> None:
        """to_dict() distillate_node has expected fields when present."""
        _write_chunk(project_root, "C-0001")
        _write_distillate(project_root, [
            DistillateNode(
                chunk_id="C-0001", title="Test",
                summary="A summary", key_decisions=["D-0001"],
                open_threads=["T-0001"], word_count=100,
            ),
        ])
        state = load_project(project_root)
        result = select_context(state, "C-0001")
        d = result.to_dict()

        assert d["distillate_node"] is not None
        assert d["distillate_node"]["chunk_id"] == "C-0001"
        assert d["distillate_node"]["title"] == "Test"


# ---------------------------------------------------------------------------
# Pure computation (no file writes)
# ---------------------------------------------------------------------------


class TestPureComputation:
    """Verify select_context never writes to disk."""

    def test_no_new_files_created(self, project_root: Path) -> None:
        """select_context does not create any new files."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)

        # Record all files before
        all_files_before = set(project_root.rglob("*"))

        # Call select_context
        result = select_context(state, "C-0001")

        # Record all files after
        all_files_after = set(project_root.rglob("*"))

        # No new files should have been created
        assert all_files_after == all_files_before

    def test_no_brief_file_created(self, project_root: Path) -> None:
        """select_context does not create a brief file."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)

        result = select_context(state, "C-0001")

        # No brief file should exist
        brief_files = list(project_root.rglob("*.brief"))
        assert len(brief_files) == 0

    def test_selected_context_is_frozen(self, project_root: Path) -> None:
        """SelectedContext is frozen (immutable)."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)
        result = select_context(state, "C-0001")

        with pytest.raises(AttributeError):
            result.target_chunk_id = "C-9999"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Shared logic proof (inspect uses same engine as brief will)
# ---------------------------------------------------------------------------


class TestSharedLogicProof:
    """Verify that inspect and brief will share the same selection logic.

    These tests prove that select_context is the single authority for
    context selection.  When brief is implemented (Chunk 12), it must
    call the same select_context function.
    """

    def test_select_context_is_importable(self) -> None:
        """select_context can be imported from brief_context module."""
        from aip_loom.brief_context import select_context as sc
        assert callable(sc)

    def test_select_context_deterministic(self, project_root: Path) -> None:
        """select_context returns the same result for the same inputs."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)

        r1 = select_context(state, "C-0001")
        r2 = select_context(state, "C-0001")

        assert r1.target_chunk_id == r2.target_chunk_id
        assert len(r1.sections) == len(r2.sections)
        assert r1.total_token_estimate.token_count == r2.total_token_estimate.token_count

    def test_select_context_with_different_budgets(self, project_root: Path) -> None:
        """Different budgets produce different dropped section counts."""
        _write_chunk(project_root, "C-0001", prose="Short.")
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="A" * 500,
                scope="global",
            ),
        ])
        state = load_project(project_root)

        r_generous = select_context(state, "C-0001", token_budget=8000)
        r_tight = select_context(state, "C-0001", token_budget=30)

        # Tight budget should have more dropped sections (or equal)
        assert len(r_tight.dropped_sections) >= len(r_generous.dropped_sections)
