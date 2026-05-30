"""Exclusive locking for AIP_Loom project operations.

This module is the **single authority** for acquiring and releasing
exclusive locks on AIP_Loom projects.  No other module may implement
its own locking mechanism — it must use :class:`ProjectLock` here.

Design principles (BuildSpec §6.1–6.2 and §3A):

- **Exclusive create semantics**: Lock acquisition uses ``O_CREAT | O_EXCL``
  to atomically create the lock file.  If the file already exists, the
  lock is held and acquisition fails with ``LOCK_HELD``.
- **PID liveness check**: When a lock file exists, the module reads the
  PID from it and checks whether the process is still alive.  On POSIX,
  this uses ``os.kill(pid, 0)``.  On other platforms, liveness is
  uncertain and the lock is treated as potentially held.
- **Stale lock detection**: If the lock-holding process is dead, the
  lock is stale.  Stale lock detection includes the PID, age of the
  lock file, the command that held it, and recovery instructions.
- **No silent lock deletion**: A stale lock is never deleted
  automatically.  The caller must explicitly call :meth:`force_release`
  after reviewing diagnostics.  This prevents accidental corruption
  from race conditions.
- **Context manager**: :class:`ProjectLock` supports the ``with``
  statement for automatic release.
- **Lock file format**: The lock file is a single-line text file
  containing ``<pid>:<command>`` (e.g. ``12345:reconcile``).
"""

from __future__ import annotations

import os
import platform
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from .errors import (
    LOCK_ACQUIRE_FAILED,
    LOCK_HELD,
    LOCK_STALE,
    LoomError,
    LoomWarning,
    STALE_LOCK_DETECTED,
)
from .layout import LayoutError, ProjectLayout

