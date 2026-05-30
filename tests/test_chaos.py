"""P0 chaos / failure injection tests for AIP_Loom Phase 1.

These tests prove that the system survives realistic failure scenarios
and honours every recovery contract from BuildSpec §15.  Each test is
designed to be deterministic, isolated, and reproducible.

The chaos tests are organized by the failure scenario they inject,
with clear docstrings explaining what each test proves.

P0 Chaos Cases (minimum coverage from BuildSpec §17):

1. Crash / exception during staged write → state unchanged
2. Crash during canonical replacement → restore from snapshots
3. Git commit failure after successful apply → RECONCILE_APPLIED_BUT_GIT_FAILED,
   RECOVERY.md written, writer data preserved
4. Dirty Git tree (default blocks reconcile)
5. Hostile / malformed model output (parser + planner reject cleanly)
6. Stale lock present
7. Malformed update YAML / missing fence
8. Duplicate IDs in project
9. Concurrent reconcile attempts (lock contention)
10. Build on project with critical validation errors
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from aip_loom.cli import app
from aip_loom.errors import (
    BUILD_FORMAT_UNSUPPORTED,
    BUILD_VALIDATION_FAILED,
    GIT_DIRTY,
    LOCK_HELD,
    RECONCILE_APPLIED_BUT_GIT_FAILED,
    RECONCILE_POST_VALIDATION_FAILED,
    RECONCILE_PRE_VALIDATION_FAILED,
    RECONCILE_RESTORED_AFTER_FAILURE,
    RECONCILE_STAGED_VALIDATION_FAILED,
    RECOVERY_FILE_EXISTS,
    UPDATE_BLOCK_LEGACY_FENCE,
    UPDATE_BLOCK_MALFORMED,
    UPDATE_BLOCK_MISSING,
)
from aip_loom.git import GitError
from aip_loom.schemas import SUPPORTED_SCHEMA_VERSION

from .helpers import build_model_output, parse_json_output, write_model_output


# (write_model_output is imported from .helpers)


# ---------------------------------------------------------------------------
# 1. Crash / exception during staged write → state unchanged
# ---------------------------------------------------------------------------


class TestCrashDuringStagedWrite:
    """Prove that a crash during the staged write phase leaves the
    project in its pre-apply state.  No canonical files are modified.
    """

    def test_staged_failure_no_canonical_changes(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """If staged validation fails, no canonical files change on disk.

        This proves the RECONCILE_STAGED_VALIDATION_FAILED contract:
        nothing has been written to canonical files yet when staged
        validation fails, so the project is in its exact pre-apply state.
        """
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            # Capture original chunk content
            original_chunk = (root / "chunks" / "C-0002.md").read_text(encoding="utf-8")

            # Force a staged failure by making the ledger application fail.
            # We do this by corrupting the decisions ledger so it cannot be
            # loaded during the apply step's in-memory ledger mutation.
            from aip_loom.project import load_project

            model_output = build_model_output(
                target_chunk="C-0002",
                new_decisions=[{"provisional_id": "new-1", "summary": "A decision"}],
            )
            output_path = write_model_output(root, "staged_fail.md", model_output)

            # Corrupt the decisions ledger to make _apply_ledger_changes fail
            ledger_path = root / "ledgers" / "decisions.yaml"
            original_ledger = ledger_path.read_text(encoding="utf-8")

            # Write invalid YAML that will pass load_project but fail
            # during in-memory mutation. Since this is hard to arrange
            # without mocking, we use a direct mock instead.
            from aip_loom import reconcile_apply

            with patch.object(
                reconcile_apply, "_apply_ledger_changes",
                side_effect=ValueError("Simulated staged failure"),
            ):
                result = runner.invoke(app, [
                    "reconcile", "C-0002",
                    "--output", str(output_path),
                    "--allow-dirty-git",
                    "--json",
                ])

            # Should fail with staged validation error
            data = parse_json_output(result.output)
            assert data["ok"] is False
            assert data["code"] == RECONCILE_STAGED_VALIDATION_FAILED

            # Canonical files should be unchanged
            current_chunk = (root / "chunks" / "C-0002.md").read_text(encoding="utf-8")
            assert current_chunk == original_chunk, (
                "Chunk file was modified despite staged validation failure!"
            )

        finally:
            os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# 2. Crash during canonical replacement → restore from snapshots
# ---------------------------------------------------------------------------


class TestCrashDuringCanonicalReplacement:
    """Prove that a crash during canonical replacement triggers
    snapshot restore, leaving the project in its pre-apply state.
    """

    def test_write_failure_restores_snapshots(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """If a canonical write fails, all snapshotted files are restored.

        This proves the RECONCILE_RESTORED_AFTER_FAILURE contract:
        on any failure before canonical replacement completes, all
        snapshotted files are restored.  The project is in its exact
        pre-apply state.
        """
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            from aip_loom.project import load_project

            # Capture original state
            original_state = load_project(root)
            original_prose = original_state.chunks["C-0002"].prose_body

            model_output = build_model_output(target_chunk="C-0002")
            output_path = write_model_output(root, "canon_fail.md", model_output)

            # Mock safe_write_text to fail after the first call
            from aip_loom import reconcile_apply
            original_safe_write = reconcile_apply.safe_write_text
            call_count = 0

            def failing_safe_write(path, content, layout=None):
                nonlocal call_count
                call_count += 1
                if call_count > 1:
                    raise OSError("Simulated write failure on canonical file")
                return original_safe_write(path, content, layout)

            with patch.object(
                reconcile_apply, "safe_write_text",
                side_effect=failing_safe_write,
            ):
                result = runner.invoke(app, [
                    "reconcile", "C-0002",
                    "--output", str(output_path),
                    "--allow-dirty-git",
                    "--json",
                ])

            data = parse_json_output(result.output)
            assert data["ok"] is False
            assert data["code"] == RECONCILE_RESTORED_AFTER_FAILURE

            # Chunk file should be restored to original prose
            restored_state = load_project(root)
            restored_prose = restored_state.chunks["C-0002"].prose_body
            assert restored_prose == original_prose, (
                f"Prose was not restored! Got {restored_prose!r}, "
                f"expected {original_prose!r}"
            )

        finally:
            os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# 3. Git commit failure → RECOVERY.md written, writer data preserved
# ---------------------------------------------------------------------------


class TestGitCommitFailure:
    """Prove that when canonical writes succeed but Git commit fails,
    RECOVERY.md is written and writer data is preserved on disk.
    """

    def test_git_failure_writes_recovery_and_preserves_data(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """Git commit failure writes RECOVERY.md with recovery commands
        and preserves the applied writer data on disk.

        This proves the RECONCILE_APPLIED_BUT_GIT_FAILED contract:
        writer data is preserved and RECOVERY.md provides exact
        manual recovery commands.
        """
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            from aip_loom.errors import LoomError

            model_output = build_model_output(target_chunk="C-0002")
            output_path = write_model_output(root, "git_fail.md", model_output)

            with patch("aip_loom.reconcile_apply.git_commit") as mock_commit:
                mock_commit.side_effect = GitError(
                    LoomError(
                        code="GIT_COMMIT_FAILED",
                        message="Simulated git commit failure",
                        detail={},
                    )
                )

                result = runner.invoke(app, [
                    "reconcile", "C-0002",
                    "--output", str(output_path),
                    "--allow-dirty-git",
                    "--json",
                ])

            data = parse_json_output(result.output)
            assert data["ok"] is False
            assert data["code"] == RECONCILE_APPLIED_BUT_GIT_FAILED

            # RECOVERY.md should exist
            recovery_path = root / "RECOVERY.md"
            assert recovery_path.exists(), "RECOVERY.md was not written!"

            recovery_content = recovery_path.read_text(encoding="utf-8")
            assert "git add" in recovery_content
            assert "git commit" in recovery_content
            assert "C-0002" in recovery_content

            # Writer data should be preserved on disk
            from aip_loom.project import load_project

            state = load_project(root)
            chunk = state.chunks["C-0002"]
            assert "revised methodology" in chunk.prose_body.lower(), (
                "Writer data was not preserved after Git failure!"
            )

        finally:
            os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# 4. Dirty Git tree (default blocks reconcile)
# ---------------------------------------------------------------------------


class TestDirtyGitBlocksReconcile:
    """Prove that a dirty Git working tree blocks reconcile by default."""

    def test_dirty_git_blocks_reconcile(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """Dirty Git working tree produces GIT_DIRTY error.

        This proves that reconcile refuses to proceed when the working
        tree is dirty, preventing data loss from uncommitted changes.
        """
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            # Make the working tree dirty
            (root / "uncommitted-file.txt").write_text("dirty", encoding="utf-8")

            model_output = build_model_output(target_chunk="C-0002")
            output_path = write_model_output(root, "dirty_git.md", model_output)

            result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--json",
            ])

            data = parse_json_output(result.output)
            assert data["ok"] is False
            assert data["code"] == GIT_DIRTY

        finally:
            os.chdir(original_cwd)

    def test_dirty_git_allowed_with_flag(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """--allow-dirty-git bypasses the dirty Git check."""
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            (root / "uncommitted-file.txt").write_text("dirty", encoding="utf-8")

            model_output = build_model_output(target_chunk="C-0002")
            output_path = write_model_output(root, "dirty_allowed.md", model_output)

            result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--allow-dirty-git",
                "--json",
            ])

            data = parse_json_output(result.output)
            # Should NOT fail with GIT_DIRTY
            assert data["code"] != GIT_DIRTY

        finally:
            os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# 5. Hostile / malformed model output (parser + planner reject cleanly)
# ---------------------------------------------------------------------------


class TestHostileModelOutput:
    """Prove that the parser and planner reject malformed model output
    cleanly with stable error codes and no silent acceptance.
    """

    def test_no_loom_update_block(self, project_with_chunks: Path, runner: CliRunner) -> None:
        """Model output without a loom-update fence is rejected."""
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            output_path = write_model_output(
                root, "no_fence.md",
                "This is just plain text with no fence at all.",
            )

            result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--preview",
                "--json",
            ])

            data = parse_json_output(result.output)
            assert data["ok"] is False
            assert data["code"] == UPDATE_BLOCK_MISSING

        finally:
            os.chdir(original_cwd)

    def test_legacy_thread_update_fence(self, project_with_chunks: Path, runner: CliRunner) -> None:
        """Model output with thread-update fence is rejected."""
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            legacy_output = '```thread-update\nschema_version: "0.1.0"\n```\n'
            output_path = write_model_output(root, "legacy_fence.md", legacy_output)

            result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--preview",
                "--json",
            ])

            data = parse_json_output(result.output)
            assert data["ok"] is False
            assert data["code"] == UPDATE_BLOCK_LEGACY_FENCE

        finally:
            os.chdir(original_cwd)

    def test_malformed_yaml_inside_fence(self, project_with_chunks: Path, runner: CliRunner) -> None:
        """Model output with invalid YAML inside the fence is rejected."""
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            malformed_output = "```loom-update\ninvalid: {:\n```\n"
            output_path = write_model_output(root, "bad_yaml.md", malformed_output)

            result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--preview",
                "--json",
            ])

            data = parse_json_output(result.output)
            assert data["ok"] is False
            assert data["code"] == UPDATE_BLOCK_MALFORMED

        finally:
            os.chdir(original_cwd)

    def test_missing_required_fields(self, project_with_chunks: Path, runner: CliRunner) -> None:
        """Model output with missing required fields is rejected."""
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            # Missing target_chunk
            incomplete_output = f'```loom-update\nschema_version: "{SUPPORTED_SCHEMA_VERSION}"\nfence_type: loom-update\nmode: full_replacement\n```\n'
            output_path = write_model_output(root, "missing_fields.md", incomplete_output)

            result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--preview",
                "--json",
            ])

            data = parse_json_output(result.output)
            assert data["ok"] is False

        finally:
            os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# 6. Stale lock present
# ---------------------------------------------------------------------------


class TestStaleLock:
    """Prove that a stale lock (dead PID) is detected and reported
    properly, and that force-release resolves it.
    """

    def test_stale_lock_detected(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """A stale lock (dead PID) is detected by status and reported."""
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            from aip_loom.lock import ProjectLock
            from aip_loom.project import load_project

            # Create a lock with a dead PID
            state = load_project(root)
            layout = state.layout
            lock_path = layout.lock_path
            lock_path.parent.mkdir(parents=True, exist_ok=True)

            # Use a PID that definitely doesn't exist
            dead_pid = 9999999
            lock_path.write_text(f"{dead_pid}:reconcile\n", encoding="utf-8")

            # Status should report the stale lock as a warning
            result = runner.invoke(app, ["status", "--json"])
            data = parse_json_output(result.output)
            # The warning should mention the stale lock
            warning_codes = [w["code"] for w in data.get("warnings", [])]
            assert "STALE_LOCK_DETECTED" in warning_codes, (
                f"Expected STALE_LOCK_DETECTED warning, got: {warning_codes}"
            )

        finally:
            os.chdir(original_cwd)

    def test_force_release_stale_lock(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """Force-release of a stale lock allows subsequent operations."""
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            from aip_loom.lock import ProjectLock
            from aip_loom.project import load_project

            state = load_project(root)
            layout = state.layout
            lock_path = layout.lock_path

            # Create a stale lock
            dead_pid = 9999999
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(f"{dead_pid}:reconcile\n", encoding="utf-8")

            # Force-release the stale lock
            lock = ProjectLock(layout, command="test")
            lock.force_release()

            # Lock file should be gone
            assert not lock_path.exists()

        finally:
            os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# 7. Malformed update YAML / missing fence
# ---------------------------------------------------------------------------


class TestMalformedUpdateYAML:
    """Prove that various forms of malformed update YAML are rejected
    with stable error codes.
    """

    def test_empty_loom_update_block(self, project_with_chunks: Path, runner: CliRunner) -> None:
        """An empty loom-update block is rejected."""
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            output_path = write_model_output(
                root, "empty_block.md",
                "```loom-update\n```\n",
            )

            result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--preview",
                "--json",
            ])

            data = parse_json_output(result.output)
            assert data["ok"] is False
            assert data["code"] == UPDATE_BLOCK_MALFORMED

        finally:
            os.chdir(original_cwd)

    def test_chunk_mismatch_rejected(self, project_with_chunks: Path, runner: CliRunner) -> None:
        """CLI chunk argument that doesn't match model output target is rejected."""
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            # Model output targets C-0002, but CLI says C-0001
            model_output = build_model_output(target_chunk="C-0002")
            output_path = write_model_output(root, "mismatch.md", model_output)

            result = runner.invoke(app, [
                "reconcile", "C-0001",
                "--output", str(output_path),
                "--preview",
                "--json",
            ])

            data = parse_json_output(result.output)
            assert data["ok"] is False
            assert data["code"] == RECONCILE_PRE_VALIDATION_FAILED

        finally:
            os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# 8. Duplicate IDs in project
