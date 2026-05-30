"""Single, auditable Git wrapper for AIP_Loom.

This module is the **single authority** for all Git operations in AIP_Loom.
No other module may call ``subprocess.run(['git', ...])`` directly or use
GitPython.  All Git interactions must go through this module.

Design principles (BuildSpec §3A and §6):

- **subprocess only**: Uses ``subprocess.run`` exclusively.  No GitPython,
  no libgit2 bindings.  This ensures deterministic behaviour and avoids
  dependency on GitPython's internal caching or state management.
- **Capture both stdout and stderr**: Every Git invocation captures both
  streams so that errors are fully diagnostic.  Stderr is never silently
  discarded.
- **Never hide Git errors**: If Git returns a non-zero exit code, the
  error is surfaced as a :class:`GitError` with the full stderr content.
  Pre-commit hook failures are not suppressed.
- **Structured results**: :func:`git_status` returns a :class:`GitStatus`
  frozen dataclass rather than a raw string.  This makes the status
  machine-parseable and testable.
- **Local test config**: :func:`configure_local_git` sets ``user.name``
  and ``user.email`` in the local repo config so tests do not depend on
  global Git configuration.
- **Commit failure is real failure**: :func:`git_commit` surfaces stderr
  on failure.  It does **not** auto-restore writer data or treat a
  failed commit as a no-op.
- **Binary check**: :func:`_run_git` verifies that the ``git`` binary
  is available before attempting to run any command.  A missing binary
  produces ``GIT_BINARY_MISSING`` rather than a generic ``FileNotFoundError``.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import (
    GIT_BINARY_MISSING,
    GIT_COMMIT_FAILED,
    GIT_DIRTY,
    GIT_NOT_REPO,
    LoomError,
)

__all__ = [
    "GitError",
    "GitStatus",
    "is_git_repo",
    "git_status",
    "is_git_clean",
    "git_add",
    "git_commit",
    "configure_local_git",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GitError(Exception):
    """Raised when a Git operation fails.

    Carries a :class:`LoomError` with a stable error code.
    """

    def __init__(self, loom_error: LoomError) -> None:
        self.loom_error = loom_error
        super().__init__(loom_error.message)


# ---------------------------------------------------------------------------
# Structured results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitStatus:
    """Structured representation of ``git status --porcelain`` output.

    Attributes
    ----------
    is_repo:
        Whether the directory is inside a Git repository.
    clean:
        Whether the working tree is clean (no staged, unstaged, or
        untracked changes).
    staged:
        Files with changes staged in the index (XY codes where X != ' '
        and X != '?').
    unstaged:
        Files with changes in the working tree that are not staged
        (XY codes where Y != ' ' and X != '?').
    untracked:
        Untracked files (XY code ``??``).
    raw:
        The raw ``git status --porcelain`` output, for diagnostics.
    """

    is_repo: bool
    clean: bool
    staged: tuple[str, ...]
    unstaged: tuple[str, ...]
    untracked: tuple[str, ...]
    raw: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_git_binary() -> str:
    """Locate the ``git`` binary on the system PATH.

    Returns
    -------
    str
        The absolute path to the ``git`` binary.

    Raises
    ------
    GitError
        If the ``git`` binary cannot be found (``GIT_BINARY_MISSING``).
    """
    git_path = shutil.which("git")
    if git_path is None:
        raise GitError(
            LoomError(
                code=GIT_BINARY_MISSING,
                message=(
                    "Git binary not found on PATH.  "
                    "AIP_Loom requires Git to be installed and accessible."
                ),
                detail={"search_path": shutil.get_exec_path()},
            )
        )
    return git_path


def _run_git(
    root: Path,
    args: list[str],
    *,
    check: bool = True,
    input_data: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a Git subprocess with consistent error handling.

    All Git commands are run with ``-C <root>`` to specify the working
    directory, and both stdout and stderr are captured as text.

    Parameters
    ----------
    root:
        The repository root directory (passed via ``git -C``).
    args:
        Git command arguments (e.g. ``["status", "--porcelain"]``).
        The ``git`` binary and ``-C`` flag are prepended automatically.
    check:
        If ``True`` (default), a non-zero return code raises
        :class:`GitError`.  If ``False``, the completed process is
        returned regardless of exit code.
    input_data:
        Optional stdin data to pass to the subprocess.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The completed process result with captured stdout and stderr.

    Raises
    ------
    GitError
        If the ``git`` binary is missing, or if ``check=True`` and the
        subprocess returns a non-zero exit code.
    """
    git_bin = _find_git_binary()
    cmd = [git_bin, "-C", str(root)] + args

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            input=input_data,
        )
    except OSError as exc:
        raise GitError(
            LoomError(
                code=GIT_BINARY_MISSING,
                message=f"Cannot execute Git binary: {exc}",
                detail={"binary": git_bin, "error": str(exc)},
            )
        ) from exc

    if check and result.returncode != 0:
        stderr = result.stderr.strip()
        raise GitError(
            LoomError(
                code=GIT_COMMIT_FAILED,
                message=f"Git command failed: git {' '.join(args)}",
                detail={
                    "command": "git " + " ".join(args),
                    "returncode": result.returncode,
                    "stdout": result.stdout.strip(),
                    "stderr": stderr,
                },
            )
        )

    return result


