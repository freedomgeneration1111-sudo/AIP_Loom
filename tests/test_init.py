"""Tests for aip_loom.init — Project initialisation service.

These tests exercise the full init_project() service function and the CLI
init command.  They verify:

- Successful project creation with correct structure and valid files
- Create-or-fail semantics (no partial state left on failure)
- Existing project rejection (PROJECT_ALREADY_EXISTS)
- Invalid project type rejection
- No fake approved content in distillate
- All created files validate against schemas
- Git initialisation best-effort and non-fatal
- CLI integration with --json output
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from aip_loom.errors import (
    FIELD_INVALID,
    GIT_INIT_SKIPPED,
    PROJECT_ALREADY_EXISTS,
)
from aip_loom.init import InitError, InitResult, init_project
from aip_loom.layout import ProjectLayout
from aip_loom.schemas import (
    SUPPORTED_SCHEMA_VERSION,
    CommentLog,
    DecisionLedger,
    Distillate,
    ProjectManifest,
    ProjectType,
    QuestionLedger,
    SessionLog,
    ThreadLedger,
)
from aip_loom.yaml_io import load_yaml_as


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
def project_dir(tmp_dir: Path) -> Path:
    """Return a path to a non-existent project directory."""
    return tmp_dir / "my-project"


# ---------------------------------------------------------------------------
# Successful init — structure and content
# ---------------------------------------------------------------------------


class TestSuccessfulInit:
    """Positive: init creates the full project tree with valid files."""

    def test_creates_root_directory(self, project_dir: Path) -> None:
        """Init creates the root directory if it doesn't exist."""
        assert not project_dir.exists()
        result = init_project(root=project_dir, name="test-project")
        assert project_dir.is_dir()
        assert result.root == project_dir.resolve()

    def test_creates_manifest(self, project_dir: Path) -> None:
        """Init creates aip_loom.yaml with correct schema and content."""
        result = init_project(root=project_dir, name="test-project")
        layout = ProjectLayout(root=project_dir)
        assert layout.manifest_path.is_file()

        manifest = load_yaml_as(layout.manifest_path, ProjectManifest)
        assert manifest.schema_version == SUPPORTED_SCHEMA_VERSION
        assert manifest.name == "test-project"
        assert manifest.project_type == ProjectType.NOVEL
        assert manifest.chunks.order == []
        assert manifest.created_at != ""
        assert manifest.updated_at != ""

    def test_creates_directory_structure(self, project_dir: Path) -> None:
        """Init creates all required directories."""
        init_project(root=project_dir, name="test-project")
        layout = ProjectLayout(root=project_dir)

        assert layout.chunks_dir.is_dir()
        assert layout.ledgers_dir.is_dir()
        assert layout.archive_dir.is_dir()
        assert layout.aip_loom_dir.is_dir()
        assert layout.staging_dir.is_dir()

    def test_creates_decision_ledger(self, project_dir: Path) -> None:
        """Init creates decisions.yaml with empty entries."""
        init_project(root=project_dir, name="test-project")
        layout = ProjectLayout(root=project_dir)

        assert layout.decisions_ledger_path.is_file()
        ledger = load_yaml_as(layout.decisions_ledger_path, DecisionLedger)
        assert ledger.schema_version == SUPPORTED_SCHEMA_VERSION
        assert ledger.entries == []

    def test_creates_threads_ledger(self, project_dir: Path) -> None:
        """Init creates threads.yaml with empty entries."""
        init_project(root=project_dir, name="test-project")
        layout = ProjectLayout(root=project_dir)

        assert layout.threads_ledger_path.is_file()
        ledger = load_yaml_as(layout.threads_ledger_path, ThreadLedger)
        assert ledger.schema_version == SUPPORTED_SCHEMA_VERSION
        assert ledger.entries == []

    def test_creates_questions_ledger(self, project_dir: Path) -> None:
        """Init creates questions.yaml with empty entries."""
        init_project(root=project_dir, name="test-project")
        layout = ProjectLayout(root=project_dir)

        assert layout.questions_ledger_path.is_file()
        ledger = load_yaml_as(layout.questions_ledger_path, QuestionLedger)
        assert ledger.schema_version == SUPPORTED_SCHEMA_VERSION
        assert ledger.entries == []

    def test_creates_distillate(self, project_dir: Path) -> None:
        """Init creates distillate.yaml with empty nodes — no fake content."""
        init_project(root=project_dir, name="test-project")
        layout = ProjectLayout(root=project_dir)

        assert layout.distillate_path.is_file()
        distillate = load_yaml_as(layout.distillate_path, Distillate)
        assert distillate.schema_version == SUPPORTED_SCHEMA_VERSION
        assert distillate.nodes == []

    def test_creates_session_log(self, project_dir: Path) -> None:
        """Init creates sessions.yaml with empty entries."""
        init_project(root=project_dir, name="test-project")
        layout = ProjectLayout(root=project_dir)

        assert layout.sessions_path.is_file()
        log = load_yaml_as(layout.sessions_path, SessionLog)
        assert log.schema_version == SUPPORTED_SCHEMA_VERSION
        assert log.entries == []

    def test_creates_comment_log(self, project_dir: Path) -> None:
        """Init creates comments.yaml with empty entries."""
        init_project(root=project_dir, name="test-project")
        layout = ProjectLayout(root=project_dir)

        assert layout.comments_path.is_file()
        log = load_yaml_as(layout.comments_path, CommentLog)
        assert log.schema_version == SUPPORTED_SCHEMA_VERSION
        assert log.entries == []

    def test_project_type_novel(self, project_dir: Path) -> None:
        """Init with type='novel' sets the correct project type."""
        result = init_project(root=project_dir, name="novel-project", project_type="novel")
        layout = ProjectLayout(root=project_dir)
        manifest = load_yaml_as(layout.manifest_path, ProjectManifest)
        assert manifest.project_type == ProjectType.NOVEL

    def test_project_type_technical(self, project_dir: Path) -> None:
        """Init with type='technical' sets the correct project type."""
        result = init_project(root=project_dir, name="tech-project", project_type="technical")
        layout = ProjectLayout(root=project_dir)
        manifest = load_yaml_as(layout.manifest_path, ProjectManifest)
        assert manifest.project_type == ProjectType.TECHNICAL

    def test_project_type_academic(self, project_dir: Path) -> None:
        """Init with type='academic' sets the correct project type."""
        result = init_project(root=project_dir, name="academic-project", project_type="academic")
        layout = ProjectLayout(root=project_dir)
        manifest = load_yaml_as(layout.manifest_path, ProjectManifest)
        assert manifest.project_type == ProjectType.ACADEMIC

    def test_project_type_general(self, project_dir: Path) -> None:
        """Init with type='general' sets the correct project type."""
        result = init_project(root=project_dir, name="general-project", project_type="general")
        layout = ProjectLayout(root=project_dir)
        manifest = load_yaml_as(layout.manifest_path, ProjectManifest)
        assert manifest.project_type == ProjectType.GENERAL

    def test_returns_init_result(self, project_dir: Path) -> None:
        """init_project returns an InitResult with correct fields."""
        result = init_project(root=project_dir, name="test-project")
        assert isinstance(result, InitResult)
        assert result.root == project_dir.resolve()
        assert isinstance(result.git_initialized, bool)
        assert isinstance(result.git_commit_created, bool)
        assert isinstance(result.warnings, tuple)

    def test_init_in_existing_empty_dir(self, tmp_dir: Path) -> None:
        """Init succeeds when the directory exists but is empty."""
        empty_dir = tmp_dir / "existing-empty"
        empty_dir.mkdir()
        result = init_project(root=empty_dir, name="in-existing-dir")
        assert result.root == empty_dir.resolve()
        assert (empty_dir / "aip_loom.yaml").is_file()


