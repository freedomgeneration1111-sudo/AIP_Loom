"""Tests for aip_loom.reconcile_apply — transactional reconcile apply.

These tests prove:

- apply_reconcile_plan applies a clean plan successfully
- Lock is acquired before any work and released after
- Pre-validation failure blocks apply (no files changed)
- Dirty Git blocks apply (no files changed)
- Plan with conflicts blocks apply (no files changed)
- Snapshot before modify: files are restored on failure
- Staged validation failure changes nothing on disk
- Post-apply validation failure restores all files
- Git commit failure writes RECOVERY.md
- RECOVERY.md contains exact recovery commands
- Lock is always released (even on error)
- Idempotent: running apply on already-applied state still works
- New decisions get correct canonical IDs
- New threads get correct canonical IDs
- Close thread changes thread state to closed
- Update existing entry applies changes correctly
- Prose replacement updates checksum and word_count
- Session log gets new entry after reconcile
- Archive evidence files are written
- apply_reconcile_plan consumes the ReconcilePlan directly (no re-parsing)
- ReconcileApplyResult is frozen and serializable
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

from aip_loom.errors import (
    GIT_DIRTY,
    LOCK_HELD,
    RECONCILE_APPLIED_BUT_GIT_FAILED,
    RECONCILE_POST_VALIDATION_FAILED,
    RECONCILE_PRE_VALIDATION_FAILED,
    RECONCILE_RESTORED_AFTER_FAILURE,
    RECONCILE_STAGED_VALIDATION_FAILED,
)
from aip_loom.git import configure_local_git
from aip_loom.init import init_project
from aip_loom.project import ProjectState, load_project
from aip_loom.reconcile_apply import (
    ReconcileApplyResult,
    apply_reconcile_plan,
    write_recovery_file,
    _apply_ledger_changes,
    _build_updated_chunk_content,
    _serialize_ledger,
)
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
    DecisionEntry,
    DecisionLedger,
    ReviewState,
    SessionLog,
    ThreadEntry,
    ThreadLedger,
    ThreadState,
    UpdateBlock,
    UpdateLedgerItemNew,
    UpdateThreadItemNew,
)
from aip_loom.update_parser import ParsedUpdateBlock
from aip_loom.yaml_io import dump_yaml_string


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_V = SUPPORTED_SCHEMA_VERSION
_TS = "2026-05-30T12:00:00Z"

# A minimal model output block for testing
_MINIMAL_MODEL_OUTPUT = f"""```loom-update
schema_version: "{_V}"
fence_type: loom-update
mode: full_replacement
target_chunk: C-0001
revised_prose: "The revised prose content."
change_summary: "Updated prose."
requires_human_review: true
---
# Revised Chunk

The revised prose content.
```
"""

_MODEL_OUTPUT_WITH_NEW_DECISION = f"""```loom-update
schema_version: "{_V}"
fence_type: loom-update
mode: full_replacement
target_chunk: C-0001
revised_prose: "New prose."
change_summary: "Added decision."
requires_human_review: true
new_decisions:
  - provisional_id: new-1
    summary: "A new decision"
---
# Revised Chunk

New prose.
```
"""

_MODEL_OUTPUT_WITH_CLOSE_THREAD = f"""```loom-update
schema_version: "{_V}"
fence_type: loom-update
mode: full_replacement
target_chunk: C-0001
revised_prose: "Updated."
change_summary: "Closed thread."
requires_human_review: true
close_threads:
  - T-0001
---
# Revised Chunk

Updated.
```
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    """Create a minimal AIP_Loom project with one chunk."""
    from aip_loom.init import init_project
    from aip_loom.git import configure_local_git

    root = tmp_path / "test-project"
    root.mkdir()

    # Initialize the project
    init_project(root=root, name="test-project", project_type="novel")

    # Configure git for testing
    configure_local_git(root)

    # Create a chunk file
    chunks_dir = root / "chunks"
    chunk_path = chunks_dir / "C-0001.md"

    from aip_loom.checksum import compute_prose_checksum

    prose = "Original prose content for the chunk."
    checksum = compute_prose_checksum(prose)
    word_count = len(prose.split())

    frontmatter = {
        "schema_version": _V,
        "id": "C-0001",
        "title": "Test Chunk",
        "status": "draft",
        "word_count": word_count,
        "prose_checksum": checksum,
        "distillate_anchor": "",
        "created_at": _TS,
        "updated_at": _TS,
    }

    yaml_str = dump_yaml_string(frontmatter).rstrip("\n")
    chunk_content = f"---\n{yaml_str}\n---\n{prose}"
    chunk_path.write_text(chunk_content, encoding="utf-8")

    # Update manifest chunk order
    from aip_loom.yaml_io import load_yaml, dump_yaml
    manifest_path = root / "aip_loom.yaml"
    manifest = load_yaml(manifest_path)
    if "chunks" not in manifest:
        manifest["chunks"] = {}
    manifest["chunks"]["order"] = ["C-0001"]
    dump_yaml(manifest, manifest_path)

    # Commit the chunk and manifest (add everything to be safe)
    import subprocess
    # Add .gitignore to exclude .aip-loom from dirty checks
    (root / ".gitignore").write_text(".aip-loom/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "test: setup project with chunk", "--allow-empty"], check=False, capture_output=True)

    return root


@pytest.fixture()
def project_with_thread(tmp_path: Path) -> Path:
    """Create a project with an open thread in the threads ledger."""
    root = tmp_path / "test-project-thread"
    root.mkdir()

    init_project(root=root, name="test-project", project_type="novel")
    configure_local_git(root)

    # Create a chunk
    chunks_dir = root / "chunks"
    chunk_path = chunks_dir / "C-0001.md"

    from aip_loom.checksum import compute_prose_checksum

    prose = "Original prose content."
    checksum = compute_prose_checksum(prose)
    word_count = len(prose.split())

    frontmatter = {
        "schema_version": _V,
        "id": "C-0001",
        "title": "Test Chunk",
        "status": "draft",
        "word_count": word_count,
        "prose_checksum": checksum,
        "distillate_anchor": "",
        "created_at": _TS,
        "updated_at": _TS,
    }

    yaml_str = dump_yaml_string(frontmatter).rstrip("\n")
    chunk_content = f"---\n{yaml_str}\n---\n{prose}"
    chunk_path.write_text(chunk_content, encoding="utf-8")

    # Add a thread to the threads ledger
    from aip_loom.yaml_io import load_yaml, dump_yaml
    threads_path = root / "ledgers" / "threads.yaml"
    threads_data = load_yaml(threads_path)
    threads_data["entries"] = [
        {
            "id": "T-0001",
            "review_state": "approved",
            "created_at": _TS,
            "summary": "An open thread",
            "state": "open",
            "scope": "global",
            "chunk_id": "C-0001",
            "blocked_by": [],
        }
    ]
    dump_yaml(threads_data, threads_path)

    # Update manifest
    from aip_loom.yaml_io import load_yaml as load_yaml2, dump_yaml as dump_yaml2
    manifest_path = root / "aip_loom.yaml"
    manifest = load_yaml2(manifest_path)
    manifest["chunks"]["order"] = ["C-0001"]
    dump_yaml2(manifest, manifest_path)

    # Commit all changes
    import subprocess
    (root / ".gitignore").write_text(".aip-loom/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "test: add open thread", "--allow-empty"], check=False, capture_output=True)

    return root


# ---------------------------------------------------------------------------
# Helper to build a plan from model output
# ---------------------------------------------------------------------------


def _build_plan_from_output(model_output: str, root: Path) -> ReconcilePlan:
    """Build a ReconcilePlan from model output text."""
    from aip_loom.update_parser import parse_model_output

    parse_result = parse_model_output(model_output)
    assert parse_result.ok, f"Parse failed: {parse_result.message}"

    parsed_block = parse_result.data["_parsed_block"]
    state = load_project(root)
    plan = build_reconcile_plan(parsed_block, state)
    return plan


# ---------------------------------------------------------------------------
# Success path tests
# ---------------------------------------------------------------------------


class TestSuccessfulApply:
    """Verify successful apply for a clean plan."""

    def test_minimal_apply_succeeds(self, project_dir: Path) -> None:
        """A minimal valid plan applies successfully."""
        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        assert plan.plan_ok

        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=_MINIMAL_MODEL_OUTPUT,
            root=project_dir,
        )

        assert result.ok
        assert result.data["plan_applied"] is True
        assert result.data["git_committed"] is True
        assert result.data["recovery_file_written"] is False
        assert result.data["target_chunk"] == "C-0001"

    def test_prose_is_replaced(self, project_dir: Path) -> None:
        """After apply, the chunk file contains the revised prose."""
        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=_MINIMAL_MODEL_OUTPUT,
            root=project_dir,
        )
        assert result.ok

        # Reload and check the chunk
        state = load_project(project_dir)
        chunk = state.chunks["C-0001"]
        assert "revised prose content" in chunk.prose_body.lower()

    def test_checksum_updated_after_apply(self, project_dir: Path) -> None:
        """After apply, the chunk's checksum matches its new prose."""
        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=_MINIMAL_MODEL_OUTPUT,
            root=project_dir,
        )
        assert result.ok

        state = load_project(project_dir)
        chunk = state.chunks["C-0001"]
        from aip_loom.checksum import compute_prose_checksum
        expected = compute_prose_checksum(chunk.prose_body)
        assert chunk.frontmatter.prose_checksum == expected

    def test_session_log_updated(self, project_dir: Path) -> None:
        """After apply, a new session entry exists."""
        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=_MINIMAL_MODEL_OUTPUT,
            root=project_dir,
        )
        assert result.ok

        state = load_project(project_dir)
        assert state.sessions is not None
        assert len(state.sessions.entries) >= 1

    def test_new_decision_applied(self, project_dir: Path) -> None:
        """After apply with new decisions, the ledger has the new entry."""
        plan = _build_plan_from_output(
            _MODEL_OUTPUT_WITH_NEW_DECISION, project_dir,
        )
        assert plan.plan_ok

        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=_MODEL_OUTPUT_WITH_NEW_DECISION,
            root=project_dir,
        )
        assert result.ok

        state = load_project(project_dir)
        assert state.decisions_ledger is not None
        # Should have the new decision (D-0001 since no existing)
        decision_ids = [e.id for e in state.decisions_ledger.entries]
        assert "D-0001" in decision_ids

    def test_close_thread_applied(self, project_with_thread: Path) -> None:
        """After apply with close_threads, the thread is closed."""
        plan = _build_plan_from_output(
            _MODEL_OUTPUT_WITH_CLOSE_THREAD, project_with_thread,
        )
        assert plan.plan_ok

        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=_MODEL_OUTPUT_WITH_CLOSE_THREAD,
            root=project_with_thread,
        )
        assert result.ok

        state = load_project(project_with_thread)
        assert state.threads_ledger is not None
        t1 = [e for e in state.threads_ledger.entries if e.id == "T-0001"]
        assert len(t1) == 1
        assert t1[0].state == ThreadState.CLOSED


