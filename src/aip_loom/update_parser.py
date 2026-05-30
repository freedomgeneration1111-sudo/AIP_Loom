"""Strict parser for model-output ``loom-update`` fenced blocks.

This module is the **single authority** and **only** parser for model-output
update blocks in AIP_Loom.  No other module may independently parse, extract,
or interpret model output — it must delegate to :func:`parse_model_output`
here.  This module is the security boundary between untrusted model output
and the rest of the system.

Design principles (BuildSpec §7, §3A, and Chunk 13 description):

- **Untrusted input**: Model output is treated as hostile.  Every field is
  validated; nothing is trusted by default.  No auto-correction, no guessing,
  no silent fallbacks.  Fail fast with helpful error messages.
- **Exact fence matching**: The fence type ``loom-update`` must match
  **exactly** — no near-miss variants (``loom_update``, ``Loom-Update``,
  ``LOOM-UPDATE``) are accepted.  The legacy ``thread-update`` fence is
  explicitly rejected with ``UPDATE_BLOCK_LEGACY_FENCE``.
- **Single block per response**: Exactly one ``loom-update`` block must be
  present.  Zero blocks → ``UPDATE_BLOCK_MISSING``; two or more →
  ``UPDATE_BLOCK_MULTIPLE``.
- **Strict YAML mode**: All YAML inside the block is parsed through
  :func:`yaml_io.load_yaml_string_as` with ``YamlMode.UPDATE_BLOCK``,
  which rejects anchors, aliases, tags, and duplicate keys.
- **Schema validation**: The parsed YAML is validated against
  :class:`UpdateBlock` from :mod:`aip_loom.schemas`, which enforces:
  ``extra="forbid"``, ``mode=full_replacement`` (patch rejected),
  canonical ID rejection in new items, and target chunk ID format.
- **Prose extraction**: For ``full_replacement`` mode, the revised prose
  is extracted from the Markdown section under the ``# Revised Chunk``
  heading, ending before ``# Change Summary`` or the next heading of
  equal or higher level.  Ambiguous extraction produces
  ``PROSE_EXTRACTION_AMBIGUOUS``.
- **Size/depth limits**: The raw content and YAML structure depth are
  bounded to prevent resource-exhaustion attacks from hostile model output.
- **Model-assigned ID rejection**: If the model attempts to assign canonical
  IDs (like ``D-0001``) to new ledger items, the parser rejects with
  ``MODEL_ASSIGNED_ID``.  Schema-level enforcement provides defense-in-depth.
- **Honest failure**: Every failure produces a :class:`CommandResult` with
  a stable error code, a human-readable message, and machine-readable detail.
  No failure is silent; no malformed input is accepted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .errors import (
    MODEL_ASSIGNED_ID,
    PATCH_MODE_UNSUPPORTED,
    PROSE_EXTRACTION_AMBIGUOUS,
    UPDATE_BLOCK_LEGACY_FENCE,
    UPDATE_BLOCK_MALFORMED,
    UPDATE_BLOCK_MISSING,
    UPDATE_BLOCK_MULTIPLE,
    YAML_ANCHORS_ALIASES,
    YAML_DUPLICATE_KEYS,
    YAML_PARSE_ERROR,
    YAML_TAGS_REJECTED,
    SCHEMA_VALIDATION_FAILED,
    LoomError,
)
from .results import CommandResult
from .schemas import (
    SUPPORTED_SCHEMA_VERSION,
    UpdateBlock,
    UpdateLedgerItemNew,
    UpdateThreadItemNew,
    _CHUNK_ID_RE,
    _LEDGER_ID_RE,
    _PROVISIONAL_ID_RE,
)
from .yaml_io import YamlLoadError, YamlMode, load_yaml_string_as

__all__ = [
    "ParsedUpdateBlock",
    "parse_model_output",
    "MAX_UPDATE_BLOCK_SIZE",
    "MAX_YAML_DEPTH",
    "FENCE_TYPE_LOOM_UPDATE",
    "FENCE_TYPE_THREAD_UPDATE",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The exact fence type string for loom-update blocks.
FENCE_TYPE_LOOM_UPDATE = "loom-update"

#: The legacy fence type string that must be explicitly rejected.
FENCE_TYPE_THREAD_UPDATE = "thread-update"

#: Maximum size of the raw model output text (in characters).
#: This prevents resource-exhaustion from absurdly large model output.
MAX_UPDATE_BLOCK_SIZE = 500_000

#: Maximum nesting depth for the YAML structure.
MAX_YAML_DEPTH = 10

#: Regex for the opening fence of a loom-update block.
#: Matches ``` ` `` `loom-update` ``` or ``` ` ```loom-update``` ```.
_FENCE_OPEN_RE = re.compile(
    r"^```" + re.escape(FENCE_TYPE_LOOM_UPDATE) + r"\s*$",
    re.MULTILINE,
)

#: Regex for the opening fence of a thread-update block (legacy).
_FENCE_OPEN_THREAD_UPDATE_RE = re.compile(
    r"^```" + re.escape(FENCE_TYPE_THREAD_UPDATE) + r"\s*$",
    re.MULTILINE,
)

#: Regex for a closing fence (three or more backticks on their own line).
_FENCE_CLOSE_RE = re.compile(r"^```\s*$", re.MULTILINE)

#: Regex for the "Revised Chunk" heading (H1 or H2).
_REVISED_CHUNK_HEADING_RE = re.compile(
    r"^#{1,2}\s+Revised\s+Chunk\s*$",
    re.MULTILINE,
)

#: Regex for the "Change Summary" heading (H1 or H2).
_CHANGE_SUMMARY_HEADING_RE = re.compile(
    r"^#{1,2}\s+Change\s+Summary\s*$",
    re.MULTILINE,
)

#: Regex for any heading (H1-H6) — used for prose boundary detection.
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedUpdateBlock:
    """The result of successfully parsing a model-output update block.

    This is an immutable, validated representation of the model's proposed
    changes.  It contains the fully validated :class:`UpdateBlock` schema
    instance, the extracted revised prose, and metadata about the parse.

    Attributes
    ----------
    update_block:
        The fully validated UpdateBlock schema instance.
    revised_prose:
        The extracted revised prose text from the ``# Revised Chunk``
        section.  Empty string if no prose section was found (which
        may be valid if the model only proposes ledger changes).
    raw_content:
        The raw text content between the fences (before YAML parsing).
    fence_start:
        The character offset where the opening fence begins.
    fence_end:
        The character offset where the closing fence ends.
    """

    update_block: UpdateBlock
    revised_prose: str
    raw_content: str
    fence_start: int
    fence_end: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary of the parsed block."""
        return {
            "target_chunk": self.update_block.target_chunk,
            "mode": self.update_block.mode.value,
            "fence_type": self.update_block.fence_type,
            "revised_prose_length": len(self.revised_prose),
            "raw_content_length": len(self.raw_content),
            "new_decisions_count": len(self.update_block.new_decisions),
            "new_threads_count": len(self.update_block.new_threads),
            "close_threads_count": len(self.update_block.close_threads),
            "update_existing_count": len(self.update_block.update_existing),
            "requires_human_review": self.update_block.requires_human_review,
            "fence_start": self.fence_start,
            "fence_end": self.fence_end,
        }


