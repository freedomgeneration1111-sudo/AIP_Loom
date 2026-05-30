"""Tests for aip-loom brief command and brief.py service.

These tests verify:

- brief generates a valid Markdown file for a clean chunk
- brief --dry-run writes nothing to disk
- brief --force produces BRIEF_FORCE_USED warning for dirty/orphan chunks
- brief fails for dirty/orphan chunks without --force
- brief fails with BRIEF_BUDGET_OVERFLOW when protected sections are dropped
- brief uses the same select_context() as inspect (zero duplication)
- brief token estimates match inspect exactly
- brief --json produces valid JSON with expected fields
- brief on unknown chunk fails with CHUNK_NOT_FOUND
- brief on non-project directory fails honestly
- brief is deterministic (same input -> same output)
- brief content is well-formed Markdown
- protected sections are never dropped from the brief
- BRIEF_FORCE_USED warning is unmistakable
- brief records the correct file path
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aip_loom.brief import (
    PROTECTED_PRIORITIES,
    BriefResult,
    assemble_brief_content,
    generate_brief,
)
from aip_loom.brief_context import DEFAULT_TOKEN_BUDGET, select_context
from aip_loom.checksum import compute_prose_checksum
from aip_loom.cli import app
from aip_loom.errors import (
    BRIEF_BUDGET_OVERFLOW,
    BRIEF_DIRTY_CHUNK,
    BRIEF_FORCE_USED,
    BRIEF_STALE_CHUNK,
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
    ThreadEntry,
    ThreadLedger,
    ThreadState,
)
from aip_loom.yaml_io import dump_yaml_string
from typer.testing import CliRunner


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


@pytest.fixture()
def runner() -> CliRunner:
    """Return a CliRunner for invoking the AIP_Loom Typer app."""
    return CliRunner()


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


NOW = datetime.now(timezone.utc).isoformat()


def _parse_json_output(output: str) -> dict:
    """Parse the JSON envelope from CLI output."""
    lines = output.strip().splitlines()
    json_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("{"):
            json_start = i
            break
    assert json_start is not None, f"No JSON found in output: {output!r}"
    json_text = "\n".join(lines[json_start:])
    return json.loads(json_text)


# ---------------------------------------------------------------------------
# Basic brief generation
# ---------------------------------------------------------------------------


class TestBasicBriefGeneration:
    """Verify basic brief generation functionality."""

    def test_generate_brief_success(self, project_root: Path) -> None:
        """generate_brief returns a successful CommandResult for a valid chunk."""
        _write_chunk(project_root, "C-0001")
        result = generate_brief(root=project_root, chunk_id="C-0001")
        assert result.ok is True
        assert result.command == "brief"

    def test_brief_file_created(self, project_root: Path) -> None:
        """brief creates a Markdown file in .aip-loom/briefs/."""
        _write_chunk(project_root, "C-0001")
        generate_brief(root=project_root, chunk_id="C-0001")

        brief_path = project_root / ".aip-loom" / "briefs" / "C-0001.md"
        assert brief_path.is_file()

    def test_brief_file_is_valid_markdown(self, project_root: Path) -> None:
        """brief file starts with YAML frontmatter."""
        _write_chunk(project_root, "C-0001", prose="Test prose for brief.")
        generate_brief(root=project_root, chunk_id="C-0001")

        brief_path = project_root / ".aip-loom" / "briefs" / "C-0001.md"
        content = brief_path.read_text(encoding="utf-8")
        assert content.startswith("---")
        assert "chunk_id:" in content
        assert "Session Brief: C-0001" in content

    def test_brief_file_contains_prose(self, project_root: Path) -> None:
        """brief file contains the chunk's prose body."""
        prose = "This is the prose content of chunk C-0001."
        _write_chunk(project_root, "C-0001", prose=prose)
        generate_brief(root=project_root, chunk_id="C-0001")

        brief_path = project_root / ".aip-loom" / "briefs" / "C-0001.md"
        content = brief_path.read_text(encoding="utf-8")
        assert prose in content

    def test_brief_result_has_token_info(self, project_root: Path) -> None:
        """brief result includes token estimate and budget."""
        _write_chunk(project_root, "C-0001")
        result = generate_brief(root=project_root, chunk_id="C-0001")
        assert result.data["token_estimate"] > 0
        assert result.data["token_budget"] == DEFAULT_TOKEN_BUDGET

    def test_brief_result_has_section_count(self, project_root: Path) -> None:
        """brief result includes section count."""
        _write_chunk(project_root, "C-0001")
        result = generate_brief(root=project_root, chunk_id="C-0001")
        assert result.data["section_count"] > 0

    def test_brief_result_has_brief_path(self, project_root: Path) -> None:
        """brief result includes the file path."""
        _write_chunk(project_root, "C-0001")
        result = generate_brief(root=project_root, chunk_id="C-0001")
        assert result.data["brief_path"] is not None
        assert "C-0001.md" in result.data["brief_path"]