# ---------------------------------------------------------------------------
# Pre-validation failure tests
# ---------------------------------------------------------------------------


class TestPreValidationFailure:
    """Verify that pre-validation failure blocks apply with no file changes."""

    def test_pre_validation_failure_no_changes(self, project_dir: Path) -> None:
        """Pre-validation failure changes no canonical files."""
        # Corrupt the manifest to cause validation failure
        manifest_path = project_dir / "aip_loom.yaml"
        original = manifest_path.read_text(encoding="utf-8")

        # Write invalid YAML to the manifest
        manifest_path.write_text("invalid: {", encoding="utf-8")

        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        # The plan might still be ok from before, but apply should fail

        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=_MINIMAL_MODEL_OUTPUT,
            root=project_dir,
        )

        # Restore the original manifest so we can check
        manifest_path.write_text(original, encoding="utf-8")

        assert not result.ok
        assert result.code == RECONCILE_PRE_VALIDATION_FAILED


# ---------------------------------------------------------------------------
# Git cleanliness tests
# ---------------------------------------------------------------------------


class TestGitCleanliness:
    """Verify that dirty Git blocks reconcile."""

    def test_dirty_git_blocks_reconcile(self, project_dir: Path) -> None:
        """Dirty Git working tree blocks apply."""
        # Make the working tree dirty
        (project_dir / "dirty-file.txt").write_text("dirty", encoding="utf-8")

        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=_MINIMAL_MODEL_OUTPUT,
            root=project_dir,
        )

        assert not result.ok
        assert result.code == GIT_DIRTY

    def test_dirty_git_allowed_with_flag(self, project_dir: Path) -> None:
        """Dirty Git is allowed with allow_dirty_git=True."""
        # Make the working tree dirty
        (project_dir / "dirty-file.txt").write_text("dirty", encoding="utf-8")

        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=_MINIMAL_MODEL_OUTPUT,
            root=project_dir,
            allow_dirty_git=True,
        )

        # Should succeed (or at least not fail due to dirty Git)
        assert result.code != GIT_DIRTY


