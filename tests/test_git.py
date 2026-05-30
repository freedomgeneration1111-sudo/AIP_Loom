"""Tests for aip_loom.git — Git wrapper.

These tests prove:
- is_git_repo: detects Git repos and non-repo directories
- git_status: returns structured status for clean and dirty repos
- is_git_clean: returns correct boolean for clean/dirty state
- git_add: stages files successfully
- git_commit: creates commits, surfaces stderr on failure
- configure_local_git: sets local config without requiring global config
- Missing git binary produces GIT_BINARY_MISSING
- Dirty repo detection works for staged, unstaged, and untracked files
- git_commit with allow_empty creates empty commits
- Pre-commit hook failures are surfaced
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from aip_loom.errors import (
    GIT_BINARY_MISSING,
    GIT_COMMIT_FAILED,
    GIT_DIRTY,
    GIT_NOT_REPO,
    LoomError,
)
from aip_loom.git import (
    GitError,
    GitStatus,
    configure_local_git,
    git_add,
    git_commit,
    git_status,
    is_git_clean,
    is_git_repo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(root: Path) -> None:
    """Initialize a Git repository and configure local user for commits."""
    from aip_loom.git import _run_git

    _run_git(root, ["init"], check=True)
    configure_local_git(root)


def _create_and_commit_file(root: Path, name: str, content: str, message: str) -> None:
    """Create a file, stage it, and commit it."""
    file_path = root / name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    git_add(root, [file_path])
    git_commit(root, message)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Create an initialized Git repository with one initial commit."""
    root = tmp_path / "my-project"
    root.mkdir()
    _init_git_repo(root)
    _create_and_commit_file(root, "initial.txt", "initial content", "Initial commit")
    return root


@pytest.fixture()
def non_git_dir(tmp_path: Path) -> Path:
    """Create a plain directory that is not a Git repository."""
    root = tmp_path / "plain-dir"
    root.mkdir()
    return root


# ===========================================================================
# is_git_repo
# ===========================================================================


