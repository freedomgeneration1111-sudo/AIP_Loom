"""Stable error and warning code taxonomy for AIP_Loom.

This module is the single source of truth for all error and warning codes.
Every command must use codes from this module; no command may invent its own
ad-hoc code strings.  New codes are added here as new chunks are implemented.

The dataclasses ``LoomError`` and ``LoomWarning`` are the structured
representations carried inside ``CommandResult``.  They are frozen to prevent
mutation after construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Stable error codes
# ---------------------------------------------------------------------------

# General / command-level
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
UNKNOWN_COMMAND = "UNKNOWN_COMMAND"
INVALID_ARGUMENT = "INVALID_ARGUMENT"

# Project / filesystem
PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
PROJECT_ALREADY_EXISTS = "PROJECT_ALREADY_EXISTS"
PROJECT_MALFORMED = "PROJECT_MALFORMED"
FILE_NOT_FOUND = "FILE_NOT_FOUND"
FILE_READ_ERROR = "FILE_READ_ERROR"
FILE_WRITE_ERROR = "FILE_WRITE_ERROR"
PATH_UNSAFE = "PATH_UNSAFE"

# Schema / validation
SCHEMA_VERSION_UNKNOWN = "SCHEMA_VERSION_UNKNOWN"
SCHEMA_VERSION_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"
SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
FIELD_UNKNOWN = "FIELD_UNKNOWN"
FIELD_MISSING = "FIELD_MISSING"
FIELD_INVALID = "FIELD_INVALID"

# YAML
YAML_PARSE_ERROR = "YAML_PARSE_ERROR"
YAML_DUPLICATE_KEYS = "YAML_DUPLICATE_KEYS"
YAML_ANCHORS_ALIASES = "YAML_ANCHORS_ALIASES"
YAML_TAGS_REJECTED = "YAML_TAGS_REJECTED"
YAML_ROUND_TRIP_MISMATCH = "YAML_ROUND_TRIP_MISMATCH"

# Locking
LOCK_HELD = "LOCK_HELD"
LOCK_STALE = "LOCK_STALE"
LOCK_ACQUIRE_FAILED = "LOCK_ACQUIRE_FAILED"

# Transaction / reconcile
RECONCILE_PRE_VALIDATION_FAILED = "RECONCILE_PRE_VALIDATION_FAILED"
RECONCILE_STAGED_VALIDATION_FAILED = "RECONCILE_STAGED_VALIDATION_FAILED"
RECONCILE_POST_VALIDATION_FAILED = "RECONCILE_POST_VALIDATION_FAILED"
RECONCILE_RESTORED_AFTER_FAILURE = "RECONCILE_RESTORED_AFTER_FAILURE"
RECONCILE_PARTIAL_CORRUPTION = "RECONCILE_PARTIAL_CORRUPTION"
RECONCILE_APPLIED_BUT_GIT_FAILED = "RECONCILE_APPLIED_BUT_GIT_FAILED"

# Transaction workspace
TX_ALREADY_ACTIVE = "TX_ALREADY_ACTIVE"
TX_NOT_ACTIVE = "TX_NOT_ACTIVE"
TX_SNAPSHOT_FAILED = "TX_SNAPSHOT_FAILED"
TX_RESTORE_FAILED = "TX_RESTORE_FAILED"
TX_FILE_NOT_SNAPSHOTTED = "TX_FILE_NOT_SNAPSHOTTED"
TX_HASH_MISMATCH = "TX_HASH_MISMATCH"

# Update block
UPDATE_BLOCK_MISSING = "UPDATE_BLOCK_MISSING"
UPDATE_BLOCK_MULTIPLE = "UPDATE_BLOCK_MULTIPLE"
UPDATE_BLOCK_MALFORMED = "UPDATE_BLOCK_MALFORMED"
UPDATE_BLOCK_LEGACY_FENCE = "UPDATE_BLOCK_LEGACY_FENCE"
PATCH_MODE_UNSUPPORTED = "PATCH_MODE_UNSUPPORTED"
PROSE_EXTRACTION_AMBIGUOUS = "PROSE_EXTRACTION_AMBIGUOUS"
MODEL_ASSIGNED_ID = "MODEL_ASSIGNED_ID"

# Chunk / ID
CHUNK_NOT_FOUND = "CHUNK_NOT_FOUND"
CHUNK_ORDER_FALLBACK_USED = "CHUNK_ORDER_FALLBACK_USED"
CHUNK_ID_INVALID = "CHUNK_ID_INVALID"
ID_DUPLICATE = "ID_DUPLICATE"
CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"

# Git
GIT_NOT_REPO = "GIT_NOT_REPO"
GIT_DIRTY = "GIT_DIRTY"
GIT_COMMIT_FAILED = "GIT_COMMIT_FAILED"
GIT_BINARY_MISSING = "GIT_BINARY_MISSING"

# Brief / context
BRIEF_BUDGET_OVERFLOW = "BRIEF_BUDGET_OVERFLOW"
BRIEF_DIRTY_CHUNK = "BRIEF_DIRTY_CHUNK"
BRIEF_STALE_CHUNK = "BRIEF_STALE_CHUNK"
BRIEF_ORPHAN_CHUNK = "BRIEF_ORPHAN_CHUNK"
BRIEF_TEMPLATE_ERROR = "BRIEF_TEMPLATE_ERROR"

# Review state
REVIEW_STATE_PENDING = "REVIEW_STATE_PENDING"
AUTO_APPROVAL_BLOCKED = "AUTO_APPROVAL_BLOCKED"


# ---------------------------------------------------------------------------
# Stable warning codes
# ---------------------------------------------------------------------------

CHUNK_ORDER_FALLBACK_WARNING = "CHUNK_ORDER_FALLBACK_WARNING"
LEGACY_MANIFEST_DETECTED = "LEGACY_MANIFEST_DETECTED"
CHECKSUM_DIRTY = "CHECKSUM_DIRTY"
TOKEN_COUNT_APPROXIMATE = "TOKEN_COUNT_APPROXIMATE"
BRIEF_FORCE_USED = "BRIEF_FORCE_USED"
RECOVERY_FILE_EXISTS = "RECOVERY_FILE_EXISTS"
STALE_LOCK_DETECTED = "STALE_LOCK_DETECTED"
GIT_INIT_SKIPPED = "GIT_INIT_SKIPPED"


# ---------------------------------------------------------------------------
# Structured error / warning dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoomError:
    """A structured error with a stable code and optional detail.

    Attributes
    ----------
    code:
        Stable error-code string from this module (e.g. ``NOT_IMPLEMENTED``).
    message:
        Human-readable error description.
    detail:
        Optional machine-readable context (field name, path, etc.).
    """

    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoomWarning:
    """A structured warning with a stable code.

    Attributes
    ----------
    code:
        Stable warning-code string from this module.
    message:
        Human-readable warning description.
    detail:
        Optional machine-readable context.
    """

    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
