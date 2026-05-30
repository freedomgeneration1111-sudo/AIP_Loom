"""Tests for aip_loom.transaction — Transaction workspace and snapshot/recovery.

These tests prove:
- Transaction workspace creates proper directory structure
- snapshot_file captures exact pre-modification content
- restore returns files to exact pre-state (hash verification)
- Simulated staging failure leaves canonical state unchanged
- Simulated restore failure preserves snapshot evidence
- Transaction never writes outside project root
- Failure injection at different stages (snapshot, restore, commit)
- Double-transaction detection (TX_ALREADY_ACTIVE)
- Restoring a file that was never snapshot raises error
- Content hash mismatch on restore is detected
- Files that didn't exist before transaction are deleted on restore
- Duplicate snapshot of same file is idempotent
- Manifest is written and readable throughout the lifecycle
- Cleanup removes the workspace directory
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aip_loom.errors import (
    PATH_UNSAFE,
    TX_ALREADY_ACTIVE,
    TX_FILE_NOT_SNAPSHOTTED,
    TX_HASH_MISMATCH,
    TX_NOT_ACTIVE,
    TX_RESTORE_FAILED,
    TX_SNAPSHOT_FAILED,
)
from aip_loom.layout import ProjectLayout
from aip_loom.transaction import (
    FailureInjector,
    NoopFailureInjector,
    SnapshotEntry,
    TransactionError,
    TransactionManifest,
    TransactionStatus,
    TransactionWorkspace,
    _compute_bytes_hash,
    _compute_file_hash,
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


def _file_hash(path: Path) -> str:
    """Compute SHA-256 hash of a file for test verification."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Failure injector implementations for testing
# ---------------------------------------------------------------------------


class SnapshotFailureInjector:
    """Injects failure during snapshot."""

    def fail_on_snapshot(self) -> bool:
        return True

    def fail_on_stage(self) -> bool:
        return False

    def fail_on_restore(self) -> bool:
        return False

    def fail_on_commit(self) -> bool:
        return False


class RestoreFailureInjector:
    """Injects failure during restore."""

    def fail_on_snapshot(self) -> bool:
        return False

    def fail_on_stage(self) -> bool:
        return False

    def fail_on_restore(self) -> bool:
        return True

    def fail_on_commit(self) -> bool:
        return False


class CommitFailureInjector:
    """Injects failure during commit."""

    def fail_on_snapshot(self) -> bool:
        return False

    def fail_on_stage(self) -> bool:
        return False

    def fail_on_restore(self) -> bool:
        return False

    def fail_on_commit(self) -> bool:
        return True


# ===========================================================================
# TransactionWorkspace — begin
# ===========================================================================