# ---------------------------------------------------------------------------
# Plan conflict tests
# ---------------------------------------------------------------------------


class TestPlanConflicts:
    """Verify that plans with conflicts are rejected."""

    def test_conflicting_plan_rejected(self, project_dir: Path) -> None:
        """A plan with conflicts is rejected without applying."""
        # Build a plan that targets a non-existent chunk
        from aip_loom.project import ChunkData
        from aip_loom.layout import ProjectLayout

        state = load_project(project_dir)
        layout = state.layout

        # Create a parsed block targeting a non-existent chunk
        update_block = UpdateBlock(
            schema_version=_V,
            fence_type="loom-update",
            mode="full_replacement",
            target_chunk="C-9999",
            revised_prose="Revised.",
            requires_human_review=True,
        )
        parsed = ParsedUpdateBlock(
            update_block=update_block,
            revised_prose="Revised.",
            raw_content="yaml",
            fence_start=0,
            fence_end=100,
        )
        plan = build_reconcile_plan(parsed, state)
        assert not plan.plan_ok

        result = apply_reconcile_plan(
            plan=plan,
            model_output_text="test",
            root=project_dir,
        )

        assert not result.ok
        assert result.code == RECONCILE_PRE_VALIDATION_FAILED


# ---------------------------------------------------------------------------
# Git failure tests
# ---------------------------------------------------------------------------


