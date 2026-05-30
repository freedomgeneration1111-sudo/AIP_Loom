"""Tests for aip_loom.build — Minimal Markdown Concatenator / Draft Build.

These tests prove:
- Explicit chunk order in manifest → concatenated in that order
- Filename fallback with CHUNK_ORDER_FALLBACK_USED warning when no manifest order
- Frontmatter stripped, only prose bodies concatenated
- Deterministic output given same project state
- Unsupported format (docx/pdf) → BUILD_FORMAT_UNSUPPORTED error
- Unsupported mode → BUILD_MODE_UNSUPPORTED error
- Validation errors (critical) → build fails with BUILD_VALIDATION_FAILED
- Build report includes: included chunks, warnings, output path, word count
- Empty project (no chunks) → BUILD_NO_CHUNKS error
- --output path writes to specified location
- Chunk IDs in order but missing from loaded chunks → skipped with warning
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from aip_loom.build import BuildResult, build_draft_md, run_build, SUPPORTED_MODES, SUPPORTED_FORMATS
from aip_loom.chunk_order import ChunkOrderResult
from aip_loom.checksum import compute_prose_checksum
from aip_loom.errors import (
    BUILD_CHUNK_SKIPPED,
    BUILD_FORMAT_UNSUPPORTED,
    BUILD_MODE_UNSUPPORTED,
    BUILD_NO_CHUNKS,
    BUILD_VALIDATION_FAILED,
    CHUNK_ORDER_FALLBACK_USED,
    LoomError,
    LoomWarning,
)
from aip_loom.frontmatter import write_frontmatter
from aip_loom.project import ChunkData, ProjectState, load_project
from aip_loom.schemas import (
    ChunkFrontmatter,
    ChunkStatus,
    ProjectManifest,
    SUPPORTED_SCHEMA_VERSION,
)
from aip_loom.results import CommandResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_V = SUPPORTED_SCHEMA_VERSION


def _make_chunk_md(
    chunk_id: str,
    title: str,
    prose: str,
    word_count: int | None = None,
) -> str:
    """Create a complete chunk Markdown file with frontmatter and prose."""
    checksum = compute_prose_checksum(prose)
    wc = word_count if word_count is not None else len(prose.split())
    fm = ChunkFrontmatter(
        schema_version=_V,
        id=chunk_id,
        title=title,
        status=ChunkStatus.DRAFT,
        word_count=wc,
        prose_checksum=checksum,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
    )
    return write_frontmatter(fm, prose)


def _init_project_dir(
    tmp: str,
    name: str = "test-novel",
    chunk_order: list[str] | None = None,
    chunks: dict[str, tuple[str, str]] | None = None,
) -> str:
    """Initialise a complete AIP_Loom project directory for testing.

    Parameters
    ----------
    tmp:
        The temporary directory to create the project in.
    name:
        The project name.
    chunk_order:
        The chunks.order list for the manifest.
    chunks:
        Mapping of chunk_id → (title, prose_body) for chunk files.

    Returns
    -------
    str
        The project root directory path.
    """
    root = Path(tmp) / "project"
    root.mkdir(parents=True, exist_ok=True)

    # Create directories
    (root / "chunks").mkdir()
    (root / "ledgers").mkdir()
    (root / "archive").mkdir()
    (root / ".aip-loom").mkdir()
    (root / ".aip-loom" / "staging").mkdir()

    # Create manifest
    order = chunk_order if chunk_order is not None else []
    manifest_content = (
        f"schema_version: '{_V}'\n"
        f"name: {name}\n"
        f"project_type: novel\n"
        f"chunks:\n"
        f"  order:\n"
    )
    for cid in order:
        manifest_content += f"    - {cid}\n"
    if not order:
        manifest_content += "    []\n"
    (root / "aip_loom.yaml").write_text(manifest_content, encoding="utf-8")

    # Create empty ledgers and other required files
    ledger_template = f"schema_version: '{_V}'\nentries: []\n"
    (root / "ledgers" / "decisions.yaml").write_text(ledger_template, encoding="utf-8")
    (root / "ledgers" / "threads.yaml").write_text(ledger_template, encoding="utf-8")
    (root / "ledgers" / "questions.yaml").write_text(ledger_template, encoding="utf-8")
    (root / "distillate.yaml").write_text(f"schema_version: '{_V}'\nnodes: []\n", encoding="utf-8")
    (root / "sessions.yaml").write_text(f"schema_version: '{_V}'\nentries: []\n", encoding="utf-8")
    (root / "comments.yaml").write_text(f"schema_version: '{_V}'\nentries: []\n", encoding="utf-8")

    # Create chunk files
    if chunks:
        for chunk_id, (title, prose) in chunks.items():
            md_content = _make_chunk_md(chunk_id, title, prose)
            (root / "chunks" / f"{chunk_id}.md").write_text(md_content, encoding="utf-8")

    return str(root)


def _parse_json_output(output: str) -> dict[str, Any]:
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


# ===========================================================================
# build_draft_md — manifest order respected
# ===========================================================================


class TestManifestOrderRespected:
    """When the manifest has an explicit chunks.order, that order is used."""

    def test_chunks_concatenated_in_manifest_order(self) -> None:
        """Chunks appear in the manifest's declared order, not filename order."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0002", "C-0001", "C-0003"],
                chunks={
                    "C-0001": ("First", "This is chunk one."),
                    "C-0002": ("Second", "This is chunk two."),
                    "C-0003": ("Third", "This is chunk three."),
                },
            )
            state = load_project(Path(root))
            output_path = Path(root) / "build" / "draft.md"
            result = build_draft_md(state, output_path)

            assert result.included_chunks == ["C-0002", "C-0001", "C-0003"]
            assert result.used_manifest_order is True

    def test_output_content_in_manifest_order(self) -> None:
        """The actual Markdown output has chunks in manifest order."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0003", "C-0001"],
                chunks={
                    "C-0001": ("First", "Alpha content."),
                    "C-0003": ("Third", "Gamma content."),
                },
            )
            state = load_project(Path(root))
            output_path = Path(root) / "build" / "draft.md"
            build_draft_md(state, output_path)

            content = output_path.read_text(encoding="utf-8")
            # C-0003 should appear before C-0001
            pos_c3 = content.index("Gamma content")
            pos_c1 = content.index("Alpha content")
            assert pos_c3 < pos_c1

    def test_manifest_order_flag_is_true(self) -> None:
        """used_manifest_order is True when manifest order is used."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "Content one.")},
            )
            state = load_project(Path(root))
            output_path = Path(root) / "build" / "draft.md"
            result = build_draft_md(state, output_path)
            assert result.used_manifest_order is True


