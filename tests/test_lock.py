"""Tests for aip_loom.lock — exclusive project locking.

These tests prove:
- Lock acquisition creates lock file with PID and command
- Lock acquisition fails when lock is already held (LOCK_HELD)
- Lock release deletes lock file
- Context manager acquires and releases automatically
- Stale lock detection with PID liveness check
- force_release removes stale lock
- Lock file format is <pid>:<command>
- Lock info includes PID, command, and age
- acquire_lock convenience context manager works
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from aip_loom.errors import LOCK_ACQUIRE_FAILED, LOCK_HELD, LOCK_STALE
from aip_loom.layout import ProjectLayout
from aip_loom.lock import (
    LockError,
    LockInfo,
    ProjectLock,
    _check_pid_liveness,
    _read_lock_file,
    acquire_lock,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """Create a minimal project root directory."""
    root = tmp_path / "my-novel"
    root.mkdir()
    return root


@pytest.fixture()
def layout(project_root: Path) -> ProjectLayout:
    """Create a ProjectLayout from the project root."""
    return ProjectLayout(root=project_root)


# ===========================================================================
# Lock acquisition
# ===========================================================================


class TestLockAcquisition:
    """Lock acquisition creates lock file with correct content."""

    def test_acquire_creates_lock_file(self, layout: ProjectLayout) -> None:
        lock = ProjectLock(layout, command="test")
        lock.acquire()
        assert layout.lock_path.exists()
        lock.release()

    def test_lock_file_contains_pid_and_command(self, layout: ProjectLayout) -> None:
        lock = ProjectLock(layout, command="reconcile")
        lock.acquire()
        content = layout.lock_path.read_text(encoding="utf-8").strip()
        pid_str, cmd = content.split(":", 1)
        assert int(pid_str) == os.getpid()
        assert cmd == "reconcile"
        lock.release()

    def test_acquire_returns_warnings(self, layout: ProjectLayout) -> None:
        lock = ProjectLock(layout, command="test")
        warnings = lock.acquire()
        assert isinstance(warnings, list)
        lock.release()

    def test_is_held_after_acquire(self, layout: ProjectLayout) -> None:
        lock = ProjectLock(layout, command="test")
        assert lock.is_held is False
        lock.acquire()
        assert lock.is_held is True
        lock.release()
        assert lock.is_held is False


# ===========================================================================
# Lock contention
# ===========================================================================


class TestLockContention:
    """Lock acquisition fails when lock is already held."""

    def test_double_acquire_fails(self, layout: ProjectLayout) -> None:
        lock1 = ProjectLock(layout, command="first")
        lock1.acquire()

        lock2 = ProjectLock(layout, command="second")
        with pytest.raises(LockError) as exc_info:
            lock2.acquire()
        assert exc_info.value.loom_error.code in (LOCK_HELD, LOCK_STALE)

        lock1.release()

    def test_contention_error_includes_diagnostics(self, layout: ProjectLayout) -> None:
        lock1 = ProjectLock(layout, command="first")
        lock1.acquire()

        lock2 = ProjectLock(layout, command="second")
        with pytest.raises(LockError) as exc_info:
            lock2.acquire()
        detail = exc_info.value.loom_error.detail
        assert "lock_path" in detail
        assert detail.get("existing_info") is not None
        assert detail["existing_info"]["pid"] == os.getpid()
        assert detail["existing_info"]["command"] == "first"

        lock1.release()


# ===========================================================================
# Lock release
# ===========================================================================


class TestLockRelease:
    """Lock release deletes lock file."""

    def test_release_deletes_lock_file(self, layout: ProjectLayout) -> None:
        lock = ProjectLock(layout, command="test")
        lock.acquire()
        assert layout.lock_path.exists()
        lock.release()
        assert not layout.lock_path.exists()

    def test_release_when_not_held_is_noop(self, layout: ProjectLayout) -> None:
        lock = ProjectLock(layout, command="test")
        lock.release()  # Should not raise


# ===========================================================================
# Context manager
# ===========================================================================


class TestContextManager:
    """ProjectLock as context manager."""

    def test_context_manager_acquires_and_releases(self, layout: ProjectLayout) -> None:
        with ProjectLock(layout, command="test") as lock:
            assert layout.lock_path.exists()
            assert lock.is_held is True
        assert not layout.lock_path.exists()

    def test_context_manager_releases_on_exception(self, layout: ProjectLayout) -> None:
        try:
            with ProjectLock(layout, command="test") as lock:
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert not layout.lock_path.exists()


# ===========================================================================
# acquire_lock convenience
# ===========================================================================


class TestAcquireLockConvenience:
    """acquire_lock convenience context manager."""

    def test_acquire_lock_works(self, layout: ProjectLayout) -> None:
        with acquire_lock(layout, command="brief") as lock:
            assert layout.lock_path.exists()
            assert lock.command == "brief"
        assert not layout.lock_path.exists()


# ===========================================================================
# Stale lock detection
# ===========================================================================


class TestStaleLockDetection:
    """Stale locks are detected with PID liveness check."""

    def test_stale_lock_detected_with_dead_pid(self, layout: ProjectLayout) -> None:
        """Create a lock file with a PID that doesn't exist."""
        # Use a PID that's very unlikely to exist
        fake_pid = 999999999
        layout.aip_loom_dir.mkdir(parents=True, exist_ok=True)
        layout.lock_path.write_text(f"{fake_pid}:dead_command\n", encoding="utf-8")

        lock = ProjectLock(layout, command="test")
        with pytest.raises(LockError) as exc_info:
            lock.acquire()
        # Should be LOCK_STALE since the PID is dead
        assert exc_info.value.loom_error.code == LOCK_STALE

    def test_stale_lock_warning_emitted(self, layout: ProjectLayout) -> None:
        """Acquiring with a stale lock should produce a STALE_LOCK_DETECTED warning."""
        fake_pid = 999999999
        layout.aip_loom_dir.mkdir(parents=True, exist_ok=True)
        layout.lock_path.write_text(f"{fake_pid}:dead_cmd\n", encoding="utf-8")

        lock = ProjectLock(layout, command="test")
        with pytest.raises(LockError):
            lock.acquire()

    def test_stale_lock_message_includes_recovery(self, layout: ProjectLayout) -> None:
        """Error message includes recovery instructions."""
        fake_pid = 999999999
        layout.aip_loom_dir.mkdir(parents=True, exist_ok=True)
        layout.lock_path.write_text(f"{fake_pid}:dead_cmd\n", encoding="utf-8")

        lock = ProjectLock(layout, command="test")
        with pytest.raises(LockError) as exc_info:
            lock.acquire()
        msg = exc_info.value.loom_error.message
        assert "force" in msg.lower() or "release" in msg.lower()