# ---------------------------------------------------------------------------
# Internal helpers — fence scanning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FenceMatch:
    """A matched fence pair (opening and closing)."""

    fence_type: str
    open_start: int
    open_end: int
    close_start: int
    close_end: int
    content: str  # text between fences


def _scan_fences(text: str) -> tuple[list[_FenceMatch], list[_FenceMatch], list[_FenceMatch]]:
    """Scan text for fenced code blocks and categorise them.

    Returns
    -------
    tuple of (loom_update_fences, thread_update_fences, unknown_fences)
        Lists of :class:`_FenceMatch` instances for each category.
    """
    loom_update_fences: list[_FenceMatch] = []
    thread_update_fences: list[_FenceMatch] = []
    unknown_fences: list[_FenceMatch] = []

    # Find all opening fences (both loom-update and thread-update)
    all_opens: list[tuple[int, int, str]] = []  # (start, end, fence_type)

    for m in _FENCE_OPEN_RE.finditer(text):
        all_opens.append((m.start(), m.end(), FENCE_TYPE_LOOM_UPDATE))

    for m in _FENCE_OPEN_THREAD_UPDATE_RE.finditer(text):
        all_opens.append((m.start(), m.end(), FENCE_TYPE_THREAD_UPDATE))

    # Sort by position
    all_opens.sort(key=lambda x: x[0])

    # For each opening fence, find the matching closing fence
    for open_start, open_end, fence_type in all_opens:
        # Search for closing fence after the opening
        close_match = _FENCE_CLOSE_RE.search(text, pos=open_end)
        if close_match is None:
            # No closing fence found — record as unknown (will be caught
            # later by the validation logic)
            unknown_fences.append(
                _FenceMatch(
                    fence_type=fence_type,
                    open_start=open_start,
                    open_end=open_end,
                    close_start=-1,
                    close_end=-1,
                    content="",
                )
            )
            continue

        close_start = close_match.start()
        close_end = close_match.end()

        # Extract content between fences
        content = text[open_end:close_start]
        # Strip leading newline if present
        if content.startswith("\n"):
            content = content[1:]

        match = _FenceMatch(
            fence_type=fence_type,
            open_start=open_start,
            open_end=open_end,
            close_start=close_start,
            close_end=close_end,
            content=content,
        )

        if fence_type == FENCE_TYPE_LOOM_UPDATE:
            loom_update_fences.append(match)
        elif fence_type == FENCE_TYPE_THREAD_UPDATE:
            thread_update_fences.append(match)
        else:
            unknown_fences.append(match)

    return loom_update_fences, thread_update_fences, unknown_fences