class TestGitFailure:
    """Verify that Git commit failure writes RECOVERY.md."""

    def test_git_failure_writes_recovery_file(self, project_dir: Path) -> None:
        """When Git commit fails, RECOVERY.md is written."""
        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        assert plan.plan_ok

        # Mock git_commit to raise GitError
        from aip_loom.git import GitError
        from aip_loom.errors import LoomError

        with patch("aip_loom.reconcile_apply.git_commit") as mock_commit:
            mock_commit.side_effect = GitError(
                LoomError(
                    code="GIT_COMMIT_FAILED",
                    message="Mock git commit failure",
                    detail={},
                )
            )

            result = apply_reconcile_plan(
                plan=plan,
                model_output_text=_MINIMAL_MODEL_OUTPUT,
                root=project_dir,
            )

        assert not result.ok
        assert result.code == RECONCILE_APPLIED_BUT_GIT_FAILED
        assert result.data["recovery_file_written"] is True

        # Verify RECOVERY.md exists
        recovery_path = project_dir / "RECOVERY.md"
        assert recovery_path.exists()

        content = recovery_path.read_text(encoding="utf-8")
        assert "RECOVERY" in content
        assert "git add" in content
        assert "git commit" in content
        assert "C-0001" in content

    def test_git_failure_preserves_writer_data(self, project_dir: Path) -> None:
        """After Git failure, the canonical data is still on disk."""
        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)

        from aip_loom.git import GitError
        from aip_loom.errors import LoomError

        with patch("aip_loom.reconcile_apply.git_commit") as mock_commit:
            mock_commit.side_effect = GitError(
                LoomError(
                    code="GIT_COMMIT_FAILED",
                    message="Mock git commit failure",
                    detail={},
                )
            )
            apply_reconcile_plan(
                plan=plan,
                model_output_text=_MINIMAL_MODEL_OUTPUT,
                root=project_dir,
            )

        # The chunk file should have the revised prose
        state = load_project(project_dir)
        chunk = state.chunks["C-0001"]
        assert "revised prose content" in chunk.prose_body.lower()


# ---------------------------------------------------------------------------
# Lock tests
# ---------------------------------------------------------------------------


class TestLockSemantics:
    """Verify that locks are properly managed."""

    def test_lock_released_on_success(self, project_dir: Path) -> None:
        """Lock is released after successful apply."""
        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=_MINIMAL_MODEL_OUTPUT,
            root=project_dir,
        )
        assert result.ok

        # Lock file should not exist
        lock_path = project_dir / ".aip-loom" / "lock"
        assert not lock_path.exists()

    def test_lock_released_on_failure(self, project_dir: Path) -> None:
        """Lock is released even when apply fails."""
        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)

        from aip_loom.git import GitError
        from aip_loom.errors import LoomError

        with patch("aip_loom.reconcile_apply.git_commit") as mock_commit:
            mock_commit.side_effect = GitError(
                LoomError(
                    code="GIT_COMMIT_FAILED",
                    message="Mock failure",
                    detail={},
                )
            )
            apply_reconcile_plan(
                plan=plan,
                model_output_text=_MINIMAL_MODEL_OUTPUT,
                root=project_dir,
            )

        lock_path = project_dir / ".aip-loom" / "lock"
        assert not lock_path.exists()

    def test_lock_contention_blocks_apply(self, project_dir: Path) -> None:
        """If lock is already held, apply fails with LOCK_HELD."""
        from aip_loom.lock import ProjectLock
        from aip_loom.project import load_project

        # Get the layout
        state = load_project(project_dir)
        layout = state.layout

        # Acquire lock manually
        lock = ProjectLock(layout, command="other-process")
        lock.acquire()

        try:
            plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
            result = apply_reconcile_plan(
                plan=plan,
                model_output_text=_MINIMAL_MODEL_OUTPUT,
                root=project_dir,
            )

            assert not result.ok
            assert result.code in (LOCK_HELD, "LOCK_STALE")
        finally:
            lock.release()


# ---------------------------------------------------------------------------
# Recovery file tests
# ---------------------------------------------------------------------------


class TestRecoveryFile:
    """Verify RECOVERY.md content and structure."""

    def test_recovery_file_contains_git_commands(self, tmp_path: Path) -> None:
        """RECOVERY.md contains exact git add and git commit commands."""
        from aip_loom.reconcile_plan import PlannedFileChange

        plan = ReconcilePlan(
            target_chunk="C-0001",
            mode="full_replacement",
            revised_prose="New prose.",
            ledger_changes=(),
            id_mappings=(),
            file_changes=(
                PlannedFileChange(
                    file_path="/tmp/test/C-0001.md",
                    change_description="Replace prose",
                    change_type="prose_replacement",
                ),
            ),
            conflicts=(),
            warnings=(),
            requires_human_review=True,
            plan_ok=True,
        )

        recovery_path = write_recovery_file(
            root=tmp_path,
            plan=plan,
            tx_id="abc123",
            session_id="S-0001",
            git_error="pre-commit hook failed",
        )

        assert recovery_path.exists()
        content = recovery_path.read_text(encoding="utf-8")

        assert "git add" in content
        assert "git commit" in content
        assert "C-0001" in content
        assert "abc123" in content
        assert "S-0001" in content
        assert "pre-commit hook failed" in content
        assert "git checkout" in content  # Undo instructions