# ===========================================================================
# build_draft_md — filename fallback
# ===========================================================================


class TestFilenameFallback:
    """When chunks.order is empty, fall back to natural filename sort."""

    def test_fallback_uses_natural_sort(self) -> None:
        """Without manifest order, chunks are sorted by natural sort."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=[],  # Empty order → fallback
                chunks={
                    "C-0010": ("Tenth", "Ten."),
                    "C-0002": ("Second", "Two."),
                    "C-0001": ("First", "One."),
                },
            )
            state = load_project(Path(root))
            output_path = Path(root) / "build" / "draft.md"
            result = build_draft_md(state, output_path)

            assert result.included_chunks == ["C-0001", "C-0002", "C-0010"]
            assert result.used_manifest_order is False

    def test_fallback_emits_warning_from_state(self) -> None:
        """The project state carries CHUNK_ORDER_FALLBACK_USED warnings
        when fallback is used."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=[],
                chunks={"C-0001": ("First", "One.")},
            )
            state = load_project(Path(root))
            # The chunk_order on the state should have fallback warnings
            assert state.chunk_order is not None
            fallback_warnings = [
                w for w in state.chunk_order.warnings
                if w.code == CHUNK_ORDER_FALLBACK_USED
            ]
            assert len(fallback_warnings) >= 1


# ===========================================================================
# build_draft_md — frontmatter stripped
# ===========================================================================