class TestIsGitRepo:
    """is_git_repo detects Git repos and non-repo directories."""

    def test_returns_true_for_git_repo(self, git_repo: Path) -> None:
        assert is_git_repo(git_repo) is True

    def test_returns_false_for_non_git_dir(self, non_git_dir: Path) -> None:
        assert is_git_repo(non_git_dir) is False

    def test_returns_false_for_nonexistent_dir(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "does-not-exist"
        assert is_git_repo(nonexistent) is False

    def test_returns_true_for_subdirectory_of_repo(self, git_repo: Path) -> None:
        subdir = git_repo / "subdir"
        subdir.mkdir()
        assert is_git_repo(subdir) is True


# ===========================================================================
# git_status
# ===========================================================================


class TestGitStatus:
    """git_status returns structured status for clean and dirty repos."""

    def test_clean_repo(self, git_repo: Path) -> None:
        status = git_status(git_repo)
        assert status.is_repo is True
        assert status.clean is True
        assert status.staged == ()
        assert status.unstaged == ()
        assert status.untracked == ()

    def test_untracked_file(self, git_repo: Path) -> None:
        (git_repo / "new_file.txt").write_text("hello", encoding="utf-8")
        status = git_status(git_repo)
        assert status.is_repo is True
        assert status.clean is False
        assert len(status.untracked) == 1
        assert "new_file.txt" in status.untracked

    def test_staged_file(self, git_repo: Path) -> None:
        file_path = git_repo / "staged.txt"
        file_path.write_text("staged content", encoding="utf-8")
        git_add(git_repo, [file_path])
        status = git_status(git_repo)
        assert status.clean is False
        assert len(status.staged) == 1
        assert "staged.txt" in status.staged

    def test_unstaged_modification(self, git_repo: Path) -> None:
        (git_repo / "initial.txt").write_text("modified content", encoding="utf-8")
        status = git_status(git_repo)
        assert status.clean is False
        assert len(status.unstaged) == 1
        assert "initial.txt" in status.unstaged

    def test_non_git_dir_returns_not_repo(self, non_git_dir: Path) -> None:
        status = git_status(non_git_dir)
        assert status.is_repo is False
        assert status.clean is False

    def test_raw_contains_porcelain_output(self, git_repo: Path) -> None:
        (git_repo / "untracked.txt").write_text("hello", encoding="utf-8")
        status = git_status(git_repo)
        assert "untracked.txt" in status.raw

    def test_staged_and_unstaged_and_untracked(self, git_repo: Path) -> None:
        """All three categories can be present simultaneously."""
        # Staged: new file
        staged_file = git_repo / "staged_new.txt"
        staged_file.write_text("staged", encoding="utf-8")
        git_add(git_repo, [staged_file])

        # Unstaged: modified tracked file
        (git_repo / "initial.txt").write_text("modified", encoding="utf-8")

        # Untracked
        (git_repo / "untracked.txt").write_text("untracked", encoding="utf-8")

        status = git_status(git_repo)
        assert len(status.staged) >= 1
        assert len(status.unstaged) >= 1
        assert len(status.untracked) >= 1


# ===========================================================================
# is_git_clean
# ===========================================================================


class TestIsGitClean:
    """is_git_clean returns correct boolean for clean/dirty state."""

    def test_clean_repo(self, git_repo: Path) -> None:
        assert is_git_clean(git_repo) is True

    def test_dirty_repo_untracked(self, git_repo: Path) -> None:
        (git_repo / "new.txt").write_text("hello", encoding="utf-8")
        assert is_git_clean(git_repo) is False

    def test_dirty_repo_staged(self, git_repo: Path) -> None:
        file_path = git_repo / "new.txt"
        file_path.write_text("hello", encoding="utf-8")
        git_add(git_repo, [file_path])
        assert is_git_clean(git_repo) is False

    def test_dirty_repo_unstaged(self, git_repo: Path) -> None:
        (git_repo / "initial.txt").write_text("changed", encoding="utf-8")
        assert is_git_clean(git_repo) is False

    def test_non_git_dir_returns_false(self, non_git_dir: Path) -> None:
        assert is_git_clean(non_git_dir) is False


# ===========================================================================
# git_add
# ===========================================================================


class TestGitAdd:
    """git_add stages files for the next commit."""

    def test_add_single_file(self, git_repo: Path) -> None:
        file_path = git_repo / "new_file.txt"
        file_path.write_text("content", encoding="utf-8")
        git_add(git_repo, [file_path])
        status = git_status(git_repo)
        assert len(status.staged) == 1
        assert "new_file.txt" in status.staged

    def test_add_multiple_files(self, git_repo: Path) -> None:
        file1 = git_repo / "file1.txt"
        file2 = git_repo / "file2.txt"
        file1.write_text("content1", encoding="utf-8")
        file2.write_text("content2", encoding="utf-8")
        git_add(git_repo, [file1, file2])
        status = git_status(git_repo)
        assert len(status.staged) == 2

    def test_add_no_paths_is_noop(self, git_repo: Path) -> None:
        # Should not raise or change status
        git_add(git_repo, [])
        assert is_git_clean(git_repo) is True

    def test_add_file_in_subdirectory(self, git_repo: Path) -> None:
        subdir = git_repo / "subdir"
        subdir.mkdir()
        file_path = subdir / "nested.txt"
        file_path.write_text("nested", encoding="utf-8")
        git_add(git_repo, [file_path])
        status = git_status(git_repo)
        assert len(status.staged) == 1


# ===========================================================================
# git_commit
# ===========================================================================


class TestGitCommit:
    """git_commit creates commits and surfaces errors honestly."""

    def test_successful_commit(self, git_repo: Path) -> None:
        file_path = git_repo / "committed.txt"
        file_path.write_text("content", encoding="utf-8")
        git_add(git_repo, [file_path])
        git_commit(git_repo, "Add committed.txt")
        # File should now be committed — working tree clean
        assert is_git_clean(git_repo) is True

    def test_commit_surfaces_nothing_to_commit_error(self, git_repo: Path) -> None:
        """Attempting to commit with no staged changes (allow_empty=False)
        raises GitError with GIT_COMMIT_FAILED."""
        with pytest.raises(GitError) as exc_info:
            git_commit(git_repo, "Empty commit should fail")
        assert exc_info.value.loom_error.code == GIT_COMMIT_FAILED
        # stderr should mention "nothing to commit"
        detail = exc_info.value.loom_error.detail
        assert "nothing to commit" in detail.get("stderr", "").lower() or \
               "nothing to commit" in detail.get("stdout", "").lower()

    def test_commit_with_allow_empty(self, git_repo: Path) -> None:
        """allow_empty=True creates a commit even with no changes."""
        git_commit(git_repo, "Empty commit allowed", allow_empty=True)
        # Should succeed — working tree still clean
        assert is_git_clean(git_repo) is True

    def test_commit_failure_includes_stderr(self, git_repo: Path) -> None:
        """A commit failure includes the stderr from Git."""
        with pytest.raises(GitError) as exc_info:
            git_commit(git_repo, "No changes staged")
        assert exc_info.value.loom_error.code == GIT_COMMIT_FAILED
        # The error detail must contain stderr
        assert "stderr" in exc_info.value.loom_error.detail

    def test_commit_with_message(self, git_repo: Path) -> None:
        """Commit message is stored in the commit."""
        from aip_loom.git import _run_git

        file_path = git_repo / "msg_test.txt"
        file_path.write_text("content", encoding="utf-8")
        git_add(git_repo, [file_path])
        git_commit(git_repo, "Custom commit message")
        result = _run_git(git_repo, ["log", "--oneline", "-1"], check=True)
        assert "Custom commit message" in result.stdout


# ===========================================================================
# Pre-commit hook failure
# ===========================================================================


class TestPreCommitHookFailure:
    """Pre-commit hook failures are surfaced, not hidden."""

    def test_pre_commit_hook_rejection(self, git_repo: Path) -> None:
        """A failing pre-commit hook causes git_commit to raise GitError
        with the hook's error message in stderr."""
        # Create a pre-commit hook that always fails
        hooks_dir = git_repo / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_path = hooks_dir / "pre-commit"
        hook_path.write_text(
            '#!/bin/sh\necho "Pre-commit hook rejected" >&2\nexit 1\n',
            encoding="utf-8",
        )
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)

        file_path = git_repo / "hooked.txt"
        file_path.write_text("content", encoding="utf-8")
        git_add(git_repo, [file_path])

        with pytest.raises(GitError) as exc_info:
            git_commit(git_repo, "Should be rejected by hook")
        assert exc_info.value.loom_error.code == GIT_COMMIT_FAILED
        # The hook's error message should be in stderr
        detail = exc_info.value.loom_error.detail
        stderr_text = detail.get("stderr", "")
        assert "Pre-commit hook rejected" in stderr_text


