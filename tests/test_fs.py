"""Tests for aip_loom.fs — atomic write and path safety.

These tests prove:
- atomic_write creates target file with correct content on success
- atomic_write leaves original file intact on failure
- atomic_write cleans up temp file on failure
- safe_write_text writes UTF-8 content atomically
- safe_write_bytes writes binary content atomically
- atomic_write rejects paths outside project root
- ensure_directory creates directories
- atomic_write overwrites existing file
- Temp file is in the same directory as target
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aip_loom.errors import FILE_WRITE_ERROR, PATH_UNSAFE
from aip_loom.fs import (
    AtomicWriteError,
    atomic_write,
    ensure_directory,
    safe_write_bytes,
    safe_write_text,
)
from aip_loom.layout import LayoutError, ProjectLayout


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
# atomic_write — success cases
# ===========================================================================


class TestAtomicWriteSuccess:
    """Atomic write produces correct results on success."""

    def test_creates_target_file(self, layout: ProjectLayout) -> None:
        target = layout.root / "test.txt"
        with atomic_write(target, layout) as tmp:
            tmp.write_text("hello world", encoding="utf-8")
        assert target.read_text(encoding="utf-8") == "hello world"

    def test_creates_parent_directories(self, layout: ProjectLayout) -> None:
        target = layout.root / "subdir" / "deep" / "test.txt"
        with atomic_write(target, layout) as tmp:
            tmp.write_text("deep content", encoding="utf-8")
        assert target.read_text(encoding="utf-8") == "deep content"

    def test_overwrites_existing_file(self, layout: ProjectLayout) -> None:
        target = layout.root / "existing.txt"
        target.write_text("old content", encoding="utf-8")
        with atomic_write(target, layout) as tmp:
            tmp.write_text("new content", encoding="utf-8")
        assert target.read_text(encoding="utf-8") == "new content"

    def test_temp_file_in_same_directory(self, layout: ProjectLayout) -> None:
        target = layout.root / "test.txt"
        with atomic_write(target, layout) as tmp:
            assert tmp.parent == target.parent
            assert tmp.name.startswith(".aip-loom-tmp-")
            assert tmp.name.endswith(".tmp")
            tmp.write_text("content", encoding="utf-8")

    def test_no_temp_file_left_after_success(self, layout: ProjectLayout) -> None:
        target = layout.root / "test.txt"
        with atomic_write(target, layout) as tmp:
            tmp.write_text("content", encoding="utf-8")
        # Temp file should not exist anymore (it was renamed to target)
        tmp_files = list(layout.root.glob(".aip-loom-tmp-*.tmp"))
        assert len(tmp_files) == 0

    def test_writes_utf8_content(self, layout: ProjectLayout) -> None:
        target = layout.root / "unicode.txt"
        content = "日本語テスト 🌍"
        with atomic_write(target, layout) as tmp:
            tmp.write_text(content, encoding="utf-8")
        assert target.read_text(encoding="utf-8") == content


# ===========================================================================
# atomic_write — failure cases
# ===========================================================================


class TestAtomicWriteFailure:
    """Atomic write failure leaves original file intact."""

    def test_failure_leaves_original_intact(self, layout: ProjectLayout) -> None:
        target = layout.root / "test.txt"
        target.write_text("original content", encoding="utf-8")
        with pytest.raises(AtomicWriteError):
            with atomic_write(target, layout) as tmp:
                tmp.write_text("partial content", encoding="utf-8")
                raise RuntimeError("Simulated write failure")
        assert target.read_text(encoding="utf-8") == "original content"

    def test_failure_cleans_up_temp_file(self, layout: ProjectLayout) -> None:
        target = layout.root / "test.txt"
        with pytest.raises(AtomicWriteError):
            with atomic_write(target, layout) as tmp:
                tmp.write_text("partial", encoding="utf-8")
                raise RuntimeError("fail")
        tmp_files = list(layout.root.glob(".aip-loom-tmp-*.tmp"))
        assert len(tmp_files) == 0

    def test_rejects_path_outside_root(self, layout: ProjectLayout) -> None:
        target = Path("/tmp/outside_project.txt")
        with pytest.raises(LayoutError) as exc_info:
            with atomic_write(target, layout) as tmp:
                tmp.write_text("bad", encoding="utf-8")
        assert exc_info.value.loom_error.code == PATH_UNSAFE

    def test_rejects_dot_dot_path(self, layout: ProjectLayout) -> None:
        target = layout.root / ".." / "outside.txt"
        with pytest.raises(LayoutError) as exc_info:
            with atomic_write(target, layout) as tmp:
                tmp.write_text("bad", encoding="utf-8")
        assert exc_info.value.loom_error.code == PATH_UNSAFE


# ===========================================================================
# safe_write_text
# ===========================================================================


class TestSafeWriteText:
    """safe_write_text convenience function."""

    def test_writes_text(self, layout: ProjectLayout) -> None:
        target = layout.root / "text.txt"
        safe_write_text(target, "Hello, world!", layout)
        assert target.read_text(encoding="utf-8") == "Hello, world!"

    def test_overwrites_existing(self, layout: ProjectLayout) -> None:
        target = layout.root / "text.txt"
        target.write_text("old", encoding="utf-8")
        safe_write_text(target, "new", layout)
        assert target.read_text(encoding="utf-8") == "new"


# ===========================================================================
# safe_write_bytes
# ===========================================================================


class TestSafeWriteBytes:
    """safe_write_bytes convenience function."""

    def test_writes_bytes(self, layout: ProjectLayout) -> None:
        target = layout.root / "data.bin"
        safe_write_bytes(target, b"\x00\x01\x02\x03", layout)
        assert target.read_bytes() == b"\x00\x01\x02\x03"


# ===========================================================================
# ensure_directory
# ===========================================================================


class TestEnsureDirectory:
    """ensure_directory creates directories."""

    def test_creates_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "new_dir"
        ensure_directory(d)
        assert d.is_dir()

    def test_creates_nested_directories(self, tmp_path: Path) -> None:
        d = tmp_path / "a" / "b" / "c"
        ensure_directory(d)
        assert d.is_dir()

    def test_existing_directory_ok(self, tmp_path: Path) -> None:
        d = tmp_path / "existing"
        d.mkdir()
        ensure_directory(d)  # Should not raise
        assert d.is_dir()