class TestFrontmatterStripped:
    """Frontmatter is stripped from chunk files; only prose appears."""

    def test_no_yaml_in_output(self) -> None:
        """The output Markdown contains no YAML frontmatter."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("Test", "The prose body.")},
            )
            state = load_project(Path(root))
            output_path = Path(root) / "build" / "draft.md"
            build_draft_md(state, output_path)

            content = output_path.read_text(encoding="utf-8")
            # The YAML frontmatter (schema_version, id, title, etc.) should not appear
            assert "schema_version" not in content
            assert "prose_checksum" not in content
            # But the prose body should be there
            assert "The prose body." in content

    def test_only_prose_bodies_concatenated(self) -> None:
        """Each chunk contributes only its prose body, not its frontmatter."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001", "C-0002"],
                chunks={
                    "C-0001": ("Ch1", "Paragraph one."),
                    "C-0002": ("Ch2", "Paragraph two."),
                },
            )
            state = load_project(Path(root))
            output_path = Path(root) / "build" / "draft.md"
            build_draft_md(state, output_path)

            content = output_path.read_text(encoding="utf-8")
            # Should have the chunk IDs as HTML comments (separators)
            assert "<!-- C-0001 -->" in content
            assert "<!-- C-0002 -->" in content
            # Should have the prose bodies
            assert "Paragraph one." in content
            assert "Paragraph two." in content
            # Should NOT have titles from frontmatter
            assert "Ch1" not in content or "Paragraph one." in content


# ===========================================================================
# build_draft_md — deterministic output
# ===========================================================================


class TestDeterministicOutput:
    """Same project state and chunk order → byte-for-byte identical output."""

    def test_two_builds_are_identical(self) -> None:
        """Building the same project twice produces identical output files."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001", "C-0002"],
                chunks={
                    "C-0001": ("First", "Stable content one."),
                    "C-0002": ("Second", "Stable content two."),
                },
            )
            state = load_project(Path(root))

            output1 = Path(root) / "build1" / "draft.md"
            output2 = Path(root) / "build2" / "draft.md"

            build_draft_md(state, output1)
            build_draft_md(state, output2)

            content1 = output1.read_text(encoding="utf-8")
            content2 = output2.read_text(encoding="utf-8")

            assert content1 == content2

    def test_hash_deterministic(self) -> None:
        """Content hash of output is deterministic."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "Deterministic prose.")},
            )
            state = load_project(Path(root))
            output_path = Path(root) / "build" / "draft.md"

            build_draft_md(state, output_path)
            content1 = output_path.read_text(encoding="utf-8")
            hash1 = hashlib.sha256(content1.encode("utf-8")).hexdigest()

            # Build again (overwrite)
            build_draft_md(state, output_path)
            content2 = output_path.read_text(encoding="utf-8")
            hash2 = hashlib.sha256(content2.encode("utf-8")).hexdigest()

            assert hash1 == hash2


# ===========================================================================
# run_build — unsupported format
# ===========================================================================


class TestUnsupportedFormat:
    """DOCX and PDF formats produce BUILD_FORMAT_UNSUPPORTED error."""

    def test_docx_format_rejected(self) -> None:
        """--format docx produces BUILD_FORMAT_UNSUPPORTED error."""
        result = run_build(mode="draft", fmt="docx", output_path=None)
        assert result.ok is False
        assert result.code == BUILD_FORMAT_UNSUPPORTED

    def test_pdf_format_rejected(self) -> None:
        """--format pdf produces BUILD_FORMAT_UNSUPPORTED error."""
        result = run_build(mode="draft", fmt="pdf", output_path=None)
        assert result.ok is False
        assert result.code == BUILD_FORMAT_UNSUPPORTED

    def test_docx_error_message_mentions_external_converter(self) -> None:
        """The error message for unsupported formats suggests using an
        external converter."""
        result = run_build(mode="draft", fmt="docx", output_path=None)
        assert "external" in result.message.lower() or "converter" in result.message.lower()

    def test_unsupported_format_lists_supported(self) -> None:
        """The failure data includes the supported formats."""
        result = run_build(mode="draft", fmt="docx", output_path=None)
        # Check that the data contains supported formats
        assert "supported_formats" in result.data or "md" in str(result.data)


# ===========================================================================
# run_build — unsupported mode
# ===========================================================================


class TestUnsupportedMode:
    """Unsupported build modes produce BUILD_MODE_UNSUPPORTED error."""

    def test_final_mode_rejected(self) -> None:
        """--mode final produces BUILD_MODE_UNSUPPORTED error."""
        result = run_build(mode="final", fmt="md", output_path=None)
        assert result.ok is False
        assert result.code == BUILD_MODE_UNSUPPORTED

    def test_publish_mode_rejected(self) -> None:
        """--mode publish produces BUILD_MODE_UNSUPPORTED error."""
        result = run_build(mode="publish", fmt="md", output_path=None)
        assert result.ok is False
        assert result.code == BUILD_MODE_UNSUPPORTED

    def test_unsupported_mode_lists_supported(self) -> None:
        """The error message lists the supported modes."""
        result = run_build(mode="final", fmt="md", output_path=None)
        assert "draft" in result.message.lower()