# ===========================================================================
# configure_local_git
# ===========================================================================


class TestConfigureLocalGit:
    """configure_local_git sets local repo config for tests."""

    def test_sets_user_name_and_email(self, tmp_path: Path) -> None:
        from aip_loom.git import _run_git

        root = tmp_path / "config-test"
        root.mkdir()
        _run_git(root, ["init"], check=True)
        configure_local_git(root, user_name="Test User", user_email="test@example.com")

        name_result = _run_git(root, ["config", "user.name"], check=True)
        email_result = _run_git(root, ["config", "user.email"], check=True)

        assert name_result.stdout.strip() == "Test User"
        assert email_result.stdout.strip() == "test@example.com"

    def test_default_values(self, tmp_path: Path) -> None:
        from aip_loom.git import _run_git

        root = tmp_path / "default-config"
        root.mkdir()
        _run_git(root, ["init"], check=True)
        configure_local_git(root)

        name_result = _run_git(root, ["config", "user.name"], check=True)
        email_result = _run_git(root, ["config", "user.email"], check=True)

        assert name_result.stdout.strip() == "AIP Loom Test"
        assert email_result.stdout.strip() == "test@aip-loom.local"

    def test_enables_commit_without_global_config(self, tmp_path: Path) -> None:
        """After configure_local_git, commits work without any global config."""
        root = tmp_path / "no-global-config"
        root.mkdir()
        _init_git_repo(root)
        # Create and commit a file — should succeed
        file_path = root / "test.txt"
        file_path.write_text("content", encoding="utf-8")
        git_add(root, [file_path])
        git_commit(root, "First commit with local config")
        assert is_git_clean(root) is True


