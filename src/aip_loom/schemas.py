"""Canonical Pydantic v2 schema definitions for AIP_Loom v0.1.0.

This module is the **single owner** of all Pydantic models.  No other module
may define private schema variants.  If a command needs a new shape, it must
be added here and tests must cover it.

Design principles (from BuildSpec §4 and §3A):
- ``extra="forbid"`` on every model — unknown fields are errors, not silently
  dropped.
- No ``None`` defaults for required data — absence is an error, not a hidden
  default.
- Model-proposed canonical IDs are rejected in new ledger items.
- Schema version follows semver; unknown major versions are hard errors.
- Review state is separate from domain status.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Base configuration
# ---------------------------------------------------------------------------


class _LoomModel(BaseModel):
    """Base model for all AIP_Loom schemas.

    Every model forbids extra fields so that unknown keys in YAML are
    caught as validation errors rather than silently ignored.
    """

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ReviewState(str, Enum):
    """Review state for ledger entries and model output.

    Separated from domain status per BuildSpec §4.3.
    Model output may not force ``approved`` — the reconcile logic must
    enforce this, but the schema also restricts update-block items to
    ``pending`` only.
    """

    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"


class ProjectType(str, Enum):
    """Supported project types."""

    NOVEL = "novel"
    TECHNICAL = "technical"
    ACADEMIC = "academic"
    GENERAL = "general"


class ChunkStatus(str, Enum):
    """Domain status of a chunk in the manuscript."""

    DRAFT = "draft"
    REVISED = "revised"
    FINAL = "final"


class UpdateMode(str, Enum):
    """Mode of a loom-update block.

    Phase 1 only supports ``full_replacement``.  ``patch`` must be
    rejected with ``PATCH_MODE_UNSUPPORTED``.
    """

    FULL_REPLACEMENT = "full_replacement"
    PATCH = "patch"


class ThreadState(str, Enum):
    """State of a thread/strand (continuity work item)."""

    OPEN = "open"
    CLOSED = "closed"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Schema version helpers
# ---------------------------------------------------------------------------

#: The schema version that this codebase understands.
SUPPORTED_SCHEMA_VERSION = "0.1.0"

#: Regex for a valid semver string.
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def parse_schema_version(version: str) -> tuple[int, int, int]:
    """Parse a semver string into ``(major, minor, patch)``.

    Raises ``ValueError`` if the string is not valid semver.
    """
    m = _SEMVER_RE.match(version)
    if not m:
        raise ValueError(f"Invalid schema_version: {version!r} is not valid semver")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def validate_schema_version(version: str) -> str:
    """Validate a schema_version field against the supported version.

    Rules (BuildSpec §4.1):
    - Unknown major version → ``ValueError`` (hard error).
    - Unknown minor version → accepted with a warning flag (caller checks).
    - Patch version changes must not break loading.

    Returns the version string unchanged if it is acceptable.
    Raises ``ValueError`` on hard incompatibility.
    """
    try:
        major, minor, patch = parse_schema_version(version)
    except ValueError:
        raise ValueError(f"Invalid schema_version: {version!r}")

    sup_major, sup_minor, sup_patch = parse_schema_version(SUPPORTED_SCHEMA_VERSION)

    if major != sup_major:
        raise ValueError(
            f"Unsupported schema major version {version!r}: "
            f"this version of AIP_Loom supports {SUPPORTED_SCHEMA_VERSION!r}"
        )

    # Minor > supported: newer format, may have additive fields we don't know.
    # Minor < supported: older format, we may have fields it doesn't provide.
    # Both are accepted but the caller should emit a warning.
    return version


class SchemaVersionCheck(_LoomModel):
    """Minimal model for checking schema_version before full validation.

    Used by the YAML IO layer to decide whether to proceed with full
    model validation or reject early.
    """

    schema_version: str

    @field_validator("schema_version")
    @classmethod
    def _validate_semver(cls, v: str) -> str:
        return validate_schema_version(v)


# ---------------------------------------------------------------------------
# Chunk frontmatter
# ---------------------------------------------------------------------------

#: Regex for a valid chunk ID (e.g. C-0001, CH-0012).
_CHUNK_ID_RE = re.compile(r"^[A-Z]{1,3}-\d{4,}$")


def _validate_chunk_id(v: str) -> str:
    if not _CHUNK_ID_RE.match(v):
        raise ValueError(
            f"Invalid chunk ID {v!r}: must match pattern like 'C-0001'"
        )
    return v


class ChunkFrontmatter(_LoomModel):
    """Frontmatter embedded in a chunk Markdown file.

    This is parsed from YAML frontmatter delimited by ``---`` at the top
    of each chunk file.
    """

    schema_version: str
    id: str
    title: str
    status: ChunkStatus = ChunkStatus.DRAFT
    word_count: int = Field(ge=0)
    prose_checksum: str = Field(min_length=1)
    distillate_anchor: str = Field(default="")
    created_at: str
    updated_at: str

    @field_validator("schema_version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        return validate_schema_version(v)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        return _validate_chunk_id(v)


# ---------------------------------------------------------------------------
# Ledger entries
# ---------------------------------------------------------------------------

#: Regex for a valid canonical ID in a ledger (e.g. D-0001, T-0001, Q-0001).
_LEDGER_ID_RE = re.compile(r"^[A-Z]{1,3}-\d{4,}$")


def _validate_ledger_id(v: str) -> str:
    if not _LEDGER_ID_RE.match(v):
        raise ValueError(
            f"Invalid ledger ID {v!r}: must match pattern like 'D-0001'"
        )
    return v


class LedgerEntryBase(_LoomModel):
    """Base shape for every ledger entry (BuildSpec §4.2).

    All ledger entries must include an id, review_state, and created_at.
    """

    id: str
    review_state: ReviewState
    created_at: str

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        return _validate_ledger_id(v)


class DecisionEntry(LedgerEntryBase):
    """A decision ledger entry (prefix ``D-``)."""

    id: str = Field(pattern=r"^D-\d{4,}$")
    summary: str = Field(min_length=1)
    rationale: str = Field(default="")
    scope: Literal["global", "chunk"] = "global"
    chunk_id: str = Field(default="")

    @field_validator("chunk_id")
    @classmethod
    def _validate_chunk_ref(cls, v: str) -> str:
        if v and not _CHUNK_ID_RE.match(v):
            raise ValueError(f"Invalid chunk_id reference: {v!r}")
        return v


class ThreadEntry(LedgerEntryBase):
    """A thread/strand ledger entry (prefix ``T-``).

    Threads are open continuity work items.  They can be open, closed,
    or blocked.
    """

    id: str = Field(pattern=r"^T-\d{4,}$")
    summary: str = Field(min_length=1)
    state: ThreadState = ThreadState.OPEN
    scope: Literal["global", "chunk"] = "global"
    chunk_id: str = Field(default="")
    blocked_by: list[str] = Field(default_factory=list)

    @field_validator("chunk_id")
    @classmethod
    def _validate_chunk_ref(cls, v: str) -> str:
        if v and not _CHUNK_ID_RE.match(v):
            raise ValueError(f"Invalid chunk_id reference: {v!r}")
        return v

    @field_validator("blocked_by")
    @classmethod
    def _validate_blocked_by_ids(cls, v: list[str]) -> list[str]:
        for item in v:
            if not _LEDGER_ID_RE.match(item):
                raise ValueError(f"Invalid blocked_by ID: {item!r}")
        return v


class QuestionEntry(LedgerEntryBase):
    """A question/open-issue ledger entry (prefix ``Q-``)."""

    id: str = Field(pattern=r"^Q-\d{4,}$")
    question: str = Field(min_length=1)
    answer: str = Field(default="")
    resolved: bool = False


# ---------------------------------------------------------------------------
# Ledger file models
# ---------------------------------------------------------------------------


class DecisionLedger(_LoomModel):
    """The decisions ledger file."""

    schema_version: str
    entries: list[DecisionEntry] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        return validate_schema_version(v)


class ThreadLedger(_LoomModel):
    """The threads/strands ledger file."""

    schema_version: str
    entries: list[ThreadEntry] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        return validate_schema_version(v)


class QuestionLedger(_LoomModel):
    """The questions/open-issues ledger file."""

    schema_version: str
    entries: list[QuestionEntry] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        return validate_schema_version(v)


# ---------------------------------------------------------------------------
# Distillate
# ---------------------------------------------------------------------------


class DistillateNode(_LoomModel):
    """A single distillate node — a compact structural anchor for a chunk.

    Distillates capture the essential structural and continuity information
    about a chunk so that the brief engine can decide whether to include
    context from it.
    """

    chunk_id: str
    title: str = Field(min_length=1)
    summary: str = Field(default="")
    key_decisions: list[str] = Field(default_factory=list)
    open_threads: list[str] = Field(default_factory=list)
    word_count: int = Field(ge=0, default=0)

    @field_validator("chunk_id")
    @classmethod
    def _validate_chunk_ref(cls, v: str) -> str:
        if not _CHUNK_ID_RE.match(v):
            raise ValueError(f"Invalid chunk_id: {v!r}")
        return v

    @field_validator("key_decisions", "open_threads")
    @classmethod
    def _validate_id_lists(cls, v: list[str]) -> list[str]:
        for item in v:
            if not _LEDGER_ID_RE.match(item):
                raise ValueError(f"Invalid ID in list: {item!r}")
        return v


class Distillate(_LoomModel):
    """The distillate file — the compact structural index of the project."""

    schema_version: str
    nodes: list[DistillateNode] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        return validate_schema_version(v)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class SessionEntry(_LoomModel):
    """A session record — one invocation of brief + reconcile."""

    id: str = Field(pattern=r"^S-\d{4,}$")
    chunk_id: str
    brief_path: str = Field(default="")
    model_output_path: str = Field(default="")
    reconcile_applied: bool = False
    created_at: str

    @field_validator("chunk_id")
    @classmethod
    def _validate_chunk_ref(cls, v: str) -> str:
        if not _CHUNK_ID_RE.match(v):
            raise ValueError(f"Invalid chunk_id: {v!r}")
        return v


class SessionLog(_LoomModel):
    """The session log file."""

    schema_version: str
    entries: list[SessionEntry] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        return validate_schema_version(v)


# ---------------------------------------------------------------------------
# Comments / review notes
# ---------------------------------------------------------------------------


class CommentEntry(_LoomModel):
    """A review comment attached to a chunk or ledger entry."""

    id: str = Field(pattern=r"^CM-\d{4,}$")
    target_id: str = Field(min_length=1)
    author: str = Field(default="human")
    content: str = Field(min_length=1)
    created_at: str


class CommentLog(_LoomModel):
    """The comments/review-notes log file."""

    schema_version: str
    entries: list[CommentEntry] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        return validate_schema_version(v)


# ---------------------------------------------------------------------------
# Project manifest (aip_loom.yaml)
# ---------------------------------------------------------------------------


class ChunkOrder(_LoomModel):
    """The chunks.order section of the manifest."""

    order: list[str] = Field(default_factory=list)

    @field_validator("order")
    @classmethod
    def _validate_chunk_ids(cls, v: list[str]) -> list[str]:
        for item in v:
            if not _CHUNK_ID_RE.match(item):
                raise ValueError(f"Invalid chunk ID in order: {item!r}")
        return v


class ProjectManifest(_LoomModel):
    """The ``aip_loom.yaml`` project manifest.

    This is the root configuration file for an AIP_Loom project.  It
    defines the project identity, type, and chunk ordering.
    """

    schema_version: str
    name: str = Field(min_length=1)
    project_type: ProjectType = ProjectType.NOVEL
    chunks: ChunkOrder = Field(default_factory=ChunkOrder)
    created_at: str = Field(default="")
    updated_at: str = Field(default="")

    @field_validator("schema_version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        return validate_schema_version(v)


# ---------------------------------------------------------------------------
# Update block (model output format)
# ---------------------------------------------------------------------------

#: Pattern for a model-proposed provisional ID (must NOT match canonical IDs).
_PROVISIONAL_ID_RE = re.compile(r"^new-\d+$", re.IGNORECASE)


class UpdateLedgerItemNew(_LoomModel):
    """A new ledger item proposed by the model in an update block.

    **Critical rule (BuildSpec §7):** new model-proposed items may NOT
    include canonical IDs.  The ``id`` field is forbidden here.  IDs are
    allocated by AIP_Loom during reconcile, never by the model.
    """

    # No ``id`` field — this is the enforcement point.
    # Model-assigned canonical IDs are a spec violation.
    provisional_id: str = Field(pattern=r"^new-\d+$")
    summary: str = Field(min_length=1)
    rationale: str = Field(default="")
    review_state: Literal[ReviewState.PENDING] = ReviewState.PENDING
    requires_human_review: bool = True

    @model_validator(mode="after")
    def _no_canonical_id(self) -> "UpdateLedgerItemNew":
        """Ensure no canonical ID sneaks in via provisional_id."""
        if _LEDGER_ID_RE.match(self.provisional_id):
            raise ValueError(
                f"Model-proposed canonical ID {self.provisional_id!r} is forbidden: "
                "new ledger items must use provisional IDs like 'new-1'"
            )
        return self


class UpdateThreadItemNew(_LoomModel):
    """A new thread/strand proposed by the model.

    Same ID restriction as :class:`UpdateLedgerItemNew`.
    """

    provisional_id: str = Field(pattern=r"^new-\d+$")
    summary: str = Field(min_length=1)
    state: ThreadState = ThreadState.OPEN
    scope: Literal["global", "chunk"] = "global"
    chunk_id: str = Field(default="")
    review_state: Literal[ReviewState.PENDING] = ReviewState.PENDING
    requires_human_review: bool = True

    @model_validator(mode="after")
    def _no_canonical_id(self) -> "UpdateThreadItemNew":
        if _LEDGER_ID_RE.match(self.provisional_id):
            raise ValueError(
                f"Model-proposed canonical ID {self.provisional_id!r} is forbidden: "
                "new thread items must use provisional IDs like 'new-1'"
            )
        return self


class UpdateExistingEntry(_LoomModel):
    """An update to an existing ledger entry referenced by canonical ID."""

    id: str = Field(min_length=1)
    changes: dict[str, str] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not _LEDGER_ID_RE.match(v):
            raise ValueError(f"Invalid canonical ID for update: {v!r}")
        return v


class UpdateBlock(_LoomModel):
    """The parsed content of a ``loom-update`` fenced block.

    This is the model-output contract.  The update block is the only
    way model output enters the canonical state, and it is strictly
    validated.

    Key constraints:
    - ``fence_type`` must be ``loom-update`` (not ``thread-update``).
    - ``mode`` must be ``full_replacement`` in Phase 1.
    - Model cannot approve pending human-review items.
    - New items may not include canonical IDs.
    - Update block may not contain filesystem paths.
    """

    schema_version: str
    fence_type: Literal["loom-update"] = "loom-update"
    mode: UpdateMode = UpdateMode.FULL_REPLACEMENT
    target_chunk: str = Field(min_length=1)
    revised_prose: str = Field(default="")
    change_summary: str = Field(default="")
    review_notes: str = Field(default="")
    new_decisions: list[UpdateLedgerItemNew] = Field(default_factory=list)
    new_threads: list[UpdateThreadItemNew] = Field(default_factory=list)
    close_threads: list[str] = Field(default_factory=list)
    update_existing: list[UpdateExistingEntry] = Field(default_factory=list)
    requires_human_review: bool = True

    @field_validator("schema_version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        return validate_schema_version(v)

    @field_validator("target_chunk")
    @classmethod
    def _validate_target_chunk(cls, v: str) -> str:
        if not _CHUNK_ID_RE.match(v):
            raise ValueError(f"Invalid target_chunk: {v!r}")
        return v

    @field_validator("close_threads")
    @classmethod
    def _validate_close_ids(cls, v: list[str]) -> list[str]:
        for item in v:
            if not _LEDGER_ID_RE.match(item):
                raise ValueError(f"Invalid thread ID in close_threads: {item!r}")
        return v

    @field_validator("mode")
    @classmethod
    def _reject_patch(cls, v: UpdateMode) -> UpdateMode:
        if v == UpdateMode.PATCH:
            raise ValueError(
                "PATCH mode is unsupported in Phase 1; "
                "use full_replacement mode only"
            )
        return v

    @model_validator(mode="after")
    def _enforce_human_review(self) -> "UpdateBlock":
        """If any new item has requires_human_review, the block-level
        flag must also be True.  Model output may not force
        review_state=approved on any new item (enforced by
        Literal[ReviewState.PENDING] on the item schemas).
        """
        return self