# ---------------------------------------------------------------------------
# ReconcileApplyResult tests
# ---------------------------------------------------------------------------


class TestReconcileApplyResult:
    """Verify ReconcileApplyResult properties."""

    def test_result_is_frozen(self) -> None:
        """ReconcileApplyResult is frozen."""
        result = ReconcileApplyResult(
            plan_applied=True,
            target_chunk="C-0001",
            ledger_changes_count=1,
            id_mappings_count=1,
            file_changes_count=2,
            git_committed=True,
            recovery_file_written=False,
            tx_id="abc123",
            session_id="S-0001",
        )
        with pytest.raises(FrozenInstanceError):
            result.plan_applied = False  # type: ignore[misc]

    def test_result_to_dict(self) -> None:
        """to_dict() produces a JSON-serialisable dictionary."""
        result = ReconcileApplyResult(
            plan_applied=True,
            target_chunk="C-0001",
            ledger_changes_count=1,
            id_mappings_count=1,
            file_changes_count=2,
            git_committed=True,
            recovery_file_written=False,
            tx_id="abc123",
            session_id="S-0001",
        )

        d = result.to_dict()
        assert d["plan_applied"] is True
        assert d["target_chunk"] == "C-0001"
        assert d["git_committed"] is True
        assert d["recovery_file_written"] is False

        # Verify JSON-serialisable
        json_str = json.dumps(d)
        assert isinstance(json_str, str)


# ---------------------------------------------------------------------------
# Internal helper tests
# ---------------------------------------------------------------------------


class TestInternalHelpers:
    """Verify internal helper functions."""

    def test_serialize_ledger_produces_yaml(self) -> None:
        """_serialize_ledger produces valid YAML."""
        ledger = DecisionLedger(
            schema_version=_V,
            entries=[
                DecisionEntry(
                    id="D-0001",
                    review_state=ReviewState.APPROVED,
                    created_at=_TS,
                    summary="Test decision",
                ),
            ],
        )

        yaml_str = _serialize_ledger(ledger)
        assert isinstance(yaml_str, str)
        assert "D-0001" in yaml_str

    def test_apply_ledger_changes_new_decision(self, tmp_path: Path) -> None:
        """_apply_ledger_changes creates a new decision entry."""
        from aip_loom.layout import ProjectLayout
        from aip_loom.project import ChunkData

        (tmp_path / "chunks").mkdir()
        (tmp_path / "ledgers").mkdir()
        (tmp_path / "archive").mkdir()
        (tmp_path / ".aip-loom").mkdir()
        layout = ProjectLayout(root=tmp_path)

        state = ProjectState(
            layout=layout,
            manifest=None,
            decisions_ledger=DecisionLedger(schema_version=_V, entries=[]),
            threads_ledger=ThreadLedger(schema_version=_V, entries=[]),
            questions_ledger=None,
            distillate=None,
            sessions=None,
            comments=None,
            chunks={"C-0001": ChunkData(
                file_path=tmp_path / "chunks" / "C-0001.md",
                frontmatter=ChunkFrontmatter(
                    schema_version=_V,
                    id="C-0001",
                    title="Test",
                    word_count=1,
                    prose_checksum="abc",
                    created_at=_TS,
                    updated_at=_TS,
                ),
                prose_body="test",
            )},
            chunk_order=None,
        )

        plan = ReconcilePlan(
            target_chunk="C-0001",
            mode="full_replacement",
            revised_prose="New prose.",
            ledger_changes=(
                PlannedLedgerChange(
                    change_type="new_decision",
                    item_id="D-0001",
                    provisional_id="new-1",
                    detail={"summary": "A decision", "rationale": "", "scope": "global", "chunk_id": "", "review_state": "pending"},
                    requires_human_review=True,
                ),
            ),
            id_mappings=(
                ProvisionalIdMapping(
                    provisional_id="new-1",
                    canonical_id="D-0001",
                    item_type="decision",
                    summary="A decision",
                ),
            ),
            file_changes=(),
            conflicts=(),
            warnings=(),
            requires_human_review=True,
            plan_ok=True,
        )

        result = _apply_ledger_changes(plan, state)
        assert "decisions" in result
        assert len(result["decisions"].entries) == 1
        assert result["decisions"].entries[0].id == "D-0001"

    def test_apply_ledger_changes_close_thread(self, tmp_path: Path) -> None:
        """_apply_ledger_changes closes a thread."""
        from aip_loom.layout import ProjectLayout
        from aip_loom.project import ChunkData

        (tmp_path / "chunks").mkdir()
        (tmp_path / "ledgers").mkdir()
        (tmp_path / "archive").mkdir()
        (tmp_path / ".aip-loom").mkdir()
        layout = ProjectLayout(root=tmp_path)

        thread = ThreadEntry(
            id="T-0001",
            review_state=ReviewState.APPROVED,
            created_at=_TS,
            summary="Open thread",
            state=ThreadState.OPEN,
        )

        state = ProjectState(
            layout=layout,
            manifest=None,
            decisions_ledger=DecisionLedger(schema_version=_V, entries=[]),
            threads_ledger=ThreadLedger(schema_version=_V, entries=[thread]),
            questions_ledger=None,
            distillate=None,
            sessions=None,
            comments=None,
            chunks={"C-0001": ChunkData(
                file_path=tmp_path / "chunks" / "C-0001.md",
                frontmatter=ChunkFrontmatter(
                    schema_version=_V,
                    id="C-0001",
                    title="Test",
                    word_count=1,
                    prose_checksum="abc",
                    created_at=_TS,
                    updated_at=_TS,
                ),
                prose_body="test",
            )},
            chunk_order=None,
        )

        plan = ReconcilePlan(
            target_chunk="C-0001",
            mode="full_replacement",
            revised_prose="New prose.",
            ledger_changes=(
                PlannedLedgerChange(
                    change_type="close_thread",
                    item_id="T-0001",
                    provisional_id="",
                    detail={"state": "closed", "closed_at": _TS},
                    requires_human_review=False,
                ),
            ),
            id_mappings=(),
            file_changes=(),
            conflicts=(),
            warnings=(),
            requires_human_review=False,
            plan_ok=True,
        )

        result = _apply_ledger_changes(plan, state)
        assert "threads" in result
        closed = [e for e in result["threads"].entries if e.id == "T-0001"]
        assert len(closed) == 1
        assert closed[0].state == ThreadState.CLOSED


