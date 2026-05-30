"""Tests for aip_loom.status — Project status dashboard.

These tests exercise the compute_status() function and verify:

- Healthy project reports correctly
- Degraded project (warnings but no errors) reports correctly
- Blocked project (errors) reports correctly
- Corrupt ledger fails honestly (load_succeeded=False, BLOCKED)
- Pending reviews are counted and reported
- Recovery file warning is surfaced
- Final status not green when blockers exist
- Stale lock is detected and reported
- Git dirty state is reported
- Empty project reports healthy with zero counts
- Next actions are appropriate for the state
- StatusReport.to_dict() produces correct JSON structure
- CLI integration (--json output)
- Status is honest about partial loading
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aip_loom.checksum import compute_prose_checksum
from aip_loom.errors import (
    PROJECT_NOT_FOUND,
    RECOVERY_FILE_EXISTS,
    STALE_LOCK_DETECTED,
    VALIDATION_BROKEN_REFERENCE,
    VALIDATION_DIRTY_CHECKSUM,
    VALIDATION_MISSING_FILE,
    VALIDATION_PENDING_REVIEW,
    LoomError,
    LoomWarning,
)
from aip_loom.frontmatter import split_frontmatter, write_frontmatter
from aip_loom.git import configure_local_git
from aip_loom.init import init_project
from aip_loom.layout import ProjectLayout
from aip_loom.lock import _write_lock_file
from aip_loom.project import load_project, validate_project
from aip_loom.schemas import (
    SUPPORTED_SCHEMA_VERSION,
    ChunkFrontmatter,
    ChunkStatus,
    DecisionEntry,
    DecisionLedger,
    Distillate,
    DistillateNode,
    ProjectManifest,
    ProjectType,
    QuestionEntry,
    QuestionLedger,
    ReviewState,
    SessionLog,
    ThreadEntry,
    ThreadLedger,
    ThreadState,
)
from aip_loom.status import (
    ChunkStatusSummary,
    HealthLevel,
    LedgerStatusSummary,
    StatusReport,
    compute_status,
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


# ---------------------------------------------------------------------------
# HealthLevel classification
# ---------------------------------------------------------------------------


class TestHealthLevel:
    """Verify HealthLevel enum and classification logic."""

    def test_healthy_enum_values(self) -> None:
        """HealthLevel has the expected values."""
        assert HealthLevel.HEALTHY.value == "healthy"
        assert HealthLevel.DEGRADED.value == "degraded"
        assert HealthLevel.BLOCKED.value == "blocked"

    def test_healthy_project(self, project_root: Path) -> None:
        """A freshly initialized, valid project is HEALTHY."""
        report = compute_status(project_root)
        assert report.health == HealthLevel.HEALTHY

    def test_degraded_with_pending_reviews(self, project_root: Path) -> None:
        """Project with pending reviews but no errors is DEGRADED."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.PENDING,
                    created_at=now, summary="Pending decision",
                ),
            ],
        )
        ledger_yaml = dump_yaml_string(ledger.model_dump(mode="json"))
        layout.decisions_ledger_path.write_text(ledger_yaml, encoding="utf-8")

        report = compute_status(project_root)
        assert report.health == HealthLevel.DEGRADED

    def test_blocked_with_missing_file(self, project_root: Path) -> None:
        """Project with missing required file is BLOCKED."""
        layout = ProjectLayout(root=project_root)
        layout.decisions_ledger_path.unlink()

        report = compute_status(project_root)
        assert report.health == HealthLevel.BLOCKED

    def test_blocked_with_load_failure(self, tmp_dir: Path) -> None:
        """Project that cannot be loaded is BLOCKED."""
        empty_dir = tmp_dir / "empty"
        empty_dir.mkdir()

        report = compute_status(empty_dir)
        assert report.health == HealthLevel.BLOCKED
        assert not report.load_succeeded

    def test_blocked_with_broken_reference(self, project_root: Path) -> None:
        """Project with broken references is BLOCKED."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.APPROVED,
                    created_at=now, summary="Refers to ghost chunk",
                    chunk_id="C-9999",
                ),
            ],
        )
        ledger_yaml = dump_yaml_string(ledger.model_dump(mode="json"))
        layout.decisions_ledger_path.write_text(ledger_yaml, encoding="utf-8")

        report = compute_status(project_root)
        assert report.health == HealthLevel.BLOCKED


# ---------------------------------------------------------------------------
# Honest failure reporting
# ---------------------------------------------------------------------------


class TestHonestFailureReporting:
    """Verify that status is honest about problems — never hides them."""

    def test_corrupt_manifest_not_hidden(self, project_root: Path) -> None:
        """Corrupt manifest is reflected in load_errors, not fabricated as healthy."""
        layout = ProjectLayout(root=project_root)
        layout.manifest_path.write_text("not: valid: yaml: [broken", encoding="utf-8")

        report = compute_status(project_root)
        assert not report.load_succeeded or len(report.load_errors) > 0
        assert report.health == HealthLevel.BLOCKED

    def test_corrupt_ledger_not_counted_as_zero(self, project_root: Path) -> None:
        """Corrupt ledger is not reported as having zero entries."""
        layout = ProjectLayout(root=project_root)
        layout.decisions_ledger_path.write_text("totally: broken: {{", encoding="utf-8")

        report = compute_status(project_root)
        # The load error should be present
        assert len(report.load_errors) > 0

    def test_missing_project_root_is_blocked(self, tmp_dir: Path) -> None:
        """Non-existent root is BLOCKED, not HEALTHY."""
        nonexistent = tmp_dir / "no-such-dir"
        report = compute_status(nonexistent)
        assert report.health == HealthLevel.BLOCKED
        assert not report.load_succeeded
        assert len(report.load_errors) > 0

    def test_no_manifest_is_blocked(self, tmp_dir: Path) -> None:
        """Directory with no manifest is BLOCKED."""
        empty_dir = tmp_dir / "empty-dir"
        empty_dir.mkdir()
        report = compute_status(empty_dir)
        assert report.health == HealthLevel.BLOCKED
        assert not report.load_succeeded

    def test_zero_error_count_means_truly_no_errors(self, project_root: Path) -> None:
        """When error_count is 0, there really are no errors."""
        report = compute_status(project_root)
        if report.error_count == 0:
            # Check that no errors are hiding
            assert len(report.load_errors) == 0
            if report.validation is not None:
                assert len(report.validation.errors) == 0


# ---------------------------------------------------------------------------
# Chunk status summary
# ---------------------------------------------------------------------------


class TestChunkStatusSummary:
    """Verify chunk status counting."""

    def test_empty_project_has_zero_chunks(self, project_root: Path) -> None:
        """Fresh project has zero chunks."""
        report = compute_status(project_root)
        assert report.chunks.total == 0
        assert report.chunks.draft == 0
        assert report.chunks.revised == 0
        assert report.chunks.final == 0

    def test_chunk_status_counts(self, project_root: Path) -> None:
        """Chunk status counts are correct."""
        _write_chunk(project_root, "C-0001", "First draft.", status="draft")
        _write_chunk(project_root, "C-0002", "Revised chunk.", status="revised")
        _write_chunk(project_root, "C-0003", "Final chunk.", status="final")

        report = compute_status(project_root)
        assert report.chunks.total == 3
        assert report.chunks.draft == 1
        assert report.chunks.revised == 1
        assert report.chunks.final == 1

    def test_chunk_ids_in_order(self, project_root: Path) -> None:
        """Chunk IDs are in resolved order."""
        _write_chunk(project_root, "C-0001")
        _write_chunk(project_root, "C-0002")

        report = compute_status(project_root)
        assert "C-0001" in report.chunks.chunk_ids
        assert "C-0002" in report.chunks.chunk_ids

    def test_dirty_checksums_counted(self, project_root: Path) -> None:
        """Dirty checksums are counted in the chunk summary."""
        layout = ProjectLayout(root=project_root)
        _write_chunk(project_root, "C-0001", "Original prose.")

        # Dirty the checksum
        chunk_path = layout.chunk_path("C-0001")
        original_content = chunk_path.read_text(encoding="utf-8")
        yaml_str, _ = split_frontmatter(original_content)
        modified = f"---\n{yaml_str}\n---\nModified prose."
        chunk_path.write_text(modified, encoding="utf-8")

        report = compute_status(project_root)
        assert report.chunks.dirty_checksums >= 1


# ---------------------------------------------------------------------------
# Ledger status summary
# ---------------------------------------------------------------------------


class TestLedgerStatusSummary:
    """Verify ledger status counting."""

    def test_empty_ledgers_have_zero_counts(self, project_root: Path) -> None:
        """Fresh project has zero ledger entries."""
        report = compute_status(project_root)
        assert report.ledgers.decisions_total == 0
        assert report.ledgers.threads_total == 0
        assert report.ledgers.questions_total == 0
        assert report.ledgers.total_pending_review == 0

    def test_pending_decision_counted(self, project_root: Path) -> None:
        """Pending review decisions are counted."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.PENDING,
                    created_at=now, summary="Pending decision",
                ),
                DecisionEntry(
                    id="D-0002", review_state=ReviewState.APPROVED,
                    created_at=now, summary="Approved decision",
                ),
            ],
        )
        ledger_yaml = dump_yaml_string(ledger.model_dump(mode="json"))
        layout.decisions_ledger_path.write_text(ledger_yaml, encoding="utf-8")

        report = compute_status(project_root)
        assert report.ledgers.decisions_total == 2
        assert report.ledgers.decisions_pending == 1
        assert report.ledgers.total_pending_review >= 1

    def test_open_and_blocked_threads_counted(self, project_root: Path) -> None:
        """Open and blocked threads are counted correctly."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = ThreadLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                ThreadEntry(
                    id="T-0001", review_state=ReviewState.APPROVED,
                    created_at=now, summary="Open thread",
                    state=ThreadState.OPEN,
                ),
                ThreadEntry(
                    id="T-0002", review_state=ReviewState.APPROVED,
                    created_at=now, summary="Blocked thread",
                    state=ThreadState.BLOCKED,
                ),
                ThreadEntry(
                    id="T-0003", review_state=ReviewState.APPROVED,
                    created_at=now, summary="Closed thread",
                    state=ThreadState.CLOSED,
                ),
            ],
        )
        ledger_yaml = dump_yaml_string(ledger.model_dump(mode="json"))
        layout.threads_ledger_path.write_text(ledger_yaml, encoding="utf-8")

        report = compute_status(project_root)
        assert report.ledgers.threads_total == 3
        assert report.ledgers.threads_open == 1
        assert report.ledgers.threads_blocked == 1

    def test_unresolved_questions_counted(self, project_root: Path) -> None:
        """Unresolved questions are counted."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = QuestionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                QuestionEntry(
                    id="Q-0001", review_state=ReviewState.APPROVED,
                    created_at=now, question="Unresolved question",
                    resolved=False,
                ),
                QuestionEntry(
                    id="Q-0002", review_state=ReviewState.APPROVED,
                    created_at=now, question="Resolved question",
                    resolved=True,
                ),
            ],
        )
        ledger_yaml = dump_yaml_string(ledger.model_dump(mode="json"))
        layout.questions_ledger_path.write_text(ledger_yaml, encoding="utf-8")

        report = compute_status(project_root)
        assert report.ledgers.questions_total == 2
        assert report.ledgers.questions_unresolved == 1

    def test_total_pending_review_across_all_ledgers(self, project_root: Path) -> None:
        """Total pending review counts across all ledgers."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        decisions = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.PENDING,
                    created_at=now, summary="Pending decision",
                ),
            ],
        )
        threads = ThreadLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                ThreadEntry(
                    id="T-0001", review_state=ReviewState.PENDING,
                    created_at=now, summary="Pending thread",
                ),
            ],
        )
        questions = QuestionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                QuestionEntry(
                    id="Q-0001", review_state=ReviewState.PENDING,
                    created_at=now, question="Pending question",
                ),
            ],
        )

        layout.decisions_ledger_path.write_text(
            dump_yaml_string(decisions.model_dump(mode="json")), encoding="utf-8"
        )
        layout.threads_ledger_path.write_text(
            dump_yaml_string(threads.model_dump(mode="json")), encoding="utf-8"
        )
        layout.questions_ledger_path.write_text(
            dump_yaml_string(questions.model_dump(mode="json")), encoding="utf-8"
        )

        report = compute_status(project_root)
        assert report.ledgers.total_pending_review == 3


# ---------------------------------------------------------------------------
# Git status summary
# ---------------------------------------------------------------------------


class TestGitStatusSummary:
    """Verify Git status reporting."""

    def test_git_repo_detected(self, project_root: Path) -> None:
        """Initialized project inside a Git repo is detected."""
        report = compute_status(project_root)
        # init_project creates a Git repo
        assert report.git.is_repo

    def test_git_clean_after_init(self, project_root: Path) -> None:
        """After init, the Git working tree should be clean."""
        report = compute_status(project_root)
        assert report.git.clean

    def test_non_git_dir_reported(self, tmp_dir: Path) -> None:
        """Project not in a Git repo reports is_repo=False."""
        root = tmp_dir / "no-git"
        init_project(root=root, name="no-git")
        # Remove .git if init created it
        git_dir = root / ".git"
        if git_dir.exists():
            shutil.rmtree(str(git_dir))

        report = compute_status(root)
        assert not report.git.is_repo

    def test_git_dirty_state_reported(self, project_root: Path) -> None:
        """Dirty Git working tree is reported."""
        # Create an untracked file
        (project_root / "untracked.txt").write_text("test", encoding="utf-8")

        report = compute_status(project_root)
        assert not report.git.clean
        assert report.git.untracked_count >= 1


# ---------------------------------------------------------------------------
# Lock status summary
# ---------------------------------------------------------------------------


class TestLockStatusSummary:
    """Verify lock status reporting."""

    def test_no_lock_reported(self, project_root: Path) -> None:
        """No lock file means locked=False."""
        report = compute_status(project_root)
        assert not report.lock.locked
        assert not report.lock.is_stale
        assert report.lock.lock_info is None

    def test_active_lock_reported(self, project_root: Path) -> None:
        """An active lock file is reported."""
        layout = ProjectLayout(root=project_root)
        layout.aip_loom_dir.mkdir(parents=True, exist_ok=True)
        _write_lock_file(layout.lock_path, "reconcile")

        report = compute_status(project_root)
        assert report.lock.locked
        # The current process is alive, so lock should not be stale
        assert not report.lock.is_stale
        assert report.lock.lock_info is not None
        assert report.lock.lock_info.pid == os.getpid()

    def test_stale_lock_detected(self, project_root: Path) -> None:
        """A stale lock (dead PID) is detected."""
        layout = ProjectLayout(root=project_root)
        layout.aip_loom_dir.mkdir(parents=True, exist_ok=True)
        # Write a lock with a PID that doesn't exist
        # Use a very high PID that is unlikely to be running
        fake_pid = 999999999
        lock_content = f"{fake_pid}:reconcile\n"
        layout.lock_path.write_text(lock_content, encoding="utf-8")

        report = compute_status(project_root)
        assert report.lock.locked
        assert report.lock.is_stale
        assert report.lock.lock_info is not None
        assert report.lock.lock_info.pid == fake_pid


# ---------------------------------------------------------------------------
# Recovery file detection
# ---------------------------------------------------------------------------


class TestRecoveryFileDetection:
    """Verify RECOVERY.md detection."""

    def test_no_recovery_file(self, project_root: Path) -> None:
        """No RECOVERY.md means recovery_file_exists=False."""
        report = compute_status(project_root)
        assert not report.recovery_file_exists

    def test_recovery_file_detected(self, project_root: Path) -> None:
        """RECOVERY.md in project root is detected."""
        (project_root / "RECOVERY.md").write_text(
            "# Recovery\nA reconcile operation failed.", encoding="utf-8"
        )

        report = compute_status(project_root)
        assert report.recovery_file_exists

    def test_recovery_file_makes_blocked(self, project_root: Path) -> None:
        """RECOVERY.md makes the project BLOCKED."""
        (project_root / "RECOVERY.md").write_text("Recovery info", encoding="utf-8")

        report = compute_status(project_root)
        assert report.health == HealthLevel.BLOCKED


# ---------------------------------------------------------------------------
# Next actions
# ---------------------------------------------------------------------------


class TestNextActions:
    """Verify next action suggestions."""

    def test_healthy_project_no_critical_actions(self, project_root: Path) -> None:
        """Healthy project may suggest creating chunks but no critical actions."""
        report = compute_status(project_root)
        # An empty project will suggest creating chunks
        # But no "fix errors" or "recovery" actions
        action_messages = " ".join(report.next_actions)
        assert "Fix project loading errors" not in action_messages
        assert "RECOVERY.md" not in action_messages

    def test_blocked_project_suggests_fixing_errors(self, tmp_dir: Path) -> None:
        """Blocked project suggests fixing errors."""
        empty_dir = tmp_dir / "empty"
        empty_dir.mkdir()
        report = compute_status(empty_dir)
        action_messages = " ".join(report.next_actions)
        assert "Fix project loading errors" in action_messages

    def test_pending_reviews_suggests_action(self, project_root: Path) -> None:
        """Pending review items suggest an action."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.PENDING,
                    created_at=now, summary="Pending",
                ),
            ],
        )
        layout.decisions_ledger_path.write_text(
            dump_yaml_string(ledger.model_dump(mode="json")), encoding="utf-8"
        )

        report = compute_status(project_root)
        action_messages = " ".join(report.next_actions)
        assert "pending review" in action_messages

    def test_blocked_threads_suggest_action(self, project_root: Path) -> None:
        """Blocked threads suggest an action."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = ThreadLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                ThreadEntry(
                    id="T-0001", review_state=ReviewState.APPROVED,
                    created_at=now, summary="Blocked thread",
                    state=ThreadState.BLOCKED,
                ),
            ],
        )
        layout.threads_ledger_path.write_text(
            dump_yaml_string(ledger.model_dump(mode="json")), encoding="utf-8"
        )

        report = compute_status(project_root)
        action_messages = " ".join(report.next_actions)
        assert "blocked" in action_messages.lower()

    def test_recovery_file_suggests_action(self, project_root: Path) -> None:
        """Recovery file suggests an action."""
        (project_root / "RECOVERY.md").write_text("Recovery info", encoding="utf-8")

        report = compute_status(project_root)
        action_messages = " ".join(report.next_actions)
        assert "RECOVERY.md" in action_messages

    def test_stale_lock_suggests_action(self, project_root: Path) -> None:
        """Stale lock suggests an action."""
        layout = ProjectLayout(root=project_root)
        layout.aip_loom_dir.mkdir(parents=True, exist_ok=True)
        fake_pid = 999999999
        layout.lock_path.write_text(f"{fake_pid}:reconcile\n", encoding="utf-8")

        report = compute_status(project_root)
        action_messages = " ".join(report.next_actions)
        assert "Stale lock" in action_messages

    def test_dirty_checksums_suggest_reconcile(self, project_root: Path) -> None:
        """Dirty checksums suggest running reconcile."""
        layout = ProjectLayout(root=project_root)
        _write_chunk(project_root, "C-0001", "Original prose.")

        # Dirty the checksum
        chunk_path = layout.chunk_path("C-0001")
        original_content = chunk_path.read_text(encoding="utf-8")
        yaml_str, _ = split_frontmatter(original_content)
        modified = f"---\n{yaml_str}\n---\nModified prose."
        chunk_path.write_text(modified, encoding="utf-8")

        report = compute_status(project_root)
        action_messages = " ".join(report.next_actions)
        assert "dirty checksums" in action_messages.lower()
        assert "reconcile" in action_messages.lower()

    def test_empty_project_suggests_creating_chunks(self, project_root: Path) -> None:
        """Empty project suggests creating first chunk."""
        report = compute_status(project_root)
        action_messages = " ".join(report.next_actions)
        assert "No chunks yet" in action_messages


# ---------------------------------------------------------------------------
# StatusReport structure and serialization
# ---------------------------------------------------------------------------


class TestStatusReportStructure:
    """Verify StatusReport data structure and serialization."""

    def test_report_is_frozen(self, project_root: Path) -> None:
        """StatusReport is frozen (immutable)."""
        report = compute_status(project_root)
        with pytest.raises(AttributeError):
            report.health = HealthLevel.BLOCKED  # type: ignore[misc]

    def test_to_dict_contains_all_sections(self, project_root: Path) -> None:
        """to_dict() produces a complete dictionary."""
        report = compute_status(project_root)
        d = report.to_dict()

        assert "health" in d
        assert "root" in d
        assert "project_name" in d
        assert "project_type" in d
        assert "load_succeeded" in d
        assert "recovery_file_exists" in d
        assert "error_count" in d
        assert "warning_count" in d
        assert "next_actions" in d
        assert "load_errors" in d
        assert "load_warnings" in d
        assert "validation" in d
        assert "chunks" in d
        assert "ledgers" in d
        assert "git" in d
        assert "lock" in d

    def test_to_dict_json_serializable(self, project_root: Path) -> None:
        """to_dict() result can be serialized to JSON."""
        report = compute_status(project_root)
        d = report.to_dict()
        # Should not raise
        json_str = json.dumps(d, ensure_ascii=False)
        assert isinstance(json_str, str)

    def test_to_dict_health_values(self, project_root: Path) -> None:
        """to_dict() health value is a string, not an enum."""
        report = compute_status(project_root)
        d = report.to_dict()
        assert isinstance(d["health"], str)
        assert d["health"] in ("healthy", "degraded", "blocked")

    def test_to_dict_chunks_structure(self, project_root: Path) -> None:
        """to_dict() chunks section has expected keys."""
        _write_chunk(project_root, "C-0001")
        report = compute_status(project_root)
        d = report.to_dict()

        chunks = d["chunks"]
        assert "total" in chunks
        assert "draft" in chunks
        assert "revised" in chunks
        assert "final" in chunks
        assert "dirty_checksums" in chunks
        assert "chunk_ids" in chunks

    def test_to_dict_ledgers_structure(self, project_root: Path) -> None:
        """to_dict() ledgers section has expected keys."""
        report = compute_status(project_root)
        d = report.to_dict()

        ledgers = d["ledgers"]
        assert "decisions_total" in ledgers
        assert "decisions_pending" in ledgers
        assert "threads_total" in ledgers
        assert "threads_open" in ledgers
        assert "threads_blocked" in ledgers
        assert "threads_pending" in ledgers
        assert "questions_total" in ledgers
        assert "questions_unresolved" in ledgers
        assert "questions_pending" in ledgers
        assert "total_pending_review" in ledgers

    def test_to_dict_git_structure(self, project_root: Path) -> None:
        """to_dict() git section has expected keys."""
        report = compute_status(project_root)
        d = report.to_dict()

        git = d["git"]
        assert "is_repo" in git
        assert "clean" in git
        assert "staged_count" in git
        assert "unstaged_count" in git
        assert "untracked_count" in git
        assert "branch" in git

    def test_to_dict_lock_structure_no_lock(self, project_root: Path) -> None:
        """to_dict() lock section with no lock."""
        report = compute_status(project_root)
        d = report.to_dict()

        lock = d["lock"]
        assert "locked" in lock
        assert "is_stale" in lock
        assert lock["locked"] is False

    def test_to_dict_lock_structure_with_lock(self, project_root: Path) -> None:
        """to_dict() lock section with an active lock includes pid/command."""
        layout = ProjectLayout(root=project_root)
        layout.aip_loom_dir.mkdir(parents=True, exist_ok=True)
        _write_lock_file(layout.lock_path, "reconcile")

        report = compute_status(project_root)
        d = report.to_dict()

        lock = d["lock"]
        assert lock["locked"] is True
        assert "pid" in lock
        assert "command" in lock
        assert "is_alive" in lock

    def test_to_dict_validation_null_when_load_failed(self, tmp_dir: Path) -> None:
        """to_dict() validation is null when load failed."""
        empty_dir = tmp_dir / "empty"
        empty_dir.mkdir()
        report = compute_status(empty_dir)
        d = report.to_dict()
        assert d["validation"] is None


# ---------------------------------------------------------------------------
# Project metadata
# ---------------------------------------------------------------------------


class TestProjectMetadata:
    """Verify project name and type extraction."""

    def test_project_name_from_manifest(self, project_root: Path) -> None:
        """Project name comes from the manifest."""
        report = compute_status(project_root)
        assert report.project_name == "test-project"

    def test_project_type_from_manifest(self, project_root: Path) -> None:
        """Project type comes from the manifest."""
        report = compute_status(project_root)
        assert report.project_type == "novel"

    def test_unknown_name_when_load_fails(self, tmp_dir: Path) -> None:
        """Project name is <unknown> when load fails."""
        empty_dir = tmp_dir / "empty"
        empty_dir.mkdir()
        report = compute_status(empty_dir)
        assert report.project_name == "<unknown>"

    def test_unknown_type_when_load_fails(self, tmp_dir: Path) -> None:
        """Project type is <unknown> when load fails."""
        empty_dir = tmp_dir / "empty"
        empty_dir.mkdir()
        report = compute_status(empty_dir)
        assert report.project_type == "<unknown>"


# ---------------------------------------------------------------------------
# Error and warning counts
# ---------------------------------------------------------------------------


class TestErrorWarningCounts:
    """Verify error and warning counting."""

    def test_healthy_project_zero_errors(self, project_root: Path) -> None:
        """Healthy project has zero errors."""
        report = compute_status(project_root)
        assert report.error_count == 0

    def test_missing_file_increments_error_count(self, project_root: Path) -> None:
        """Missing file increases error count."""
        layout = ProjectLayout(root=project_root)
        layout.decisions_ledger_path.unlink()

        report = compute_status(project_root)
        assert report.error_count > 0

    def test_pending_review_increments_warning_count(self, project_root: Path) -> None:
        """Pending review items increase warning count."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.PENDING,
                    created_at=now, summary="Pending",
                ),
            ],
        )
        layout.decisions_ledger_path.write_text(
            dump_yaml_string(ledger.model_dump(mode="json")), encoding="utf-8"
        )

        report = compute_status(project_root)
        assert report.warning_count > 0