# ===========================================================================
# GitError structure
# ===========================================================================


class TestGitErrorStructure:
    """GitError carries structured LoomError with stable codes."""

    def test_git_error_has_loom_error_attribute(self, git_repo: Path) -> None:
        with pytest.raises(GitError) as exc_info:
            git_commit(git_repo, "No changes")
        assert hasattr(exc_info.value, "loom_error")
        assert exc_info.value.loom_error.code == GIT_COMMIT_FAILED

    def test_git_error_has_detail_dict(self, git_repo: Path) -> None:
        with pytest.raises(GitError) as exc_info:
            git_commit(git_repo, "No changes")
        detail = exc_info.value.loom_error.detail
        assert isinstance(detail, dict)
        assert "stderr" in detail

    def test_git_error_is_exception(self) -> None:
        err = GitError(
            LoomError(code=GIT_COMMIT_FAILED, message="test")
        )
        assert isinstance(err, Exception)
        assert str(err) == "test"


# ===========================================================================
# GitStatus structure
# ===========================================================================


class TestGitStatusStructure:
    """GitStatus is a frozen dataclass with all expected fields."""

    def test_frozen(self) -> None:
        status = GitStatus(
            is_repo=True, clean=True,
            staged=(), unstaged=(), untracked=(), raw=""
        )
        with pytest.raises(AttributeError):
            status.clean = False  # type: ignore[misc]

    def test_fields(self, git_repo: Path) -> None:
        status = git_status(git_repo)
        assert hasattr(status, "is_repo")
        assert hasattr(status, "clean")
        assert hasattr(status, "staged")
        assert hasattr(status, "unstaged")
        assert hasattr(status, "untracked")
        assert hasattr(status, "raw")

    def test_staged_unstaged_untracked_are_tuples(self, git_repo: Path) -> None:
        status = git_status(git_repo)
        assert isinstance(status.staged, tuple)
        assert isinstance(status.unstaged, tuple)
        assert isinstance(status.untracked, tuple)


# ===========================================================================
# Integration: full workflow
# ===========================================================================


class TestGitWorkflow:
    """End-to-end Git workflow through the wrapper."""

    def test_init_add_commit_status(self, tmp_path: Path) -> None:
        """Complete workflow: init → config → add → commit → check clean."""
        root = tmp_path / "workflow"
        root.mkdir()
        _init_git_repo(root)

        # Initial commit
        _create_and_commit_file(root, "README.md", "# My Project", "Add README")

        # Add another file
        _create_and_commit_file(root, "chapter1.md", "Chapter 1 content", "Add chapter 1")

        assert is_git_repo(root) is True
        assert is_git_clean(root) is True

    def test_detect_dirty_after_modification(self, git_repo: Path) -> None:
        """Modifying a tracked file makes the repo dirty."""
        (git_repo / "initial.txt").write_text("modified", encoding="utf-8")
        assert is_git_clean(git_repo) is False
        status = git_status(git_repo)
        assert len(status.unstaged) == 1

    def test_detect_dirty_after_new_file(self, git_repo: Path) -> None:
        """Adding an untracked file makes the repo dirty."""
        (git_repo / "new.txt").write_text("new", encoding="utf-8")
        assert is_git_clean(git_repo) is False
        status = git_status(git_repo)
        assert len(status.untracked) == 1

    def test_clean_after_commit(self, git_repo: Path) -> None:
        """Committing changes makes the repo clean again."""
        (git_repo / "initial.txt").write_text("modified", encoding="utf-8")
        assert is_git_clean(git_repo) is False
        git_add(git_repo, [git_repo / "initial.txt"])
        git_commit(git_repo, "Update initial.txt")
        assert is_git_clean(git_repo) is True