# ---------------------------------------------------------------------------
# Internal helpers — YAML depth measurement
# ---------------------------------------------------------------------------


def _measure_depth(data: Any, current: int = 0) -> int:
    """Measure the maximum nesting depth of a data structure.

    Used to enforce depth limits on parsed YAML.
    """
    if isinstance(data, dict):
        if not data:
            return current
        return max(_measure_depth(v, current + 1) for v in data.values())
    elif isinstance(data, list):
        if not data:
            return current
        return max(_measure_depth(v, current + 1) for v in data)
    return current


# ---------------------------------------------------------------------------
# Internal helpers — prose extraction
# ---------------------------------------------------------------------------


def _extract_revised_prose(markdown_text: str) -> tuple[str, LoomError | None]:
    """Extract the revised prose from the Markdown text of an update block.

    For ``full_replacement`` mode, the revised prose is the content under
    the ``# Revised Chunk`` heading.  It ends before the ``# Change Summary``
    heading or any heading of equal/higher level.

    Parameters
    ----------
    markdown_text:
        The full Markdown text from the update block (after the YAML
        frontmatter has been removed).

    Returns
    -------
    tuple[str, LoomError | None]
        The extracted prose and an optional error.  If extraction is
        ambiguous (e.g. multiple ``# Revised Chunk`` headings), returns
        an empty string and a ``PROSE_EXTRACTION_AMBIGUOUS`` error.
    """
    # Find all "Revised Chunk" headings
    revised_matches = list(_REVISED_CHUNK_HEADING_RE.finditer(markdown_text))

    if not revised_matches:
        # No "Revised Chunk" section — this is valid if the model only
        # proposes ledger changes (no prose replacement).
        return "", None

    if len(revised_matches) > 1:
        # Multiple "Revised Chunk" headings — ambiguous
        return "", LoomError(
            code=PROSE_EXTRACTION_AMBIGUOUS,
            message=(
                "Multiple '# Revised Chunk' headings found in the update "
                "block.  Exactly one is required for prose extraction."
            ),
            detail={
                "heading_count": len(revised_matches),
                "positions": [m.start() for m in revised_matches],
            },
        )

    revised_match = revised_matches[0]
    prose_start = revised_match.end()

    # Skip past the heading line and any blank lines immediately after
    remaining = markdown_text[prose_start:]
    if remaining.startswith("\n"):
        remaining = remaining[1:]

    # Find the end of the prose section:
    # 1. "# Change Summary" heading
    # 2. Any heading of equal or higher level (H1 or H2)
    # 3. End of text

    # Look for "# Change Summary" first
    change_match = _CHANGE_SUMMARY_HEADING_RE.search(remaining)
    if change_match:
        prose_text = remaining[:change_match.start()]
    else:
        # Look for any H1 or H2 heading after the Revised Chunk heading
        # that would indicate a new section
        heading_pattern = re.compile(r"^#{1,2}\s+", re.MULTILINE)
        heading_match = heading_pattern.search(remaining)
        if heading_match:
            # Only cut if the heading is NOT the Revised Chunk heading
            prose_text = remaining[:heading_match.start()]
        else:
            prose_text = remaining

    # Strip trailing whitespace but preserve internal structure
    prose_text = prose_text.rstrip("\n")

    return prose_text, None