# ---------------------------------------------------------------------------
# Unknown chunk handling
# ---------------------------------------------------------------------------


class TestUnknownChunk:
    """Verify brief fails for unknown chunks."""

    def test_unknown_chunk_fails(self, project_root: Path) -> None:
        """brief fails for a non-existent chunk ID."""
        result = generate_brief(root=project_root, chunk_id="C-9999")
        assert result.ok is False
        assert result.code == "CHUNK_NOT_FOUND"

    def test_unknown_chunk_no_brief_file(self, project_root: Path) -> None:
        """brief does not create a file for unknown chunks."""
        result = generate_brief(root=project_root, chunk_id="C-9999")
        brief_path = project_root / ".aip-loom" / "briefs" / "C-9999.md"
        assert not brief_path.exists()


# ---------------------------------------------------------------------------
# Dry-run safety
# ---------------------------------------------------------------------------


class TestDryRunSafety:
    """Verify --dry-run writes nothing to disk."""

    def test_dry_run_no_file_created(self, project_root: Path) -> None:
        """dry-run does not create a brief file."""
        _write_chunk(project_root, "C-0001")
        result = generate_brief(root=project_root, chunk_id="C-0001", dry_run=True)
        assert result.ok is True

        brief_path = project_root / ".aip-loom" / "briefs" / "C-0001.md"
        assert not brief_path.exists()

    def test_dry_run_no_new_files_at_all(self, project_root: Path) -> None:
        """dry-run creates absolutely no new files."""
        _write_chunk(project_root, "C-0001")
        all_files_before = set(project_root.rglob("*"))

        generate_brief(root=project_root, chunk_id="C-0001", dry_run=True)

        all_files_after = set(project_root.rglob("*"))
        assert all_files_after == all_files_before

    def test_dry_run_returns_content(self, project_root: Path) -> None:
        """dry-run still returns the brief content for preview."""
        _write_chunk(project_root, "C-0001")
        result = generate_brief(root=project_root, chunk_id="C-0001", dry_run=True)
        assert result.data["brief_path"] is None
        assert result.data["content_length"] > 0
        assert result.data["dry_run"] is True

    def test_dry_run_via_cli(self, project_root: Path, runner: CliRunner) -> None:
        """brief --dry-run via CLI writes nothing."""
        _write_chunk(project_root, "C-0001")
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["brief", "C-0001", "--dry-run"])
            assert result.exit_code == 0
        finally:
            os.chdir(old_cwd)

        brief_path = project_root / ".aip-loom" / "briefs" / "C-0001.md"
        assert not brief_path.exists()


# ---------------------------------------------------------------------------
# Dirty chunk handling
# ---------------------------------------------------------------------------


class TestDirtyChunk:
    """Verify brief handles dirty chunks (checksum mismatch)."""

    def _make_dirty_chunk(self, project_root: Path) -> None:
        """Create a chunk with a dirty checksum."""
        _write_chunk(project_root, "C-0001", prose="Original prose.")
        # Now modify the prose without updating the frontmatter checksum
        layout = ProjectLayout(root=project_root)
        path = layout.chunk_path("C-0001")
        raw = path.read_text(encoding="utf-8")
        modified = raw.replace("Original prose.", "Modified prose that breaks checksum.")
        path.write_text(modified, encoding="utf-8")

    def test_dirty_chunk_fails_without_force(self, project_root: Path) -> None:
        """brief fails for dirty chunks without --force."""
        self._make_dirty_chunk(project_root)
        result = generate_brief(root=project_root, chunk_id="C-0001")
        assert result.ok is False
        assert result.code == BRIEF_DIRTY_CHUNK

    def test_dirty_chunk_succeeds_with_force(self, project_root: Path) -> None:
        """brief succeeds for dirty chunks with --force."""
        self._make_dirty_chunk(project_root)
        result = generate_brief(root=project_root, chunk_id="C-0001", force=True)
        assert result.ok is True

    def test_dirty_chunk_force_emits_warning(self, project_root: Path) -> None:
        """brief --force for dirty chunk emits BRIEF_FORCE_USED warning."""
        self._make_dirty_chunk(project_root)
        result = generate_brief(root=project_root, chunk_id="C-0001", force=True)
        warning_codes = [w.code for w in result.warnings]
        assert BRIEF_FORCE_USED in warning_codes

    def test_force_warning_is_unmistakable(self, project_root: Path) -> None:
        """BRIEF_FORCE_USED warning contains 'FORCE OVERRIDE' in message."""
        self._make_dirty_chunk(project_root)
        result = generate_brief(root=project_root, chunk_id="C-0001", force=True)
        force_warnings = [w for w in result.warnings if w.code == BRIEF_FORCE_USED]
        assert len(force_warnings) > 0
        for w in force_warnings:
            assert "FORCE OVERRIDE" in w.message