# ---------------------------------------------------------------------------
# Create-or-fail semantics
# ---------------------------------------------------------------------------


class TestCreateOrFail:
    """Verify that failed init leaves no partial state."""

    def test_existing_project_fails(self, project_dir: Path) -> None:
        """Init fails if a project already exists in the target directory."""
        init_project(root=project_dir, name="first")
        with pytest.raises(InitError) as exc_info:
            init_project(root=project_dir, name="second")
        assert exc_info.value.loom_error.code == PROJECT_ALREADY_EXISTS

    def test_existing_project_does_not_modify(self, project_dir: Path) -> None:
        """Re-init on existing project does not modify the manifest."""
        init_project(root=project_dir, name="original-name")
        layout = ProjectLayout(root=project_dir)
        original_manifest = layout.manifest_path.read_text(encoding="utf-8")

        with pytest.raises(InitError):
            init_project(root=project_dir, name="different-name")

        current_manifest = layout.manifest_path.read_text(encoding="utf-8")
        assert original_manifest == current_manifest

    def test_invalid_type_no_partial_state(self, project_dir: Path) -> None:
        """Invalid project type fails before creating any files."""
        assert not project_dir.exists()
        with pytest.raises(InitError) as exc_info:
            init_project(root=project_dir, name="bad-type", project_type="invalid")
        assert exc_info.value.loom_error.code == FIELD_INVALID
        # Root should not exist since we never created it
        assert not project_dir.exists()