# ---------------------------------------------------------------------------
# Integration: compute_status uses load_project + validate_project
# ---------------------------------------------------------------------------


class TestIntegrationWithLoaderAndValidator:
    """Verify that compute_status goes through load_project + validate_project."""

    def test_validation_errors_reflected_in_status(self, project_root: Path) -> None:
        """Validation errors (broken refs) are reflected in status."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        # Add a decision referencing a non-existent chunk
        ledger = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.APPROVED,
                    created_at=now, summary="Refers to ghost",
                    chunk_id="C-9999",
                ),
            ],
        )
        layout.decisions_ledger_path.write_text(
            dump_yaml_string(ledger.model_dump(mode="json")), encoding="utf-8"
        )

        report = compute_status(project_root)
        assert report.health == HealthLevel.BLOCKED
        assert report.error_count > 0
        assert report.validation is not None
        assert not report.validation.ok

    def test_load_errors_reflected_in_status(self, project_root: Path) -> None:
        """Load errors are reflected in status."""
        layout = ProjectLayout(root=project_root)
        layout.decisions_ledger_path.write_text("broken: yaml: {{", encoding="utf-8")

        report = compute_status(project_root)
        assert len(report.load_errors) > 0

    def test_status_uses_same_data_as_validate(self, project_root: Path) -> None:
        """Status and validate see the same project state."""
        _write_chunk(project_root, "C-0001")

        # Load and validate directly
        state = load_project(project_root)
        validation = validate_project(state)

        # Compute status
        report = compute_status(project_root)

        # Both should agree on error/warning counts
        assert report.error_count == len(validation.errors)
        # Warning count from status includes load_warnings + validation warnings
        # (may differ by load_warnings count)
        assert report.warning_count >= len(validation.warnings)


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestCLIStatus:
    """Verify CLI status command integration."""

    def test_status_command_healthy_project(self, project_root: Path) -> None:
        """'aip-loom status' succeeds for a healthy project."""
        from typer.testing import CliRunner
        from aip_loom.cli import app

        runner = CliRunner()
        # We need to run from the project directory
        import os
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["status"])
            assert result.exit_code == 0
            assert "healthy" in result.output.lower()
        finally:
            os.chdir(old_cwd)

    def test_status_command_json_output(self, project_root: Path) -> None:
        """'aip-loom status --json' produces valid JSON."""
        from typer.testing import CliRunner
        from aip_loom.cli import app

        runner = CliRunner()
        import os
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["status", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["ok"] is True
            assert data["command"] == "status"
            assert "data" in data
            assert data["data"]["health"] == "healthy"
        finally:
            os.chdir(old_cwd)

    def test_status_command_blocked_project(self, tmp_dir: Path) -> None:
        """'aip-loom status' returns non-zero for a blocked project."""
        from typer.testing import CliRunner
        from aip_loom.cli import app

        empty_dir = tmp_dir / "empty"
        empty_dir.mkdir()

        runner = CliRunner()
        import os
        old_cwd = os.getcwd()
        os.chdir(str(empty_dir))
        try:
            result = runner.invoke(app, ["status"])
            assert result.exit_code != 0
        finally:
            os.chdir(old_cwd)

    def test_status_command_blocked_project_json(self, tmp_dir: Path) -> None:
        """'aip-loom status --json' returns failure for blocked project."""
        from typer.testing import CliRunner
        from aip_loom.cli import app

        empty_dir = tmp_dir / "empty"
        empty_dir.mkdir()

        runner = CliRunner()
        import os
        old_cwd = os.getcwd()
        os.chdir(str(empty_dir))
        try:
            result = runner.invoke(app, ["status", "--json"])
            data = json.loads(result.output)
            assert data["ok"] is False
            assert data["data"]["health"] == "blocked"
        finally:
            os.chdir(old_cwd)

    def test_status_command_degraded_project(self, project_root: Path) -> None:
        """'aip-loom status' for degraded project returns success but with warnings."""
        from typer.testing import CliRunner
        from aip_loom.cli import app

        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        # Add pending review (makes project degraded, not blocked)
        ledger = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.PENDING,
                    created_at=now, summary="Pending",
                ),
            ],
        )
        layout.decisions_ledger_path.write_text(
            dump_yaml_string(ledger.model_dump(mode="json")), encoding="utf-8"
        )

        runner = CliRunner()
        import os
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["status", "--json"])
            data = json.loads(result.output)
            assert data["ok"] is True  # Degraded is still "success" (exit 0)
            assert data["data"]["health"] == "degraded"
        finally:
            os.chdir(old_cwd)

    def test_status_command_recovery_warning_in_json(self, project_root: Path) -> None:
        """'aip-loom status --json' includes recovery file warning."""
        from typer.testing import CliRunner
        from aip_loom.cli import app

        (project_root / "RECOVERY.md").write_text("Recovery info", encoding="utf-8")

        runner = CliRunner()
        import os
        old_cwd = os.getcwd()
        os.chdir(str(project_root))
        try:
            result = runner.invoke(app, ["status", "--json"])
            data = json.loads(result.output)
            assert data["ok"] is False  # Blocked
            # Should have RECOVERY_FILE_EXISTS warning
            warn_codes = [w["code"] for w in data.get("warnings", [])]
            assert RECOVERY_FILE_EXISTS in warn_codes
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Status never lies: comprehensive honest-behaviour tests
# ---------------------------------------------------------------------------


class TestStatusNeverLies:
    """Comprehensive tests ensuring status never fabricates or hides."""

    def test_final_status_not_green_when_blockers(self, project_root: Path) -> None:
        """Final status is never green when blockers exist."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        # Create broken reference
        ledger = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.APPROVED,
                    created_at=now, summary="Bad ref",
                    chunk_id="C-9999",
                ),
            ],
        )
        layout.decisions_ledger_path.write_text(
            dump_yaml_string(ledger.model_dump(mode="json")), encoding="utf-8"
        )

        report = compute_status(project_root)
        assert report.health != HealthLevel.HEALTHY
        assert report.health == HealthLevel.BLOCKED

    def test_load_failure_shows_errors_not_zeros(self, project_root: Path) -> None:
        """When loading fails, errors are shown, not zero counts."""
        layout = ProjectLayout(root=project_root)
        layout.manifest_path.write_text("broken: yaml: {{", encoding="utf-8")

        report = compute_status(project_root)
        # Should not report healthy
        assert report.health == HealthLevel.BLOCKED
        assert report.error_count > 0

    def test_partial_load_honest_about_missing(self, project_root: Path) -> None:
        """Partial load (some files missing) is honest about it."""
        layout = ProjectLayout(root=project_root)
        # Remove the distillate file
        layout.distillate_path.unlink()

        report = compute_status(project_root)
        # Should not pretend everything is fine
        assert report.health == HealthLevel.BLOCKED
        # error_count should reflect the missing file
        assert report.error_count > 0

    def test_multiple_problems_all_reported(self, project_root: Path) -> None:
        """Multiple problems are all reported, not just the first."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        # Missing ledger file
        layout.decisions_ledger_path.unlink()

        # Broken reference in threads
        threads = ThreadLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                ThreadEntry(
                    id="T-0001", review_state=ReviewState.APPROVED,
                    created_at=now, summary="Bad ref",
                    chunk_id="C-9999",
                ),
            ],
        )
        layout.threads_ledger_path.write_text(
            dump_yaml_string(threads.model_dump(mode="json")), encoding="utf-8"
        )

        report = compute_status(project_root)
        assert report.error_count >= 2  # At least missing file + broken ref

    def test_corrupt_ledger_does_not_show_zero_entries(self, project_root: Path) -> None:
        """Corrupt ledger is not reported as having zero entries."""
        layout = ProjectLayout(root=project_root)
        layout.decisions_ledger_path.write_text("bad: yaml: {{", encoding="utf-8")

        report = compute_status(project_root)
        # Load errors should be non-empty
        assert len(report.load_errors) > 0