__all__ = [
    "LockError",
    "LockInfo",
    "ProjectLock",
    "acquire_lock",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LockError(Exception):
    """Raised when a lock operation fails.

    Carries a :class:`LoomError` with a stable error code.
    """

    def __init__(self, loom_error: LoomError) -> None:
        self.loom_error = loom_error
        super().__init__(loom_error.message)


# ---------------------------------------------------------------------------
# Lock info
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LockInfo:
    """Information parsed from an existing lock file.

    Attributes
    ----------
    pid:
        The PID of the process that acquired the lock.
    command:
        The command name that acquired the lock (e.g. ``"reconcile"``).
    acquired_at:
        The modification time of the lock file (as a Unix timestamp).
    is_alive:
        Whether the process with *pid* is still alive.
        ``None`` means liveness could not be determined.
    """

    pid: int
    command: str
    acquired_at: float
    is_alive: bool | None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_pid_liveness(pid: int) -> bool | None:
    """Check whether a process with the given PID is alive.

    On POSIX systems, uses ``os.kill(pid, 0)`` which returns True if
    the process exists and we have permission to signal it.  On other
    platforms, returns ``None`` (uncertain).

    Returns
    -------
    bool | None
        ``True`` if the process is alive, ``False`` if it is definitely
        dead, ``None`` if liveness cannot be determined.
    """
    if platform.system() == "Windows":
        # Windows does not support os.kill(pid, 0) in the same way.
        # Return None to indicate uncertainty.
        return None

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't have permission to signal it.
        # It's alive.
        return True
    except OSError:
        # Unexpected error — treat as uncertain.
        return None


def _read_lock_file(lock_path: Path) -> LockInfo | None:
    """Read and parse a lock file.

    Returns ``None`` if the file does not exist or cannot be parsed.

    The lock file format is ``<pid>:<command>`` on a single line.
    """
    if not lock_path.exists():
        return None

    try:
        content = lock_path.read_text(encoding="utf-8").strip()
        mtime = lock_path.stat().st_mtime
    except OSError:
        return None

    parts = content.split(":", 1)
    if len(parts) != 2:
        return None

    try:
        pid = int(parts[0].strip())
    except ValueError:
        return None

    command = parts[1].strip()
    is_alive = _check_pid_liveness(pid)

    return LockInfo(
        pid=pid,
        command=command,
        acquired_at=mtime,
        is_alive=is_alive,
    )


def _write_lock_file(lock_path: Path, command: str) -> None:
    """Write the current PID and command to the lock file.

    Uses ``O_CREAT | O_EXCL`` for atomic creation.  If the file
    already exists, raises :class:`LockError` with ``LOCK_HELD``.
    """
    content = f"{os.getpid()}:{command}\n"
    data = content.encode("utf-8")

    try:
        fd = os.open(
            str(lock_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o644,
        )
    except FileExistsError:
        # Lock is already held — read info for diagnostics
        info = _read_lock_file(lock_path)
        if info is not None:
            age = time.time() - info.acquired_at
            liveness = "alive" if info.is_alive else (
                "dead" if info.is_alive is False else "unknown"
            )
            message = (
                f"Lock is held by PID {info.pid} "
                f"(command: {info.command!r}, "
                f"age: {age:.0f}s, "
                f"process: {liveness}). "
                f"If the process is dead, run 'aip-loom lock release --force' "
                f"to remove the stale lock."
            )
            code = LOCK_STALE if info.is_alive is False else LOCK_HELD
        else:
            message = "Lock is held but lock file could not be read."
            code = LOCK_HELD

        raise LockError(
            LoomError(
                code=code,
                message=message,
                detail={
                    "lock_path": str(lock_path),
                    "existing_info": (
                        {"pid": info.pid, "command": info.command, "is_alive": info.is_alive}
                        if info
                        else None
                    ),
                },
            )
        )
    except OSError as exc:
        raise LockError(
            LoomError(
                code=LOCK_ACQUIRE_FAILED,
                message=f"Cannot create lock file: {exc}",
                detail={"lock_path": str(lock_path), "error": str(exc)},
            )
        ) from exc

    try:
        os.write(fd, data)
        os.fsync(fd)
    except OSError as exc:
        # Clean up the partially-written lock file
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            lock_path.unlink()
        except OSError:
            pass
        raise LockError(
            LoomError(
                code=LOCK_ACQUIRE_FAILED,
                message=f"Cannot write to lock file: {exc}",
                detail={"lock_path": str(lock_path), "error": str(exc)},
            )
        ) from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# ProjectLock class
# ---------------------------------------------------------------------------


class ProjectLock:
    """Exclusive lock for an AIP_Loom project.

    This class implements the lock protocol described in BuildSpec §6:

    - Acquire uses ``O_CREAT | O_EXCL`` for atomic creation.
    - If the lock file exists, reads PID and checks liveness.
    - Stale locks (dead PID) are reported with full diagnostics.
    - Release deletes the lock file.
    - Supports ``with`` statement for automatic release.

    Usage::

        layout = ProjectLayout(root=project_root)
        with ProjectLock(layout, command="reconcile") as lock:
            # ... do exclusive work ...
            pass
        # Lock is automatically released

    Attributes
    ----------
    layout:
        The project layout (provides the lock file path).
    command:
        The command name that is acquiring the lock (for diagnostics).
    """

    def __init__(self, layout: ProjectLayout, command: str = "unknown") -> None:
        self._layout = layout
        self._command = command
        self._held = False

    @property
    def layout(self) -> ProjectLayout:
        """The project layout."""
        return self._layout

    @property
    def command(self) -> str:
        """The command name that acquired the lock."""
        return self._command

    @property
    def is_held(self) -> bool:
        """Whether this instance currently holds the lock."""
        return self._held

    def acquire(self) -> list[LoomWarning]:
        """Acquire the exclusive lock.

        Returns
        -------
        list[LoomWarning]
            Any warnings generated during acquisition (e.g. stale lock
            detection).

        Raises
        ------
        LockError
            If the lock cannot be acquired (held by another process,
            or filesystem error).
        LayoutError
            If the ``.aip-loom`` directory cannot be created.
        """
        warnings: list[LoomWarning] = []

        # Ensure the .aip-loom directory exists
        self._layout.aip_loom_dir.mkdir(parents=True, exist_ok=True)

        lock_path = self._layout.lock_path

        # Check for an existing lock before trying to create
        if lock_path.exists():
            info = _read_lock_file(lock_path)
            if info is not None:
                if info.is_alive is False:
                    # Stale lock detected — report but still fail
                    age = time.time() - info.acquired_at
                    warnings.append(
                        LoomWarning(
                            code=STALE_LOCK_DETECTED,
                            message=(
                                f"Stale lock detected: PID {info.pid} "
                                f"(command: {info.command!r}) is dead. "
                                f"Lock age: {age:.0f}s. "
                                f"Run 'aip-loom lock release --force' to remove."
                            ),
                            detail={
                                "pid": info.pid,
                                "command": info.command,
                                "age_seconds": age,
                            },
                        )
                    )
                elif info.is_alive is None:
                    # Uncertain liveness — treat as held
                    pass
                # If alive, lock is genuinely held

        # Attempt atomic creation
        _write_lock_file(lock_path, self._command)
        self._held = True
        return warnings

    def release(self) -> None:
        """Release the exclusive lock by deleting the lock file.

        Raises
        ------
        LockError
            If the lock file cannot be deleted.
        """
        if not self._held:
            return

        lock_path = self._layout.lock_path
        try:
            if lock_path.exists():
                lock_path.unlink()
        except OSError as exc:
            raise LockError(
                LoomError(
                    code=LOCK_ACQUIRE_FAILED,
                    message=f"Cannot release lock file: {exc}",
                    detail={"lock_path": str(lock_path), "error": str(exc)},
                )
            ) from exc
        finally:
            self._held = False

    def force_release(self) -> LockInfo | None:
        """Force-release a stale lock, returning the previous lock info.

        This method deletes the lock file regardless of whether the
        holding process is alive.  It should only be called after
        reviewing diagnostics (PID, command, liveness).

        Returns
        -------
        LockInfo | None
            Information about the previous lock holder, or ``None``
            if the lock file could not be read.

        Raises
        ------
        LockError
            If the lock file cannot be deleted.
        """
        lock_path = self._layout.lock_path
        info = _read_lock_file(lock_path)
        try:
            if lock_path.exists():
                lock_path.unlink()
        except OSError as exc:
            raise LockError(
                LoomError(
                    code=LOCK_ACQUIRE_FAILED,
                    message=f"Cannot force-release lock file: {exc}",
                    detail={"lock_path": str(lock_path), "error": str(exc)},
                )
            ) from exc
        self._held = False
        return info

    def read_lock_info(self) -> LockInfo | None:
        """Read information from the current lock file without acquiring.

        Returns
        -------
        LockInfo | None
            Lock information, or ``None`` if no lock file exists.
        """
        return _read_lock_file(self._layout.lock_path)

    # -- context manager ----------------------------------------------------

    def __enter__(self) -> "ProjectLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[override]
        self.release()


# ---------------------------------------------------------------------------
# Convenience context manager
# ---------------------------------------------------------------------------


@contextmanager
def acquire_lock(
    layout: ProjectLayout,
    command: str = "unknown",
) -> Generator[ProjectLock, None, None]:
    """Context manager to acquire and automatically release a project lock.

    Usage::

        with acquire_lock(layout, command="reconcile") as lock:
            # ... exclusive work ...
            pass

    Parameters
    ----------
    layout:
        The project layout.
    command:
        The command name for lock diagnostics.

    Yields
    ------
    ProjectLock
        The acquired lock.

    Raises
    ------
    LockError
        If the lock cannot be acquired.
    """
    lock = ProjectLock(layout, command=command)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()