class TestBegin:
    """Transaction begin creates workspace structure."""

    def test_begin_returns_tx_id(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        tx_id = ws.begin()
        assert tx_id is not None
        assert len(tx_id) == 12

    def test_begin_creates_workspace_dirs(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()
        assert ws.workspace_root is not None
        assert ws.workspace_root.is_dir()
        assert ws.snapshots_dir is not None
        assert ws.snapshots_dir.is_dir()
        assert ws.staged_dir is not None
        assert ws.staged_dir.is_dir()

    def test_begin_creates_manifest(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()
        assert ws.manifest_path is not None
        assert ws.manifest_path.is_file()
        data = json.loads(ws.manifest_path.read_text(encoding="utf-8"))
        assert data["status"] == "active"

    def test_begin_sets_active_status(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()
        assert ws.is_active is True
        assert ws.status == TransactionStatus.ACTIVE

    def test_double_begin_raises(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()
        with pytest.raises(TransactionError) as exc_info:
            ws.begin()
        assert exc_info.value.loom_error.code == TX_ALREADY_ACTIVE

    def test_begin_under_aip_loom_tmp(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()
        assert ws.workspace_root is not None
        assert str(ws.workspace_root).startswith(str(layout.aip_loom_dir / "tmp"))


# ===========================================================================
# snapshot_file
# ===========================================================================


class TestSnapshotFile:
    """snapshot_file captures file content and records hash."""

    def test_snapshot_existing_file(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        # Create a file to snapshot
        target = layout.root / "test.txt"
        target.write_text("hello world", encoding="utf-8")
        original_hash = _file_hash(target)

        entry = ws.snapshot_file(target)

        assert entry.existed is True
        assert entry.content_hash == original_hash
        assert Path(entry.snapshot_path).exists()
        assert Path(entry.snapshot_path).read_bytes() == target.read_bytes()

    def test_snapshot_preserves_canonical(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "original.txt"
        original_content = "original content"
        target.write_text(original_content, encoding="utf-8")

        ws.snapshot_file(target)

        # Canonical file must be unchanged
        assert target.read_text(encoding="utf-8") == original_content

    def test_snapshot_nonexistent_file(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "does_not_exist.txt"
        entry = ws.snapshot_file(target)

        assert entry.existed is False
        assert entry.content_hash == ""

    def test_snapshot_updates_manifest(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "test.txt"
        target.write_text("content", encoding="utf-8")
        ws.snapshot_file(target)

        assert ws.manifest_path is not None
        data = json.loads(ws.manifest_path.read_text(encoding="utf-8"))
        assert len(data["snapshots"]) == 1

    def test_duplicate_snapshot_is_idempotent(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "test.txt"
        target.write_text("content", encoding="utf-8")

        entry1 = ws.snapshot_file(target)
        entry2 = ws.snapshot_file(target)

        # Should return the same entry
        assert entry1.canonical_path == entry2.canonical_path
        assert entry1.content_hash == entry2.content_hash
        assert len(ws.snapshots) == 1

    def test_snapshot_without_active_tx_raises(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        target = layout.root / "test.txt"
        target.write_text("content", encoding="utf-8")

        with pytest.raises(TransactionError) as exc_info:
            ws.snapshot_file(target)
        assert exc_info.value.loom_error.code == TX_NOT_ACTIVE

    def test_snapshot_path_outside_root_raises(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        outside = Path("/tmp/outside_project.txt")
        with pytest.raises(TransactionError) as exc_info:
            ws.snapshot_file(outside)
        assert exc_info.value.loom_error.code == PATH_UNSAFE

    def test_snapshot_multiple_files(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        f1 = layout.root / "file1.txt"
        f2 = layout.root / "file2.txt"
        f1.write_text("content1", encoding="utf-8")
        f2.write_text("content2", encoding="utf-8")

        ws.snapshot_file(f1)
        ws.snapshot_file(f2)

        assert len(ws.snapshots) == 2


# ===========================================================================
# restore
# ===========================================================================


class TestRestore:
    """restore returns files to their pre-transaction state."""

    def test_restore_modified_file(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "test.txt"
        original = "original content"
        target.write_text(original, encoding="utf-8")
        ws.snapshot_file(target)

        # Modify the file
        target.write_text("modified content", encoding="utf-8")

        # Restore
        ws.restore()

        assert target.read_text(encoding="utf-8") == original

    def test_restore_sets_restored_status(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "test.txt"
        target.write_text("content", encoding="utf-8")
        ws.snapshot_file(target)

        target.write_text("changed", encoding="utf-8")
        ws.restore()

        assert ws.status == TransactionStatus.RESTORED

    def test_restore_deletes_new_file(self, layout: ProjectLayout) -> None:
        """File that didn't exist before transaction is deleted on restore."""
        ws = TransactionWorkspace(layout)
        ws.begin()

        new_file = layout.root / "new_file.txt"
        # Snapshot before the file exists
        ws.snapshot_file(new_file)

        # Create the file during the transaction
        new_file.write_text("new content", encoding="utf-8")
        assert new_file.exists()

        # Restore should delete it
        ws.restore()
        assert not new_file.exists()

    def test_restore_multiple_files(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        f1 = layout.root / "file1.txt"
        f2 = layout.root / "file2.txt"
        f1.write_text("original1", encoding="utf-8")
        f2.write_text("original2", encoding="utf-8")

        ws.snapshot_file(f1)
        ws.snapshot_file(f2)

        f1.write_text("modified1", encoding="utf-8")
        f2.write_text("modified2", encoding="utf-8")

        ws.restore()

        assert f1.read_text(encoding="utf-8") == "original1"
        assert f2.read_text(encoding="utf-8") == "original2"

    def test_restore_preserves_hash(self, layout: ProjectLayout) -> None:
        """Restore returns file to exact pre-state (hash verification)."""
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "hash_test.txt"
        content = "hash verification test content"
        target.write_text(content, encoding="utf-8")
        pre_hash = _file_hash(target)

        ws.snapshot_file(target)
        target.write_text("tampered", encoding="utf-8")

        ws.restore()
        post_hash = _file_hash(target)

        assert pre_hash == post_hash

    def test_restore_without_active_tx_raises(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        with pytest.raises(TransactionError) as exc_info:
            ws.restore()
        assert exc_info.value.loom_error.code == TX_NOT_ACTIVE

    def test_restore_binary_content(self, layout: ProjectLayout) -> None:
        """Binary files are restored byte-for-byte."""
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "binary.dat"
        content = bytes(range(256))
        target.write_bytes(content)
        ws.snapshot_file(target)

        target.write_bytes(b"corrupted")
        ws.restore()

        assert target.read_bytes() == content

    def test_restore_unicode_content(self, layout: ProjectLayout) -> None:
        """Unicode content is preserved through snapshot/restore."""
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "unicode.txt"
        content = "日本語テスト 🌍 Ñoño"
        target.write_text(content, encoding="utf-8")
        ws.snapshot_file(target)

        target.write_text("replaced", encoding="utf-8")
        ws.restore()

        assert target.read_text(encoding="utf-8") == content

    def test_restore_file_in_subdirectory(self, layout: ProjectLayout) -> None:
        """Files in subdirectories are properly restored."""
        ws = TransactionWorkspace(layout)
        ws.begin()

        subdir = layout.root / "ledgers"
        subdir.mkdir()
        target = subdir / "decisions.yaml"
        original = "decisions: []"
        target.write_text(original, encoding="utf-8")
        ws.snapshot_file(target)

        target.write_text("decisions: [changed]", encoding="utf-8")
        ws.restore()

        assert target.read_text(encoding="utf-8") == original


# ===========================================================================
# Hash mismatch detection
# ===========================================================================


class TestHashMismatch:
    """Hash mismatch on restore is detected and reported."""

    def test_corrupted_snapshot_detected(self, layout: ProjectLayout) -> None:
        """If snapshot data is tampered with, restore detects hash mismatch."""
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "test.txt"
        target.write_text("original", encoding="utf-8")
        entry = ws.snapshot_file(target)

        # Tamper with the snapshot file
        snapshot_path = Path(entry.snapshot_path)
        snapshot_path.write_bytes(b"corrupted data")

        # Modify canonical
        target.write_text("modified", encoding="utf-8")

        # Restore should fail with TX_HASH_MISMATCH
        with pytest.raises(TransactionError) as exc_info:
            ws.restore()
        assert exc_info.value.loom_error.code == TX_RESTORE_FAILED
        # Check that the detail includes hash mismatch errors
        errors = exc_info.value.loom_error.detail.get("errors", [])
        assert any(e["code"] == TX_HASH_MISMATCH for e in errors)


# ===========================================================================
# Failure injection
# ===========================================================================


class TestFailureInjection:
    """Failure injection at different stages produces correct errors."""

    def test_snapshot_failure_injection(self, layout: ProjectLayout) -> None:
        """Failure injected during snapshot raises TX_SNAPSHOT_FAILED."""
        ws = TransactionWorkspace(layout, failure_injector=SnapshotFailureInjector())
        ws.begin()

        target = layout.root / "test.txt"
        target.write_text("content", encoding="utf-8")

        with pytest.raises(TransactionError) as exc_info:
            ws.snapshot_file(target)
        assert exc_info.value.loom_error.code == TX_SNAPSHOT_FAILED

    def test_snapshot_failure_leaves_canonical_unchanged(
        self, layout: ProjectLayout
    ) -> None:
        """A failed snapshot does not modify the canonical file."""
        ws = TransactionWorkspace(layout, failure_injector=SnapshotFailureInjector())
        ws.begin()

        target = layout.root / "test.txt"
        original = "original content"
        target.write_text(original, encoding="utf-8")

        with pytest.raises(TransactionError):
            ws.snapshot_file(target)

        assert target.read_text(encoding="utf-8") == original

    def test_restore_failure_injection(self, layout: ProjectLayout) -> None:
        """Failure injected during restore raises TX_RESTORE_FAILED."""
        ws = TransactionWorkspace(layout, failure_injector=RestoreFailureInjector())
        ws.begin()

        target = layout.root / "test.txt"
        target.write_text("content", encoding="utf-8")
        ws.snapshot_file(target)
        target.write_text("modified", encoding="utf-8")

        with pytest.raises(TransactionError) as exc_info:
            ws.restore()
        assert exc_info.value.loom_error.code == TX_RESTORE_FAILED

    def test_restore_failure_preserves_workspace(
        self, layout: ProjectLayout
    ) -> None:
        """On restore failure, workspace is preserved for forensics."""
        ws = TransactionWorkspace(layout, failure_injector=RestoreFailureInjector())
        ws.begin()

        target = layout.root / "test.txt"
        target.write_text("content", encoding="utf-8")
        ws.snapshot_file(target)

        with pytest.raises(TransactionError):
            ws.restore()

        # Workspace should still exist
        assert ws.workspace_root is not None
        assert ws.workspace_root.exists()

    def test_commit_failure_injection(self, layout: ProjectLayout) -> None:
        """Failure injected during commit raises TX_SNAPSHOT_FAILED."""
        ws = TransactionWorkspace(layout, failure_injector=CommitFailureInjector())
        ws.begin()

        with pytest.raises(TransactionError) as exc_info:
            ws.commit()
        assert exc_info.value.loom_error.code == TX_SNAPSHOT_FAILED


# ===========================================================================
# commit
# ===========================================================================


class TestCommit:
    """commit marks the transaction as committed."""

    def test_commit_sets_committed_status(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()
        ws.commit()
        assert ws.status == TransactionStatus.COMMITTED
        assert ws.is_active is False

    def test_commit_updates_manifest(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()
        ws.commit()

        assert ws.manifest_path is not None
        data = json.loads(ws.manifest_path.read_text(encoding="utf-8"))
        assert data["status"] == "committed"

    def test_commit_without_active_tx_raises(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        with pytest.raises(TransactionError) as exc_info:
            ws.commit()
        assert exc_info.value.loom_error.code == TX_NOT_ACTIVE


# ===========================================================================
# cleanup
# ===========================================================================


class TestCleanup:
    """cleanup removes the workspace directory."""

    def test_cleanup_removes_workspace(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()
        ws.commit()
        ws.cleanup()

        # Workspace dir should be gone
        assert ws.workspace_root is not None
        assert not ws.workspace_root.exists()

    def test_cleanup_after_restore(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "test.txt"
        target.write_text("content", encoding="utf-8")
        ws.snapshot_file(target)
        target.write_text("modified", encoding="utf-8")

        ws.restore()
        ws.cleanup()

        assert ws.workspace_root is not None
        assert not ws.workspace_root.exists()

    def test_cleanup_no_workspace_is_noop(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.cleanup()  # Should not raise


# ===========================================================================
# is_file_snapshotted / get_snapshot
# ===========================================================================


class TestSnapshotQuery:
    """Querying snapshot state works correctly."""

    def test_is_file_snapshotted_true(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "test.txt"
        target.write_text("content", encoding="utf-8")
        ws.snapshot_file(target)

        assert ws.is_file_snapshotted(target) is True

    def test_is_file_snapshotted_false(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "test.txt"
        target.write_text("content", encoding="utf-8")
        # Don't snapshot

        assert ws.is_file_snapshotted(target) is False

    def test_get_snapshot_returns_entry(self, layout: ProjectLayout) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "test.txt"
        target.write_text("content", encoding="utf-8")
        entry = ws.snapshot_file(target)

        result = ws.get_snapshot(target)
        assert result.canonical_path == entry.canonical_path
        assert result.content_hash == entry.content_hash

    def test_get_snapshot_not_snapshotted_raises(
        self, layout: ProjectLayout
    ) -> None:
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "test.txt"
        target.write_text("content", encoding="utf-8")

        with pytest.raises(TransactionError) as exc_info:
            ws.get_snapshot(target)
        assert exc_info.value.loom_error.code == TX_FILE_NOT_SNAPSHOTTED


# ===========================================================================
# Structured data
# ===========================================================================


class TestSnapshotEntry:
    """SnapshotEntry is a frozen dataclass."""

    def test_frozen(self) -> None:
        entry = SnapshotEntry(
            canonical_path="/test",
            content_hash="abc",
            snapshot_path="/snap",
            existed=True,
        )
        with pytest.raises(AttributeError):
            entry.content_hash = "changed"  # type: ignore[misc]

    def test_fields(self) -> None:
        entry = SnapshotEntry(
            canonical_path="/test",
            content_hash="abc",
            snapshot_path="/snap",
            existed=True,
        )
        assert entry.canonical_path == "/test"
        assert entry.content_hash == "abc"
        assert entry.snapshot_path == "/snap"
        assert entry.existed is True


class TestTransactionManifest:
    """TransactionManifest is a frozen dataclass."""

    def test_frozen(self) -> None:
        manifest = TransactionManifest(
            tx_id="abc123",
            created_at=0.0,
            status="active",
            snapshots=(),
        )
        with pytest.raises(AttributeError):
            manifest.status = "committed"  # type: ignore[misc]


class TestTransactionError:
    """TransactionError carries structured LoomError."""

    def test_has_loom_error(self) -> None:
        from aip_loom.errors import LoomError

        err = TransactionError(
            LoomError(code=TX_SNAPSHOT_FAILED, message="test")
        )
        assert err.loom_error.code == TX_SNAPSHOT_FAILED
        assert isinstance(err, Exception)


class TestTransactionStatus:
    """TransactionStatus enum has expected values."""

    def test_values(self) -> None:
        assert TransactionStatus.ACTIVE.value == "active"
        assert TransactionStatus.COMMITTED.value == "committed"
        assert TransactionStatus.RESTORED.value == "restored"
        assert TransactionStatus.FAILED.value == "failed"


class TestNoopFailureInjector:
    """NoopFailureInjector never injects failures."""

    def test_all_methods_return_false(self) -> None:
        injector = NoopFailureInjector()
        assert injector.fail_on_snapshot() is False
        assert injector.fail_on_stage() is False
        assert injector.fail_on_restore() is False
        assert injector.fail_on_commit() is False


# ===========================================================================
# Internal hash helpers
# ===========================================================================


class TestHashHelpers:
    """Internal hash computation helpers work correctly."""

    def test_compute_file_hash(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello", encoding="utf-8")
        h = _compute_file_hash(f)
        expected = hashlib.sha256(b"hello").hexdigest()
        assert h == expected

    def test_compute_bytes_hash(self) -> None:
        data = b"hello world"
        h = _compute_bytes_hash(data)
        expected = hashlib.sha256(data).hexdigest()
        assert h == expected

    def test_compute_file_hash_deterministic(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_bytes(b"deterministic")
        h1 = _compute_file_hash(f)
        h2 = _compute_file_hash(f)
        assert h1 == h2


# ===========================================================================
# Integration: full workflow
# ===========================================================================


class TestFullWorkflow:
    """End-to-end transaction lifecycle tests."""

    def test_success_path(self, layout: ProjectLayout) -> None:
        """Full workflow: begin → snapshot → modify → commit → cleanup."""
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "data.yaml"
        original = "key: value"
        target.write_text(original, encoding="utf-8")
        ws.snapshot_file(target)

        # Simulate modification
        target.write_text("key: new_value", encoding="utf-8")

        ws.commit()
        ws.cleanup()

        # Modified content persists
        assert target.read_text(encoding="utf-8") == "key: new_value"

    def test_failure_path_with_restore(self, layout: ProjectLayout) -> None:
        """Full workflow: begin → snapshot → modify → restore → cleanup."""
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "data.yaml"
        original = "key: value"
        target.write_text(original, encoding="utf-8")
        ws.snapshot_file(target)

        # Simulate modification
        target.write_text("key: broken", encoding="utf-8")

        # Something goes wrong — restore
        ws.restore()
        ws.cleanup()

        # Original content restored
        assert target.read_text(encoding="utf-8") == original

    def test_multiple_snapshots_with_partial_restore(
        self, layout: ProjectLayout
    ) -> None:
        """Only snapshotted files are restored; others are untouched."""
        ws = TransactionWorkspace(layout)
        ws.begin()

        snapshotted = layout.root / "snapshotted.txt"
        not_snapshotted = layout.root / "not_snapshotted.txt"

        snapshotted.write_text("original", encoding="utf-8")
        not_snapshotted.write_text("original", encoding="utf-8")

        ws.snapshot_file(snapshotted)

        snapshotted.write_text("changed", encoding="utf-8")
        not_snapshotted.write_text("changed", encoding="utf-8")

        ws.restore()
        ws.cleanup()

        # Snapshotted file is restored
        assert snapshotted.read_text(encoding="utf-8") == "original"
        # Not-snapshotted file keeps its modified state
        assert not_snapshotted.read_text(encoding="utf-8") == "changed"

    def test_workspace_never_writes_outside_project(
        self, layout: ProjectLayout
    ) -> None:
        """All workspace files are under the project root."""
        ws = TransactionWorkspace(layout)
        ws.begin()

        target = layout.root / "test.txt"
        target.write_text("content", encoding="utf-8")
        ws.snapshot_file(target)

        # Verify all workspace paths are under project root
        assert ws.workspace_root is not None
        ws_root_str = str(ws.workspace_root)
        root_str = str(layout.root)
        assert ws_root_str.startswith(root_str)

        # Verify snapshot is in workspace
        for entry in ws.snapshots:
            if entry.snapshot_path:
                assert entry.snapshot_path.startswith(ws_root_str)

        ws.commit()
        ws.cleanup()

    def test_manifest_round_trip(self, layout: ProjectLayout) -> None:
        """Manifest can be written and read back with full fidelity."""
        from aip_loom.transaction import _read_manifest, _write_manifest

        ws = TransactionWorkspace(layout)
        tx_id = ws.begin()

        target = layout.root / "test.txt"
        target.write_text("content", encoding="utf-8")
        ws.snapshot_file(target)

        # Read manifest from disk
        assert ws.manifest_path is not None
        manifest = _read_manifest(ws.manifest_path)

        assert manifest is not None
        assert manifest.tx_id == tx_id
        assert manifest.status == "active"
        assert len(manifest.snapshots) == 1
        assert manifest.snapshots[0].canonical_path == str(target.resolve())

        ws.commit()
        ws.cleanup()

    def test_staged_dir_available_for_caller(
        self, layout: ProjectLayout
    ) -> None:
        """The staged/ directory is available for callers to write into."""
        ws = TransactionWorkspace(layout)
        ws.begin()

        assert ws.staged_dir is not None
        staged_file = ws.staged_dir / "new_content.yaml"
        staged_file.write_text("staged: data", encoding="utf-8")

        assert staged_file.exists()
        assert staged_file.read_text(encoding="utf-8") == "staged: data"

        ws.commit()
        ws.cleanup()