# ---------------------------------------------------------------------------


class TestDuplicateIDs:
    """Prove that duplicate chunk IDs are detected and reported as
    validation errors.
    """

    def test_duplicate_chunk_id_detected(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """Validation detects duplicate chunk IDs and reports them."""
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            from aip_loom.checksum import compute_prose_checksum
            from aip_loom.yaml_io import dump_yaml_string

            # Create a second chunk file with the same ID as C-0002
            # but a different filename
            chunk_path = root / "chunks" / "C-0002-copy.md"
            prose = "Duplicate ID prose."
            checksum = compute_prose_checksum(prose)
            frontmatter = {
                "schema_version": SUPPORTED_SCHEMA_VERSION,
                "id": "C-0002",  # Duplicate!
                "title": "Duplicate",
                "status": "draft",
                "word_count": len(prose.split()),
                "prose_checksum": checksum,
                "distillate_anchor": "",
                "created_at": "2026-05-30T12:00:00Z",
                "updated_at": "2026-05-30T12:00:00Z",
            }
            yaml_str = dump_yaml_string(frontmatter).rstrip("\n")
            chunk_content = f"---\n{yaml_str}\n---\n{prose}"
            chunk_path.write_text(chunk_content, encoding="utf-8")

            result = runner.invoke(app, ["validate", "--json"])
            data = parse_json_output(result.output)
            assert data["ok"] is False
            error_codes = [e["code"] for e in data.get("errors", [])]
            assert "VALIDATION_DUPLICATE_ID" in error_codes, (
                f"Expected VALIDATION_DUPLICATE_ID, got: {error_codes}"
            )

        finally:
            os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# 9. Concurrent reconcile attempts (lock contention)
# ---------------------------------------------------------------------------


class TestConcurrentReconcile:
    """Prove that concurrent reconcile attempts are blocked by the lock."""

    def test_lock_contention_blocks_second_reconcile(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """If another reconcile holds the lock, a second reconcile fails
        with LOCK_HELD.

        This proves that lock contention prevents concurrent mutations,
        which is critical for data integrity.
        """
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            from aip_loom.lock import ProjectLock
            from aip_loom.project import load_project

            # Acquire the lock manually
            state = load_project(root)
            layout = state.layout
            lock = ProjectLock(layout, command="other-reconcile")
            lock.acquire()

            try:
                model_output = build_model_output(target_chunk="C-0002")
                output_path = write_model_output(root, "locked.md", model_output)

                result = runner.invoke(app, [
                    "reconcile", "C-0002",
                    "--output", str(output_path),
                    "--json",
                ])

                data = parse_json_output(result.output)
                assert data["ok"] is False
                assert data["code"] in (LOCK_HELD, "LOCK_STALE")
            finally:
                lock.release()

        finally:
            os.chdir(original_cwd)

    def test_lock_released_after_failed_reconcile(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """After a failed reconcile, the lock is released so subsequent
        operations can proceed.

        This proves that lock release happens on every exit path.
        """
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            # Make reconcile fail (dirty git)
            (root / "dirty-file.txt").write_text("dirty", encoding="utf-8")

            model_output = build_model_output(target_chunk="C-0002")
            output_path = write_model_output(root, "lock_release.md", model_output)

            result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--json",
            ])
            # Should fail with GIT_DIRTY
            data = parse_json_output(result.output)
            assert data["ok"] is False

            # Lock should be released
            lock_path = root / ".aip-loom" / "lock"
            assert not lock_path.exists(), "Lock was not released after failed reconcile!"

        finally:
            os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# 10. Build on project with critical validation errors
# ---------------------------------------------------------------------------


class TestBuildWithValidationErrors:
    """Prove that build refuses to produce output when the project has
    critical validation errors.
    """

    def test_build_blocked_by_validation_errors(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """Build fails with BUILD_VALIDATION_FAILED when the project
        has critical validation errors.

        This proves that build never produces output from a corrupt
        project state.
        """
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            # Corrupt the manifest to cause a validation error
            manifest_path = root / "aip_loom.yaml"
            original_manifest = manifest_path.read_text(encoding="utf-8")

            # Write invalid YAML
            manifest_path.write_text("invalid: {", encoding="utf-8")

            result = runner.invoke(app, [
                "build", "--mode", "draft", "--format", "md", "--json",
            ])

            # Restore manifest
            manifest_path.write_text(original_manifest, encoding="utf-8")

            data = parse_json_output(result.output)
            assert data["ok"] is False
            assert data["code"] == BUILD_VALIDATION_FAILED

        finally:
            os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# Additional chaos: RECOVERY.md blocks second reconcile
# ---------------------------------------------------------------------------


class TestRecoveryFileBlocksReconcile:
    """Prove that a pre-existing RECOVERY.md blocks reconcile until
    the user resolves the previous failed reconcile.
    """

    def test_recovery_file_blocks_new_reconcile(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """If RECOVERY.md exists, reconcile is refused with RECOVERY_FILE_EXISTS.

        This prevents a second reconcile from overwriting the data
        from a previous reconcile that was applied but not committed.
        """
        root = project_with_chunks
        original_cwd = os.getcwd()

        try:
            os.chdir(root)

            # Write a RECOVERY.md to simulate a previous failed reconcile
            (root / "RECOVERY.md").write_text(
                "# RECOVERY\nPrevious reconcile failed.",
                encoding="utf-8",
            )

            model_output = build_model_output(target_chunk="C-0002")
            output_path = write_model_output(root, "blocked.md", model_output)

            result = runner.invoke(app, [
                "reconcile", "C-0002",
                "--output", str(output_path),
                "--json",
            ])

            data = parse_json_output(result.output)
            assert data["ok"] is False
            assert data["code"] == RECOVERY_FILE_EXISTS

        finally:
            os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# Additional chaos: Post-apply validation failure → restore
# ---------------------------------------------------------------------------


class TestPostApplyValidationFailure:
    """Prove that post-apply validation failure restores all files."""

    def test_post_apply_failure_restores_snapshots(
        self, project_with_chunks: Path, runner: CliRunner,
    ) -> None:
        """If post-apply validation fails, all files are restored from
        snapshots.

        This proves the RECONCILE_POST_VALIDATION_FAILED contract:
        even if canonical writes succeed, if post-apply validation
        finds errors, everything is rolled back.

        We test this at the service layer (apply_reconcile_plan) rather
        than the CLI because mocking internal validation at the CLI level
        is fragile.
        """
        root = project_with_chunks

        from aip_loom.project import load_project
        from aip_loom.reconcile_apply import apply_reconcile_plan
        from aip_loom.reconcile_plan import build_reconcile_plan
        from aip_loom.update_parser import parse_model_output
        from aip_loom.errors import LoomError
        from aip_loom.project import ValidationResult

        # Capture original state
        original_state = load_project(root)
        original_prose = original_state.chunks["C-0002"].prose_body

        model_output = build_model_output(target_chunk="C-0002")
        output_path = write_model_output(root, "post_fail.md", model_output)

        # Parse and build plan
        parse_result = parse_model_output(model_output)
        assert parse_result.ok
        parsed_block = parse_result.data["_parsed_block"]
        state = load_project(root)
        plan = build_reconcile_plan(parsed_block, state)
        assert plan.plan_ok

        # Mock validate_project to return a failing result on the second call
        # (the first call is pre-validation, second is post-apply)
        call_count = 0
        original_validate = __import__(
            "aip_loom.reconcile_apply", fromlist=["validate_project"],
        ).validate_project

        def failing_validate(state, chunk_scope=None):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                # Post-apply validation: fail
                return ValidationResult(
                    errors=(LoomError(
                        code="VALIDATION_BROKEN_REFERENCE",
                        message="broken ref in post-apply",
                    ),),
                    warnings=(),
                    ok=False,
                )
            return original_validate(state, chunk_scope)

        with patch(
            "aip_loom.reconcile_apply.validate_project",
            side_effect=failing_validate,
        ):
            result = apply_reconcile_plan(
                plan=plan,
                model_output_text=model_output,
                root=root,
                allow_dirty_git=True,
            )

        assert not result.ok
        assert result.code == RECONCILE_POST_VALIDATION_FAILED

        # Chunk should be restored to original
        restored_state = load_project(root)
        restored_prose = restored_state.chunks["C-0002"].prose_body
        assert restored_prose == original_prose, (
            f"Post-apply validation failure did not restore! "
            f"Got {restored_prose!r}, expected {original_prose!r}"
        )