# ---------------------------------------------------------------------------
# Internal helpers — model-assigned ID detection
# ---------------------------------------------------------------------------


def _check_model_assigned_ids(block: UpdateBlock) -> LoomError | None:
    """Check if the model attempted to assign canonical IDs.

    The schema already rejects canonical IDs in ``provisional_id`` fields
    (via pattern validation), but we also check here as defense-in-depth
    and to provide a clearer error message with the ``MODEL_ASSIGNED_ID``
    code.

    Parameters
    ----------
    block:
        The validated UpdateBlock instance.

    Returns
    -------
    LoomError | None
        An error if model-assigned IDs were detected, or None.
    """
    offending_ids: list[str] = []

    for item in block.new_decisions:
        if _LEDGER_ID_RE.match(item.provisional_id):
            offending_ids.append(item.provisional_id)

    for item in block.new_threads:
        if _LEDGER_ID_RE.match(item.provisional_id):
            offending_ids.append(item.provisional_id)

    if offending_ids:
        return LoomError(
            code=MODEL_ASSIGNED_ID,
            message=(
                f"Model attempted to assign canonical ID(s): "
                f"{', '.join(offending_ids[:5])}.  New ledger items must "
                f"use provisional IDs like 'new-1'.  IDs are allocated "
                f"by AIP_Loom during reconcile, never by the model."
            ),
            detail={
                "offending_ids": offending_ids[:10],
                "count": len(offending_ids),
            },
        )

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_model_output(text: str) -> CommandResult:
    """Parse model output text and extract a validated loom-update block.

    This is the **single entry point** for parsing model output in AIP_Loom.
    No other function may independently parse, extract, or interpret model
    output.  This function is the security boundary between untrusted model
    output and the rest of the system.

    The parser enforces these rules strictly:

    1. **Size limit**: The raw text must not exceed ``MAX_UPDATE_BLOCK_SIZE``.
    2. **Fence validation**: Exactly one ``loom-update`` fence must be present.
       Zero fences → ``UPDATE_BLOCK_MISSING``.  Two or more →
       ``UPDATE_BLOCK_MULTIPLE``.  Legacy ``thread-update`` fences →
       ``UPDATE_BLOCK_LEGACY_FENCE``.
    3. **YAML strictness**: The content inside the fence is parsed with
       ``YamlMode.UPDATE_BLOCK``, which rejects anchors, aliases, tags,
       and duplicate keys.
    4. **Schema validation**: The parsed YAML must validate against
       :class:`UpdateBlock`.  Patch mode is rejected with
       ``PATCH_MODE_UNSUPPORTED``.  Unknown fields are rejected.
    5. **Model-assigned IDs**: Canonical IDs in new ledger items are
       rejected with ``MODEL_ASSIGNED_ID``.
    6. **Depth limit**: The YAML structure must not exceed
       ``MAX_YAML_DEPTH``.
    7. **Prose extraction**: If ``mode=full_replacement``, the revised
       prose is extracted from the ``# Revised Chunk`` heading.  Ambiguous
       extraction → ``PROSE_EXTRACTION_AMBIGUOUS``.

    Parameters
    ----------
    text:
        The raw model output text (potentially multi-paragraph Markdown
        with one or more fenced code blocks).

    Returns
    -------
    CommandResult
        On success, ``data["parsed"]`` contains a dictionary representation
        of the :class:`ParsedUpdateBlock`.  On failure, the result carries
        stable error codes and detailed diagnostic information.
    """
    errors: list[LoomError] = []

    # ---------------------------------------------------------------
    # 1. Size limit check
    # ---------------------------------------------------------------
    if len(text) > MAX_UPDATE_BLOCK_SIZE:
        errors.append(
            LoomError(
                code=UPDATE_BLOCK_MALFORMED,
                message=(
                    f"Model output exceeds maximum size limit "
                    f"({len(text)} > {MAX_UPDATE_BLOCK_SIZE} characters).  "
                    f"This may be a sign of a runaway model response or "
                    f"a resource-exhaustion attempt."
                ),
                detail={
                    "actual_size": len(text),
                    "max_size": MAX_UPDATE_BLOCK_SIZE,
                },
            )
        )
        return CommandResult.failure(
            command="parse_update",
            code=UPDATE_BLOCK_MALFORMED,
            message="Model output exceeds size limit",
            errors=errors,
        )

    # ---------------------------------------------------------------
    # 2. Scan for fences
    # ---------------------------------------------------------------
    loom_fences, thread_fences, unknown_fences = _scan_fences(text)

    # Check for legacy thread-update fences FIRST (before missing block
    # check) — if the model used the wrong fence, we want to tell them
    # explicitly rather than just "missing block".
    if thread_fences:
        # If there are thread-update fences, report them regardless of
        # whether loom-update fences also exist
        positions = [f.open_start for f in thread_fences]
        errors.append(
            LoomError(
                code=UPDATE_BLOCK_LEGACY_FENCE,
                message=(
                    f"Found {len(thread_fences)} 'thread-update' fence(s) "
                    f"in model output.  The 'thread-update' fence type is "
                    f"not supported in Phase 1.  Use 'loom-update' instead."
                ),
                detail={
                    "count": len(thread_fences),
                    "positions": positions[:5],
                },
            )
        )
        return CommandResult.failure(
            command="parse_update",
            code=UPDATE_BLOCK_LEGACY_FENCE,
            message="Legacy 'thread-update' fence found; use 'loom-update'",
            errors=errors,
        )

    # Check for zero loom-update blocks
    if not loom_fences:
        errors.append(
            LoomError(
                code=UPDATE_BLOCK_MISSING,
                message=(
                    "No 'loom-update' fenced block found in model output.  "
                    "Every model response must contain exactly one "
                    "```loom-update block with the proposed changes."
                ),
                detail={
                    "text_length": len(text),
                    "unknown_fences": len(unknown_fences),
                },
            )
        )
        return CommandResult.failure(
            command="parse_update",
            code=UPDATE_BLOCK_MISSING,
            message="No loom-update block found in model output",
            errors=errors,
        )

    # Check for multiple loom-update blocks
    if len(loom_fences) > 1:
        positions = [f.open_start for f in loom_fences]
        errors.append(
            LoomError(
                code=UPDATE_BLOCK_MULTIPLE,
                message=(
                    f"Found {len(loom_fences)} 'loom-update' blocks in "
                    f"model output.  Exactly one is required.  Multiple "
                    f"blocks indicate ambiguous or conflicting updates."
                ),
                detail={
                    "count": len(loom_fences),
                    "positions": positions[:10],
                },
            )
        )
        return CommandResult.failure(
            command="parse_update",
            code=UPDATE_BLOCK_MULTIPLE,
            message="Multiple loom-update blocks found; exactly one required",
            errors=errors,
        )

    # Exactly one fence — proceed
    fence = loom_fences[0]
    raw_content = fence.content

    # Check for unclosed fence (should not happen given our scan logic,
    # but defense-in-depth)
    if fence.close_start == -1:
        errors.append(
            LoomError(
                code=UPDATE_BLOCK_MALFORMED,
                message=(
                    "The 'loom-update' block is missing its closing fence "
                    "(```).  Every opening ```loom-update must have a "
                    "matching closing ```."
                ),
                detail={"fence_start": fence.open_start},
            )
        )
        return CommandResult.failure(
            command="parse_update",
            code=UPDATE_BLOCK_MALFORMED,
            message="Unclosed loom-update fence",
            errors=errors,
        )

    # ---------------------------------------------------------------
    # 3. Check for empty content
    # ---------------------------------------------------------------
    if not raw_content.strip():
        errors.append(
            LoomError(
                code=UPDATE_BLOCK_MALFORMED,
                message=(
                    "The 'loom-update' block is empty — it contains no "
                    "YAML or Markdown content between the fences."
                ),
                detail={"fence_start": fence.open_start},
            )
        )
        return CommandResult.failure(
            command="parse_update",
            code=UPDATE_BLOCK_MALFORMED,
            message="Empty loom-update block",
            errors=errors,
        )

    # ---------------------------------------------------------------
    # 4. Separate YAML frontmatter from Markdown body
    # ---------------------------------------------------------------
    # The update block format is:
    #   ```loom-update
    #   <YAML frontmatter>
    #   ---
    #   <Markdown body with # Revised Chunk, # Change Summary, etc.>
    #   ```
    #
    # The YAML frontmatter is delimited by --- at the start and a
    # closing --- before the Markdown body.  This mirrors the chunk
    # file format.

    yaml_str, markdown_body = _split_update_block_content(raw_content)

    # ---------------------------------------------------------------
    # 5. Parse YAML with strict UPDATE_BLOCK mode
    # ---------------------------------------------------------------
    try:
        update_block = load_yaml_string_as(
            yaml_str,
            UpdateBlock,
            mode=YamlMode.UPDATE_BLOCK,
            source_label="loom-update block",
        )
    except TypeError as exc:
        # This happens when the YAML content is not a mapping (e.g. a list)
        # and thus cannot be unpacked into the Pydantic model.  We catch
        # it here and convert to a proper error.
        errors.append(
            LoomError(
                code=UPDATE_BLOCK_MALFORMED,
                message=(
                    f"Update block YAML must be a mapping/dictionary, "
                    f"not a {type(exc).__name__}.  The model output "
                    f"must be a YAML mapping with the required fields."
                ),
                detail={"original_error": str(exc)},
            )
        )
        return CommandResult.failure(
            command="parse_update",
            code=UPDATE_BLOCK_MALFORMED,
            message="Update block YAML must be a mapping",
            errors=errors,
        )
    except YamlLoadError as exc:
        # Map YamlLoadError codes to update-parser codes
        code = exc.loom_error.code
        message = exc.loom_error.message
        detail = exc.loom_error.detail

        # Special handling for patch mode — the schema already rejects
        # it, but we want to surface PATCH_MODE_UNSUPPORTED specifically
        if "PATCH mode" in message or "patch" in str(detail).lower():
            errors.append(
                LoomError(
                    code=PATCH_MODE_UNSUPPORTED,
                    message=(
                        "PATCH mode is not supported in Phase 1.  "
                        "Use 'full_replacement' mode only.  The model "
                        "must provide the complete revised prose, not a "
                        "diff or patch."
                    ),
                    detail={"original_error": message},
                )
            )
            return CommandResult.failure(
                command="parse_update",
                code=PATCH_MODE_UNSUPPORTED,
                message="PATCH mode is unsupported; use full_replacement",
                errors=errors,
            )

        # Schema validation failure — check if it's a fence_type issue
        if "thread-update" in str(detail).lower():
            errors.append(
                LoomError(
                    code=UPDATE_BLOCK_LEGACY_FENCE,
                    message=(
                        "The update block uses 'thread-update' as the "
                        "fence_type value, which is not supported.  The "
                        "fence_type must be 'loom-update'."
                    ),
                    detail={"original_error": message},
                )
            )
            return CommandResult.failure(
                command="parse_update",
                code=UPDATE_BLOCK_LEGACY_FENCE,
                message="Legacy fence_type 'thread-update' in YAML",
                errors=errors,
            )

        # General schema/YAML error
        errors.append(
            LoomError(
                code=UPDATE_BLOCK_MALFORMED,
                message=f"Update block YAML validation failed: {message}",
                detail=detail,
            )
        )
        return CommandResult.failure(
            command="parse_update",
            code=UPDATE_BLOCK_MALFORMED,
            message="Update block validation failed",
            errors=errors,
        )

    # ---------------------------------------------------------------
    # 6. Depth limit check
    # ---------------------------------------------------------------
    # Convert to plain dict for depth measurement
    plain_dict = update_block.model_dump(mode="json")
    depth = _measure_depth(plain_dict)
    if depth > MAX_YAML_DEPTH:
        errors.append(
            LoomError(
                code=UPDATE_BLOCK_MALFORMED,
                message=(
                    f"Update block YAML structure exceeds maximum depth "
                    f"({depth} > {MAX_YAML_DEPTH}).  This may indicate "
                    f"an attempt to create deeply nested structures."
                ),
                detail={
                    "actual_depth": depth,
                    "max_depth": MAX_YAML_DEPTH,
                },
            )
        )
        return CommandResult.failure(
            command="parse_update",
            code=UPDATE_BLOCK_MALFORMED,
            message="Update block exceeds YAML depth limit",
            errors=errors,
        )

    # ---------------------------------------------------------------
    # 7. Model-assigned ID check (defense-in-depth)
    # ---------------------------------------------------------------
    id_error = _check_model_assigned_ids(update_block)
    if id_error is not None:
        errors.append(id_error)
        return CommandResult.failure(
            command="parse_update",
            code=MODEL_ASSIGNED_ID,
            message=id_error.message,
            errors=errors,
        )

    # ---------------------------------------------------------------
    # 8. Prose extraction (for full_replacement mode)
    # ---------------------------------------------------------------
    revised_prose = ""
    if update_block.mode.value == "full_replacement" and markdown_body.strip():
        prose, prose_error = _extract_revised_prose(markdown_body)
        if prose_error is not None:
            errors.append(prose_error)
            return CommandResult.failure(
                command="parse_update",
                code=PROSE_EXTRACTION_AMBIGUOUS,
                message=prose_error.message,
                errors=errors,
            )
        revised_prose = prose

    # If the schema already has revised_prose set, use that as the
    # authoritative source.  Only fall back to Markdown extraction if
    # the schema field is empty.
    if update_block.revised_prose:
        revised_prose = update_block.revised_prose
    elif revised_prose:
        # Update the block with the extracted prose
        # (We can't mutate the frozen Pydantic model, so we store it
        # in the ParsedUpdateBlock separately)
        pass

    # ---------------------------------------------------------------
    # 9. Build success result
    # ---------------------------------------------------------------
    parsed = ParsedUpdateBlock(
        update_block=update_block,
        revised_prose=revised_prose,
        raw_content=raw_content,
        fence_start=fence.open_start,
        fence_end=fence.close_end,
    )

    return CommandResult.success(
        command="parse_update",
        message=(
            f"Successfully parsed loom-update block for chunk "
            f"{update_block.target_chunk}"
        ),
        data={
            "parsed": parsed.to_dict(),
            "target_chunk": update_block.target_chunk,
            "mode": update_block.mode.value,
            "revised_prose_length": len(revised_prose),
            "new_decisions": len(update_block.new_decisions),
            "new_threads": len(update_block.new_threads),
            "close_threads": len(update_block.close_threads),
            "update_existing": len(update_block.update_existing),
            "requires_human_review": update_block.requires_human_review,
        },
    )