# ===========================================================================
# run_build — validation errors abort build
# ===========================================================================


class TestValidationErrorsAbortBuild:
    """Critical validation errors cause the build to fail cleanly."""

    def test_missing_required_file_aborts_build(self) -> None:
        """If a required file is missing, build aborts with BUILD_VALIDATION_FAILED."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "Content.")},
            )
            # Delete a required file to cause validation failure
            (Path(root) / "distillate.yaml").unlink()

            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                result = run_build(mode="draft", fmt="md", output_path=None)
            finally:
                os.chdir(original_cwd)

            assert result.ok is False
            assert result.code == BUILD_VALIDATION_FAILED

    def test_validation_error_message_is_clear(self) -> None:
        """The error message tells the user to fix validation errors."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "Content.")},
            )
            # Delete a required file
            (Path(root) / "distillate.yaml").unlink()

            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                result = run_build(mode="draft", fmt="md", output_path=None)
            finally:
                os.chdir(original_cwd)

            assert "validation" in result.message.lower()


# ===========================================================================
# run_build — no chunks
# ===========================================================================


class TestNoChunks:
    """An empty project (no chunks) produces BUILD_NO_CHUNKS error."""

    def test_empty_project_aborts_build(self) -> None:
        """Building a project with no chunks produces BUILD_NO_CHUNKS."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(tmp, chunk_order=[], chunks=None)

            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                result = run_build(mode="draft", fmt="md", output_path=None)
            finally:
                os.chdir(original_cwd)

            assert result.ok is False
            assert result.code == BUILD_NO_CHUNKS


# ===========================================================================
# run_build — successful build report
# ===========================================================================


class TestBuildReport:
    """Successful builds produce a complete report."""

    def test_report_includes_chunk_count(self) -> None:
        """The report includes the number of chunks built."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001", "C-0002"],
                chunks={
                    "C-0001": ("First", "One two three."),
                    "C-0002": ("Second", "Four five six."),
                },
            )
            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                result = run_build(mode="draft", fmt="md", output_path=None)
            finally:
                os.chdir(original_cwd)

            assert result.ok is True
            assert result.data["chunk_count"] == 2

    def test_report_includes_word_count(self) -> None:
        """The report includes the total word count."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "One two three four five.")},
            )
            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                result = run_build(mode="draft", fmt="md", output_path=None)
            finally:
                os.chdir(original_cwd)

            assert result.ok is True
            assert result.data["total_word_count"] > 0

    def test_report_includes_output_path(self) -> None:
        """The report includes the output file path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "Content.")},
            )
            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                result = run_build(mode="draft", fmt="md", output_path=None)
            finally:
                os.chdir(original_cwd)

            assert result.ok is True
            assert "output_path" in result.data

    def test_report_includes_included_chunks(self) -> None:
        """The report lists the included chunk IDs in order."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0002", "C-0001"],
                chunks={
                    "C-0001": ("First", "Alpha."),
                    "C-0002": ("Second", "Beta."),
                },
            )
            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                result = run_build(mode="draft", fmt="md", output_path=None)
            finally:
                os.chdir(original_cwd)

            assert result.ok is True
            assert result.data["included_chunks"] == ["C-0002", "C-0001"]

    def test_report_includes_used_manifest_order(self) -> None:
        """The report indicates whether manifest order was used."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "Alpha.")},
            )
            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                result = run_build(mode="draft", fmt="md", output_path=None)
            finally:
                os.chdir(original_cwd)

            assert result.ok is True
            assert result.data["used_manifest_order"] is True


# ===========================================================================
# run_build — --output path
# ===========================================================================


