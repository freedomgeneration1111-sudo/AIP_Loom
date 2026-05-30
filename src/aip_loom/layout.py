"""Canonical project filesystem layout for AIP_Loom.

This module is the **single authority** for resolving every canonical
path in an AIP_Loom project.  No other module may construct paths using
f-strings, ``pathlib`` composition from IDs, or any other ad-hoc method.
All path resolution must go through :class:`ProjectLayout`.

Design principles (BuildSpec §5 and §3A):

- **Single owner of paths**: Every canonical file and directory in an
  AIP_Loom project has exactly one resolution point here.  Later modules
  must never build paths directly from model-provided strings.
- **ID validation before path construction**: Methods like
  :meth:`chunk_path` validate the chunk ID against the schema regex
  before building a path.  This prevents path traversal or injection
  through malformed IDs.
- **Path safety**: :meth:`validate_path` ensures that any resolved path
  remains within the project root.  It rejects symlinks that escape the
  root, path components with ``..``, and other traversal attempts.
- **Frozen**: ``ProjectLayout`` instances are immutable after construction.
- **Root must exist**: The project root directory must exist at
  construction time.  A non-existent root raises :class:`LayoutError`
  with ``PROJECT_NOT_FOUND``.

Directory layout (BuildSpec §5)::

    <project_root>/
    ├── aip_loom.yaml          # Project manifest
    ├── chunks/                # Chunk Markdown files (C-NNNN.md)
    ├── ledgers/
    │   ├── decisions.yaml     # Decision ledger
    │   ├── threads.yaml       # Thread/strand ledger
    │   └── questions.yaml     # Question/open-issue ledger
    ├── distillate.yaml        # Structural index
    ├── sessions.yaml          # Session log
    ├── comments.yaml          # Review comments
    ├── archive/               # Archived chunks (deprecated state)
    └── .aip-loom/
        ├── lock               # Exclusive lock file
        └── staging/           # Staging area for reconcile
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import (
    CHUNK_ID_INVALID,
    PATH_UNSAFE,
    PROJECT_NOT_FOUND,
    LoomError,
)
from .schemas import _CHUNK_ID_RE

__all__ = [
    "LayoutError",
    "ProjectLayout",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LayoutError(Exception):
    """Raised when a layout or path-safety check fails.

    Carries a :class:`LoomError` with a stable error code.
    """

    def __init__(self, loom_error: LoomError) -> None:
        self.loom_error = loom_error
        super().__init__(loom_error.message)


# ---------------------------------------------------------------------------
# ProjectLayout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectLayout:
    """The single source of truth for all canonical paths in a project.

    Every file and directory that AIP_Loom manages is resolved through
    this class.  No other module may construct paths from IDs, filenames,
    or model-provided strings.

    Attributes
    ----------
    root:
        The absolute path to the project root directory.
    """

    root: Path

    def __post_init__(self) -> None:
        """Validate and normalize the root path."""
        # Normalize to absolute path
        root = Path(self.root).resolve()
        object.__setattr__(self, "root", root)

        if not root.is_dir():
            raise LayoutError(
                LoomError(
                    code=PROJECT_NOT_FOUND,
                    message=f"Project root does not exist or is not a directory: {root}",
                    detail={"root": str(root)},
                )
            )

    # -- top-level files ----------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        """Path to the project manifest (``aip_loom.yaml``)."""
        return self.root / "aip_loom.yaml"

    @property
    def distillate_path(self) -> Path:
        """Path to the distillate file (``distillate.yaml``)."""
        return self.root / "distillate.yaml"

    @property
    def sessions_path(self) -> Path:
        """Path to the session log (``sessions.yaml``)."""
        return self.root / "sessions.yaml"

    @property
    def comments_path(self) -> Path:
        """Path to the comments log (``comments.yaml``)."""
        return self.root / "comments.yaml"

    # -- directories --------------------------------------------------------

    @property
    def chunks_dir(self) -> Path:
        """Path to the chunks directory."""
        return self.root / "chunks"

    @property
    def ledgers_dir(self) -> Path:
        """Path to the ledgers directory."""
        return self.root / "ledgers"

    @property
    def archive_dir(self) -> Path:
        """Path to the archive directory."""
        return self.root / "archive"

    @property
    def aip_loom_dir(self) -> Path:
        """Path to the ``.aip-loom`` internal directory."""
        return self.root / ".aip-loom"

    @property
    def staging_dir(self) -> Path:
        """Path to the staging directory inside ``.aip-loom``."""
        return self.aip_loom_dir / "staging"

    # -- ledger files -------------------------------------------------------

    @property
    def decisions_ledger_path(self) -> Path:
        """Path to the decisions ledger."""
        return self.ledgers_dir / "decisions.yaml"

    @property
    def threads_ledger_path(self) -> Path:
        """Path to the threads/strands ledger."""
        return self.ledgers_dir / "threads.yaml"

    @property
    def questions_ledger_path(self) -> Path:
        """Path to the questions/open-issues ledger."""
        return self.ledgers_dir / "questions.yaml"

    # -- lock file ----------------------------------------------------------

    @property
    def lock_path(self) -> Path:
        """Path to the exclusive lock file."""
        return self.aip_loom_dir / "lock"

    # -- chunk paths --------------------------------------------------------

    def chunk_path(self, chunk_id: str) -> Path:
        """Resolve the path for a chunk Markdown file.

        The *chunk_id* is validated against the canonical ID regex
        (``_CHUNK_ID_RE``) before path construction.  This prevents path
        traversal through malformed IDs (e.g. ``../../../etc/passwd``).

        Parameters
        ----------
        chunk_id:
            A valid chunk ID like ``"C-0001"`` or ``"CH-0012"``.

        Returns
        -------
        Path
            The absolute path to the chunk file (e.g.
            ``<root>/chunks/C-0001.md``).

        Raises
        ------
        LayoutError
            If *chunk_id* does not match the canonical pattern, or if
            the resolved path escapes the project root.
        """
        if not _CHUNK_ID_RE.match(chunk_id):
            raise LayoutError(
                LoomError(
                    code=CHUNK_ID_INVALID,
                    message=(
                        f"Invalid chunk ID {chunk_id!r}: "
                        "must match pattern like 'C-0001' before path construction"
                    ),
                    detail={"chunk_id": chunk_id},
                )
            )

        path = self.chunks_dir / f"{chunk_id}.md"
        self.validate_path(path)
        return path

    def archive_chunk_path(self, chunk_id: str) -> Path:
        """Resolve the path for an archived chunk file.

        Parameters
        ----------
        chunk_id:
            A valid chunk ID like ``"C-0001"``.

        Returns
        -------
        Path
            The absolute path to the archived chunk file.

        Raises
        ------
        LayoutError
            If *chunk_id* does not match the canonical pattern, or if
            the resolved path escapes the project root.
        """
        if not _CHUNK_ID_RE.match(chunk_id):
            raise LayoutError(
                LoomError(
                    code=CHUNK_ID_INVALID,
                    message=(
                        f"Invalid chunk ID {chunk_id!r}: "
                        "must match pattern like 'C-0001' before path construction"
                    ),
                    detail={"chunk_id": chunk_id},
                )
            )

        path = self.archive_dir / f"{chunk_id}.md"
        self.validate_path(path)
        return path

    # -- path safety --------------------------------------------------------

    def validate_path(self, path: Path) -> Path:
        """Validate that a path is safely within the project root.

        This method checks:

        1. The resolved (absolute, symlink-followed) path must start
           with the project root.
        2. No path component may be ``..`` (before resolution).
        3. If the path is a symlink, its target must also be within
           the project root.

        Parameters
        ----------
        path:
            The path to validate.  May be relative (resolved against
            the project root) or absolute.

        Returns
        -------
        Path
            The validated absolute path.

        Raises
        ------
        LayoutError
            If the path escapes the project root or violates safety rules.
        """
        # Check for .. components in the original path
        parts = Path(path).parts
        if ".." in parts:
            raise LayoutError(
                LoomError(
                    code=PATH_UNSAFE,
                    message=f"Path contains '..' component: {path}",
                    detail={"path": str(path), "reason": "dot_dot_component"},
                )
            )

        # Resolve to absolute path
        resolved = Path(path).resolve()

        # Check that the resolved path is within the project root
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise LayoutError(
                LoomError(
                    code=PATH_UNSAFE,
                    message=(
                        f"Resolved path escapes project root: {resolved} "
                        f"is not under {self.root}"
                    ),
                    detail={
                        "path": str(path),
                        "resolved": str(resolved),
                        "root": str(self.root),
                        "reason": "path_escape",
                    },
                )
            )

        # If the path is a symlink, check its target is also within root
        if resolved.is_symlink():
            target = os.readlink(resolved)
            target_resolved = (resolved.parent / target).resolve()
            try:
                target_resolved.relative_to(self.root)
            except ValueError:
                raise LayoutError(
                    LoomError(
                        code=PATH_UNSAFE,
                        message=(
                            f"Symlink target escapes project root: "
                            f"{resolved} -> {target_resolved}"
                        ),
                        detail={
                            "path": str(path),
                            "symlink": str(resolved),
                            "target": str(target_resolved),
                            "root": str(self.root),
                            "reason": "symlink_escape",
                        },
                    )
                )

        return resolved

    # -- convenience --------------------------------------------------------

    def is_project_initialized(self) -> bool:
        """Check whether the project root has the minimum required files.

        A project is considered initialized if the manifest file exists.
        """
        return self.manifest_path.is_file()