# ---------------------------------------------------------------------------
# Internal helpers — content splitting
# ---------------------------------------------------------------------------


def _split_update_block_content(raw_content: str) -> tuple[str, str]:
    """Split the raw content of a loom-update block into YAML and Markdown.

    The content inside the fences follows this format::

        schema_version: "0.1.0"
        fence_type: loom-update
        mode: full_replacement
        target_chunk: C-0001
        ---
        # Revised Chunk

        The revised prose goes here.

        # Change Summary

        Summary of changes.

    The YAML frontmatter is everything before the first standalone ``---``
    line.  The Markdown body is everything after it.

    If no ``---`` separator is found, the entire content is treated as
    YAML (the Markdown body is empty).  This handles the case where the
    model only provides YAML without prose.

    Parameters
    ----------
    raw_content:
        The text between the opening and closing fences.

    Returns
    -------
    tuple[str, str]
        A 2-tuple of ``(yaml_str, markdown_body)``.
    """
    lines = raw_content.split("\n")
    separator_index = None

    for i, line in enumerate(lines):
        if line.strip() == "---":
            separator_index = i
            break

    if separator_index is None:
        # No separator found — entire content is YAML
        return raw_content, ""

    yaml_str = "\n".join(lines[:separator_index])
    markdown_body = "\n".join(lines[separator_index + 1:])

    return yaml_str, markdown_body