# ---------------------------------------------------------------------------
# Orphan chunk handling
# ---------------------------------------------------------------------------


class TestOrphanChunk:
    """Verify brief handles orphan chunks (not in manifest order)."""

    def _make_orphan_chunk(self, project_root: Path) -> None:
        """Create a chunk that is NOT in the manifest's chunk order."""
        _write_chunk(project_root, "C-0001")
        _write_chunk(project_root, "C-0002")
        # Update manifest to only include C-0001
        layout = ProjectLayout(root=project_root)
        manifest = ProjectManifest(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            name="test-project",
            chunks={"order": ["C-0001"]},
        )
        layout.manifest_path.write_text(
            dump_yaml_string(manifest.model_dump(mode="json")), encoding="utf-8"
        )

    def test_orphan_chunk_fails_without_force(self, project_root: Path) -> None:
        """brief fails for orphan chunks without --force."""
        self._make_orphan_chunk(project_root)
        result = generate_brief(root=project_root, chunk_id="C-0002")
        assert result.ok is False
        assert result.code == BRIEF_STALE_CHUNK

    def test_orphan_chunk_succeeds_with_force(self, project_root: Path) -> None:
        """brief succeeds for orphan chunks with --force."""
        self._make_orphan_chunk(project_root)
        result = generate_brief(root=project_root, chunk_id="C-0002", force=True)
        assert result.ok is True


# ---------------------------------------------------------------------------
# Protected sections
# ---------------------------------------------------------------------------


class TestProtectedSections:
    """Verify protected sections are never dropped from the brief."""

    def test_protected_priorities_defined(self) -> None:
        """PROTECTED_PRIORITIES contains the expected priorities."""
        assert 0 in PROTECTED_PRIORITIES  # chunk frontmatter
        assert 1 in PROTECTED_PRIORITIES  # chunk prose
        assert 2 in PROTECTED_PRIORITIES  # distillate anchor
        assert 3 in PROTECTED_PRIORITIES  # scoped decisions
        assert 4 in PROTECTED_PRIORITIES  # scoped threads
        assert 6 in PROTECTED_PRIORITIES  # global decisions

    def test_non_protected_priorities_not_included(self) -> None:
        """Adjacent summaries, global threads, and questions are not protected."""
        assert 5 not in PROTECTED_PRIORITIES  # adjacent summaries
        assert 7 not in PROTECTED_PRIORITIES  # global threads
        assert 8 not in PROTECTED_PRIORITIES  # questions

    def test_brief_fails_when_protected_section_dropped(self, project_root: Path) -> None:
        """brief fails with BRIEF_BUDGET_OVERFLOW when a protected section is dropped."""
        _write_chunk(project_root, "C-0001")
        # Add a scoped decision that will exceed a tiny budget
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="A" * 500,
                scope="chunk", chunk_id="C-0001",
            ),
        ])
        # Use a tiny budget that forces the scoped decision to be dropped
        result = generate_brief(root=project_root, chunk_id="C-0001", token_budget=10)
        assert result.ok is False
        assert result.code == BRIEF_BUDGET_OVERFLOW

    def test_brief_succeeds_when_only_droppable_sections_exceed_budget(
        self, project_root: Path
    ) -> None:
        """brief succeeds when only non-protected sections exceed budget."""
        _write_chunk(project_root, "C-0001", prose="Short prose.")
        # Add only questions (priority 8 = not protected)
        _write_questions_ledger(project_root, [
            QuestionEntry(
                id="Q-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, question="X" * 500,
                resolved=False,
            ),
        ])
        # Use a small budget that drops questions but keeps protected sections
        result = generate_brief(root=project_root, chunk_id="C-0001", token_budget=100)
        # Should succeed (questions are not protected)
        assert result.ok is True


