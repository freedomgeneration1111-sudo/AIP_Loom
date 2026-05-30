"""Tests for aip_loom.project — Project loader and validation engine.

These tests exercise the load_project() and validate_project() functions
and verify:

- Successful project loading with correct state
- Honest partial loading (malformed YAML captured, not hidden)
- Validation is side-effect-free (no file mutations)
- Duplicate ID detection
- Broken reference detection
- Checksum mismatch reporting (warning, not auto-fix)
- Missing file detection
- Chunk order issues
- Pending review item reporting
- Chunk scoping (--chunk)
- ProjectError raised for fundamental problems
- Validation never mutates files
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aip_loom.checksum import compute_prose_checksum
from aip_loom.errors import (
    CHECKSUM_DIRTY,
    CHECKSUM_MISMATCH,
    ID_DUPLICATE,
    PROJECT_NOT_FOUND,
    SCHEMA_VALIDATION_FAILED,
    VALIDATION_BROKEN_REFERENCE,
    VALIDATION_CHUNK_ORDER_MISMATCH,
    VALIDATION_DIRTY_CHECKSUM,
    VALIDATION_DUPLICATE_ID,
    VALIDATION_MISSING_FILE,
    VALIDATION_PENDING_REVIEW,
    YAML_PARSE_ERROR,
    LoomError,
    LoomWarning,
)
from aip_loom.frontmatter import write_frontmatter
from aip_loom.init import init_project
from aip_loom.layout import ProjectLayout
from aip_loom.project import (
    ChunkData,
    ProjectError,
    ProjectState,
    ValidationResult,
    load_project,
    validate_project,
)
from aip_loom.schemas import (
    SUPPORTED_SCHEMA_VERSION,
    ChunkFrontmatter,
    ChunkStatus,
    CommentLog,
    DecisionEntry,
    DecisionLedger,
    Distillate,
    DistillateNode,
    ProjectManifest,
    ProjectType,
    QuestionEntry,
    QuestionLedger,
    ReviewState,
    SessionLog,
    ThreadEntry,
    ThreadLedger,
    ThreadState,
)
from aip_loom.yaml_io import dump_yaml_string


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
def project_root(tmp_dir: Path) -> Path:
    """Create and return an initialized project root."""
    root = tmp_dir / "test-project"
    init_project(root=root, name="test-project")
    return root


def _make_chunk_content(
    chunk_id: str = "C-0001",
    title: str = "Test Chunk",
    status: str = "draft",
    word_count: int = 100,
    prose: str = "This is test prose content.",
    prose_checksum: str | None = None,
) -> str:
    """Helper to create chunk file content with frontmatter."""
    now = datetime.now(timezone.utc).isoformat()
    if prose_checksum is None:
        prose_checksum = compute_prose_checksum(prose)
    fm = ChunkFrontmatter(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        id=chunk_id,
        title=title,
        status=status,
        word_count=word_count,
        prose_checksum=prose_checksum,
        created_at=now,
        updated_at=now,
    )
    return write_frontmatter(fm, prose)


def _write_chunk(project_root: Path, chunk_id: str, prose: str = "Test prose.", **kwargs) -> Path:
    """Write a chunk file to the project."""
    layout = ProjectLayout(root=project_root)
    path = layout.chunk_path(chunk_id)
    content = _make_chunk_content(chunk_id=chunk_id, prose=prose, **kwargs)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# load_project — fundamental checks
# ---------------------------------------------------------------------------


class TestLoadProjectFundamentals:
    """Verify that load_project handles fundamental cases."""

    def test_load_initialized_project(self, project_root: Path) -> None:
        """load_project succeeds on an initialized project."""
        state = load_project(project_root)
        assert isinstance(state, ProjectState)
        assert state.manifest is not None
        assert state.manifest.name == "test-project"

    def test_load_nonexistent_root_raises(self, tmp_dir: Path) -> None:
        """load_project raises ProjectError for non-existent root."""
        with pytest.raises(ProjectError) as exc_info:
            load_project(tmp_dir / "nonexistent")
        assert exc_info.value.loom_error.code == PROJECT_NOT_FOUND

    def test_load_no_manifest_raises(self, tmp_dir: Path) -> None:
        """load_project raises ProjectError when manifest is missing."""
        empty_dir = tmp_dir / "empty-dir"
        empty_dir.mkdir()
        with pytest.raises(ProjectError) as exc_info:
            load_project(empty_dir)
        assert exc_info.value.loom_error.code == PROJECT_NOT_FOUND

    def test_load_returns_all_ledgers(self, project_root: Path) -> None:
        """load_project returns all ledgers and metadata files."""
        state = load_project(project_root)
        assert state.decisions_ledger is not None
        assert state.threads_ledger is not None
        assert state.questions_ledger is not None
        assert state.distillate is not None
        assert state.sessions is not None
        assert state.comments is not None

    def test_load_empty_project_has_no_chunks(self, project_root: Path) -> None:
        """load_project returns no chunks for a fresh project."""
        state = load_project(project_root)
        assert len(state.chunks) == 0

    def test_load_empty_project_has_no_errors(self, project_root: Path) -> None:
        """load_project has no errors for a properly initialized project."""
        state = load_project(project_root)
        assert len(state.load_errors) == 0

    def test_load_with_chunks(self, project_root: Path) -> None:
        """load_project discovers and parses chunk files."""
        _write_chunk(project_root, "C-0001", "First chunk prose.")
        _write_chunk(project_root, "C-0002", "Second chunk prose.")

        state = load_project(project_root)
        assert len(state.chunks) == 2
        assert "C-0001" in state.chunks
        assert "C-0002" in state.chunks
        assert state.chunks["C-0001"].frontmatter.id == "C-0001"
        assert state.chunks["C-0001"].prose_body.strip() == "First chunk prose."

    def test_load_chunk_order_resolved(self, project_root: Path) -> None:
        """load_project resolves chunk order."""
        _write_chunk(project_root, "C-0001")
        state = load_project(project_root)
        assert state.chunk_order is not None
        assert "C-0001" in state.chunk_order.ordered_ids


# ---------------------------------------------------------------------------
# load_project — honest partial loading
# ---------------------------------------------------------------------------


class TestHonestPartialLoading:
    """Verify that load_project is honest about partial failures."""

    def test_malformed_manifest_captured(self, project_root: Path) -> None:
        """Malformed manifest is captured as a load error, not hidden."""
        layout = ProjectLayout(root=project_root)
        # Overwrite manifest with invalid YAML
        layout.manifest_path.write_text("not: valid: yaml: [broken", encoding="utf-8")

        state = load_project(project_root)
        assert state.manifest is None
        error_codes = [e.code for e in state.load_errors]
        assert YAML_PARSE_ERROR in error_codes or SCHEMA_VALIDATION_FAILED in error_codes

    def test_malformed_ledger_captured(self, project_root: Path) -> None:
        """Malformed ledger is captured as a load error, not replaced with empty."""
        layout = ProjectLayout(root=project_root)
        # Overwrite decisions ledger with invalid content
        layout.decisions_ledger_path.write_text("totally: broken: {{", encoding="utf-8")

        state = load_project(project_root)
        assert state.decisions_ledger is None
        error_codes = [e.code for e in state.load_errors]
        assert YAML_PARSE_ERROR in error_codes or SCHEMA_VALIDATION_FAILED in error_codes

    def test_malformed_chunk_captured(self, project_root: Path) -> None:
        """Malformed chunk is captured as a load error."""
        layout = ProjectLayout(root=project_root)
        # Write a file without proper frontmatter
        bad_chunk = layout.chunks_dir / "C-0001.md"
        bad_chunk.write_text("No frontmatter here, just prose.", encoding="utf-8")

        state = load_project(project_root)
        assert "C-0001" not in state.chunks
        error_codes = [e.code for e in state.load_errors]
        # Should have some frontmatter error
        assert len(error_codes) > 0

    def test_missing_ledger_captured(self, project_root: Path) -> None:
        """Missing ledger file is captured as a load error."""
        layout = ProjectLayout(root=project_root)
        layout.decisions_ledger_path.unlink()

        state = load_project(project_root)
        assert state.decisions_ledger is None
        error_codes = [e.code for e in state.load_errors]
        assert "FILE_NOT_FOUND" in error_codes

    def test_malformed_ledger_not_counted_as_empty(self, project_root: Path) -> None:
        """A malformed ledger is None, not an empty ledger with zero entries."""
        layout = ProjectLayout(root=project_root)
        layout.decisions_ledger_path.write_text("bad: {{yaml", encoding="utf-8")

        state = load_project(project_root)
        # The ledger should be None, not a DecisionLedger with entries=[]
        assert state.decisions_ledger is None

    def test_mixed_good_and_bad_files(self, project_root: Path) -> None:
        """Good files are loaded even when some are bad."""
        layout = ProjectLayout(root=project_root)
        # Corrupt the decisions ledger but keep everything else
        layout.decisions_ledger_path.write_text("broken: yaml: {{", encoding="utf-8")

        state = load_project(project_root)
        # Manifest and other files should still be loaded
        assert state.manifest is not None
        assert state.threads_ledger is not None
        assert state.questions_ledger is not None
        # Only decisions ledger failed
        assert state.decisions_ledger is None


# ---------------------------------------------------------------------------
# validate_project — clean project
# ---------------------------------------------------------------------------


class TestValidateCleanProject:
    """Verify that validate_project passes on a clean project."""

    def test_clean_project_validates(self, project_root: Path) -> None:
        """A freshly initialized project validates successfully."""
        state = load_project(project_root)
        result = validate_project(state)
        assert result.ok
        assert len(result.errors) == 0

    def test_clean_project_may_have_warnings(self, project_root: Path) -> None:
        """A fresh project may have warnings (e.g. chunk order fallback) but no errors."""
        state = load_project(project_root)
        result = validate_project(state)
        # Warnings are allowed but errors are not
        assert result.ok

    def test_project_with_valid_chunks_validates(self, project_root: Path) -> None:
        """A project with valid chunks passes validation."""
        _write_chunk(project_root, "C-0001", "First chunk.")
        _write_chunk(project_root, "C-0002", "Second chunk.")

        state = load_project(project_root)
        result = validate_project(state)
        assert result.ok


# ---------------------------------------------------------------------------
# validate_project — duplicate IDs
# ---------------------------------------------------------------------------


class TestDuplicateIdDetection:
    """Verify that duplicate IDs are detected."""

    def test_duplicate_chunk_ids_detected(self, project_root: Path) -> None:
        """Two chunk files with the same frontmatter ID are detected."""
        layout = ProjectLayout(root=project_root)
        # Write two files with the same chunk ID in frontmatter
        content = _make_chunk_content(chunk_id="C-0001", prose="Chunk one.")
        (layout.chunks_dir / "C-0001.md").write_text(content, encoding="utf-8")
        (layout.chunks_dir / "C-0001_copy.md").write_text(content, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        error_codes = [e.code for e in result.errors]
        assert VALIDATION_DUPLICATE_ID in error_codes

    def test_duplicate_ledger_entry_ids_detected(self, project_root: Path) -> None:
        """Duplicate ledger entry IDs are detected."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        # Write decisions ledger with duplicate IDs
        ledger = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.APPROVED,
                    created_at=now, summary="First decision",
                ),
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.APPROVED,
                    created_at=now, summary="Duplicate decision",
                ),
            ],
        )
        ledger_yaml = dump_yaml_string(ledger.model_dump(mode="json"))
        layout.decisions_ledger_path.write_text(ledger_yaml, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        error_codes = [e.code for e in result.errors]
        assert VALIDATION_DUPLICATE_ID in error_codes

    def test_unique_ids_no_error(self, project_root: Path) -> None:
        """Unique IDs across all files produce no duplicate errors."""
        _write_chunk(project_root, "C-0001")
        _write_chunk(project_root, "C-0002")

        state = load_project(project_root)
        result = validate_project(state)
        dup_errors = [e for e in result.errors if e.code == VALIDATION_DUPLICATE_ID]
        assert len(dup_errors) == 0


# ---------------------------------------------------------------------------
# validate_project — broken references
# ---------------------------------------------------------------------------


class TestBrokenReferenceDetection:
    """Verify that broken references are detected."""

    def test_decision_references_nonexistent_chunk(self, project_root: Path) -> None:
        """Decision entry referencing non-existent chunk is detected."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.APPROVED,
                    created_at=now, summary="A decision",
                    chunk_id="C-9999",  # Does not exist
                ),
            ],
        )
        ledger_yaml = dump_yaml_string(ledger.model_dump(mode="json"))
        layout.decisions_ledger_path.write_text(ledger_yaml, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        error_codes = [e.code for e in result.errors]
        assert VALIDATION_BROKEN_REFERENCE in error_codes

    def test_thread_references_nonexistent_chunk(self, project_root: Path) -> None:
        """Thread entry referencing non-existent chunk is detected."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = ThreadLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                ThreadEntry(
                    id="T-0001", review_state=ReviewState.APPROVED,
                    created_at=now, summary="A thread",
                    chunk_id="C-9999",
                ),
            ],
        )
        ledger_yaml = dump_yaml_string(ledger.model_dump(mode="json"))
        layout.threads_ledger_path.write_text(ledger_yaml, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        error_codes = [e.code for e in result.errors]
        assert VALIDATION_BROKEN_REFERENCE in error_codes

    def test_thread_blocked_by_nonexistent_thread(self, project_root: Path) -> None:
        """Thread blocked_by referencing non-existent thread is detected."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = ThreadLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                ThreadEntry(
                    id="T-0001", review_state=ReviewState.APPROVED,
                    created_at=now, summary="A thread",
                    blocked_by=["T-9999"],  # Does not exist
                ),
            ],
        )
        ledger_yaml = dump_yaml_string(ledger.model_dump(mode="json"))
        layout.threads_ledger_path.write_text(ledger_yaml, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        error_codes = [e.code for e in result.errors]
        assert VALIDATION_BROKEN_REFERENCE in error_codes

    def test_distillate_references_nonexistent_chunk(self, project_root: Path) -> None:
        """Distillate node referencing non-existent chunk is detected."""
        layout = ProjectLayout(root=project_root)

        distillate = Distillate(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            nodes=[
                DistillateNode(
                    chunk_id="C-9999", title="Ghost chunk",
                ),
            ],
        )
        distillate_yaml = dump_yaml_string(distillate.model_dump(mode="json"))
        layout.distillate_path.write_text(distillate_yaml, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        error_codes = [e.code for e in result.errors]
        assert VALIDATION_BROKEN_REFERENCE in error_codes

    def test_distillate_references_nonexistent_decision(self, project_root: Path) -> None:
        """Distillate node key_decisions referencing non-existent decision is detected."""
        layout = ProjectLayout(root=project_root)

        distillate = Distillate(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            nodes=[
                DistillateNode(
                    chunk_id="C-0001", title="Test",
                    key_decisions=["D-9999"],  # Does not exist
                ),
            ],
        )
        distillate_yaml = dump_yaml_string(distillate.model_dump(mode="json"))
        layout.distillate_path.write_text(distillate_yaml, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        error_codes = [e.code for e in result.errors]
        assert VALIDATION_BROKEN_REFERENCE in error_codes

    def test_valid_references_no_error(self, project_root: Path) -> None:
        """Valid references produce no broken reference errors."""
        _write_chunk(project_root, "C-0001", "A chunk.")
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.APPROVED,
                    created_at=now, summary="A decision",
                    chunk_id="C-0001",  # Exists
                ),
            ],
        )
        ledger_yaml = dump_yaml_string(ledger.model_dump(mode="json"))
        layout.decisions_ledger_path.write_text(ledger_yaml, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        ref_errors = [e for e in result.errors if e.code == VALIDATION_BROKEN_REFERENCE]
        assert len(ref_errors) == 0


# ---------------------------------------------------------------------------
# validate_project — checksum mismatches
# ---------------------------------------------------------------------------


class TestChecksumMismatches:
    """Verify that checksum mismatches are reported but not auto-fixed."""

    def test_dirty_checksum_reported_as_warning(self, project_root: Path) -> None:
        """A chunk with edited prose but unchanged checksum is a warning."""
        layout = ProjectLayout(root=project_root)
        # Write a chunk with a valid checksum
        _write_chunk(project_root, "C-0001", "Original prose.")

        # Now edit the prose but don't update the checksum in frontmatter
        chunk_path = layout.chunk_path("C-0001")
        original_content = chunk_path.read_text(encoding="utf-8")
        # Replace the prose body while keeping the frontmatter
        from aip_loom.frontmatter import split_frontmatter
        yaml_str, _ = split_frontmatter(original_content)
        # Write back with modified prose but original frontmatter
        modified = f"---\n{yaml_str}\n---\nModified prose that differs."
        chunk_path.write_text(modified, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        warning_codes = [w.code for w in result.warnings]
        assert VALIDATION_DIRTY_CHECKSUM in warning_codes

    def test_dirty_checksum_not_auto_fixed(self, project_root: Path) -> None:
        """Dirty checksums are reported but the file is never modified."""
        layout = ProjectLayout(root=project_root)
        _write_chunk(project_root, "C-0001", "Original prose.")

        chunk_path = layout.chunk_path("C-0001")
        original_content = chunk_path.read_text(encoding="utf-8")
        from aip_loom.frontmatter import split_frontmatter
        yaml_str, _ = split_frontmatter(original_content)
        modified = f"---\n{yaml_str}\n---\nModified prose."
        chunk_path.write_text(modified, encoding="utf-8")

        # Record file state before validation
        before_mtime = chunk_path.stat().st_mtime
        before_content = chunk_path.read_text(encoding="utf-8")

        state = load_project(project_root)
        validate_project(state)

        # File must not be modified by validation
        after_mtime = chunk_path.stat().st_mtime
        after_content = chunk_path.read_text(encoding="utf-8")
        assert before_mtime == after_mtime
        assert before_content == after_content

    def test_correct_checksum_no_warning(self, project_root: Path) -> None:
        """Chunks with correct checksums produce no dirty checksum warning."""
        _write_chunk(project_root, "C-0001", "Consistent prose.")

        state = load_project(project_root)
        result = validate_project(state)
        dirty_warnings = [w for w in result.warnings if w.code == VALIDATION_DIRTY_CHECKSUM]
        assert len(dirty_warnings) == 0


# ---------------------------------------------------------------------------
# validate_project — missing files
# ---------------------------------------------------------------------------


class TestMissingFiles:
    """Verify that missing required files are detected."""

    def test_missing_ledger_detected(self, project_root: Path) -> None:
        """Missing ledger file is detected as a validation error."""
        layout = ProjectLayout(root=project_root)
        layout.decisions_ledger_path.unlink()

        state = load_project(project_root)
        result = validate_project(state)
        error_codes = [e.code for e in result.errors]
        assert VALIDATION_MISSING_FILE in error_codes

    def test_missing_distillate_detected(self, project_root: Path) -> None:
        """Missing distillate file is detected as a validation error."""
        layout = ProjectLayout(root=project_root)
        layout.distillate_path.unlink()

        state = load_project(project_root)
        result = validate_project(state)
        error_codes = [e.code for e in result.errors]
        assert VALIDATION_MISSING_FILE in error_codes

    def test_missing_chunks_dir_detected(self, project_root: Path) -> None:
        """Missing chunks directory is detected as a validation error."""
        layout = ProjectLayout(root=project_root)
        shutil.rmtree(str(layout.chunks_dir))

        state = load_project(project_root)
        result = validate_project(state)
        error_codes = [e.code for e in result.errors]
        assert VALIDATION_MISSING_FILE in error_codes

    def test_missing_session_log_detected(self, project_root: Path) -> None:
        """Missing session log is detected as a validation error."""
        layout = ProjectLayout(root=project_root)
        layout.sessions_path.unlink()

        state = load_project(project_root)
        result = validate_project(state)
        error_codes = [e.code for e in result.errors]
        assert VALIDATION_MISSING_FILE in error_codes


# ---------------------------------------------------------------------------
# validate_project — chunk order issues
# ---------------------------------------------------------------------------


class TestChunkOrderIssues:
    """Verify that chunk order mismatches are detected."""

    def test_manifest_references_nonexistent_chunk(self, project_root: Path) -> None:
        """Chunks in manifest order but not on disk are errors."""
        layout = ProjectLayout(root=project_root)
        # Update manifest to reference a non-existent chunk
        manifest = ProjectManifest(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            name="test-project",
            project_type=ProjectType.NOVEL,
            chunks={"order": ["C-0001", "C-0002"]},
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        manifest_yaml = dump_yaml_string(manifest.model_dump(mode="json"))
        layout.manifest_path.write_text(manifest_yaml, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        error_codes = [e.code for e in result.errors]
        assert VALIDATION_CHUNK_ORDER_MISMATCH in error_codes

    def test_chunk_not_in_manifest_order_warning(self, project_root: Path) -> None:
        """Chunks on disk but not in manifest order produce a warning."""
        layout = ProjectLayout(root=project_root)
        _write_chunk(project_root, "C-0001", "First chunk.")
        _write_chunk(project_root, "C-0002", "Second chunk.")

        # Set manifest order to only C-0001
        manifest = ProjectManifest(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            name="test-project",
            project_type=ProjectType.NOVEL,
            chunks={"order": ["C-0001"]},
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        manifest_yaml = dump_yaml_string(manifest.model_dump(mode="json"))
        layout.manifest_path.write_text(manifest_yaml, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        warn_codes = [w.code for w in result.warnings]
        assert VALIDATION_CHUNK_ORDER_MISMATCH in warn_codes


# ---------------------------------------------------------------------------
# validate_project — pending review items
# ---------------------------------------------------------------------------


class TestPendingReviewItems:
    """Verify that pending review items are reported as warnings."""

    def test_pending_decision_reported(self, project_root: Path) -> None:
        """Pending review decisions are reported as warnings."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.PENDING,
                    created_at=now, summary="Pending decision",
                ),
            ],
        )
        ledger_yaml = dump_yaml_string(ledger.model_dump(mode="json"))
        layout.decisions_ledger_path.write_text(ledger_yaml, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        warn_codes = [w.code for w in result.warnings]
        assert VALIDATION_PENDING_REVIEW in warn_codes

    def test_pending_thread_reported(self, project_root: Path) -> None:
        """Pending review threads are reported as warnings."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = ThreadLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                ThreadEntry(
                    id="T-0001", review_state=ReviewState.PENDING,
                    created_at=now, summary="Pending thread",
                ),
            ],
        )
        ledger_yaml = dump_yaml_string(ledger.model_dump(mode="json"))
        layout.threads_ledger_path.write_text(ledger_yaml, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        warn_codes = [w.code for w in result.warnings]
        assert VALIDATION_PENDING_REVIEW in warn_codes

    def test_approved_items_no_warning(self, project_root: Path) -> None:
        """Approved items do not trigger pending review warnings."""
        layout = ProjectLayout(root=project_root)
        now = datetime.now(timezone.utc).isoformat()

        ledger = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.APPROVED,
                    created_at=now, summary="Approved decision",
                ),
            ],
        )
        ledger_yaml = dump_yaml_string(ledger.model_dump(mode="json"))
        layout.decisions_ledger_path.write_text(ledger_yaml, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        pending_warnings = [w for w in result.warnings if w.code == VALIDATION_PENDING_REVIEW]
        assert len(pending_warnings) == 0


# ---------------------------------------------------------------------------
# validate_project — chunk scoping
# ---------------------------------------------------------------------------


class TestChunkScoping:
    """Verify that --chunk scoping limits validation to a specific chunk."""

    def test_chunk_scope_limits_checksum_check(self, project_root: Path) -> None:
        """With --chunk, only the specified chunk's checksum is checked."""
        layout = ProjectLayout(root=project_root)
        _write_chunk(project_root, "C-0001", "First chunk.")
        _write_chunk(project_root, "C-0002", "Second chunk.")

        # Dirty only C-0001's checksum
        chunk_path = layout.chunk_path("C-0001")
        original_content = chunk_path.read_text(encoding="utf-8")
        from aip_loom.frontmatter import split_frontmatter
        yaml_str, _ = split_frontmatter(original_content)
        modified = f"---\n{yaml_str}\n---\nModified first chunk."
        chunk_path.write_text(modified, encoding="utf-8")

        # Validate scoped to C-0002 — should not report C-0001's dirty checksum
        state = load_project(project_root)
        result = validate_project(state, chunk_scope="C-0002")
        dirty_warnings = [w for w in result.warnings if w.code == VALIDATION_DIRTY_CHECKSUM]
        assert len(dirty_warnings) == 0

    def test_chunk_scope_includes_specified_chunk(self, project_root: Path) -> None:
        """With --chunk, the specified chunk's checksum IS checked."""
        layout = ProjectLayout(root=project_root)
        _write_chunk(project_root, "C-0001", "First chunk.")
        _write_chunk(project_root, "C-0002", "Second chunk.")

        # Dirty C-0001's checksum
        chunk_path = layout.chunk_path("C-0001")
        original_content = chunk_path.read_text(encoding="utf-8")
        from aip_loom.frontmatter import split_frontmatter
        yaml_str, _ = split_frontmatter(original_content)
        modified = f"---\n{yaml_str}\n---\nModified first chunk."
        chunk_path.write_text(modified, encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state, chunk_scope="C-0001")
        dirty_warnings = [w for w in result.warnings if w.code == VALIDATION_DIRTY_CHECKSUM]
        assert len(dirty_warnings) == 1

    def test_chunk_scope_limits_broken_reference_check(self, project_root: Path) -> None:
        """With --chunk, only references to the scoped chunk are checked."""
        layout = ProjectLayout(root=project_root)
        _write_chunk(project_root, "C-0001", "First chunk.")
        now = datetime.now(timezone.utc).isoformat()

        ledger = DecisionLedger(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            entries=[
                DecisionEntry(
                    id="D-0001", review_state=ReviewState.APPROVED,
                    created_at=now, summary="Refers to C-0001",
                    chunk_id="C-0001",
                ),
                DecisionEntry(
                    id="D-0002", review_state=ReviewState.APPROVED,
                    created_at=now, summary="Refers to C-9999",
                    chunk_id="C-9999",
                ),
            ],
        )
        ledger_yaml = dump_yaml_string(ledger.model_dump(mode="json"))
        layout.decisions_ledger_path.write_text(ledger_yaml, encoding="utf-8")

        # Scope to C-0001 — broken reference to C-9999 should still be detected
        # because it's a global reference check (all broken refs are errors)
        # Actually, the scoped chunk filtering only applies to chunk_id-referencing
        # entries that match the scope. Out-of-scope entries are skipped.
        state = load_project(project_root)
        result_scoped = validate_project(state, chunk_scope="C-0001")
        # D-0001 references C-0001 (in scope) — should be checked, and it exists
        # D-0002 references C-9999 (out of scope) — should be skipped
        ref_errors_scoped = [e for e in result_scoped.errors if e.code == VALIDATION_BROKEN_REFERENCE]
        # Since C-9999 reference is out of scope, it should not be reported
        assert len(ref_errors_scoped) == 0

        # Without scoping, the broken reference IS detected
        result_full = validate_project(state)
        ref_errors_full = [e for e in result_full.errors if e.code == VALIDATION_BROKEN_REFERENCE]
        assert len(ref_errors_full) >= 1


# ---------------------------------------------------------------------------
# validate_project — validation is pure (no mutations)
# ---------------------------------------------------------------------------


class TestValidationIsPure:
    """Verify that validation never mutates files."""

    def test_validation_does_not_modify_any_file(self, project_root: Path) -> None:
        """Running validate_project does not modify any project file."""
        _write_chunk(project_root, "C-0001", "Some prose.")
        layout = ProjectLayout(root=project_root)

        # Record file states before validation
        file_states: dict[str, tuple[float, str]] = {}
        for path in project_root.rglob("*"):
            if path.is_file():
                try:
                    file_states[str(path)] = (
                        path.stat().st_mtime,
                        path.read_text(encoding="utf-8"),
                    )
                except (OSError, UnicodeDecodeError):
                    pass

        state = load_project(project_root)
        validate_project(state)

        # Verify no file was modified
        for path_str, (mtime, content) in file_states.items():
            path = Path(path_str)
            if path.exists():
                try:
                    assert path.stat().st_mtime == mtime, f"File modified: {path}"
                    assert path.read_text(encoding="utf-8") == content, f"Content changed: {path}"
                except (OSError, UnicodeDecodeError):
                    pass

    def test_validation_does_not_create_files(self, project_root: Path) -> None:
        """Running validate_project does not create new files."""
        _write_chunk(project_root, "C-0001", "Some prose.")

        # Record all files before validation
        before_files = set(str(p) for p in project_root.rglob("*") if p.is_file())

        state = load_project(project_root)
        validate_project(state)

        # Check no new files were created
        after_files = set(str(p) for p in project_root.rglob("*") if p.is_file())
        new_files = after_files - before_files
        assert len(new_files) == 0, f"New files created: {new_files}"

    def test_validation_does_not_auto_fix_checksum(self, project_root: Path) -> None:
        """Validation does not auto-fix dirty checksums."""
        layout = ProjectLayout(root=project_root)
        _write_chunk(project_root, "C-0001", "Original prose.")

        # Dirty the checksum
        chunk_path = layout.chunk_path("C-0001")
        original_content = chunk_path.read_text(encoding="utf-8")
        from aip_loom.frontmatter import split_frontmatter
        yaml_str, _ = split_frontmatter(original_content)
        modified = f"---\n{yaml_str}\n---\nModified prose."
        chunk_path.write_text(modified, encoding="utf-8")

        before_content = chunk_path.read_text(encoding="utf-8")

        state = load_project(project_root)
        result = validate_project(state)
        # Should report dirty checksum
        dirty_warnings = [w for w in result.warnings if w.code == VALIDATION_DIRTY_CHECKSUM]
        assert len(dirty_warnings) > 0

        # But file should not be modified
        after_content = chunk_path.read_text(encoding="utf-8")
        assert before_content == after_content


# ---------------------------------------------------------------------------
# ValidationResult structure
# ---------------------------------------------------------------------------


class TestValidationResultStructure:
    """Verify ValidationResult data structure."""

    def test_from_findings_creates_result(self) -> None:
        """ValidationResult.from_findings creates proper result."""
        errors = [LoomError(code="TEST_ERROR", message="test")]
        warnings = [LoomWarning(code="TEST_WARN", message="test")]
        result = ValidationResult.from_findings(errors, warnings)
        assert not result.ok
        assert len(result.errors) == 1
        assert len(result.warnings) == 1

    def test_from_findings_no_errors_is_ok(self) -> None:
        """ValidationResult with no errors is ok."""
        result = ValidationResult.from_findings([], [])
        assert result.ok

    def test_result_is_frozen(self) -> None:
        """ValidationResult is frozen (immutable)."""
        result = ValidationResult.from_findings([], [])
        with pytest.raises(AttributeError):
            result.ok = False  # type: ignore[misc]

    def test_project_state_is_frozen(self, project_root: Path) -> None:
        """ProjectState is frozen (immutable)."""
        state = load_project(project_root)
        with pytest.raises(AttributeError):
            state.manifest = None  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ProjectError
# ---------------------------------------------------------------------------


class TestProjectError:
    """Verify ProjectError behaviour."""

    def test_project_error_carries_loom_error(self) -> None:
        """ProjectError carries a LoomError."""
        loom_err = LoomError(code=PROJECT_NOT_FOUND, message="test")
        err = ProjectError(loom_err)
        assert err.loom_error.code == PROJECT_NOT_FOUND
        assert "test" in str(err)

    def test_project_error_is_exception(self) -> None:
        """ProjectError is an Exception."""
        err = ProjectError(LoomError(code=PROJECT_NOT_FOUND, message="test"))
        assert isinstance(err, Exception)