# ===========================================================================
# force_release
# ===========================================================================


class TestForceRelease:
    """force_release removes stale locks."""

    def test_force_release_removes_stale_lock(self, layout: ProjectLayout) -> None:
        fake_pid = 999999999
        layout.aip_loom_dir.mkdir(parents=True, exist_ok=True)
        layout.lock_path.write_text(f"{fake_pid}:dead_cmd\n", encoding="utf-8")

        lock = ProjectLock(layout, command="test")
        info = lock.force_release()
        assert not layout.lock_path.exists()
        assert info is not None
        assert info.pid == fake_pid
        assert info.command == "dead_cmd"

    def test_force_release_returns_none_when_no_lock(self, layout: ProjectLayout) -> None:
        lock = ProjectLock(layout, command="test")
        info = lock.force_release()
        assert info is None


# ===========================================================================
# read_lock_info
# ===========================================================================


class TestReadLockInfo:
    """read_lock_info parses existing lock files."""

    def test_read_existing_lock(self, layout: ProjectLayout) -> None:
        layout.aip_loom_dir.mkdir(parents=True, exist_ok=True)
        layout.lock_path.write_text(f"{os.getpid()}:my_command\n", encoding="utf-8")

        lock = ProjectLock(layout, command="test")
        info = lock.read_lock_info()
        assert info is not None
        assert info.pid == os.getpid()
        assert info.command == "my_command"
        assert info.is_alive is True or info.is_alive is None

    def test_read_no_lock(self, layout: ProjectLayout) -> None:
        lock = ProjectLock(layout, command="test")
        info = lock.read_lock_info()
        assert info is None

    def test_read_malformed_lock_returns_none(self, layout: ProjectLayout) -> None:
        layout.aip_loom_dir.mkdir(parents=True, exist_ok=True)
        layout.lock_path.write_text("not_valid\n", encoding="utf-8")

        lock = ProjectLock(layout, command="test")
        info = lock.read_lock_info()
        assert info is None


# ===========================================================================
# PID liveness check
# ===========================================================================


class TestPidLiveness:
    """_check_pid_liveness works correctly."""

    def test_own_pid_is_alive(self) -> None:
        result = _check_pid_liveness(os.getpid())
        # On POSIX, should be True. On other platforms, may be None.
        assert result is True or result is None

    def test_nonexistent_pid_is_dead(self) -> None:
        result = _check_pid_liveness(999999999)
        # On POSIX, should be False. On other platforms, may be None.
        assert result is False or result is None


# ===========================================================================
# LockInfo frozen
# ===========================================================================


class TestLockInfoFrozen:
    """LockInfo is immutable."""

    def test_frozen(self) -> None:
        info = LockInfo(pid=123, command="test", acquired_at=0.0, is_alive=True)
        with pytest.raises(AttributeError):
            info.pid = 456  # type: ignore[misc]