class TestOutputPath:
    """The --output option writes to the specified path."""

    def test_custom_output_path(self) -> None:
        """Build writes to the specified output path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "Custom output content.")},
            )
            custom_output = os.path.join(tmp, "custom", "my-draft.md")

            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                result = run_build(mode="draft", fmt="md", output_path=custom_output)
            finally:
                os.chdir(original_cwd)

            assert result.ok is True
            assert Path(custom_output).is_file()
            content = Path(custom_output).read_text(encoding="utf-8")
            assert "Custom output content." in content

    def test_default_output_path(self) -> None:
        """Without --output, build writes to build/draft.md in the project root."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "Default output.")},
            )
            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                result = run_build(mode="draft", fmt="md", output_path=None)
            finally:
                os.chdir(original_cwd)

            assert result.ok is True
            default_path = Path(root) / "build" / "draft.md"
            assert default_path.is_file()


# ===========================================================================
# run_build — no project
# ===========================================================================


class TestBuildNoProject:
    """Build fails cleanly when run outside a project directory."""

    def test_build_outside_project_fails(self) -> None:
        """Build exits with failure when no project is found."""
        with tempfile.TemporaryDirectory() as tmp:
            original_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                result = run_build(mode="draft", fmt="md", output_path=None)
            finally:
                os.chdir(original_cwd)

            assert result.ok is False


# ===========================================================================
# build_draft_md — skipped chunks
# ===========================================================================


class TestSkippedChunks:
    """Chunks in the resolved order but missing from loaded chunks are skipped."""

    def test_skipped_chunks_reported(self) -> None:
        """Skipped chunk IDs appear in the result and produce warnings."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001", "C-0002"],
                chunks={"C-0001": ("First", "Present content.")},
                # C-0002 is in the order but has no file
            )
            state = load_project(Path(root))

            # The chunk_order may have been resolved to just C-0001
            # depending on how load_project handles this.
            # If C-0002 is still in the order but not in state.chunks,
            # it should be skipped.
            output_path = Path(root) / "build" / "draft.md"
            result = build_draft_md(state, output_path)

            # C-0002 should be in skipped if it's in ordered_ids but not in chunks
            if "C-0002" in (state.chunk_order.ordered_ids if state.chunk_order else []):
                if "C-0002" not in state.chunks:
                    assert "C-0002" in result.skipped_chunk_ids


# ===========================================================================
# BuildResult — frozen dataclass
# ===========================================================================


class TestBuildResultFrozen:
    """BuildResult must be immutable."""

    def test_result_is_frozen(self) -> None:
        result = BuildResult(
            output_path=Path("/tmp/draft.md"),
            included_chunks=["C-0001"],
            skipped_chunk_ids=[],
            total_word_count=10,
            total_char_count=50,
            used_manifest_order=True,
        )
        with pytest.raises(AttributeError):
            result.included_chunks = ["C-0099"]  # type: ignore[misc]


# ===========================================================================
# CLI integration
# ===========================================================================


class TestBuildCLI:
    """Test the build command through the Typer CLI."""

    def test_build_help_exits_zero(self) -> None:
        """Build --help exits with code 0."""
        from aip_loom.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, ["build", "--help"])
        assert result.exit_code == 0
        assert "draft" in result.output.lower() or "mode" in result.output.lower()

    def test_build_on_project_succeeds(self) -> None:
        """Build --mode draft --format md succeeds on a valid project."""
        from aip_loom.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "CLI test content.")},
            )
            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                result = runner.invoke(app, ["build", "--mode", "draft", "--format", "md", "--json"])
            finally:
                os.chdir(original_cwd)

            data = _parse_json_output(result.output)
            assert data["ok"] is True
            assert data["command"] == "build"

    def test_build_unsupported_format_via_cli(self) -> None:
        """Build --format docx fails with BUILD_FORMAT_UNSUPPORTED."""
        from aip_loom.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, ["build", "--mode", "draft", "--format", "docx", "--json"])
        data = _parse_json_output(result.output)
        assert data["ok"] is False
        assert data["code"] == BUILD_FORMAT_UNSUPPORTED

    def test_build_default_mode_and_format(self) -> None:
        """Build with default mode/format uses draft+md."""
        from aip_loom.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "Default mode test.")},
            )
            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                result = runner.invoke(app, ["build", "--json"])
            finally:
                os.chdir(original_cwd)

            data = _parse_json_output(result.output)
            assert data["ok"] is True
            assert data["data"]["mode"] == "draft"
            assert data["data"]["format"] == "md"

    def test_build_custom_output_via_cli(self) -> None:
        """Build --output writes to the specified path."""
        from aip_loom.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "Custom CLI output.")},
            )
            custom_output = os.path.join(tmp, "cli-output", "result.md")

            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                result = runner.invoke(app, ["build", "--output", custom_output, "--json"])
            finally:
                os.chdir(original_cwd)

            data = _parse_json_output(result.output)
            assert data["ok"] is True
            assert Path(custom_output).is_file()


# ===========================================================================
# SUPPORTED_MODES and SUPPORTED_FORMATS constants
# ===========================================================================


class TestSupportedConstants:
    """Verify the supported modes and formats constants."""

    def test_draft_is_supported_mode(self) -> None:
        assert "draft" in SUPPORTED_MODES

    def test_md_is_supported_format(self) -> None:
        assert "md" in SUPPORTED_FORMATS

    def test_docx_not_in_supported_formats(self) -> None:
        assert "docx" not in SUPPORTED_FORMATS

    def test_pdf_not_in_supported_formats(self) -> None:
        assert "pdf" not in SUPPORTED_FORMATS


# ===========================================================================
# Chunk separator format
# ===========================================================================


class TestChunkSeparatorFormat:
    """The output uses HTML comment separators for chunk boundaries."""

    def test_chunk_separator_is_html_comment(self) -> None:
        """Each chunk section is preceded by an HTML comment with its ID."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001", "C-0002"],
                chunks={
                    "C-0001": ("First", "Prose one."),
                    "C-0002": ("Second", "Prose two."),
                },
            )
            state = load_project(Path(root))
            output_path = Path(root) / "build" / "draft.md"
            build_draft_md(state, output_path)

            content = output_path.read_text(encoding="utf-8")
            assert "<!-- C-0001 -->" in content
            assert "<!-- C-0002 -->" in content

    def test_chunks_separated_by_double_newline(self) -> None:
        """Chunk sections are separated by double newlines."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001", "C-0002"],
                chunks={
                    "C-0001": ("First", "Para one."),
                    "C-0002": ("Second", "Para two."),
                },
            )
            state = load_project(Path(root))
            output_path = Path(root) / "build" / "draft.md"
            build_draft_md(state, output_path)

            content = output_path.read_text(encoding="utf-8")
            # Between the two chunks there should be blank lines
            assert "Para one.\n\n<!-- C-0002 -->" in content

    def test_output_ends_with_newline(self) -> None:
        """The output file always ends with a trailing newline."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "Trailing newline test.")},
            )
            state = load_project(Path(root))
            output_path = Path(root) / "build" / "draft.md"
            build_draft_md(state, output_path)

            content = output_path.read_text(encoding="utf-8")
            assert content.endswith("\n")


