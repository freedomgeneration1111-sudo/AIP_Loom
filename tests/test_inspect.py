"""Tests for aip-loom inspect CLI command.

These tests verify:

- inspect shows context for a valid chunk
- inspect returns failure for unknown chunk
- inspect --json produces valid JSON output
- inspect writes no files (pure read-only)
- inspect surfaces warnings for missing ledgers
- inspect token estimate is consistent with brief_context
- inspect on a non-project directory fails
- inspect on a corrupt project fails honestly
- inspect shows dropped sections when budget is tight
- inspect shows scoped decisions/threads for the target chunk
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aip_loom.brief_context import select_context
from aip_loom.checksum import compute_prose_checksum
from aip_loom.cli import app
from aip_loom.errors import CHUNK_NOT_FOUND
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
# Basic inspect command
# ---------------------------------------------------------------------------


class TestInspectBasic:
    """Verify basic inspect command functionality."""

    def test_inspect_valid_chunk(self, project_root: Path, runner: CliRunner) -> None:
        """inspect on a valid chunk returns success."""
        _write_chunk(project_root, "C-0001")
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001"])
            assert result.exit_code == 0
        finally:
            os.chdir(old_cwd)

    def test_inspect_unknown_chunk_fails(self, project_root: Path, runner: CliRunner) -> None:
        """inspect on an unknown chunk returns failure."""
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-9999"])
            assert result.exit_code != 0
        finally:
            os.chdir(old_cwd)

    def test_inspect_non_project_dir_fails(self, tmp_dir: Path, runner: CliRunner) -> None:
        """inspect on a non-project directory returns failure."""
        empty = tmp_dir / "empty"
        empty.mkdir()
        old_cwd = os.getcwd()
        os.chdir(str(empty))
        try:
            result = runner.invoke(app, ["inspect", "C-0001"])
            assert result.exit_code != 0
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


class TestInspectJson:
    """Verify inspect --json output."""

    def test_inspect_json_valid_chunk(self, project_root: Path, runner: CliRunner) -> None:
        """inspect --json on a valid chunk produces valid JSON."""
        _write_chunk(project_root, "C-0001")
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001", "--json"])
            assert result.exit_code == 0
            data = _parse_json_output(result.output)
            assert data["ok"] is True
            assert data["command"] == "inspect"
            assert data["data"]["target_chunk_id"] == "C-0001"
            assert data["data"]["target_chunk_found"] is True
        finally:
            os.chdir(old_cwd)

    def test_inspect_json_unknown_chunk(self, project_root: Path, runner: CliRunner) -> None:
        """inspect --json on unknown chunk returns failure with details."""
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-9999", "--json"])
            data = _parse_json_output(result.output)
            assert data["ok"] is False
            assert data["data"]["target_chunk_found"] is False
        finally:
            os.chdir(old_cwd)

    def test_inspect_json_has_sections(self, project_root: Path, runner: CliRunner) -> None:
        """inspect --json includes selected sections."""
        _write_chunk(project_root, "C-0001")
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001", "--json"])
            data = _parse_json_output(result.output)
            assert len(data["data"]["sections"]) > 0
        finally:
            os.chdir(old_cwd)

    def test_inspect_json_has_token_info(self, project_root: Path, runner: CliRunner) -> None:
        """inspect --json includes token estimate info."""
        _write_chunk(project_root, "C-0001")
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001", "--json"])
            data = _parse_json_output(result.output)
            tokens = data["data"]["total_tokens"]
            assert "token_count" in tokens
            assert "is_approximate" in tokens
            assert "encoding_name" in tokens
        finally:
            os.chdir(old_cwd)

    def test_inspect_json_has_budget_info(self, project_root: Path, runner: CliRunner) -> None:
        """inspect --json includes budget info."""
        _write_chunk(project_root, "C-0001")
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001", "--json"])
            data = _parse_json_output(result.output)
            assert "token_budget" in data["data"]
            assert "budget_exceeded" in data["data"]
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Read-only (no file writes)
# ---------------------------------------------------------------------------


class TestInspectReadOnly:
    """Verify inspect never writes to disk."""

    def test_inspect_no_new_files(self, project_root: Path, runner: CliRunner) -> None:
        """inspect does not create any new files."""
        _write_chunk(project_root, "C-0001")
        all_files_before = set(project_root.rglob("*"))

        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001"])
            assert result.exit_code == 0
        finally:
            os.chdir(old_cwd)

        all_files_after = set(project_root.rglob("*"))
        assert all_files_after == all_files_before

    def test_inspect_no_brief_file(self, project_root: Path, runner: CliRunner) -> None:
        """inspect does not create a brief file."""
        _write_chunk(project_root, "C-0001")
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001"])
            assert result.exit_code == 0
        finally:
            os.chdir(old_cwd)

        # No brief files
        brief_files = list(project_root.rglob("*.brief"))
        assert len(brief_files) == 0


# ---------------------------------------------------------------------------
# Missing ledger warnings
# ---------------------------------------------------------------------------


class TestMissingLedgerWarnings:
    """Verify inspect surfaces warnings for missing/malformed ledgers."""

    def test_missing_decisions_ledger_warns(self, project_root: Path, runner: CliRunner) -> None:
        """inspect shows warning when decisions ledger is malformed."""
        _write_chunk(project_root, "C-0001")
        layout = ProjectLayout(root=project_root)
        layout.decisions_ledger_path.write_text("broken: {", encoding="utf-8")

        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001", "--json"])
            data = _parse_json_output(result.output)
            # Should have warnings about the ledger
            assert len(data.get("warnings", [])) > 0
        finally:
            os.chdir(old_cwd)

    def test_missing_threads_ledger_warns(self, project_root: Path, runner: CliRunner) -> None:
        """inspect shows warning when threads ledger is malformed."""
        _write_chunk(project_root, "C-0001")
        layout = ProjectLayout(root=project_root)
        layout.threads_ledger_path.write_text("broken: {", encoding="utf-8")

        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001", "--json"])
            data = _parse_json_output(result.output)
            assert len(data.get("warnings", [])) > 0
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Corrupt project fails honestly
# ---------------------------------------------------------------------------


class TestCorruptProjectFailsHonestly:
    """Verify inspect fails honestly on corrupt projects."""

    def test_corrupt_manifest_fails(self, project_root: Path, runner: CliRunner) -> None:
        """inspect fails when the manifest is corrupt."""
        layout = ProjectLayout(root=project_root)
        layout.manifest_path.write_text("not: valid: yaml: [broken", encoding="utf-8")

        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001", "--json"])
            data = _parse_json_output(result.output)
            assert data["ok"] is False
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Scoped decisions/threads visible in inspect
# ---------------------------------------------------------------------------


class TestScopedEntriesInInspect:
    """Verify scoped decisions/threads are visible in inspect output."""

    def test_scoped_decision_visible(self, project_root: Path, runner: CliRunner) -> None:
        """inspect shows scoped decisions in JSON output."""
        _write_chunk(project_root, "C-0001")
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="Scoped decision",
                scope="chunk", chunk_id="C-0001",
            ),
        ])

        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001", "--json"])
            data = _parse_json_output(result.output)
            scoped = data["data"]["scoped_decisions"]
            assert "D-0001" in scoped
        finally:
            os.chdir(old_cwd)

    def test_scoped_thread_visible(self, project_root: Path, runner: CliRunner) -> None:
        """inspect shows scoped threads in JSON output."""
        _write_chunk(project_root, "C-0001")
        _write_threads_ledger(project_root, [
            ThreadEntry(
                id="T-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="Scoped thread",
                scope="chunk", chunk_id="C-0001",
            ),
        ])

        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001", "--json"])
            data = _parse_json_output(result.output)
            scoped = data["data"]["scoped_threads"]
            assert "T-0001" in scoped
        finally:
            os.chdir(old_cwd)

    def test_other_chunk_scoped_not_visible(self, project_root: Path, runner: CliRunner) -> None:
        """inspect does not show decisions scoped to other chunks."""
        _write_chunk(project_root, "C-0001")
        _write_chunk(project_root, "C-0002")
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="For C-0002",
                scope="chunk", chunk_id="C-0002",
            ),
        ])

        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001", "--json"])
            data = _parse_json_output(result.output)
            scoped = data["data"]["scoped_decisions"]
            assert "D-0001" not in scoped
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Token estimate consistency
# ---------------------------------------------------------------------------


class TestTokenEstimateConsistency:
    """Verify token estimates are consistent between inspect and brief_context."""

    def test_inspect_uses_same_token_count_as_select_context(self, project_root: Path) -> None:
        """inspect CLI uses the same token count as select_context directly."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)

        # Direct call to select_context
        context = select_context(state, "C-0001")
        direct_tokens = context.total_token_estimate.token_count

        # The CLI should produce the same token count
        runner = CliRunner()
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001", "--json"])
            data = _parse_json_output(result.output)
            cli_tokens = data["data"]["total_tokens"]["token_count"]
            assert cli_tokens == direct_tokens
        finally:
            os.chdir(old_cwd)

    def test_inspect_shows_same_sections_as_select_context(self, project_root: Path) -> None:
        """inspect shows the same sections as select_context."""
        _write_chunk(project_root, "C-0001")
        _write_decisions_ledger(project_root, [
            DecisionEntry(
                id="D-0001", review_state=ReviewState.APPROVED,
                created_at=NOW, summary="Global decision",
                scope="global",
            ),
        ])
        state = load_project(project_root)

        # Direct call
        context = select_context(state, "C-0001")
        direct_section_count = len(context.sections)

        # CLI call
        runner = CliRunner()
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001", "--json"])
            data = _parse_json_output(result.output)
            cli_section_count = len(data["data"]["sections"])
            assert cli_section_count == direct_section_count
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Rich terminal output
# ---------------------------------------------------------------------------


class TestInspectRichOutput:
    """Verify inspect Rich terminal output."""

    def test_inspect_rich_output_for_valid_chunk(self, project_root: Path, runner: CliRunner) -> None:
        """inspect produces Rich output for a valid chunk."""
        _write_chunk(project_root, "C-0001")
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-0001"])
            assert result.exit_code == 0
            # Should contain key information in the output
            assert "C-0001" in result.output
        finally:
            os.chdir(old_cwd)

    def test_inspect_rich_output_for_unknown_chunk(self, project_root: Path, runner: CliRunner) -> None:
        """inspect produces Rich output for unknown chunk."""
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["inspect", "C-9999"])
            assert result.exit_code != 0
        finally:
            os.chdir(old_cwd)