def _parse_porcelain(raw: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Parse ``git status --porcelain`` output into categorised file lists.

    The porcelain format is ``XY PATH`` where X is the index status and
    Y is the working tree status.  See ``git status --help`` for the full
    code table.

    Returns
    -------
    tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]
        ``(staged, unstaged, untracked)`` — each is a tuple of path
        strings.
    """
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []

    for line in raw.splitlines():
        if not line:
            continue
        # Each line is 2 status chars + space + path (possibly quoted)
        # For renames: XY old_path -> new_path
        x = line[0]
        y = line[1]
        path_part = line[3:]  # skip "XY "

        if x == "?" and y == "?":
            untracked.append(path_part)
        else:
            if x != " " and x != "?":
                staged.append(path_part)
            if y != " " and y != "?":
                unstaged.append(path_part)

    return (tuple(staged), tuple(unstaged), tuple(untracked))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_git_repo(root: Path) -> bool:
    """Check whether a directory is inside a Git repository.

    This runs ``git rev-parse --git-dir`` and returns ``True`` if it
    succeeds.  It does **not** raise on failure — it returns ``False``
    for any non-repo directory.

    Parameters
    ----------
    root:
        The directory to check.

    Returns
    -------
    bool
        ``True`` if *root* is inside a Git repository.
    """
    try:
        result = _run_git(root, ["rev-parse", "--git-dir"], check=False)
        return result.returncode == 0
    except GitError:
        # Binary missing — definitely not a repo
        return False


def git_status(root: Path) -> GitStatus:
    """Get the working tree status of a Git repository.

    Returns a :class:`GitStatus` with structured information about
    staged, unstaged, and untracked files.

    Parameters
    ----------
    root:
        The repository root directory.

    Returns
    -------
    GitStatus
        Structured status information.

    Raises
    ------
    GitError
        If *root* is not a Git repository (``GIT_NOT_REPO``).
    """
    if not is_git_repo(root):
        return GitStatus(
            is_repo=False,
            clean=False,
            staged=(),
            unstaged=(),
            untracked=(),
            raw="",
        )

    result = _run_git(root, ["status", "--porcelain"], check=True)
    raw = result.stdout
    staged, unstaged, untracked = _parse_porcelain(raw)

    clean = len(staged) == 0 and len(unstaged) == 0 and len(untracked) == 0

    return GitStatus(
        is_repo=True,
        clean=clean,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        raw=raw,
    )


def is_git_clean(root: Path) -> bool:
    """Check whether a Git repository's working tree is clean.

    This is a convenience wrapper around :func:`git_status` that returns
    a simple boolean.  For full status details, use :func:`git_status`.

    Parameters
    ----------
    root:
        The repository root directory.

    Returns
    -------
    bool
        ``True`` if the working tree has no staged, unstaged, or
        untracked changes.  ``False`` if the directory is not a Git
        repo or if there are any changes.
    """
    status = git_status(root)
    return status.clean


def git_add(root: Path, paths: list[Path]) -> None:
    """Stage files for the next commit.

    Parameters
    ----------
    root:
        The repository root directory.
    paths:
        List of file paths to stage.  Paths may be absolute or relative
        to *root*.

    Raises
    ------
    GitError
        If the ``git add`` command fails.
    """
    if not paths:
        return

    # Convert all paths to strings relative to root for consistency
    str_paths: list[str] = []
    for p in paths:
        p = Path(p)
        try:
            rel = p.relative_to(root)
            str_paths.append(str(rel))
        except ValueError:
            # Path is not relative to root — use as-is (absolute or
            # already relative)
            str_paths.append(str(p))

    _run_git(root, ["add", "--"] + str_paths, check=True)


def git_commit(
    root: Path,
    message: str,
    allow_empty: bool = False,
) -> None:
    """Create a commit with the currently staged changes.

    Parameters
    ----------
    root:
        The repository root directory.
    message:
        The commit message.
    allow_empty:
        If ``True``, allows creating a commit with no changes
        (``--allow-empty``).  Default is ``False``.

    Raises
    ------
    GitError
        If the commit fails (e.g. nothing to commit and ``allow_empty``
        is ``False``, or a pre-commit hook rejects the commit).
        The full stderr is included in the error detail so that
        callers can surface it to the user.
    """
    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")

    try:
        _run_git(root, args, check=True)
    except GitError as exc:
        # Re-raise with GIT_COMMIT_FAILED code and full stderr
        stderr = exc.loom_error.detail.get("stderr", "")
        raise GitError(
            LoomError(
                code=GIT_COMMIT_FAILED,
                message=f"Git commit failed: {stderr}" if stderr else "Git commit failed",
                detail={
                    "message": message,
                    "allow_empty": allow_empty,
                    "returncode": exc.loom_error.detail.get("returncode"),
                    "stdout": exc.loom_error.detail.get("stdout", ""),
                    "stderr": stderr,
                },
            )
        ) from exc


def configure_local_git(
    root: Path,
    user_name: str = "AIP Loom Test",
    user_email: str = "test@aip-loom.local",
) -> None:
    """Configure local Git user.name and user.email for a repository.

    This is primarily intended for test environments where no global
    Git configuration exists.  It sets ``user.name`` and ``user.email``
    in the repository-local config (``.git/config``) so that commits
    can be created without depending on a global ``~/.gitconfig``.

    Parameters
    ----------
    root:
        The repository root directory.
    user_name:
        The Git user name to set.  Default is ``"AIP Loom Test"``.
    user_email:
        The Git user email to set.  Default is ``"test@aip-loom.local"``.

    Raises
    ------
    GitError
        If the ``git config`` commands fail.
    """
    _run_git(root, ["config", "user.name", user_name], check=True)
    _run_git(root, ["config", "user.email", user_email], check=True)