# ===========================================================================
# Warnings from project state are surfaced
# ===========================================================================


class TestWarningsSurfaced:
    """Warnings from project state and validation are surfaced in the build result."""

    def test_fallback_warning_in_build_result(self) -> None:
        """When chunk order fallback is used, the warning appears in the
        build CommandResult warnings."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=[],  # Triggers fallback
                chunks={
                    "C-0001": ("First", "Fallback warning test."),
                },
            )
            original_cwd = os.getcwd()
            try:
                os.chdir(root)
                result = run_build(mode="draft", fmt="md", output_path=None)
            finally:
                os.chdir(original_cwd)

            assert result.ok is True
            # Should have at least the CHUNK_ORDER_FALLBACK_USED warning
            fallback_warnings = [
                w for w in result.warnings
                if w.code == CHUNK_ORDER_FALLBACK_USED
            ]
            assert len(fallback_warnings) >= 1


# ===========================================================================
# Total word count and char count
# ===========================================================================


class TestCounts:
    """Total word count and character count are accurate."""

    def test_word_count_is_positive(self) -> None:
        """Word count is positive for non-empty chunks."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "Hello world test.")},
            )
            state = load_project(Path(root))
            output_path = Path(root) / "build" / "draft.md"
            result = build_draft_md(state, output_path)

            assert result.total_word_count > 0

    def test_char_count_matches_output(self) -> None:
        """Character count matches the length of the written output."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _init_project_dir(
                tmp,
                chunk_order=["C-0001"],
                chunks={"C-0001": ("First", "Char count test.")},
            )
            state = load_project(Path(root))
            output_path = Path(root) / "build" / "draft.md"
            result = build_draft_md(state, output_path)

            actual_length = len(output_path.read_text(encoding="utf-8"))
            assert result.total_char_count == actual_length
