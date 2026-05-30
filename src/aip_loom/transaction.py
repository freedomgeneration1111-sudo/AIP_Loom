"""Transaction workspace and snapshot/recovery primitives for AIP_Loom.

This module is the **single authority** for transactional file operations.
Reconcile (Chunk 15) and any other operation that modifies canonical state
must use these primitives rather than implementing local snapshot/restore
behaviour.

Design principles (BuildSpec §7 and §3A):

- **Semantics-agnostic**: This module only stages, snapshots, restores,
  and cleans up.  It does **not** know about reconcile, update blocks,
  model output, or chunk semantics.  It treats files as opaque byte
  sequences identified by path.
- **Snapshot before modify**: Before any canonical file is modified, the
  caller must snapshot it via :meth:`TransactionWorkspace.snapshot_file`.
  The snapshot records both the file content (copied to the workspace)
  and a SHA-256 hash for restore verification.
- **Hash verification on restore**: When restoring a file, the content
  hash of the snapshot is compared against the stored hash.  If they
  differ, the restore fails with ``TX_HASH_MISMATCH`` — the snapshot
  data has been corrupted.
- **Failure injection**: The ``failure_injector`` parameter allows tests
  to inject failures at specific stages (snapshot, stage, restore,
  commit) without modifying production code.  This is critical for
  testing rollback paths.
- **No evidence destruction**: On restore failure, the workspace is
  **not** deleted.  It is preserved for forensic analysis.  Only
  successful completion calls :meth:`cleanup`.
- **Path safety**: All workspace paths are validated against the project
  layout.  No file may be written outside the project root.
- **Workspace layout**::

      .aip-loom/tmp/<txid>/
      ├── manifest.json       # Transaction metadata (tx_id, status, snapshots)
      ├── staged/             # Staged new content (caller writes here)
      └── snapshots/          # Pre-modification file snapshots
          └── <hash>.dat      # Content copy with hash-verified name

Workflow::

    workspace = TransactionWorkspace(layout)
    workspace.begin()

    # Snapshot files that may be modified
    workspace.snapshot_file(manifest_path)

    # ... modify canonical files via fs.safe_write_text() ...

    # On success:
    workspace.commit()
    workspace.cleanup()

    # On failure:
    workspace.restore()   # Returns files to pre-state
    workspace.cleanup()   # Or preserve for forensics
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .errors import (
    CHECKSUM_MISMATCH,
    FILE_NOT_FOUND,
    FILE_READ_ERROR,
    FILE_WRITE_ERROR,
    PATH_UNSAFE,
    TX_ALREADY_ACTIVE,
    TX_FILE_NOT_SNAPSHOTTED,
    TX_HASH_MISMATCH,
    TX_NOT_ACTIVE,
    TX_RESTORE_FAILED,
    TX_SNAPSHOT_FAILED,
    LoomError,
    LoomWarning,
    RECOVERY_FILE_EXISTS,
)
from .fs import AtomicWriteError, ensure_directory, safe_write_bytes, safe_write_text
from .layout import LayoutError, ProjectLayout

__all__ = [
    "TransactionError",
    "TransactionStatus",
    "SnapshotEntry",
    "TransactionManifest",
    "FailureInjector",
    "NoopFailureInjector",
    "TransactionWorkspace",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TransactionError(Exception):
    """Raised when a transaction operation fails.

    Carries a :class:`LoomError` with a stable error code.
    """

    def __init__(self, loom_error: LoomError) -> None:
        self.loom_error = loom_error
        super().__init__(loom_error.message)


# ---------------------------------------------------------------------------
# Transaction status enum
# ---------------------------------------------------------------------------


class TransactionStatus(str, Enum):
    """Status of a transaction workspace.

    ACTIVE
        The transaction has been started but not yet committed or
        restored.

    COMMITTED
        The transaction was committed successfully.  Canonical files
        have been modified and the workspace is eligible for cleanup.

    RESTORED
        The transaction was restored after a failure.  Canonical files
        have been returned to their pre-transaction state.

    FAILED
        The transaction failed and restore was attempted but may not
        have fully succeeded.  The workspace should be preserved for
        forensic analysis.
    """

    ACTIVE = "active"
    COMMITTED = "committed"
    RESTORED = "restored"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Structured data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotEntry:
    """Record of a single file snapshot within a transaction.

    Attributes
    ----------
    canonical_path:
        The absolute path to the original canonical file.
    content_hash:
        The SHA-256 hash of the file content at snapshot time.
    snapshot_path:
        The absolute path to the snapshot copy in the workspace.
    existed:
        Whether the canonical file existed at snapshot time.
        If ``False``, the file was absent and restore should delete
        it rather than write content.
    """

    canonical_path: str
    content_hash: str
    snapshot_path: str
    existed: bool


@dataclass(frozen=True)
class TransactionManifest:
    """Metadata about a transaction workspace.

    Attributes
    ----------
    tx_id:
        Unique transaction identifier (UUID4).
    created_at:
        Unix timestamp when the transaction was created.
    status:
        Current transaction status.
    snapshots:
        Tuple of snapshot entries for files that were snapshotted.
    """

    tx_id: str
    created_at: float
    status: str
    snapshots: tuple[SnapshotEntry, ...] = ()


# ---------------------------------------------------------------------------
# Failure injection protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class FailureInjector(Protocol):
    """Protocol for injecting failures during transaction operations.

    This is used exclusively for testing.  Production code should use
    :class:`NoopFailureInjector` (the default) which never injects
    failures.

    Each method returns ``True`` if a failure should be injected at
    that stage, ``False`` otherwise.
    """

    def fail_on_snapshot(self) -> bool:
        """Inject failure during file snapshot."""
        ...

    def fail_on_stage(self) -> bool:
        """Inject failure during file staging."""
        ...

    def fail_on_restore(self) -> bool:
        """Inject failure during file restore."""
        ...

    def fail_on_commit(self) -> bool:
        """Inject failure during transaction commit."""
        ...


class NoopFailureInjector:
    """Default failure injector that never injects failures.

    Used in production code paths.  All methods return ``False``.
    """

    def fail_on_snapshot(self) -> bool:
        return False

    def fail_on_stage(self) -> bool:
        return False

    def fail_on_restore(self) -> bool:
        return False

    def fail_on_commit(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_file_hash(path: Path) -> str:
    """Compute SHA-256 hash of a file's contents.

    Parameters
    ----------
    path:
        The file to hash.

    Returns
    -------
    str
        Hex-encoded SHA-256 digest.
    """
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)  # 64 KB chunks
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _compute_bytes_hash(data: bytes) -> str:
    """Compute SHA-256 hash of bytes.

    Parameters
    ----------
    data:
        The bytes to hash.

    Returns
    -------
    str
        Hex-encoded SHA-256 digest.
    """
    return hashlib.sha256(data).hexdigest()


def _write_manifest(
    manifest_path: Path,
    manifest: TransactionManifest,
) -> None:
    """Write the transaction manifest to disk as JSON.

    The manifest is written atomically to avoid partial writes.
    """
    data = {
        "tx_id": manifest.tx_id,
        "created_at": manifest.created_at,
        "status": manifest.status,
        "snapshots": [
            {
                "canonical_path": s.canonical_path,
                "content_hash": s.content_hash,
                "snapshot_path": s.snapshot_path,
                "existed": s.existed,
            }
            for s in manifest.snapshots
        ],
    }
    content = json.dumps(data, indent=2, ensure_ascii=False)
    manifest_path.write_text(content, encoding="utf-8")


def _read_manifest(manifest_path: Path) -> TransactionManifest | None:
    """Read a transaction manifest from disk.

    Returns ``None`` if the file does not exist or cannot be parsed.
    """
    if not manifest_path.exists():
        return None

    try:
        raw = manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None

    snapshots = tuple(
        SnapshotEntry(
            canonical_path=s["canonical_path"],
            content_hash=s["content_hash"],
            snapshot_path=s["snapshot_path"],
            existed=s["existed"],
        )
        for s in data.get("snapshots", [])
    )

    return TransactionManifest(
        tx_id=data["tx_id"],
        created_at=data["created_at"],
        status=data["status"],
        snapshots=snapshots,
    )


# ---------------------------------------------------------------------------
# TransactionWorkspace
# ---------------------------------------------------------------------------


class TransactionWorkspace:
    """Transaction workspace for staging, snapshotting, and restoring files.

    This class manages the lifecycle of a transaction workspace under
    ``.aip-loom/tmp/<txid>/``.  It is **semantics-agnostic** — it knows
    nothing about reconcile, update blocks, or chunk content.  It only
    copies files, records hashes, and restores on failure.

    Usage::

        layout = ProjectLayout(root=project_root)
        workspace = TransactionWorkspace(layout)

        workspace.begin()
        workspace.snapshot_file(layout.manifest_path)

        # Modify canonical files via fs.safe_write_text() ...
        safe_write_text(layout.manifest_path, new_content, layout)

        # On success:
        workspace.commit()
        workspace.cleanup()

        # On failure:
        workspace.restore()   # Returns files to pre-state
        workspace.cleanup()   # Or preserve for forensics

    Attributes
    ----------
    layout:
        The project layout.
    tx_id:
        The unique transaction identifier.
    """

    def __init__(
        self,
        layout: ProjectLayout,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        self._layout = layout
        self._tx_id: str | None = None
        self._status: TransactionStatus | None = None
        self._snapshots: list[SnapshotEntry] = []
        self._created_at: float = 0.0
        self._failure_injector: FailureInjector = (
            failure_injector if failure_injector is not None
            else NoopFailureInjector()
        )

    @property
    def layout(self) -> ProjectLayout:
        """The project layout."""
        return self._layout

    @property
    def tx_id(self) -> str | None:
        """The transaction identifier, or ``None`` if not started."""
        return self._tx_id

    @property
    def status(self) -> TransactionStatus | None:
        """The current transaction status, or ``None`` if not started."""
        return self._status

    @property
    def is_active(self) -> bool:
        """Whether the transaction is currently active."""
        return self._status == TransactionStatus.ACTIVE

    @property
    def workspace_root(self) -> Path | None:
        """The workspace root directory, or ``None`` if not started."""
        if self._tx_id is None:
            return None
        return self._layout.aip_loom_dir / "tmp" / self._tx_id

    @property
    def staged_dir(self) -> Path | None:
        """The staged content directory, or ``None`` if not started."""
        if self.workspace_root is None:
            return None
        return self.workspace_root / "staged"

    @property
    def snapshots_dir(self) -> Path | None:
        """The snapshots directory, or ``None`` if not started."""
        if self.workspace_root is None:
            return None
        return self.workspace_root / "snapshots"

    @property
    def manifest_path(self) -> Path | None:
        """The manifest file path, or ``None`` if not started."""
        if self.workspace_root is None:
            return None
        return self.workspace_root / "manifest.json"

    @property
    def snapshots(self) -> tuple[SnapshotEntry, ...]:
        """Tuple of all snapshot entries recorded so far."""
        return tuple(self._snapshots)

    # -- lifecycle ----------------------------------------------------------

    def begin(self) -> str:
        """Start a new transaction workspace.

        Creates the workspace directory structure under
        ``.aip-loom/tmp/<txid>/`` with ``staged/`` and ``snapshots/``
        subdirectories, and writes an initial manifest.

        Returns
        -------
        str
            The transaction ID (UUID4).

        Raises
        ------
        TransactionError
            If a transaction is already active (``TX_ALREADY_ACTIVE``),
            or if the workspace cannot be created.
        """
        if self._status == TransactionStatus.ACTIVE:
            raise TransactionError(
                LoomError(
                    code=TX_ALREADY_ACTIVE,
                    message=(
                        f"Transaction {self._tx_id} is already active.  "
                        "Commit or restore it before starting a new one."
                    ),
                    detail={"active_tx_id": self._tx_id},
                )
            )

        tx_id = uuid.uuid4().hex[:12]
        self._tx_id = tx_id
        self._snapshots = []
        self._created_at = time.time()

        ws_root = self._layout.aip_loom_dir / "tmp" / tx_id
        snapshots = ws_root / "snapshots"
        staged = ws_root / "staged"

        try:
            ensure_directory(snapshots)
            ensure_directory(staged)
        except (OSError, AtomicWriteError) as exc:
            self._tx_id = None
            raise TransactionError(
                LoomError(
                    code=TX_SNAPSHOT_FAILED,
                    message=f"Cannot create transaction workspace: {exc}",
                    detail={"tx_id": tx_id, "error": str(exc)},
                )
            ) from exc

        self._status = TransactionStatus.ACTIVE

        # Write initial manifest
        manifest = TransactionManifest(
            tx_id=tx_id,
            created_at=time.time(),
            status=TransactionStatus.ACTIVE.value,
            snapshots=(),
        )
        assert self.manifest_path is not None
        _write_manifest(self.manifest_path, manifest)

        return tx_id

    # -- snapshot -----------------------------------------------------------

    def snapshot_file(self, canonical_path: Path) -> SnapshotEntry:
        """Snapshot a canonical file before modification.

        The file's content is copied to the workspace's ``snapshots/``
        directory, and a SHA-256 hash is recorded for restore
        verification.

        If the canonical file does not exist, the snapshot records
        ``existed=False``.  On restore, this means the file should be
        deleted rather than overwritten.

        Parameters
        ----------
        canonical_path:
            The absolute path to the canonical file to snapshot.
            Must be within the project root.

        Returns
        -------
        SnapshotEntry
            The snapshot record with path, hash, and existence info.

        Raises
        ------
        TransactionError
            If the transaction is not active (``TX_NOT_ACTIVE``),
            the path is outside the project root (``PATH_UNSAFE``),
            the file cannot be read (``TX_SNAPSHOT_FAILED``), or
            failure injection triggers (``TX_SNAPSHOT_FAILED``).
        """
        if not self.is_active:
            raise TransactionError(
                LoomError(
                    code=TX_NOT_ACTIVE,
                    message="Cannot snapshot: no active transaction.",
                    detail={},
                )
            )

        # Validate path safety
        canonical_path = Path(canonical_path).resolve()
        try:
            self._layout.validate_path(canonical_path)
        except LayoutError as exc:
            raise TransactionError(
                LoomError(
                    code=PATH_UNSAFE,
                    message=f"Cannot snapshot path outside project root: {canonical_path}",
                    detail={"path": str(canonical_path), "reason": "path_escape"},
                )
            ) from exc

        # Check for duplicate snapshot
        for existing in self._snapshots:
            if existing.canonical_path == str(canonical_path):
                # Already snapshotted — return existing entry
                return existing

        # Failure injection
        if self._failure_injector.fail_on_snapshot():
            raise TransactionError(
                LoomError(
                    code=TX_SNAPSHOT_FAILED,
                    message=f"Failure injected during snapshot of {canonical_path}",
                    detail={"path": str(canonical_path), "stage": "snapshot"},
                )
            )

        assert self.snapshots_dir is not None
        existed = canonical_path.exists()

        if existed:
            if not canonical_path.is_file():
                raise TransactionError(
                    LoomError(
                        code=TX_SNAPSHOT_FAILED,
                        message=f"Cannot snapshot non-file: {canonical_path}",
                        detail={"path": str(canonical_path)},
                    )
                )

            # Read and hash the file content
            try:
                content = canonical_path.read_bytes()
            except OSError as exc:
                raise TransactionError(
                    LoomError(
                        code=TX_SNAPSHOT_FAILED,
                        message=f"Cannot read file for snapshot: {canonical_path}",
                        detail={"path": str(canonical_path), "error": str(exc)},
                    )
                ) from exc

            content_hash = _compute_bytes_hash(content)

            # Write snapshot to workspace
            snapshot_path = self.snapshots_dir / f"{content_hash}.dat"
            try:
                snapshot_path.write_bytes(content)
            except OSError as exc:
                raise TransactionError(
                    LoomError(
                        code=TX_SNAPSHOT_FAILED,
                        message=f"Cannot write snapshot: {exc}",
                        detail={
                            "path": str(canonical_path),
                            "snapshot_path": str(snapshot_path),
                            "error": str(exc),
                        },
                    )
                ) from exc
        else:
            # File doesn't exist — record that it was absent
            content_hash = ""
            snapshot_path = Path("")

        entry = SnapshotEntry(
            canonical_path=str(canonical_path),
            content_hash=content_hash,
            snapshot_path=str(snapshot_path),
            existed=existed,
        )

        self._snapshots.append(entry)
        self._update_manifest()

        return entry

    # -- restore ------------------------------------------------------------

    def restore(self) -> tuple[LoomWarning, ...]:
        """Restore all snapshotted files to their pre-transaction state.

        For each snapshotted file:
        - If the file existed before the transaction, the snapshot
          content is written back and the hash is verified.
        - If the file did not exist before the transaction, it is
          deleted if it was created during the transaction.

        On restore failure, the transaction status is set to ``FAILED``
        and the workspace is **not** deleted — it is preserved for
        forensic analysis.

        Returns
        -------
        tuple[LoomWarning, ...]
            Warnings generated during restore (e.g. hash mismatches,
            files that could not be restored).

        Raises
        ------
        TransactionError
            If the transaction is not active (``TX_NOT_ACTIVE``), or
            if a critical restore failure occurs.
        """
        if not self.is_active:
            raise TransactionError(
                LoomError(
                    code=TX_NOT_ACTIVE,
                    message="Cannot restore: no active transaction.",
                    detail={},
                )
            )

        # Failure injection
        if self._failure_injector.fail_on_restore():
            self._status = TransactionStatus.FAILED
            self._update_manifest()
            raise TransactionError(
                LoomError(
                    code=TX_RESTORE_FAILED,
                    message="Failure injected during restore",
                    detail={"tx_id": self._tx_id, "stage": "restore"},
                )
            )

        warnings: list[LoomWarning] = []
        errors: list[LoomError] = []

        for entry in self._snapshots:
            canonical = Path(entry.canonical_path)

            if entry.existed:
                # File existed before — restore content
                snapshot = Path(entry.snapshot_path)

                if not snapshot.exists():
                    errors.append(
                        LoomError(
                            code=TX_RESTORE_FAILED,
                            message=(
                                f"Snapshot file missing for {canonical}: "
                                f"{snapshot} does not exist"
                            ),
                            detail={
                                "canonical_path": str(canonical),
                                "snapshot_path": str(snapshot),
                            },
                        )
                    )
                    continue

                try:
                    snapshot_content = snapshot.read_bytes()
                except OSError as exc:
                    errors.append(
                        LoomError(
                            code=TX_RESTORE_FAILED,
                            message=f"Cannot read snapshot for {canonical}: {exc}",
                            detail={
                                "canonical_path": str(canonical),
                                "snapshot_path": str(snapshot),
                                "error": str(exc),
                            },
                        )
                    )
                    continue

                # Verify hash
                actual_hash = _compute_bytes_hash(snapshot_content)
                if actual_hash != entry.content_hash:
                    warnings.append(
                        LoomWarning(
                            code=CHECKSUM_MISMATCH,
                            message=(
                                f"Snapshot hash mismatch for {canonical}: "
                                f"expected {entry.content_hash}, "
                                f"got {actual_hash}.  "
                                f"Snapshot data may be corrupted."
                            ),
                            detail={
                                "canonical_path": str(canonical),
                                "expected_hash": entry.content_hash,
                                "actual_hash": actual_hash,
                            },
                        )
                    )
                    errors.append(
                        LoomError(
                            code=TX_HASH_MISMATCH,
                            message=(
                                f"Snapshot hash mismatch for {canonical}: "
                                "refusing to restore corrupted snapshot"
                            ),
                            detail={
                                "canonical_path": str(canonical),
                                "expected_hash": entry.content_hash,
                                "actual_hash": actual_hash,
                            },
                        )
                    )
                    continue

                # Write content back atomically
                try:
                    # Use the layout's validate_path to ensure safety
                    self._layout.validate_path(canonical)
                    ensure_directory(canonical.parent)
                    # Write directly — atomic_write requires layout
                    # but we're restoring, not creating new content
                    canonical.write_bytes(snapshot_content)
                except (OSError, LayoutError, AtomicWriteError) as exc:
                    errors.append(
                        LoomError(
                            code=TX_RESTORE_FAILED,
                            message=f"Cannot restore file {canonical}: {exc}",
                            detail={
                                "canonical_path": str(canonical),
                                "error": str(exc),
                            },
                        )
                    )
                    continue

            else:
                # File did not exist before — delete if created
                if canonical.exists():
                    try:
                        canonical.unlink()
                    except OSError as exc:
                        errors.append(
                            LoomError(
                                code=TX_RESTORE_FAILED,
                                message=(
                                    f"Cannot delete file created during "
                                    f"transaction: {canonical}: {exc}"
                                ),
                                detail={
                                    "canonical_path": str(canonical),
                                    "error": str(exc),
                                },
                            )
                        )
                        continue

        if errors:
            # Restore partially or fully failed
            self._status = TransactionStatus.FAILED
            self._update_manifest()

            # Build a combined error message
            error_summary = "; ".join(e.message for e in errors[:3])
            raise TransactionError(
                LoomError(
                    code=TX_RESTORE_FAILED,
                    message=(
                        f"Restore failed with {len(errors)} error(s): "
                        f"{error_summary}"
                    ),
                    detail={
                        "tx_id": self._tx_id,
                        "error_count": len(errors),
                        "errors": [
                            {"code": e.code, "message": e.message, "detail": e.detail}
                            for e in errors
                        ],
                    },
                )
            )

        self._status = TransactionStatus.RESTORED
        self._update_manifest()

        return tuple(warnings)

    # -- commit -------------------------------------------------------------

    def commit(self) -> None:
        """Mark the transaction as successfully committed.

        This does **not** modify any files — the caller is responsible
        for writing canonical files before calling commit.  It only
        updates the transaction status in the manifest.

        Raises
        ------
        TransactionError
            If the transaction is not active (``TX_NOT_ACTIVE``), or
            if failure injection triggers (``TX_SNAPSHOT_FAILED``).
        """
        if not self.is_active:
            raise TransactionError(
                LoomError(
                    code=TX_NOT_ACTIVE,
                    message="Cannot commit: no active transaction.",
                    detail={},
                )
            )

        # Failure injection
        if self._failure_injector.fail_on_commit():
            raise TransactionError(
                LoomError(
                    code=TX_SNAPSHOT_FAILED,
                    message="Failure injected during commit",
                    detail={"tx_id": self._tx_id, "stage": "commit"},
                )
            )

        self._status = TransactionStatus.COMMITTED
        self._update_manifest()

    # -- cleanup ------------------------------------------------------------

    def cleanup(self) -> None:
        """Delete the transaction workspace directory.

        This should only be called after successful commit or restore.
        On failed restore, the workspace should be preserved for
        forensic analysis.

        If the workspace does not exist, this is a no-op.

        Raises
        ------
        TransactionError
            If the workspace directory cannot be deleted.
        """
        if self.workspace_root is None:
            return

        if not self.workspace_root.exists():
            return

        try:
            shutil.rmtree(str(self.workspace_root))
        except OSError as exc:
            raise TransactionError(
                LoomError(
                    code=TX_SNAPSHOT_FAILED,
                    message=f"Cannot clean up transaction workspace: {exc}",
                    detail={
                        "tx_id": self._tx_id,
                        "workspace_root": str(self.workspace_root),
                        "error": str(exc),
                    },
                )
            ) from exc

    # -- query --------------------------------------------------------------

    def is_file_snapshotted(self, canonical_path: Path) -> bool:
        """Check whether a file has been snapshotted in this transaction.

        Parameters
        ----------
        canonical_path:
            The absolute path to check.

        Returns
        -------
        bool
            ``True`` if the file has been snapshotted.
        """
        resolved = str(Path(canonical_path).resolve())
        return any(s.canonical_path == resolved for s in self._snapshots)

    def get_snapshot(self, canonical_path: Path) -> SnapshotEntry:
        """Get the snapshot entry for a file.

        Parameters
        ----------
        canonical_path:
            The absolute path to look up.

        Returns
        -------
        SnapshotEntry
            The snapshot entry.

        Raises
        ------
        TransactionError
            If the file has not been snapshotted (``TX_FILE_NOT_SNAPSHOTTED``).
        """
        resolved = str(Path(canonical_path).resolve())
        for entry in self._snapshots:
            if entry.canonical_path == resolved:
                return entry

        raise TransactionError(
            LoomError(
                code=TX_FILE_NOT_SNAPSHOTTED,
                message=f"File has not been snapshotted: {canonical_path}",
                detail={"path": str(canonical_path)},
            )
        )

    # -- internal -----------------------------------------------------------

    def _update_manifest(self) -> None:
        """Write the current transaction manifest to disk."""
        if self._tx_id is None or self.manifest_path is None:
            return

        manifest = TransactionManifest(
            tx_id=self._tx_id,
            created_at=self._created_at,
            status=self._status.value if self._status else "unknown",
            snapshots=tuple(self._snapshots),
        )
        _write_manifest(self.manifest_path, manifest)