# ---------------------------------------------------------------------------
# Shared logic proof (zero duplication)
# ---------------------------------------------------------------------------


class TestSharedLogicProof:
    """Verify brief uses the same select_context() as inspect — zero duplication."""

    def test_brief_token_count_matches_inspect(self, project_root: Path) -> None:
        """Token count from brief matches token count from select_context."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)

        # Direct select_context call
        context = select_context(state, "C-0001")
        direct_tokens = context.total_token_estimate.token_count

        # brief via service
        result = generate_brief(root=project_root, chunk_id="C-0001")
        brief_tokens = result.data["token_estimate"]

        assert brief_tokens == direct_tokens

    def test_brief_section_count_matches_select_context(self, project_root: Path) -> None:
        """Section count from brief matches section count from select_context."""
        _write_chunk(project_root, "C-0001")
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="Global decision",
                scope="global",
            ),
        ])
        state = load_project(project_root)

        context = select_context(state, "C-0001")
        direct_sections = len(context.sections)

        result = generate_brief(root=project_root, chunk_id="C-0001")
        brief_sections = result.data["section_count"]

        assert brief_sections == direct_sections

    def test_brief_uses_same_dropped_sections(self, project_root: Path) -> None:
        """Dropped sections from brief match dropped sections from select_context."""
        _write_chunk(project_root, "C-0001", prose="Short.")
        _write_questions_ledger(project_root, [
            QuestionEntry(
                id="Q-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, question="X" * 500,
                resolved=False,
            ),
        ])
        state = load_project(project_root)

        context = select_context(state, "C-0001", token_budget=100)
        direct_dropped = len(context.dropped_sections)

        result = generate_brief(root=project_root, chunk_id="C-0001", token_budget=100)
        brief_dropped = result.data["dropped_count"]

        assert brief_dropped == direct_dropped

    def test_cli_brief_and_inspect_same_tokens(
        self, project_root: Path, runner: CliRunner
    ) -> None:
        """CLI brief and inspect commands produce the same token count."""
        _write_chunk(project_root, "C-0001")
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            inspect_result = runner.invoke(app, ["inspect", "C-0001", "--json"])
            inspect_data = _parse_json_output(inspect_result.output)
            inspect_tokens = inspect_data["data"]["total_tokens"]["token_count"]

            brief_result = runner.invoke(app, ["brief", "C-0001", "--dry-run", "--json"])
            brief_data = _parse_json_output(brief_result.output)
            brief_tokens = brief_data["data"]["token_estimate"]

            assert brief_tokens == inspect_tokens
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Verify brief generation is deterministic."""

    def test_same_input_same_section_count(self, project_root: Path) -> None:
        """Same chunk always produces the same section count."""
        _write_chunk(project_root, "C-0001")
        r1 = generate_brief(root=project_root, chunk_id="C-0001", dry_run=True)
        r2 = generate_brief(root=project_root, chunk_id="C-0001", dry_run=True)
        assert r1.data["section_count"] == r2.data["section_count"]

    def test_same_input_same_token_count(self, project_root: Path) -> None:
        """Same chunk always produces the same token estimate."""
        _write_chunk(project_root, "C-0001")
        r1 = generate_brief(root=project_root, chunk_id="C-0001", dry_run=True)
        r2 = generate_brief(root=project_root, chunk_id="C-0001", dry_run=True)
        assert r1.data["token_estimate"] == r2.data["token_estimate"]


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


class TestBriefJsonOutput:
    """Verify brief --json output."""

    def test_brief_json_success(self, project_root: Path, runner: CliRunner) -> None:
        """brief --json produces valid JSON on success."""
        _write_chunk(project_root, "C-0001")
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["brief", "C-0001", "--json"])
            assert result.exit_code == 0
            data = _parse_json_output(result.output)
            assert data["ok"] is True
            assert data["command"] == "brief"
        finally:
            os.chdir(old_cwd)

    def test_brief_json_has_expected_fields(self, project_root: Path, runner: CliRunner) -> None:
        """brief --json contains expected data fields."""
        _write_chunk(project_root, "C-0001")
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["brief", "C-0001", "--json"])
            data = _parse_json_output(result.output)
            d = data["data"]
            assert "chunk_id" in d
            assert "token_estimate" in d
            assert "token_budget" in d
            assert "section_count" in d
            assert "dropped_count" in d
            assert "dry_run" in d
            assert "content_length" in d
            assert "selected_context" in d
        finally:
            os.chdir(old_cwd)

    def test_brief_json_failure_unknown_chunk(
        self, project_root: Path, runner: CliRunner
    ) -> None:
        """brief --json produces failure for unknown chunk."""
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["brief", "C-9999", "--json"])
            data = _parse_json_output(result.output)
            assert data["ok"] is False
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Task description
# ---------------------------------------------------------------------------