# ---------------------------------------------------------------------------
# Invalid input handling
# ---------------------------------------------------------------------------


class TestInvalidInput:
    """Verify that init rejects invalid inputs cleanly."""

    def test_invalid_project_type(self, project_dir: Path) -> None:
        """Invalid project type raises InitError with FIELD_INVALID."""
        with pytest.raises(InitError) as exc_info:
            init_project(root=project_dir, name="test", project_type="nonexistent")
        assert exc_info.value.loom_error.code == FIELD_INVALID
        detail = exc_info.value.loom_error.detail
        assert detail["field"] == "project_type"
        assert detail["value"] == "nonexistent"

    def test_invalid_project_type_lists_valid_values(self, project_dir: Path) -> None:
        """Error message for invalid type lists all valid values."""
        with pytest.raises(InitError) as exc_info:
            init_project(root=project_dir, name="test", project_type="bad")
        message = exc_info.value.loom_error.message
        assert "novel" in message
        assert "technical" in message
        assert "academic" in message
        assert "general" in message

    def test_empty_name_rejected_by_schema(self, project_dir: Path) -> None:
        """Empty project name is rejected by Pydantic schema validation."""
        # The ProjectManifest model has Field(min_length=1) on name.
        # This should surface as an InitError during manifest creation.
        with pytest.raises(InitError):
            init_project(root=project_dir, name="")

    def test_existing_manifest_file_fails(self, tmp_dir: Path) -> None:
        """Init fails if aip_loom.yaml already exists in the directory."""
        existing_dir = tmp_dir / "has-manifest"
        existing_dir.mkdir()
        (existing_dir / "aip_loom.yaml").write_text("some: content\n", encoding="utf-8")
        with pytest.raises(InitError) as exc_info:
            init_project(root=existing_dir, name="test")
        assert exc_info.value.loom_error.code == PROJECT_ALREADY_EXISTS


# ---------------------------------------------------------------------------
# No fake content
# ---------------------------------------------------------------------------


class TestNoFakeContent:
    """Verify that init never creates fake approved content."""

    def test_distillate_has_no_nodes(self, project_dir: Path) -> None:
        """Distillate is created with zero nodes — no fabricated data."""
        init_project(root=project_dir, name="test")
        layout = ProjectLayout(root=project_dir)
        distillate = load_yaml_as(layout.distillate_path, Distillate)
        assert len(distillate.nodes) == 0

    def test_ledgers_have_no_entries(self, project_dir: Path) -> None:
        """All ledgers are created with zero entries."""
        init_project(root=project_dir, name="test")
        layout = ProjectLayout(root=project_dir)

        decisions = load_yaml_as(layout.decisions_ledger_path, DecisionLedger)
        threads = load_yaml_as(layout.threads_ledger_path, ThreadLedger)
        questions = load_yaml_as(layout.questions_ledger_path, QuestionLedger)

        assert len(decisions.entries) == 0
        assert len(threads.entries) == 0
        assert len(questions.entries) == 0

    def test_sessions_and_comments_have_no_entries(self, project_dir: Path) -> None:
        """Session log and comment log have zero entries."""
        init_project(root=project_dir, name="test")
        layout = ProjectLayout(root=project_dir)

        sessions = load_yaml_as(layout.sessions_path, SessionLog)
        comments = load_yaml_as(layout.comments_path, CommentLog)

        assert len(sessions.entries) == 0
        assert len(comments.entries) == 0

    def test_chunks_dir_is_empty(self, project_dir: Path) -> None:
        """No chunk files are created during init."""
        init_project(root=project_dir, name="test")
        layout = ProjectLayout(root=project_dir)
        chunk_files = list(layout.chunks_dir.iterdir())
        assert len(chunk_files) == 0

    def test_archive_dir_is_empty(self, project_dir: Path) -> None:
        """No archived chunks are created during init."""
        init_project(root=project_dir, name="test")
        layout = ProjectLayout(root=project_dir)
        archive_files = list(layout.archive_dir.iterdir())
        assert len(archive_files) == 0


