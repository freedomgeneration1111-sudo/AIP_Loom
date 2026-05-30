"""Atomic file write and path-safety helpers for AIP_Loom.

This module is the **single authority** for all file writing in AIP_Loom.
No other module may perform direct file writes — it must use
:func:`atomic_write` or :func:`safe_write_text` here.

Design principles (BuildSpec §5 and §3A):

- **Atomic writes**: All file writes go through a write-to-temp +
  fsync + ``os.replace`` sequence.  This ensures that the target file
  is never in a partially-written state — either the old content is
  intact, or the new content is fully in place.
- **Same-directory temp files**: The temp file is created in the same
  directory as the target to ensure that ``os.replace`` is atomic
  (same filesystem).  A cross-filesystem rename is not atomic.
- **fsync discipline**: Both the temp file and its parent directory
  are fsynced before the rename.  This ensures durability even if the
  process crashes immediately after ``os.replace`` returns.
- **Cleanup on failure**: If writing or replacing fails, the temp file
  is cleaned up.  The original file (if any) is left untouched.
- **Path safety**: All writes validate that the target path is within
  the project root via :mod:`aip_loom.layout`.  No write may escape
  the project root.
- **Encoding**: All text writes use UTF-8 with no BOM.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Generator, Iterator

from .errors import FILE_WRITE_ERROR, PATH_UNSAFE, LoomError
from .layout import LayoutError, ProjectLayout

__all__ = [
    "AtomicWriteError",
    "atomic_write",
    "safe_write_text",
    "safe_write_bytes",
    "ensure_directory",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AtomicWriteError(Exception):
    """Raised when an atomic write operation fails.

    Carries a :class:`LoomError` with a stable error code.
    """

    def __init__(self, loom_error: LoomError) -> None:
        self.loom_error = loom_error
        super().__init__(loom_error.message)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync on the parent directory.

    This is necessary on POSIX systems to ensure that directory entries
    (file creations, renames) are durable.  On some platforms or
    filesystems this may fail silently, which is acceptable — the
    important thing is that we tried.
    """
    fd = None
    try:
        fd = os.open(str(path), os.O_RDONLY)
        os.fsync(fd)
    except OSError:
        # Best-effort: some filesystems (e.g. network mounts) may not
        # support directory fsync.  We do not fail the write for this.
        pass
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Public API — atomic_write context manager
# ---------------------------------------------------------------------------


@contextmanager
def atomic_write(
    target: Path,
    layout: ProjectLayout,
) -> Iterator[Path]:
    """Context manager for atomic file writes.

    Creates a temporary file in the same directory as *target*, yields
    its path for writing, then atomically replaces *target* with the
    temp file on success.

    Usage::

        with atomic_write(target_path, layout) as tmp:
            tmp.write_text("new content", encoding="utf-8")
        # target_path now contains "new content"

    If the ``with`` block raises an exception, the temp file is cleaned
    up and *target* is left untouched.

    Parameters
    ----------
    target:
        The final target path.  Must be within the project root.
    layout:
        The :class:`ProjectLayout` for path-safety validation.

    Yields
    ------
    Path
        The path to the temporary file.  Write to this path inside
        the ``with`` block.

    Raises
    ------
    LayoutError
        If *target* is not within the project root.
    AtomicWriteError
        If the temp file cannot be created, or the rename fails.
    """
    # Validate path safety first
    layout.validate_path(target)

    # Ensure the parent directory exists
    target = Path(target).resolve()
    ensure_directory(target.parent)

    # Create temp file in same directory (same filesystem → atomic rename)
    tmp_fd: int | None = None
    tmp_path: Path | None = None

    try:
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=".aip-loom-tmp-",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        os.close(tmp_fd)
        tmp_fd = None

        yield tmp_path

        # Fsync the temp file to ensure data is on disk
        with open(tmp_path, "r+b") as fh:
            fh.flush()
            os.fsync(fh.fileno())

        # Fsync the directory to ensure the rename is durable
        _fsync_dir(target.parent)

        # Atomic replace
        os.replace(str(tmp_path), str(target))

        # Fsync the directory again after the rename
        _fsync_dir(target.parent)

        tmp_path = None  # Prevent cleanup — file was successfully replaced

    except Exception as exc:
        # Clean up the temp file if it still exists
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

        if isinstance(exc, (LayoutError, AtomicWriteError)):
            raise

        raise AtomicWriteError(
            LoomError(
                code=FILE_WRITE_ERROR,
                message=f"Atomic write failed for {target}: {exc}",
                detail={"target": str(target), "error": str(exc)},
            )
        ) from exc

    finally:
        # Ensure the temp file descriptor is closed
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Public API — convenience write functions
# ---------------------------------------------------------------------------


def safe_write_text(
    target: Path,
    content: str,
    layout: ProjectLayout,
) -> None:
    """Atomically write UTF-8 text to a file.

    Parameters
    ----------
    target:
        The target file path.  Must be within the project root.
    content:
        The text content to write.
    layout:
        The :class:`ProjectLayout` for path-safety validation.

    Raises
    ------
    LayoutError
        If *target* is not within the project root.
    AtomicWriteError
        If the write fails.
    """
    with atomic_write(target, layout) as tmp:
        tmp.write_text(content, encoding="utf-8")


def safe_write_bytes(
    target: Path,
    content: bytes,
    layout: ProjectLayout,
) -> None:
    """Atomically write binary data to a file.

    Parameters
    ----------
    target:
        The target file path.  Must be within the project root.
    content:
        The binary content to write.
    layout:
        The :class:`ProjectLayout` for path-safety validation.

    Raises
    ------
    LayoutError
        If *target* is not within the project root.
    AtomicWriteError
        If the write fails.
    """
    with atomic_write(target, layout) as tmp:
        tmp.write_bytes(content)


# ---------------------------------------------------------------------------
# Public API — directory helpers
# ---------------------------------------------------------------------------


def ensure_directory(path: Path) -> None:
    """Ensure a directory exists, creating it (and parents) if needed.

    Parameters
    ----------
    path:
        The directory path to ensure.

    Raises
    ------
    AtomicWriteError
        If the directory cannot be created.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AtomicWriteError(
            LoomError(
                code=FILE_WRITE_ERROR,
                message=f"Cannot create directory: {path}",
                detail={"path": str(path), "error": str(exc)},
            )
        ) from exc