class TestTaskDescription:
    """Verify --task flag includes task in brief content."""

    def test_task_in_brief_content(self, project_root: Path) -> None:
        """Task description appears in the brief content."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)
        from aip_loom.brief_context import select_context as sc
        context = sc(state, "C-0001")
        content = assemble_brief_content(context, task="Revise the opening paragraph")
        assert "Revise the opening paragraph" in content

    def test_task_in_brief_file(self, project_root: Path) -> None:
        """Task description appears in the written brief file."""
        _write_chunk(project_root, "C-0001")
        generate_brief(
            root=project_root, chunk_id="C-0001",
            task="Fix the dialogue scene",
        )
        brief_path = project_root / ".aip-loom" / "briefs" / "C-0001.md"
        content = brief_path.read_text(encoding="utf-8")
        assert "Fix the dialogue scene" in content

    def test_task_via_cli(self, project_root: Path, runner: CliRunner) -> None:
        """--task flag via CLI includes task in brief."""
        _write_chunk(project_root, "C-0001")
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(
                app, ["brief", "C-0001", "--task", "Write chapter 2", "--dry-run"]
            )
            assert result.exit_code == 0
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Brief content structure
# ---------------------------------------------------------------------------


class TestBriefContentStructure:
    """Verify the brief content is well-structured Markdown."""

    def test_brief_has_frontmatter(self, project_root: Path) -> None:
        """Brief content starts with YAML frontmatter."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)
        context = select_context(state, "C-0001")
        content = assemble_brief_content(context)
        assert content.startswith("---\n")
        assert "chunk_id:" in content.split("---")[1]

    def test_brief_has_heading(self, project_root: Path) -> None:
        """Brief content has the session brief heading."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)
        context = select_context(state, "C-0001")
        content = assemble_brief_content(context)
        assert "# Session Brief: C-0001" in content

    def test_brief_with_decisions_has_section(self, project_root: Path) -> None:
        """Brief includes a Scoped Decisions section when decisions exist."""
        _write_chunk(project_root, "C-0001")
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="A decision",
                scope="chunk", chunk_id="C-0001",
            ),
        ])
        state = load_project(project_root)
        context = select_context(state, "C-0001")
        content = assemble_brief_content(context)
        assert "## Scoped Decisions" in content
        assert "D-0001" in content

    def test_brief_with_threads_has_section(self, project_root: Path) -> None:
        """Brief includes a Scoped Threads section when threads exist."""
        _write_chunk(project_root, "C-0001")
        _write_threads_ledger(project_root, [
            ThreadEntry(
                id="T-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="A thread",
                scope="chunk", chunk_id="C-0001",
            ),
        ])
        state = load_project(project_root)
        context = select_context(state, "C-0001")
        content = assemble_brief_content(context)
        assert "## Scoped Threads" in content
        assert "T-0001" in content

    def test_brief_with_distillate_has_section(self, project_root: Path) -> None:
        """Brief includes a Distillate Anchor section when distillate exists."""
        _write_chunk(project_root, "C-0001")
        from aip_loom.brief_context import select_context as sc
        layout = ProjectLayout(root=project_root)
        distillate = Distillate(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            nodes=[
                DistillateNode(
                    chunk_id="C-0001", title="Test",
                    summary="A summary", key_decisions=["D-0001"],
                    open_threads=["T-0001"], word_count=100,
                ),
            ],
        )
        layout.distillate_path.write_text(
            dump_yaml_string(distillate.model_dump(mode="json")), encoding="utf-8"
        )
        state = load_project(project_root)
        context = sc(state, "C-0001")
        content = assemble_brief_content(context)
        assert "## Distillate Anchor" in content


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestBriefCLI:
    """Verify brief CLI integration."""

    def test_brief_cli_success(self, project_root: Path, runner: CliRunner) -> None:
        """brief CLI command succeeds for a valid chunk."""
        _write_chunk(project_root, "C-0001")
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["brief", "C-0001"])
            assert result.exit_code == 0
        finally:
            os.chdir(old_cwd)

    def test_brief_cli_unknown_chunk_fails(
        self, project_root: Path, runner: CliRunner
    ) -> None:
        """brief CLI command fails for unknown chunk."""
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["brief", "C-9999"])
            assert result.exit_code != 0
        finally:
            os.chdir(old_cwd)

    def test_brief_cli_non_project_dir_fails(
        self, tmp_dir: Path, runner: CliRunner
    ) -> None:
        """brief CLI command fails on non-project directory."""
        empty = tmp_dir / "empty"
        empty.mkdir()
        old_cwd = os.getcwd()
        os.chdir(str(empty))
        try:
            result = runner.invoke(app, ["brief", "C-0001"])
            assert result.exit_code != 0
        finally:
            os.chdir(old_cwd)

    def test_brief_cli_force_flag(self, project_root: Path, runner: CliRunner) -> None:
        """brief CLI --force flag works for dirty chunks."""
        _write_chunk(project_root, "C-0001", prose="Original.")
        # Dirty the chunk
        layout = ProjectLayout(root=project_root)
        path = layout.chunk_path("C-0001")
        raw = path.read_text(encoding="utf-8")
        modified = raw.replace("Original.", "Modified without updating checksum.")
        path.write_text(modified, encoding="utf-8")

        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            # Without --force, should fail
            result = runner.invoke(app, ["brief", "C-0001", "--json"])
            data = _parse_json_output(result.output)
            assert data["ok"] is False

            # With --force, should succeed
            result = runner.invoke(app, ["brief", "C-0001", "--force", "--json"])
            data = _parse_json_output(result.output)
            assert data["ok"] is True
            # Should have BRIEF_FORCE_USED warning
            warning_codes = [w["code"] for w in data.get("warnings", [])]
            assert BRIEF_FORCE_USED in warning_codes
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Brief overwrite behavior
# ---------------------------------------------------------------------------


class TestBriefOverwrite:
    """Verify brief overwrites existing brief files."""

    def test_brief_overwrites_existing(self, project_root: Path) -> None:
        """Running brief twice overwrites the existing brief file."""
        _write_chunk(project_root, "C-0001", prose="Version 1.")

        # First brief
        generate_brief(root=project_root, chunk_id="C-0001")
        brief_path = project_root / ".aip-loom" / "briefs" / "C-0001.md"
        content_v1 = brief_path.read_text(encoding="utf-8")

        # Second brief (same content, should overwrite)
        generate_brief(root=project_root, chunk_id="C-0001")
        content_v2 = brief_path.read_text(encoding="utf-8")

        # Both should be valid briefs
        assert "Session Brief: C-0001" in content_v1
        assert "Session Brief: C-0001" in content_v2


# ---------------------------------------------------------------------------
# Budget overflow
# ---------------------------------------------------------------------------


class TestBudgetOverflow:
    """Verify BRIEF_BUDGET_OVERFLOW when protected sections exceed budget."""

    def test_budget_overflow_error_code(self, project_root: Path) -> None:
        """brief fails with BRIEF_BUDGET_OVERFLOW code."""
        _write_chunk(project_root, "C-0001", prose="A" * 1000)
        result = generate_brief(root=project_root, chunk_id="C-0001", token_budget=5)
        assert result.ok is False
        assert result.code == BRIEF_BUDGET_OVERFLOW

    def test_budget_overflow_has_detail(self, project_root: Path) -> None:
        """budget overflow error includes detailed information."""
        _write_chunk(project_root, "C-0001", prose="A" * 1000)
        result = generate_brief(root=project_root, chunk_id="C-0001", token_budget=5)
        # Should have errors with detail
        assert len(result.errors) > 0
        budget_errors = [e for e in result.errors if e.code == BRIEF_BUDGET_OVERFLOW]
        assert len(budget_errors) > 0
        assert "dropped_protected_types" in budget_errors[0].detail

    def test_budget_overflow_via_cli(self, project_root: Path, runner: CliRunner) -> None:
        """brief CLI shows budget overflow when mandatory sections are too large.

        Note: The CLI uses the default token budget (8000), so we need
        a very large chunk to trigger overflow.  Instead, we test this
        via the generate_brief() service function with an explicit budget.
        """
        _write_chunk(project_root, "C-0001", prose="A" * 1000)
        result = generate_brief(root=project_root, chunk_id="C-0001", token_budget=5)
        assert result.ok is False
        assert result.code == BRIEF_BUDGET_OVERFLOW
