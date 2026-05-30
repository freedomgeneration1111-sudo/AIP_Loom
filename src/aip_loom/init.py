"""Project initialisation service for AIP_Loom.

This module is the **single authority** for creating a new AIP_Loom project.
No other module may create the project directory tree or initial files — it
must delegate to :func:`init_project` here.

Design principles (BuildSpec §3A and §3B):

- **Create-or-fail semantics**: If any step fails, all partial artefacts are
  cleaned up.  The target directory is left in the same state as before the
  call (or removed entirely if we created it).
- **No fake approved content**: The distillate placeholder is created with an
  empty ``nodes`` list.  No fabricated summaries, decisions, or approval
  states are ever written.
- **Schema-valid output**: Every file written during init must pass validation
  against the corresponding Pydantic model in :mod:`aip_loom.schemas`.
- **Path safety**: All path construction goes through :class:`ProjectLayout`.
  All file writes go through :mod:`aip_loom.fs`.
- **YAML gateway**: All YAML serialisation goes through :mod:`aip_loom.yaml_io`.
- **Git best-effort**: Git initialisation is attempted but never fatal.  If the
  ``git`` binary is missing, or ``git init`` / ``git commit`` fails, a warning
  is returned but the project is still considered successfully initialised.
- **Transaction safety**: :class:`TransactionWorkspace` is used to snapshot any
  files that might be overwritten, though for a fresh project no pre-existing
  files should exist.  The transaction provides rollback capability if init
  fails partway through.

Created project structure::

    <project_root>/
    ├── aip_loom.yaml          # Project manifest
    ├── chunks/                # Chunk Markdown files (empty)
    ├── ledgers/
    │   ├── decisions.yaml     # Decision ledger (empty entries)
    │   ├── threads.yaml       # Thread/strand ledger (empty entries)
    │   └── questions.yaml     # Question/open-issue ledger (empty entries)
    ├── distillate.yaml        # Structural index (empty nodes)
    ├── sessions.yaml          # Session log (empty entries)
    ├── comments.yaml          # Review comments (empty entries)
    ├── archive/               # Archived chunks (empty)
    └── .aip-loom/
        └── lock               # (created on first lock acquire)
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import (
    FIELD_INVALID,
    GIT_INIT_SKIPPED,
    PROJECT_ALREADY_EXISTS,
    FILE_WRITE_ERROR,
    LoomError,
    LoomWarning,
)
from .fs import AtomicWriteError, ensure_directory, safe_write_text
from .git import GitError, configure_local_git, git_add, git_commit, is_git_repo
from .layout import LayoutError, ProjectLayout
from .schemas import (
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
from .transaction import TransactionError, TransactionWorkspace
from .yaml_io import dump_yaml_string

__all__ = [
    "InitError",
    "InitResult",
    "init_project",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InitError(Exception):
    """Raised when project initialisation fails.

    Carries a :class:`LoomError` with a stable error code.
    """

    def __init__(self, loom_error: LoomError) -> None:
        self.loom_error = loom_error
        super().__init__(loom_error.message)


# ---------------------------------------------------------------------------
# Structured result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InitResult:
    """Result of a project initialisation attempt.

    Attributes
    ----------
    root:
        The absolute path to the created project root.
    git_initialized:
        Whether Git was successfully initialised.
    git_commit_created:
        Whether an initial Git commit was created.
    warnings:
        Non-fatal warnings accumulated during initialisation.
    """

    root: Path
    git_initialized: bool
    git_commit_created: bool
    warnings: tuple[LoomWarning, ...] = ()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_project_type(project_type: str) -> ProjectType:
    """Validate and convert a project type string to a ProjectType enum.

    Parameters
    ----------
    project_type:
        The project type string (e.g. ``"novel"``, ``"technical"``).

    Returns
    -------
    ProjectType
        The validated enum member.

    Raises
    ------
    InitError
        If the project type is not a valid :class:`ProjectType` value.
    """
    try:
        return ProjectType(project_type)
    except ValueError:
        valid = ", ".join(t.value for t in ProjectType)
        raise InitError(
            LoomError(
                code=FIELD_INVALID,
                message=(
                    f"Invalid project type {project_type!r}. "
                    f"Must be one of: {valid}"
                ),
                detail={
                    "field": "project_type",
                    "value": project_type,
                    "valid_values": [t.value for t in ProjectType],
                },
            )
        )


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _build_manifest_yaml(name: str, project_type: ProjectType) -> str:
    """Build the YAML content for the project manifest.

    The manifest is constructed from a validated :class:`ProjectManifest`
    model instance to ensure schema compliance.
    """
    now = _now_iso()
    manifest = ProjectManifest(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        name=name,
        project_type=project_type,
        chunks={"order": []},
        created_at=now,
        updated_at=now,
    )
    return dump_yaml_string(manifest.model_dump(mode="json"))


def _build_empty_ledger_yaml(ledger_model: type) -> str:
    """Build the YAML content for an empty ledger file.

    The ledger is constructed from a validated model instance with no
    entries to ensure schema compliance.
    """
    ledger = ledger_model(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        entries=[],
    )
    return dump_yaml_string(ledger.model_dump(mode="json"))


def _build_empty_distillate_yaml() -> str:
    """Build the YAML content for an empty distillate file.

    The distillate is created with an empty ``nodes`` list.  No fake
    approved content, summaries, or decisions are ever included.
    """
    distillate = Distillate(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        nodes=[],
    )
    return dump_yaml_string(distillate.model_dump(mode="json"))


def _build_empty_session_log_yaml() -> str:
    """Build the YAML content for an empty session log."""
    log = SessionLog(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        entries=[],
    )
    return dump_yaml_string(log.model_dump(mode="json"))


def _build_empty_comment_log_yaml() -> str:
    """Build the YAML content for an empty comment log."""
    log = CommentLog(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        entries=[],
    )
    return dump_yaml_string(log.model_dump(mode="json"))


def _cleanup_partial(root: Path, created_root: bool) -> None:
    """Clean up partial project artefacts after a failed init.

    If we created the root directory, we remove it entirely.
    If the root directory pre-existed, we do nothing — we should
    not delete a directory we did not create.
    """
    if created_root and root.exists():
        try:
            shutil.rmtree(str(root))
        except OSError:
            # Best-effort cleanup.  If we can't remove it, we leave it.
            # The user can manually inspect and delete.
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_project(
    root: Path,
    name: str,
    project_type: str = "novel",
) -> InitResult:
    """Initialise a new AIP_Loom project.

    Creates the full project directory tree, manifest, ledgers, distillate
    placeholder, and attempts Git initialisation.

    Parameters
    ----------
    root:
        The directory where the project will be created.  If the directory
        does not exist, it will be created.  If it exists and already
        contains a project (i.e. ``aip_loom.yaml`` exists), the init
        fails with ``PROJECT_ALREADY_EXISTS``.
    name:
        The project name.  Must be a non-empty string.
    project_type:
        The project type.  Must be one of the valid :class:`ProjectType`
        values (``"novel"``, ``"technical"``, ``"academic"``,
        ``"general"``).  Default is ``"novel"``.

    Returns
    -------
    InitResult
        Structured result with paths and warnings.

    Raises
    ------
    InitError
        If the project cannot be initialised (already exists, invalid
        type, write failure, etc.).
    """
    root = Path(root).resolve()
    created_root = False

    # 1. Validate project type
    ptype = _validate_project_type(project_type)

    # 2. Handle root directory existence
    if root.exists():
        # Check if a project already exists here
        if (root / "aip_loom.yaml").exists():
            raise InitError(
                LoomError(
                    code=PROJECT_ALREADY_EXISTS,
                    message=(
                        f"A project already exists at {root}.  "
                        "Use 'aip-loom status' to inspect it."
                    ),
                    detail={"root": str(root), "manifest": str(root / "aip_loom.yaml")},
                )
            )
        # Directory exists but no project — we can initialise here
    else:
        # Create the root directory
        try:
            root.mkdir(parents=True, exist_ok=False)
            created_root = True
        except OSError as exc:
            raise InitError(
                LoomError(
                    code=FILE_WRITE_ERROR,
                    message=f"Cannot create project directory: {root}: {exc}",
                    detail={"root": str(root), "error": str(exc)},
                )
            ) from exc

    # 3. Now that root exists, construct ProjectLayout
    try:
        layout = ProjectLayout(root=root)
    except LayoutError as exc:
        _cleanup_partial(root, created_root)
        raise InitError(
            LoomError(
                code=FILE_WRITE_ERROR,
                message=f"Cannot initialise project layout: {exc}",
                detail={"root": str(root), "error": str(exc)},
            )
        ) from exc

    # 4. Begin transaction for rollback safety
    workspace = TransactionWorkspace(layout)
    warnings: list[LoomWarning] = []

    try:
        workspace.begin()

        # Snapshot the manifest path (even though it shouldn't exist yet)
        # This allows rollback to delete it if we fail partway through
        workspace.snapshot_file(layout.manifest_path)

        # 5. Create directory structure
        dirs_to_create = [
            layout.chunks_dir,
            layout.ledgers_dir,
            layout.archive_dir,
            layout.aip_loom_dir,
            layout.staging_dir,
        ]
        for d in dirs_to_create:
            try:
                ensure_directory(d)
            except AtomicWriteError as exc:
                _rollback(workspace, root, created_root)
                raise InitError(
                    LoomError(
                        code=FILE_WRITE_ERROR,
                        message=f"Cannot create directory {d}: {exc}",
                        detail={"directory": str(d), "error": str(exc)},
                    )
                ) from exc

        # 6. Write project manifest
        try:
            manifest_yaml = _build_manifest_yaml(name, ptype)
            safe_write_text(layout.manifest_path, manifest_yaml, layout)
        except (AtomicWriteError, LayoutError) as exc:
            _rollback(workspace, root, created_root)
            raise InitError(
                LoomError(
                    code=FILE_WRITE_ERROR,
                    message=f"Cannot write project manifest: {exc}",
                    detail={"path": str(layout.manifest_path), "error": str(exc)},
                )
            ) from exc

        # 7. Write ledgers
        ledger_specs: list[tuple[Path, type]] = [
            (layout.decisions_ledger_path, DecisionLedger),
            (layout.threads_ledger_path, ThreadLedger),
            (layout.questions_ledger_path, QuestionLedger),
        ]
        for ledger_path, ledger_model in ledger_specs:
            try:
                workspace.snapshot_file(ledger_path)
                ledger_yaml = _build_empty_ledger_yaml(ledger_model)
                safe_write_text(ledger_path, ledger_yaml, layout)
            except (AtomicWriteError, LayoutError, TransactionError) as exc:
                _rollback(workspace, root, created_root)
                raise InitError(
                    LoomError(
                        code=FILE_WRITE_ERROR,
                        message=f"Cannot write ledger {ledger_path}: {exc}",
                        detail={"path": str(ledger_path), "error": str(exc)},
                    )
                ) from exc

        # 8. Write distillate (empty, no fake approved content)
        try:
            workspace.snapshot_file(layout.distillate_path)
            distillate_yaml = _build_empty_distillate_yaml()
            safe_write_text(layout.distillate_path, distillate_yaml, layout)
        except (AtomicWriteError, LayoutError, TransactionError) as exc:
            _rollback(workspace, root, created_root)
            raise InitError(
                LoomError(
                    code=FILE_WRITE_ERROR,
                    message=f"Cannot write distillate: {exc}",
                    detail={"path": str(layout.distillate_path), "error": str(exc)},
                )
            ) from exc

        # 9. Write session log
        try:
            workspace.snapshot_file(layout.sessions_path)
            sessions_yaml = _build_empty_session_log_yaml()
            safe_write_text(layout.sessions_path, sessions_yaml, layout)
        except (AtomicWriteError, LayoutError, TransactionError) as exc:
            _rollback(workspace, root, created_root)
            raise InitError(
                LoomError(
                    code=FILE_WRITE_ERROR,
                    message=f"Cannot write session log: {exc}",
                    detail={"path": str(layout.sessions_path), "error": str(exc)},
                )
            ) from exc

        # 10. Write comment log
        try:
            workspace.snapshot_file(layout.comments_path)
            comments_yaml = _build_empty_comment_log_yaml()
            safe_write_text(layout.comments_path, comments_yaml, layout)
        except (AtomicWriteError, LayoutError, TransactionError) as exc:
            _rollback(workspace, root, created_root)
            raise InitError(
                LoomError(
                    code=FILE_WRITE_ERROR,
                    message=f"Cannot write comment log: {exc}",
                    detail={"path": str(layout.comments_path), "error": str(exc)},
                )
            ) from exc

        # 11. Commit transaction
        workspace.commit()
        workspace.cleanup()

    except InitError:
        raise  # Already handled
    except TransactionError as exc:
        _cleanup_partial(root, created_root)
        raise InitError(
            LoomError(
                code=FILE_WRITE_ERROR,
                message=f"Transaction error during init: {exc}",
                detail={"tx_id": workspace.tx_id, "error": str(exc)},
            )
        ) from exc
    except Exception as exc:
        _cleanup_partial(root, created_root)
        raise InitError(
            LoomError(
                code=FILE_WRITE_ERROR,
                message=f"Unexpected error during init: {exc}",
                detail={"root": str(root), "error": str(exc)},
            )
        ) from exc

    # 12. Best-effort Git initialisation (non-fatal)
    git_initialized = False
    git_commit_created = False

    try:
        from .git import _run_git  # noqa: F401 — avoid pulling if not needed

        if not is_git_repo(root):
            try:
                from .git import _find_git_binary

                git_bin = _find_git_binary()
                import subprocess

                subprocess.run(
                    [git_bin, "init", str(root)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                git_initialized = True
            except (GitError, subprocess.CalledProcessError, OSError):
                pass

        if git_initialized:
            try:
                configure_local_git(root)
                # Add all project files and create initial commit
                project_files = [
                    layout.manifest_path,
                    layout.distillate_path,
                    layout.sessions_path,
                    layout.comments_path,
                    layout.decisions_ledger_path,
                    layout.threads_ledger_path,
                    layout.questions_ledger_path,
                ]
                # Add directories too (chunks/, archive/, .aip-loom/)
                git_add(root, project_files)
                # Also add the directories themselves (they may contain no files)
                try:
                    git_add(root, [layout.chunks_dir, layout.archive_dir])
                except GitError:
                    pass  # Best-effort
                git_commit(root, "Initial AIP_Loom project", allow_empty=True)
                git_commit_created = True
            except GitError:
                pass  # Non-fatal

    except Exception:
        pass  # Any Git error is non-fatal

    if not git_initialized:
        warnings.append(
            LoomWarning(
                code=GIT_INIT_SKIPPED,
                message=(
                    "Git was not initialised for this project.  "
                    "You can initialise Git later with 'git init'."
                ),
                detail={"root": str(root)},
            )
        )

    return InitResult(
        root=root,
        git_initialized=git_initialized,
        git_commit_created=git_commit_created,
        warnings=tuple(warnings),
    )


def _rollback(
    workspace: TransactionWorkspace,
    root: Path,
    created_root: bool,
) -> None:
    """Attempt to roll back a failed init.

    Tries to restore any files that were written, then cleans up the
    transaction workspace.  Finally, removes the root directory if we
    created it.
    """
    try:
        workspace.restore()
    except TransactionError:
        pass  # Best-effort rollback
    try:
        workspace.cleanup()
    except TransactionError:
        pass
    _cleanup_partial(root, created_root)
