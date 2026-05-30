"""Tests for aip_loom.layout — ProjectLayout and path safety.

These tests prove:
- ProjectLayout resolves all canonical paths correctly
- Non-existent root is rejected
- chunk_path validates chunk IDs before path construction
- archive_chunk_path validates chunk IDs
- validate_path rejects .. components
- validate_path rejects paths outside project root
- validate_path rejects symlinks escaping root
- Invalid chunk IDs are rejected (path traversal prevention)
- is_project_initialized checks for manifest file
- Layout is frozen (immutable)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aip_loom.errors import CHUNK_ID_INVALID, PATH_UNSAFE, PROJECT_NOT_FOUND
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
# Construction
# ===========================================================================


class TestConstruction:
    """ProjectLayout construction and validation."""

    def test_valid_root(self, project_root: Path) -> None:
        layout = ProjectLayout(root=project_root)
        assert layout.root == project_root.resolve()

    def test_nonexistent_root_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(LayoutError) as exc_info:
            ProjectLayout(root=tmp_path / "nonexistent")
        assert exc_info.value.loom_error.code == PROJECT_NOT_FOUND

    def test_file_as_root_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "not_a_dir"
        f.write_text("hello")
        with pytest.raises(LayoutError) as exc_info:
            ProjectLayout(root=f)
        assert exc_info.value.loom_error.code == PROJECT_NOT_FOUND

    def test_root_is_resolved_to_absolute(self, project_root: Path) -> None:
        layout = ProjectLayout(root=project_root)
        assert layout.root.is_absolute()

    def test_frozen(self, project_root: Path) -> None:
        layout = ProjectLayout(root=project_root)
        with pytest.raises(AttributeError):
            layout.root = Path("/other")  # type: ignore[misc]


# ===========================================================================
# Path resolution
# ===========================================================================


class TestPathResolution:
    """All canonical paths resolve correctly."""

    def test_manifest_path(self, layout: ProjectLayout) -> None:
        assert layout.manifest_path == layout.root / "aip_loom.yaml"

    def test_distillate_path(self, layout: ProjectLayout) -> None:
        assert layout.distillate_path == layout.root / "distillate.yaml"

    def test_sessions_path(self, layout: ProjectLayout) -> None:
        assert layout.sessions_path == layout.root / "sessions.yaml"

    def test_comments_path(self, layout: ProjectLayout) -> None:
        assert layout.comments_path == layout.root / "comments.yaml"

    def test_chunks_dir(self, layout: ProjectLayout) -> None:
        assert layout.chunks_dir == layout.root / "chunks"

    def test_ledgers_dir(self, layout: ProjectLayout) -> None:
        assert layout.ledgers_dir == layout.root / "ledgers"

    def test_archive_dir(self, layout: ProjectLayout) -> None:
        assert layout.archive_dir == layout.root / "archive"

    def test_aip_loom_dir(self, layout: ProjectLayout) -> None:
        assert layout.aip_loom_dir == layout.root / ".aip-loom"

    def test_staging_dir(self, layout: ProjectLayout) -> None:
        assert layout.staging_dir == layout.root / ".aip-loom" / "staging"

    def test_briefs_dir(self, layout: ProjectLayout) -> None:
        assert layout.briefs_dir == layout.root / ".aip-loom" / "briefs"

    def test_decisions_ledger_path(self, layout: ProjectLayout) -> None:
        assert layout.decisions_ledger_path == layout.root / "ledgers" / "decisions.yaml"

    def test_threads_ledger_path(self, layout: ProjectLayout) -> None:
        assert layout.threads_ledger_path == layout.root / "ledgers" / "threads.yaml"

    def test_questions_ledger_path(self, layout: ProjectLayout) -> None:
        assert layout.questions_ledger_path == layout.root / "ledgers" / "questions.yaml"

    def test_lock_path(self, layout: ProjectLayout) -> None:
        assert layout.lock_path == layout.root / ".aip-loom" / "lock"


# ===========================================================================
# chunk_path
# ===========================================================================


class TestChunkPath:
    """chunk_path validates IDs and resolves paths."""

    def test_valid_chunk_id(self, layout: ProjectLayout) -> None:
        path = layout.chunk_path("C-0001")
        assert path == layout.chunks_dir / "C-0001.md"

    def test_valid_chunk_id_longer_prefix(self, layout: ProjectLayout) -> None:
        path = layout.chunk_path("CH-0012")
        assert path == layout.chunks_dir / "CH-0012.md"

    def test_invalid_chunk_id_rejected(self, layout: ProjectLayout) -> None:
        with pytest.raises(LayoutError) as exc_info:
            layout.chunk_path("../../../etc/passwd")
        assert exc_info.value.loom_error.code == CHUNK_ID_INVALID

    def test_path_traversal_id_rejected(self, layout: ProjectLayout) -> None:
        with pytest.raises(LayoutError) as exc_info:
            layout.chunk_path("C-0001/../../etc/passwd")
        assert exc_info.value.loom_error.code == CHUNK_ID_INVALID

    def test_empty_id_rejected(self, layout: ProjectLayout) -> None:
        with pytest.raises(LayoutError) as exc_info:
            layout.chunk_path("")
        assert exc_info.value.loom_error.code == CHUNK_ID_INVALID

    def test_lowercase_id_rejected(self, layout: ProjectLayout) -> None:
        with pytest.raises(LayoutError) as exc_info:
            layout.chunk_path("c-0001")
        assert exc_info.value.loom_error.code == CHUNK_ID_INVALID


# ===========================================================================
# archive_chunk_path
# ===========================================================================


class TestArchiveChunkPath:
    """archive_chunk_path validates IDs and resolves paths."""

    def test_valid_archive_chunk(self, layout: ProjectLayout) -> None:
        path = layout.archive_chunk_path("C-0001")
        assert path == layout.archive_dir / "C-0001.md"

    def test_invalid_archive_chunk_id_rejected(self, layout: ProjectLayout) -> None:
        with pytest.raises(LayoutError) as exc_info:
            layout.archive_chunk_path("bad-id")
        assert exc_info.value.loom_error.code == CHUNK_ID_INVALID


# ===========================================================================
# brief_path
# ===========================================================================


class TestBriefPath:
    """brief_path validates IDs and resolves brief file paths."""

    def test_valid_brief_path(self, layout: ProjectLayout) -> None:
        path = layout.brief_path("C-0001")
        assert path == layout.briefs_dir / "C-0001.md"

    def test_valid_brief_path_longer_prefix(self, layout: ProjectLayout) -> None:
        path = layout.brief_path("CH-0012")
        assert path == layout.briefs_dir / "CH-0012.md"

    def test_invalid_brief_path_rejected(self, layout: ProjectLayout) -> None:
        with pytest.raises(LayoutError) as exc_info:
            layout.brief_path("../../../etc/passwd")
        assert exc_info.value.loom_error.code == CHUNK_ID_INVALID


# ===========================================================================
# validate_path
# ===========================================================================


class TestValidatePath:
    """validate_path rejects unsafe paths."""

    def test_valid_path_within_root(self, layout: ProjectLayout) -> None:
        path = layout.root / "chunks" / "C-0001.md"
        result = layout.validate_path(path)
        assert result == path.resolve()

    def test_dot_dot_component_rejected(self, layout: ProjectLayout) -> None:
        path = layout.root / ".." / "etc" / "passwd"
        with pytest.raises(LayoutError) as exc_info:
            layout.validate_path(path)
        assert exc_info.value.loom_error.code == PATH_UNSAFE
        assert "dot_dot_component" in exc_info.value.loom_error.detail.get("reason", "")

    def test_path_outside_root_rejected(self, layout: ProjectLayout) -> None:
        """A path that resolves outside the root is rejected."""
        with pytest.raises(LayoutError) as exc_info:
            layout.validate_path(Path("/etc/passwd"))
        assert exc_info.value.loom_error.code == PATH_UNSAFE
        assert "path_escape" in exc_info.value.loom_error.detail.get("reason", "")

    def test_symlink_escaping_root_rejected(self, layout: ProjectLayout, tmp_path: Path) -> None:
        """A symlink inside root that points outside is rejected."""
        # Create a symlink inside the project that points outside
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "evil.txt"
        outside_file.write_text("evil")

        link_path = layout.root / "evil_link"
        link_path.symlink_to(outside_file)

        # validate_path on the symlink should reject it — either
        # as path_escape (resolved target outside root) or
        # symlink_escape (if resolved target was within root but
        # readlink target wasn't).  In practice, the resolved
        # path check fires first.
        with pytest.raises(LayoutError) as exc_info:
            layout.validate_path(link_path)
        assert exc_info.value.loom_error.code == PATH_UNSAFE
        # The reason may be path_escape or symlink_escape depending
        # on which check fires first; both are valid rejections
        assert exc_info.value.loom_error.detail.get("reason") in (
            "path_escape",
            "symlink_escape",
        )

    def test_symlink_within_root_accepted(self, layout: ProjectLayout) -> None:
        """A symlink inside root that points within root is accepted."""
        target = layout.root / "real_file.txt"
        target.write_text("content")

        link = layout.root / "link_to_file.txt"
        link.symlink_to(target)

        result = layout.validate_path(link)
        assert result == link.resolve()

    def test_root_itself_is_valid(self, layout: ProjectLayout) -> None:
        result = layout.validate_path(layout.root)
        assert result == layout.root


# ===========================================================================
# is_project_initialized
# ===========================================================================


class TestIsProjectInitialized:
    """is_project_initialized checks for the manifest file."""

    def test_not_initialized_without_manifest(self, layout: ProjectLayout) -> None:
        assert layout.is_project_initialized() is False

    def test_initialized_with_manifest(self, layout: ProjectLayout) -> None:
        layout.manifest_path.write_text("schema_version: '0.1.0'\nname: test\n")
        assert layout.is_project_initialized() is True
