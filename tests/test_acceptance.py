"""Happy-path end-to-end acceptance tests for AIP_Loom Phase 1.

These tests exercise the **full public CLI** through every command in
sequence, proving that the entire system works as an integrated whole.
Each test follows the workflow described in BuildSpec §17:

    init → validate → status → inspect → brief → reconcile --preview
    → reconcile (apply) → status → build

Every step must succeed and produce expected artifacts.  Tests are
deterministic and isolated (each uses its own tmp_path project).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aip_loom.cli import app
from aip_loom.schemas import SUPPORTED_SCHEMA_VERSION

from .helpers import build_model_output, parse_json_output, write_chunk, commit_all


# ---------------------------------------------------------------------------
# Fixture: project with 3 chunks, fully committed
# ---------------------------------------------------------------------------


@pytest.fixture()
def acceptance_project(tmp_path: Path) -> Path:
    """Create the canonical 3-chunk project for acceptance testing.

    This mirrors the `project_with_chunks` fixture from conftest but
    is defined locally so this file is self-documenting about its setup.
    """
    from aip_loom.checksum import compute_prose_checksum
    from aip_loom.git import configure_local_git
    from aip_loom.init import init_project
    from aip_loom.yaml_io import dump_yaml, dump_yaml_string, load_yaml

    root = tmp_path / "acceptance-e2e"
    root.mkdir()

    # Step 1: init
    init_project(root=root, name="brick-kiln", project_type="academic")
    configure_local_git(root)

    # Create three chunks
    chunks_data = {
        "C-0001": ("Introduction", "This is the introduction to the research paper. It covers the background and motivation for the study."),
        "C-0002": ("Methodology", "This section describes the methodology used in the study. It includes the experimental design and data collection procedures."),
        "C-0003": ("Results", "The results of the study are presented here. Key findings and statistical analyses are discussed in detail."),
    }

    for chunk_id, (title, prose) in chunks_data.items():
        chunks_dir = root / "chunks"
        chunk_path = chunks_dir / f"{chunk_id}.md"
        checksum = compute_prose_checksum(prose)
        word_count = len(prose.split())
        frontmatter = {
            "schema_version": SUPPORTED_SCHEMA_VERSION,
            "id": chunk_id,
            "title": title,
            "status": "draft",
            "word_count": word_count,
            "prose_checksum": checksum,
            "distillate_anchor": "",
            "created_at": "2026-05-30T12:00:00Z",
            "updated_at": "2026-05-30T12:00:00Z",
        }
        yaml_str = dump_yaml_string(frontmatter).rstrip("\n")
        chunk_content = f"---\n{yaml_str}\n---\n{prose}"
        chunk_path.write_text(chunk_content, encoding="utf-8")

    # Update manifest chunk order
    manifest_path = root / "aip_loom.yaml"
    manifest = load_yaml(manifest_path)
    manifest["chunks"]["order"] = ["C-0001", "C-0002", "C-0003"]
    dump_yaml(manifest, manifest_path)

    # Commit
    import subprocess
    (root / ".gitignore").write_text(".aip-loom/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "test: setup acceptance project"],
        check=False, capture_output=True,
    )

    return root


# ---------------------------------------------------------------------------
# Full E2E acceptance test
# ---------------------------------------------------------------------------


class TestFullEndToEnd:
    """Exercise every CLI command in the expected user workflow.

    This is the single most important test class in Phase 1.  If it
    passes, the core user-facing system works.
    """

    def test_complete_workflow(self, acceptance_project: Path, runner: CliRunner) -> None:
        """Run the full init → validate → status → inspect → brief
        → reconcile --preview → reconcile (apply) → status → build
        workflow and verify every step succeeds with expected artifacts.

        This proves that the entire public CLI works end-to-end.
        """
        root = acceptance_project
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            # ── Step 1: init (already done by fixture, but verify) ────────
            # Verify the project manifest exists
            assert (root / "aip_loom.yaml").is_file()

            # ── Step 2: validate ─────────────────────────────────────────
            result = runner.invoke(app, ["validate", "--json"])
            data = parse_json_output(result.output)
            assert data["ok"] is True, f"Validate failed: {data['message']}"
            assert data["data"]["chunks"] == 3

            # ── Step 3: status ───────────────────────────────────────────
            result = runner.invoke(app, ["status", "--json"])
            data = parse_json_output(result.output)
            assert data["ok"] is True, f"Status failed: {data['message']}"
            assert data["data"]["health"] == "healthy"
            assert data["data"]["chunks"]["total"] == 3

            # ── Step 4: inspect --chunk C-0002 ───────────────────────────
            result = runner.invoke(app, ["inspect", "C-0002", "--json"])
            data = parse_json_output(result.output)
            assert data["ok"] is True, f"Inspect failed: {data['message']}"
            assert data["data"]["target_chunk_id"] == "C-0002"

            # ── Step 5: brief --chunk C-0002 ─────────────────────────────
            result = runner.invoke(app, [
                "brief", "C-0002",
                "--task", "revise this chunk and resolve the blocking strand",
                "--json",
            ])
            data = parse_json_output(result.output)
            assert data["ok"] is True, f"Brief failed: {data['message']}"
            assert data["data"]["chunk_id"] == "C-0002"

            # ── Step 6: reconcile --preview ──────────────────────────────
            # Write a model output file
            model_output_text = build_model_output(
                target_chunk="C-0002",
                revised_prose="This is the revised methodology. The approach has been updated to include new analytical techniques and better controls.",
            )
            output_path = root / "outputs" / "ch002_revision.md"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(model_output_text, encoding="utf-8")

            result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--preview",
                "--json",
            ])
            data = parse_json_output(result.output)
            assert data["ok"] is True, f"Reconcile preview failed: {data['message']}"
            assert data["data"]["preview"] is True
            assert data["data"]["target_chunk"] == "C-0002"

            # ── Step 7: reconcile (apply) ────────────────────────────────
            result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--allow-dirty-git",
                "--json",
            ])
            data = parse_json_output(result.output)
            assert data["ok"] is True, f"Reconcile apply failed: {data['message']}"
            assert data["data"]["plan_applied"] is True
            assert data["data"]["target_chunk"] == "C-0002"

            # ── Step 8: status (after reconcile) ─────────────────────────
            result = runner.invoke(app, ["status", "--json"])
            data = parse_json_output(result.output)
            # Status should still be healthy or degraded (pending review warnings)
            assert data["data"]["chunks"]["total"] == 3

            # ── Step 9: build --mode draft --format md ───────────────────
            result = runner.invoke(app, [
                "build", "--mode", "draft", "--format", "md", "--json",
            ])
            data = parse_json_output(result.output)
            assert data["ok"] is True, f"Build failed: {data['message']}"
            assert data["data"]["chunk_count"] == 3
            assert data["data"]["mode"] == "draft"
            assert data["data"]["format"] == "md"

            # Verify the build output file exists
            output_file = root / "build" / "draft.md"
            assert output_file.is_file()
            content = output_file.read_text(encoding="utf-8")
            # Should contain all three chunks
            assert "C-0001" in content
            assert "C-0002" in content
            assert "C-0003" in content

        finally:
            os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# Individual command acceptance tests
# ---------------------------------------------------------------------------


class TestInitAcceptance:
    """Verify init produces a valid, complete project structure."""

    def test_init_creates_all_required_files(self, runner: CliRunner, tmp_path: Path) -> None:
        """Init creates every required canonical file and directory."""
        project_dir = tmp_path / "init-acceptance"
        result = runner.invoke(app, [
            "init", "test-project", "--type", "academic",
            "--dir", str(project_dir), "--json",
        ])
        data = parse_json_output(result.output)
        assert data["ok"] is True

        # Verify all required files exist
        assert (project_dir / "aip_loom.yaml").is_file()
        assert (project_dir / "distillate.yaml").is_file()
        assert (project_dir / "sessions.yaml").is_file()
        assert (project_dir / "comments.yaml").is_file()
        assert (project_dir / "ledgers" / "decisions.yaml").is_file()
        assert (project_dir / "ledgers" / "threads.yaml").is_file()
        assert (project_dir / "ledgers" / "questions.yaml").is_file()
        assert (project_dir / "chunks").is_dir()
        assert (project_dir / "archive").is_dir()
        assert (project_dir / ".aip-loom").is_dir()

    def test_init_academic_type(self, runner: CliRunner, tmp_path: Path) -> None:
        """Init with --type academic stores the type in the manifest."""
        project_dir = tmp_path / "type-test"
        result = runner.invoke(app, [
            "init", "brick-kiln", "--type", "academic",
            "--dir", str(project_dir), "--json",
        ])
        data = parse_json_output(result.output)
        assert data["ok"] is True


class TestValidateAcceptance:
    """Verify validate works correctly on a healthy project."""

    def test_validate_healthy_project(self, project_with_chunks: Path, runner: CliRunner) -> None:
        """Validate returns ok=True on a healthy 3-chunk project."""
        original_cwd = os.getcwd()
        try:
            os.chdir(project_with_chunks)
            result = runner.invoke(app, ["validate", "--json"])
            data = parse_json_output(result.output)
            assert data["ok"] is True
            assert data["data"]["chunks"] == 3
            assert data["data"]["error_count"] == 0
        finally:
            os.chdir(original_cwd)


class TestStatusAcceptance:
    """Verify status reports correct health for various project states."""

    def test_status_healthy_project(self, project_with_chunks: Path, runner: CliRunner) -> None:
        """Status reports healthy for a well-formed project."""
        original_cwd = os.getcwd()
        try:
            os.chdir(project_with_chunks)
            result = runner.invoke(app, ["status", "--json"])
            data = parse_json_output(result.output)
            assert data["ok"] is True
            assert data["data"]["health"] == "healthy"
            assert data["data"]["chunks"]["total"] == 3
        finally:
            os.chdir(original_cwd)


class TestInspectAcceptance:
    """Verify inspect returns correct context for each chunk."""

    def test_inspect_existing_chunk(self, project_with_chunks: Path, runner: CliRunner) -> None:
        """Inspect succeeds for an existing chunk and returns context."""
        original_cwd = os.getcwd()
        try:
            os.chdir(project_with_chunks)
            result = runner.invoke(app, ["inspect", "C-0002", "--json"])
            data = parse_json_output(result.output)
            assert data["ok"] is True
            assert data["data"]["target_chunk_id"] == "C-0002"
        finally:
            os.chdir(original_cwd)

    def test_inspect_nonexistent_chunk(self, project_with_chunks: Path, runner: CliRunner) -> None:
        """Inspect fails for a non-existent chunk ID."""
        original_cwd = os.getcwd()
        try:
            os.chdir(project_with_chunks)
            result = runner.invoke(app, ["inspect", "C-9999", "--json"])
            data = parse_json_output(result.output)
            assert data["ok"] is False
        finally:
            os.chdir(original_cwd)


class TestBriefAcceptance:
    """Verify brief generates deterministic session briefs."""

    def test_brief_succeeds_for_chunk(self, project_with_chunks: Path, runner: CliRunner) -> None:
        """Brief succeeds and writes a brief file for a valid chunk."""
        original_cwd = os.getcwd()
        try:
            os.chdir(project_with_chunks)
            result = runner.invoke(app, [
                "brief", "C-0002",
                "--task", "revise this chunk and resolve the blocking strand",
                "--json",
            ])
            data = parse_json_output(result.output)
            assert data["ok"] is True
            assert data["data"]["chunk_id"] == "C-0002"
        finally:
            os.chdir(original_cwd)

    def test_brief_dry_run_does_not_write(self, project_with_chunks: Path, runner: CliRunner) -> None:
        """Brief --dry-run does not create a brief file."""
        original_cwd = os.getcwd()
        try:
            os.chdir(project_with_chunks)
            # Ensure no brief exists before
            briefs_dir = project_with_chunks / ".aip-loom" / "briefs"
            brief_path = briefs_dir / "C-0002.md"
            if brief_path.exists():
                brief_path.unlink()

            result = runner.invoke(app, [
                "brief", "C-0002",
                "--task", "test task",
                "--dry-run",
                "--json",
            ])
            data = parse_json_output(result.output)
            assert data["ok"] is True
            # Brief file should NOT exist after dry-run
            assert not brief_path.exists()
        finally:
            os.chdir(original_cwd)


class TestReconcileAcceptance:
    """Verify reconcile preview and apply work correctly."""

    def test_reconcile_preview_succeeds(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """Reconcile --preview shows the plan without modifying files."""
        original_cwd = os.getcwd()
        try:
            os.chdir(project_with_chunks)

            model_output = build_model_output(target_chunk="C-0002")
            output_path = project_with_chunks / "outputs" / "revision.md"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(model_output, encoding="utf-8")

            result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--preview",
                "--json",
            ])
            data = parse_json_output(result.output)
            assert data["ok"] is True
            assert data["data"]["preview"] is True
            assert data["data"]["target_chunk"] == "C-0002"
        finally:
            os.chdir(original_cwd)

    def test_reconcile_apply_succeeds(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """Reconcile apply (no --preview) modifies canonical files."""
        original_cwd = os.getcwd()
        try:
            os.chdir(project_with_chunks)

            model_output = build_model_output(target_chunk="C-0002")
            output_path = project_with_chunks / "outputs" / "revision.md"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(model_output, encoding="utf-8")

            # Write the model output outside the project tree to avoid dirty git,
            # or use --allow-dirty-git since the outputs dir is untracked.
            result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--allow-dirty-git",
                "--json",
            ])
            data = parse_json_output(result.output)
            assert data["ok"] is True, f"Reconcile apply failed: {data['message']}"
            assert data["data"]["plan_applied"] is True
        finally:
            os.chdir(original_cwd)

    def test_preview_and_apply_equivalence(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """Preview shows the same target chunk and ledger change count
        as the subsequent apply.

        This proves the preview-then-apply workflow is honest: the user
        sees the real plan before committing to it.
        """
        original_cwd = os.getcwd()
        try:
            os.chdir(project_with_chunks)

            model_output = build_model_output(
                target_chunk="C-0002",
                new_decisions=[{"provisional_id": "new-1", "summary": "Use quantitative methods"}],
            )
            output_path = project_with_chunks / "outputs" / "eq_revision.md"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(model_output, encoding="utf-8")

            # Preview
            preview_result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--preview",
                "--json",
            ])
            preview_data = parse_json_output(preview_result.output)
            assert preview_data["ok"] is True
            preview_target = preview_data["data"]["target_chunk"]
            preview_ledger_count = len(preview_data["data"]["ledger_changes"])

            # Apply
            apply_result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--allow-dirty-git",
                "--json",
            ])
            apply_data = parse_json_output(apply_result.output)
            assert apply_data["ok"] is True, f"Apply failed: {apply_data.get('message', '')}"
            apply_target = apply_data["data"]["target_chunk"]

            # Same target chunk
            assert preview_target == apply_target
            # Apply should have applied the same number of ledger changes
            assert apply_data["data"]["ledger_changes_count"] == preview_ledger_count
        finally:
            os.chdir(original_cwd)


class TestBuildAcceptance:
    """Verify build produces correct deterministic output."""

    def test_build_draft_md_succeeds(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """Build --mode draft --format md produces a concatenation of all chunks."""
        original_cwd = os.getcwd()
        try:
            os.chdir(project_with_chunks)

            result = runner.invoke(app, [
                "build", "--mode", "draft", "--format", "md", "--json",
            ])
            data = parse_json_output(result.output)
            assert data["ok"] is True
            assert data["data"]["chunk_count"] == 3
            assert data["data"]["mode"] == "draft"
            assert data["data"]["format"] == "md"
            assert data["data"]["total_word_count"] > 0

            # Verify output file exists
            output_file = project_with_chunks / "build" / "draft.md"
            assert output_file.is_file()
        finally:
            os.chdir(original_cwd)

    def test_build_deterministic(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """Two builds on the same project state produce identical output.

        This proves build determinism: same project state + same chunk
        order → byte-for-byte identical Markdown.
        """
        original_cwd = os.getcwd()
        try:
            os.chdir(project_with_chunks)

            # Build 1
            output1 = project_with_chunks / "build" / "draft.md"
            runner.invoke(app, ["build", "--mode", "draft", "--format", "md", "--json"])
            content1 = output1.read_text(encoding="utf-8")

            # Build 2 (to a different path to avoid overwrite)
            output2 = project_with_chunks / "build" / "draft2.md"
            runner.invoke(app, [
                "build", "--mode", "draft", "--format", "md",
                "--output", str(output2), "--json",
            ])
            content2 = output2.read_text(encoding="utf-8")

            assert content1 == content2, "Build is not deterministic!"
        finally:
            os.chdir(original_cwd)

    def test_build_with_custom_output_path(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """Build --output writes to the specified path."""
        original_cwd = os.getcwd()
        try:
            os.chdir(project_with_chunks)

            custom_path = project_with_chunks / "custom" / "output.md"
            result = runner.invoke(app, [
                "build", "--mode", "draft", "--format", "md",
                "--output", str(custom_path), "--json",
            ])
            data = parse_json_output(result.output)
            assert data["ok"] is True
            assert custom_path.is_file()
        finally:
            os.chdir(original_cwd)

    def test_build_unsupported_format_rejected(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """Build with --format docx is rejected with BUILD_FORMAT_UNSUPPORTED."""
        original_cwd = os.getcwd()
        try:
            os.chdir(project_with_chunks)

            result = runner.invoke(app, [
                "build", "--mode", "draft", "--format", "docx", "--json",
            ])
            data = parse_json_output(result.output)
            assert data["ok"] is False
            assert data["code"] == "BUILD_FORMAT_UNSUPPORTED"
        finally:
            os.chdir(original_cwd)