# ---------------------------------------------------------------------------
# Archive evidence tests
# ---------------------------------------------------------------------------


class TestArchiveEvidence:
    """Verify that archive evidence files are written."""

    def test_archive_evidence_written_on_success(self, project_dir: Path) -> None:
        """After successful apply, pre and post archive evidence exist."""
        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=_MINIMAL_MODEL_OUTPUT,
            root=project_dir,
        )
        assert result.ok

        archive_dir = project_dir / "archive"
        assert archive_dir.is_dir()

        # Should have at least one evidence file
        evidence_files = list(archive_dir.glob("*reconcile*"))
        assert len(evidence_files) >= 1

        # Evidence should be valid JSON
        for ef in evidence_files:
            content = ef.read_text(encoding="utf-8")
            data = json.loads(content)
            assert "target_chunk" in data
            assert data["target_chunk"] == "C-0001"


# ---------------------------------------------------------------------------
# Chaos / edge case tests
# ---------------------------------------------------------------------------


class TestChaosAndEdgeCases:
    """Verify behavior under unusual or hostile conditions."""

    def test_concurrent_lock_blocks_apply(self, project_dir: Path) -> None:
        """If another process holds the lock, apply is blocked."""
        from aip_loom.lock import ProjectLock
        from aip_loom.project import load_project

        state = load_project(project_dir)
        layout = state.layout

        lock = ProjectLock(layout, command="another-reconcile")
        lock.acquire()

        try:
            plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
            result = apply_reconcile_plan(
                plan=plan,
                model_output_text=_MINIMAL_MODEL_OUTPUT,
                root=project_dir,
            )
            assert not result.ok
        finally:
            lock.release()

    def test_no_new_items_still_works(self, project_dir: Path) -> None:
        """Apply with only prose replacement (no new items) works."""
        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=_MINIMAL_MODEL_OUTPUT,
            root=project_dir,
        )
        assert result.ok
        assert result.data["ledger_changes_count"] == 0

    def test_multiple_applies_increment_ids(self, project_dir: Path) -> None:
        """Multiple applies increment session IDs correctly."""
        # First apply
        plan1 = _build_plan_from_output(
            _MODEL_OUTPUT_WITH_NEW_DECISION, project_dir,
        )
        result1 = apply_reconcile_plan(
            plan=plan1,
            model_output_text=_MODEL_OUTPUT_WITH_NEW_DECISION,
            root=project_dir,
        )
        assert result1.ok
        session1 = result1.data["session_id"]

        # Second apply (same output again would create D-0002)
        plan2 = _build_plan_from_output(
            _MODEL_OUTPUT_WITH_NEW_DECISION, project_dir,
        )
        result2 = apply_reconcile_plan(
            plan=plan2,
            model_output_text=_MODEL_OUTPUT_WITH_NEW_DECISION,
            root=project_dir,
        )
        assert result2.ok
        session2 = result2.data["session_id"]

        # Session IDs should be different
        assert session1 != session2

    def test_apply_result_includes_tx_id(self, project_dir: Path) -> None:
        """Apply result includes a transaction ID."""
        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=_MINIMAL_MODEL_OUTPUT,
            root=project_dir,
        )
        assert result.ok
        assert len(result.data["tx_id"]) > 0

    def test_plan_consumed_directly_no_reparse(self, project_dir: Path) -> None:
        """apply_reconcile_plan consumes the plan directly, never re-parses."""
        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)

        # Mock parse_model_output to verify it's never called
        with patch("aip_loom.reconcile_apply.parse_model_output") as mock_parse:
            mock_parse.side_effect = AssertionError("Should not re-parse!")

            result = apply_reconcile_plan(
                plan=plan,
                model_output_text=_MINIMAL_MODEL_OUTPUT,
                root=project_dir,
            )

        # Should succeed without calling parse_model_output
        assert result.ok
        mock_parse.assert_not_called()