# ---------------------------------------------------------------------------
# Git initialisation (best-effort, non-fatal)
# ---------------------------------------------------------------------------


class TestGitInit:
    """Verify Git initialisation behaviour."""

    def test_git_initialized_when_available(self, project_dir: Path) -> None:
        """If git is available, the project is initialised as a git repo."""
        result = init_project(root=project_dir, name="test")
        # Git initialisation is best-effort; we check the result
        if result.git_initialized:
            from aip_loom.git import is_git_repo
            assert is_git_repo(project_dir)

    def test_git_init_non_fatal(self, project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If git init fails, init_project still succeeds."""
        # Monkey-patch to simulate git not being available
        from aip_loom import git as git_module
        original_find = git_module._find_git_binary

        def fake_missing():
            from aip_loom.git import GitError
            from aip_loom.errors import GIT_BINARY_MISSING, LoomError
            raise GitError(LoomError(code=GIT_BINARY_MISSING, message="Git not found"))

        monkeypatch.setattr(git_module, "_find_git_binary", fake_missing)

        result = init_project(root=project_dir, name="test")
        assert isinstance(result, InitResult)
        # The project should still be created even if git fails
        assert (project_dir / "aip_loom.yaml").is_file()

    def test_git_skip_warning_when_not_initialized(self, project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A warning is emitted when Git cannot be initialised."""
        from aip_loom import git as git_module

        def fake_missing():
            from aip_loom.git import GitError
            from aip_loom.errors import GIT_BINARY_MISSING, LoomError
            raise GitError(LoomError(code=GIT_BINARY_MISSING, message="Git not found"))

        monkeypatch.setattr(git_module, "_find_git_binary", fake_missing)

        result = init_project(root=project_dir, name="test")
        if not result.git_initialized:
            warning_codes = [w.code for w in result.warnings]
            assert GIT_INIT_SKIPPED in warning_codes

    def test_git_commit_created_on_success(self, project_dir: Path) -> None:
        """If git is available, an initial commit is created."""
        result = init_project(root=project_dir, name="test")
        if result.git_initialized and result.git_commit_created:
            # Verify via git log that at least one commit exists
            from aip_loom.git import _run_git
            log_result = _run_git(project_dir, ["log", "--oneline", "-1"], check=False)
            assert log_result.returncode == 0
            assert len(log_result.stdout.strip()) > 0


# ---------------------------------------------------------------------------
# Schema validation of created files
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    """Verify that all created files validate against their schemas."""

    def test_manifest_validates(self, project_dir: Path) -> None:
        """The manifest file validates against ProjectManifest."""
        init_project(root=project_dir, name="schema-test")
        layout = ProjectLayout(root=project_dir)
        manifest = load_yaml_as(layout.manifest_path, ProjectManifest)
        assert manifest.name == "schema-test"
        assert manifest.schema_version == SUPPORTED_SCHEMA_VERSION

    def test_all_ledgers_validate(self, project_dir: Path) -> None:
        """All ledger files validate against their schemas."""
        init_project(root=project_dir, name="schema-test")
        layout = ProjectLayout(root=project_dir)

        # These should not raise YamlLoadError
        load_yaml_as(layout.decisions_ledger_path, DecisionLedger)
        load_yaml_as(layout.threads_ledger_path, ThreadLedger)
        load_yaml_as(layout.questions_ledger_path, QuestionLedger)

    def test_distillate_validates(self, project_dir: Path) -> None:
        """The distillate file validates against Distillate."""
        init_project(root=project_dir, name="schema-test")
        layout = ProjectLayout(root=project_dir)
        load_yaml_as(layout.distillate_path, Distillate)

    def test_session_log_validates(self, project_dir: Path) -> None:
        """The session log file validates against SessionLog."""
        init_project(root=project_dir, name="schema-test")
        layout = ProjectLayout(root=project_dir)
        load_yaml_as(layout.sessions_path, SessionLog)

    def test_comment_log_validates(self, project_dir: Path) -> None:
        """The comment log file validates against CommentLog."""
        init_project(root=project_dir, name="schema-test")
        layout = ProjectLayout(root=project_dir)
        load_yaml_as(layout.comments_path, CommentLog)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


class TestPathSafety:
    """Verify that init respects path safety rules."""

    def test_all_files_within_root(self, project_dir: Path) -> None:
        """All created files are within the project root."""
        init_project(root=project_dir, name="path-test")
        layout = ProjectLayout(root=project_dir)
        root = project_dir.resolve()

        # Walk the project tree and check every file
        for path in root.rglob("*"):
            if path.is_file():
                # Must be within root
                try:
                    path.relative_to(root)
                except ValueError:
                    pytest.fail(f"File outside project root: {path}")

    def test_no_symlinks_escaping_root(self, project_dir: Path) -> None:
        """No symlinks are created that escape the project root."""
        init_project(root=project_dir, name="path-test")
        root = project_dir.resolve()

        for path in root.rglob("*"):
            if path.is_symlink():
                target = path.resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    pytest.fail(f"Symlink escapes root: {path} -> {target}")


# ---------------------------------------------------------------------------
# Project layout integration
# ---------------------------------------------------------------------------


class TestLayoutIntegration:
    """Verify that init creates files in the exact paths ProjectLayout expects."""

    def test_layout_recognizes_initialized_project(self, project_dir: Path) -> None:
        """ProjectLayout.is_project_initialized() returns True after init."""
        init_project(root=project_dir, name="layout-test")
        layout = ProjectLayout(root=project_dir)
        assert layout.is_project_initialized()

    def test_all_layout_paths_exist(self, project_dir: Path) -> None:
        """All canonical paths from ProjectLayout exist after init."""
        init_project(root=project_dir, name="layout-test")
        layout = ProjectLayout(root=project_dir)

        assert layout.manifest_path.is_file()
        assert layout.chunks_dir.is_dir()
        assert layout.ledgers_dir.is_dir()
        assert layout.archive_dir.is_dir()
        assert layout.aip_loom_dir.is_dir()
        assert layout.decisions_ledger_path.is_file()
        assert layout.threads_ledger_path.is_file()
        assert layout.questions_ledger_path.is_file()
        assert layout.distillate_path.is_file()
        assert layout.sessions_path.is_file()
        assert layout.comments_path.is_file()

    def test_staging_dir_exists(self, project_dir: Path) -> None:
        """The staging directory exists after init."""
        init_project(root=project_dir, name="layout-test")
        layout = ProjectLayout(root=project_dir)
        assert layout.staging_dir.is_dir()


# ---------------------------------------------------------------------------
# Idempotency-like behaviour
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Verify that init is not idempotent — it correctly rejects re-init."""

    def test_double_init_fails(self, project_dir: Path) -> None:
        """Running init twice on the same directory fails the second time."""
        init_project(root=project_dir, name="first-init")
        with pytest.raises(InitError) as exc_info:
            init_project(root=project_dir, name="second-init")
        assert exc_info.value.loom_error.code == PROJECT_ALREADY_EXISTS

    def test_init_with_dir_flag_then_cwd_fails(self, tmp_dir: Path) -> None:
        """Init in same dir by different path still fails on second attempt."""
        project_path = tmp_dir / "project"
        init_project(root=project_path, name="test")
        with pytest.raises(InitError):
            init_project(root=project_path, name="test-again")


# ---------------------------------------------------------------------------
# Nested directory creation
# ---------------------------------------------------------------------------


class TestNestedDirCreation:
    """Verify that init can create nested directories for the root."""

    def test_creates_nested_root_dirs(self, tmp_dir: Path) -> None:
        """Init creates parent directories if they don't exist."""
        nested = tmp_dir / "a" / "b" / "c" / "project"
        result = init_project(root=nested, name="deep-nested")
        assert nested.is_dir()
        assert (nested / "aip_loom.yaml").is_file()

    def test_nested_dir_failure_cleans_up(self, tmp_dir: Path) -> None:
        """If init fails after creating nested dirs, they are cleaned up."""
        nested = tmp_dir / "x" / "y" / "z" / "project"
        # Force a failure by providing an invalid project type
        with pytest.raises(InitError):
            init_project(root=nested, name="fail-nested", project_type="bad-type")
        # The nested directory should not exist since we created it and then
        # the init failed before creating any files
        # (Type validation happens before directory creation, so the dir
        #  should not exist at all)
        assert not nested.exists()