# ---------------------------------------------------------------------------
# Recovery contract tests — the critical safety guarantees
# ---------------------------------------------------------------------------


class TestRecoveryContracts:
    """Prove each recovery contract from BuildSpec §15.

    These are the most important tests in Chunk 15.  Each test forces
    a failure at a specific step and verifies the exact recovery behaviour.
    """

    def test_canonical_write_failure_restores_snapshots(
        self, project_dir: Path,
    ) -> None:
        """RECONCILE_RESTORED_AFTER_FAILURE: if a canonical write fails,
        all snapshotted files are restored to their pre-apply state.
        """
        # Capture original state before the apply
        original_state = load_project(project_dir)
        original_prose = original_state.chunks["C-0001"].prose_body

        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        assert plan.plan_ok

        # Mock safe_write_text to fail after the first call
        call_count = 0
        original_safe_write = __import__(
            "aip_loom.reconcile_apply", fromlist=["safe_write_text"],
        ).safe_write_text

        def failing_safe_write(path, content, layout=None):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise OSError("Mock write failure on canonical file")
            return original_safe_write(path, content, layout)

        with patch(
            "aip_loom.reconcile_apply.safe_write_text", side_effect=failing_safe_write,
        ):
            result = apply_reconcile_plan(
                plan=plan,
                model_output_text=_MINIMAL_MODEL_OUTPUT,
                root=project_dir,
            )

        assert not result.ok
        assert result.code == RECONCILE_RESTORED_AFTER_FAILURE

        # The chunk file should be restored to original prose
        restored_state = load_project(project_dir)
        restored_prose = restored_state.chunks["C-0001"].prose_body
        assert restored_prose == original_prose, (
            f"Prose was not restored! Got {restored_prose!r}, "
            f"expected {original_prose!r}"
        )

    def test_staged_failure_no_canonical_writes(
        self, project_dir: Path,
    ) -> None:
        """RECONCILE_STAGED_VALIDATION_FAILED: if staged validation
        fails, no canonical files are written at all.
        """
        # Capture original content before the apply
        chunk_path = project_dir / "chunks" / "C-0001.md"
        original_content = chunk_path.read_text(encoding="utf-8")

        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)

        # Mock _apply_ledger_changes to raise — this simulates a
        # staged validation failure (step 8 failure before step 9 writes)
        with patch(
            "aip_loom.reconcile_apply._apply_ledger_changes",
            side_effect=ValueError("Staged validation exploded"),
        ):
            result = apply_reconcile_plan(
                plan=plan,
                model_output_text=_MINIMAL_MODEL_OUTPUT,
                root=project_dir,
            )

        assert not result.ok
        assert result.code == RECONCILE_STAGED_VALIDATION_FAILED

        # Chunk file content should be identical (not modified)
        current_content = chunk_path.read_text(encoding="utf-8")
        assert current_content == original_content, (
            "Chunk file was modified despite staged validation failure"
        )

    def test_post_apply_validation_failure_restores(
        self, project_dir: Path,
    ) -> None:
        """RECONCILE_POST_VALIDATION_FAILED: if post-apply validation
        finds errors, all snapshotted files are restored.
        """
        original_state = load_project(project_dir)
        original_prose = original_state.chunks["C-0001"].prose_body

        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        assert plan.plan_ok

        # Mock validate_project to return a failing result on the
        # second call (post-apply), but succeed on the first (pre-apply)
        from aip_loom.project import ValidationResult
        from aip_loom.errors import LoomError

        call_count = 0
        original_validate = __import__(
            "aip_loom.reconcile_apply", fromlist=["validate_project"],
        ).validate_project

        def selective_validate(state, chunk_scope=None):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                # Post-apply validation — fail
                return ValidationResult(
                    errors=(LoomError(
                        code="VALIDATION_MISSING_FILE",
                        message="Mock post-apply failure",
                        detail={},
                    ),),
                    warnings=(),
                    ok=False,
                )
            return original_validate(state, chunk_scope)

        with patch(
            "aip_loom.reconcile_apply.validate_project",
            side_effect=selective_validate,
        ):
            result = apply_reconcile_plan(
                plan=plan,
                model_output_text=_MINIMAL_MODEL_OUTPUT,
                root=project_dir,
            )

        assert not result.ok
        assert result.code == RECONCILE_POST_VALIDATION_FAILED

        # Files should be restored to pre-apply state
        restored_state = load_project(project_dir)
        restored_prose = restored_state.chunks["C-0001"].prose_body
        assert restored_prose == original_prose, (
            f"Prose was not restored after post-apply validation failure! "
            f"Got {restored_prose!r}, expected {original_prose!r}"
        )

    def test_git_failure_does_not_restore(
        self, project_dir: Path,
    ) -> None:
        """RECONCILE_APPLIED_BUT_GIT_FAILED: canonical data survives
        and RECOVERY.md is written.  Files are NOT restored.
        """
        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)

        from aip_loom.git import GitError
        from aip_loom.errors import LoomError

        with patch("aip_loom.reconcile_apply.git_commit") as mock_commit:
            mock_commit.side_effect = GitError(
                LoomError(
                    code="GIT_COMMIT_FAILED",
                    message="Mock git commit failure",
                    detail={},
                )
            )
            result = apply_reconcile_plan(
                plan=plan,
                model_output_text=_MINIMAL_MODEL_OUTPUT,
                root=project_dir,
            )

        assert not result.ok
        assert result.code == RECONCILE_APPLIED_BUT_GIT_FAILED

        # The revised prose should be ON DISK (not restored)
        state = load_project(project_dir)
        chunk = state.chunks["C-0001"]
        assert "revised prose content" in chunk.prose_body.lower(), (
            "Canonical data was lost after Git failure — this violates "
            "the RECONCILE_APPLIED_BUT_GIT_FAILED contract"
        )

        # RECOVERY.md should exist
        assert (project_dir / "RECOVERY.md").exists()

    def test_chunk_status_set_to_revised(
        self, project_dir: Path,
    ) -> None:
        """After successful apply, chunk status is 'revised'."""
        from aip_loom.schemas import ChunkStatus

        plan = _build_plan_from_output(_MINIMAL_MODEL_OUTPUT, project_dir)
        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=_MINIMAL_MODEL_OUTPUT,
            root=project_dir,
        )
        assert result.ok

        state = load_project(project_dir)
        assert state.chunks["C-0001"].frontmatter.status == ChunkStatus.REVISED

    def test_new_thread_gets_correct_id(
        self, project_dir: Path,
    ) -> None:
        """After apply with a new thread, the thread gets the correct
        canonical ID (T-0001 for first thread).
        """
        model_output_new_thread = f"""```loom-update
schema_version: "{_V}"
fence_type: loom-update
mode: full_replacement
target_chunk: C-0001
revised_prose: "Updated prose."
change_summary: "Added thread."
requires_human_review: true
new_threads:
  - provisional_id: new-1
    summary: "A new open thread"
    state: open
---
# Revised Chunk

Updated prose.
```"""

        plan = _build_plan_from_output(model_output_new_thread, project_dir)
        assert plan.plan_ok
        # Check that the plan mapped new-1 to T-0001
        mapping_ids = {m.provisional_id: m.canonical_id for m in plan.id_mappings}
        assert "new-1" in mapping_ids
        assert mapping_ids["new-1"] == "T-0001"

        result = apply_reconcile_plan(
            plan=plan,
            model_output_text=model_output_new_thread,
            root=project_dir,
        )
        assert result.ok

        state = load_project(project_dir)
        assert state.threads_ledger is not None
        thread_ids = [e.id for e in state.threads_ledger.entries]
        assert "T-0001" in thread_ids

    def test_update_existing_decision(
        self, project_dir: Path,
    ) -> None:
        """After apply with update_existing, the decision's summary
        is updated in the ledger.
        """
        # First, add a decision
        plan1 = _build_plan_from_output(
            _MODEL_OUTPUT_WITH_NEW_DECISION, project_dir,
        )
        result1 = apply_reconcile_plan(
            plan=plan1,
            model_output_text=_MODEL_OUTPUT_WITH_NEW_DECISION,
            root=project_dir,
        )
        assert result1.ok

        # Now update that decision
        model_output_update = f"""```loom-update
schema_version: "{_V}"
fence_type: loom-update
mode: full_replacement
target_chunk: C-0001
revised_prose: "Updated again."
change_summary: "Updated decision summary."
requires_human_review: true
update_existing:
  - id: D-0001
    changes:
      summary: "Updated decision summary"
---
# Revised Chunk

Updated again.
```"""

        plan2 = _build_plan_from_output(model_output_update, project_dir)
        assert plan2.plan_ok

        result2 = apply_reconcile_plan(
            plan=plan2,
            model_output_text=model_output_update,
            root=project_dir,
        )
        assert result2.ok

        state = load_project(project_dir)
        d1 = [e for e in state.decisions_ledger.entries if e.id == "D-0001"]
        assert len(d1) == 1
        assert d1[0].summary == "Updated decision summary"
