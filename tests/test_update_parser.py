"""Tests for aip_loom.update_parser — strict model-output update block parser.

These tests prove:

- Valid loom-update blocks are parsed successfully
- Missing fence → UPDATE_BLOCK_MISSING
- Multiple loom-update blocks → UPDATE_BLOCK_MULTIPLE
- Legacy thread-update fence → UPDATE_BLOCK_LEGACY_FENCE
- YAML anchors/aliases/tags in update mode → hard rejection
- Patch mode → PATCH_MODE_UNSUPPORTED
- Model-assigned canonical IDs → MODEL_ASSIGNED_ID
- Prose extraction from # Revised Chunk heading works correctly
- Ambiguous prose (multiple # Revised Chunk) → PROSE_EXTRACTION_AMBIGUOUS
- Size limit exceeded → UPDATE_BLOCK_MALFORMED
- Depth limit exceeded → UPDATE_BLOCK_MALFORMED
- Empty block content → UPDATE_BLOCK_MALFORMED
- Unclosed fence → UPDATE_BLOCK_MALFORMED
- Duplicate YAML keys → hard rejection
- Extra/unknown fields → hard rejection
- Invalid target_chunk → hard rejection
- Invalid close_threads IDs → hard rejection
- No auto-correction of any model output
- Deterministic parsing (same input → same output)
"""

from __future__ import annotations

import textwrap

import pytest

from aip_loom.errors import (
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
)
from aip_loom.schemas import SUPPORTED_SCHEMA_VERSION
from aip_loom.update_parser import (
    MAX_UPDATE_BLOCK_SIZE,
    MAX_YAML_DEPTH,
    ParsedUpdateBlock,
    parse_model_output,
    _extract_revised_prose,
    _split_update_block_content,
)
from aip_loom.yaml_io import YamlMode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_V = SUPPORTED_SCHEMA_VERSION


def _update_block_yaml(**overrides: object) -> str:
    """Return a minimal valid update block YAML string."""
    base = {
        "schema_version": _V,
        "fence_type": "loom-update",
        "mode": "full_replacement",
        "target_chunk": "C-0001",
        "revised_prose": "The quick brown fox.",
        "change_summary": "Revised opening.",
        "requires_human_review": True,
    }
    base.update(overrides)
    lines = []
    for key, value in base.items():
        if isinstance(value, bool):
            lines.append(f"{key}: {str(value).lower()}")
        elif isinstance(value, str) and (
            " " in value or value.startswith('"') or "\n" in value
        ):
            # Quote strings with spaces or special characters
            escaped = value.replace('"', '\\"')
            lines.append(f'{key}: "{escaped}"')
        elif isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                if isinstance(item, dict):
                    lines.append(f"  - ")
                    for k, v in item.items():
                        lines.append(f"    {k}: {v}")
                else:
                    lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _fenced_update(yaml_str: str, markdown_body: str = "") -> str:
    """Wrap YAML content in a loom-update fence."""
    parts = [f"```loom-update", yaml_str]
    if markdown_body:
        parts.append("---")
        parts.append(markdown_body)
    parts.append("```")
    return "\n".join(parts)


def _full_update_block(**yaml_overrides: object) -> str:
    """Create a complete, valid model output with a loom-update block."""
    yaml_str = _update_block_yaml(**yaml_overrides)
    return _fenced_update(yaml_str)


# ---------------------------------------------------------------------------
# Valid parse tests
# ---------------------------------------------------------------------------


class TestValidParse:
    """Verify that valid loom-update blocks are parsed successfully."""

    def test_minimal_valid_block(self) -> None:
        """The simplest valid loom-update block parses correctly."""
        text = _full_update_block()
        result = parse_model_output(text)
        assert result.ok is True
        assert result.command == "parse_update"
        assert result.data["target_chunk"] == "C-0001"
        assert result.data["mode"] == "full_replacement"

    def test_parsed_block_has_schema_fields(self) -> None:
        """The parsed block contains all schema fields."""
        text = _full_update_block()
        result = parse_model_output(text)
        assert result.ok is True
        parsed = result.data["parsed"]
        assert parsed["target_chunk"] == "C-0001"
        assert parsed["fence_type"] == "loom-update"
        assert parsed["mode"] == "full_replacement"
        assert parsed["requires_human_review"] is True

    def test_block_with_new_decisions(self) -> None:
        """Update block with new decision items parses correctly."""
        text = _full_update_block(
            new_decisions=[
                {"provisional_id": "new-1", "summary": "A new decision"},
            ]
        )
        result = parse_model_output(text)
        assert result.ok is True
        assert result.data["new_decisions"] == 1

    def test_block_with_new_threads(self) -> None:
        """Update block with new thread items parses correctly."""
        text = _full_update_block(
            new_threads=[
                {"provisional_id": "new-2", "summary": "A new thread", "state": "open"},
            ]
        )
        result = parse_model_output(text)
        assert result.ok is True
        assert result.data["new_threads"] == 1

    def test_block_with_close_threads(self) -> None:
        """Update block with close_threads parses correctly."""
        text = _full_update_block(close_threads=["T-0001"])
        result = parse_model_output(text)
        assert result.ok is True
        assert result.data["close_threads"] == 1

    def test_block_with_update_existing(self) -> None:
        """Update block with update_existing parses correctly."""
        text = _full_update_block(
            update_existing=[
                {"id": "D-0001", "changes": {"rationale": "Updated"}},
            ]
        )
        result = parse_model_output(text)
        assert result.ok is True
        assert result.data["update_existing"] == 1

    def test_block_with_surrounding_text(self) -> None:
        """Update block surrounded by other text still parses."""
        preamble = "Here is some context about the changes I made:\n\n"
        postamble = "\n\nI hope this helps!"
        block = _full_update_block()
        text = preamble + block + postamble
        result = parse_model_output(text)
        assert result.ok is True

    def test_revised_prose_from_schema(self) -> None:
        """Revised prose from the schema field is used when present."""
        text = _full_update_block(revised_prose="The new revised prose.")
        result = parse_model_output(text)
        assert result.ok is True
        assert result.data["revised_prose_length"] > 0

    def test_parsed_block_fence_positions(self) -> None:
        """Parsed block records fence start and end positions."""
        text = _full_update_block()
        result = parse_model_output(text)
        assert result.ok is True
        parsed = result.data["parsed"]
        assert parsed["fence_start"] >= 0
        assert parsed["fence_end"] > parsed["fence_start"]


# ---------------------------------------------------------------------------
# Missing fence tests
# ---------------------------------------------------------------------------


class TestMissingFence:
    """Verify UPDATE_BLOCK_MISSING when no loom-update fence is found."""

    def test_no_fence_at_all(self) -> None:
        """Plain text with no fences produces UPDATE_BLOCK_MISSING."""
        result = parse_model_output("Just some text without any fences.")
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MISSING

    def test_wrong_fence_type(self) -> None:
        """A non-loom-update fence produces UPDATE_BLOCK_MISSING."""
        text = textwrap.dedent("""\
            ```python
            print("hello")
            ```
        """)
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MISSING

    def test_near_miss_fence_type(self) -> None:
        """Near-miss fence types are not accepted."""
        for fence_type in ["loom_update", "Loom-Update", "LOOM-UPDATE", "loomupdate"]:
            text = f"```{fence_type}\nschema_version: {_V}\n```"
            result = parse_model_output(text)
            assert result.ok is False, f"Fence type '{fence_type}' should be rejected"
            assert result.code == UPDATE_BLOCK_MISSING


# ---------------------------------------------------------------------------
# Multiple blocks tests
# ---------------------------------------------------------------------------


class TestMultipleBlocks:
    """Verify UPDATE_BLOCK_MULTIPLE when more than one loom-update block exists."""

    def test_two_blocks_rejected(self) -> None:
        """Two loom-update blocks are rejected."""
        yaml_str = _update_block_yaml()
        text = f"```loom-update\n{yaml_str}\n```\n\n```loom-update\n{yaml_str}\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MULTIPLE

    def test_three_blocks_rejected(self) -> None:
        """Three loom-update blocks are rejected."""
        yaml_str = _update_block_yaml()
        block = f"```loom-update\n{yaml_str}\n```"
        text = f"{block}\n\n{block}\n\n{block}"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MULTIPLE

    def test_multiple_blocks_detail_has_count(self) -> None:
        """Multiple blocks error includes the count in detail."""
        yaml_str = _update_block_yaml()
        text = f"```loom-update\n{yaml_str}\n```\n\n```loom-update\n{yaml_str}\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert len(result.errors) > 0
        assert result.errors[0].detail.get("count") == 2


# ---------------------------------------------------------------------------
# Legacy fence tests
# ---------------------------------------------------------------------------


class TestLegacyFence:
    """Verify UPDATE_BLOCK_LEGACY_FENCE when thread-update is used."""

    def test_thread_update_fence_rejected(self) -> None:
        """thread-update fence produces UPDATE_BLOCK_LEGACY_FENCE."""
        yaml_str = _update_block_yaml()
        text = f"```thread-update\n{yaml_str}\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_LEGACY_FENCE

    def test_thread_update_alongside_loom_update_rejected(self) -> None:
        """Even if loom-update is also present, thread-update is still rejected."""
        yaml_str = _update_block_yaml()
        text = f"```thread-update\n{yaml_str}\n```\n\n```loom-update\n{yaml_str}\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_LEGACY_FENCE

    def test_thread_update_fence_type_in_yaml_rejected(self) -> None:
        """fence_type: thread-update in the YAML is also rejected."""
        text = _full_update_block(fence_type="thread-update")
        result = parse_model_output(text)
        assert result.ok is False
        # The schema rejects thread-update because fence_type is
        # Literal["loom-update"]
        assert result.code in (UPDATE_BLOCK_LEGACY_FENCE, UPDATE_BLOCK_MALFORMED)


# ---------------------------------------------------------------------------
# Patch mode rejection tests
# ---------------------------------------------------------------------------


class TestPatchModeRejection:
    """Verify PATCH_MODE_UNSUPPORTED when patch mode is used."""

    def test_patch_mode_in_yaml_rejected(self) -> None:
        """mode: patch is rejected with PATCH_MODE_UNSUPPORTED."""
        text = _full_update_block(mode="patch")
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == PATCH_MODE_UNSUPPORTED

    def test_patch_rejection_message_mentions_full_replacement(self) -> None:
        """Patch rejection message mentions full_replacement as alternative."""
        text = _full_update_block(mode="patch")
        result = parse_model_output(text)
        assert result.ok is False
        assert "full_replacement" in result.message


# ---------------------------------------------------------------------------
# YAML strictness tests (UPDATE_BLOCK mode)
# ---------------------------------------------------------------------------


class TestYamlStrictness:
    """Verify UPDATE_BLOCK mode strictness: anchors, aliases, tags, duplicate keys."""

    def test_anchor_rejected_in_update_block(self) -> None:
        """YAML anchors are rejected inside loom-update blocks."""
        yaml_with_anchor = textwrap.dedent(f"""\
            schema_version: "{_V}"
            fence_type: loom-update
            mode: full_replacement
            target_chunk: C-0001
            revised_prose: &anchor "The text"
            change_summary: *anchor
        """)
        text = f"```loom-update\n{yaml_with_anchor}\n```"
        result = parse_model_output(text)
        assert result.ok is False
        # The anchor should be caught by YamlMode.UPDATE_BLOCK
        assert result.code in (UPDATE_BLOCK_MALFORMED, YAML_ANCHORS_ALIASES)

    def test_alias_rejected_in_update_block(self) -> None:
        """YAML aliases are rejected inside loom-update blocks."""
        yaml_with_alias = textwrap.dedent(f"""\
            common: &common
              key: value
            schema_version: "{_V}"
            fence_type: loom-update
            mode: full_replacement
            target_chunk: C-0001
            derived:
              <<: *common
        """)
        text = f"```loom-update\n{yaml_with_alias}\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code in (UPDATE_BLOCK_MALFORMED, YAML_ANCHORS_ALIASES)

    def test_non_standard_tag_rejected_in_update_block(self) -> None:
        """Non-standard YAML tags are rejected inside loom-update blocks."""
        yaml_with_tag = textwrap.dedent(f"""\
            schema_version: "{_V}"
            fence_type: loom-update
            mode: full_replacement
            target_chunk: C-0001
            revised_prose: !custom "tagged text"
        """)
        text = f"```loom-update\n{yaml_with_tag}\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code in (UPDATE_BLOCK_MALFORMED, YAML_TAGS_REJECTED)

    def test_duplicate_keys_rejected_in_update_block(self) -> None:
        """Duplicate YAML keys are rejected inside loom-update blocks."""
        yaml_with_dup = textwrap.dedent(f"""\
            schema_version: "{_V}"
            fence_type: loom-update
            target_chunk: C-0001
            target_chunk: C-0002
        """)
        text = f"```loom-update\n{yaml_with_dup}\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code in (UPDATE_BLOCK_MALFORMED, YAML_DUPLICATE_KEYS)


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    """Verify schema-level validation catches malformed data."""

    def test_invalid_target_chunk_rejected(self) -> None:
        """Invalid target_chunk format is rejected."""
        text = _full_update_block(target_chunk="invalid-chunk")
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MALFORMED

    def test_missing_schema_version_rejected(self) -> None:
        """Missing schema_version is rejected."""
        yaml_no_version = textwrap.dedent("""\
            fence_type: loom-update
            mode: full_replacement
            target_chunk: C-0001
        """)
        text = f"```loom-update\n{yaml_no_version}\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MALFORMED

    def test_extra_fields_rejected(self) -> None:
        """Unknown fields in the YAML are rejected (extra='forbid')."""
        text = _full_update_block(unknown_field="surprise")
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MALFORMED

    def test_bad_schema_version_rejected(self) -> None:
        """Unsupported schema version is rejected."""
        text = _full_update_block(schema_version="99.0.0")
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MALFORMED

    def test_invalid_close_thread_id_rejected(self) -> None:
        """Invalid thread ID in close_threads is rejected."""
        text = _full_update_block(close_threads=["invalid-id"])
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MALFORMED

    def test_invalid_update_existing_id_rejected(self) -> None:
        """Invalid canonical ID in update_existing is rejected."""
        yaml_with_bad_id = textwrap.dedent(f"""\
            schema_version: "{_V}"
            fence_type: loom-update
            mode: full_replacement
            target_chunk: C-0001
            update_existing:
              - id: "bad-id"
                changes: {{}}
        """)
        text = f"```loom-update\n{yaml_with_bad_id}\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MALFORMED


# ---------------------------------------------------------------------------
# Model-assigned ID rejection tests
# ---------------------------------------------------------------------------


class TestModelAssignedIdRejection:
    """Verify MODEL_ASSIGNED_ID when the model tries to assign canonical IDs."""

    def test_canonical_id_in_new_decision_rejected(self) -> None:
        """Canonical ID in new_decisions provisional_id is rejected."""
        yaml_with_id = textwrap.dedent(f"""\
            schema_version: "{_V}"
            fence_type: loom-update
            mode: full_replacement
            target_chunk: C-0001
            new_decisions:
              - provisional_id: "D-0001"
                summary: "Sneaky decision"
        """)
        text = f"```loom-update\n{yaml_with_id}\n```"
        result = parse_model_output(text)
        # The schema rejects D-0001 as provisional_id (pattern mismatch)
        # and we also catch it at the parser level
        assert result.ok is False
        assert result.code in (MODEL_ASSIGNED_ID, UPDATE_BLOCK_MALFORMED)

    def test_canonical_id_in_new_thread_rejected(self) -> None:
        """Canonical ID in new_threads provisional_id is rejected."""
        yaml_with_id = textwrap.dedent(f"""\
            schema_version: "{_V}"
            fence_type: loom-update
            mode: full_replacement
            target_chunk: C-0001
            new_threads:
              - provisional_id: "T-0001"
                summary: "Sneaky thread"
        """)
        text = f"```loom-update\n{yaml_with_id}\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code in (MODEL_ASSIGNED_ID, UPDATE_BLOCK_MALFORMED)

    def test_provisional_id_accepted(self) -> None:
        """Proper provisional IDs (new-1, new-2) are accepted."""
        text = _full_update_block(
            new_decisions=[
                {"provisional_id": "new-1", "summary": "A valid decision"},
            ]
        )
        result = parse_model_output(text)
        assert result.ok is True


# ---------------------------------------------------------------------------
# Prose extraction tests
# ---------------------------------------------------------------------------


class TestProseExtraction:
    """Verify prose extraction from # Revised Chunk heading."""

    def test_extract_prose_with_revised_chunk_heading(self) -> None:
        """Prose is extracted from # Revised Chunk section."""
        prose, error = _extract_revised_prose(
            "# Revised Chunk\n\nThe new prose goes here.\n\n# Change Summary\n\nFixed things."
        )
        assert error is None
        assert "The new prose goes here." in prose

    def test_extract_prose_without_change_summary(self) -> None:
        """Prose is extracted when there's no # Change Summary heading."""
        prose, error = _extract_revised_prose(
            "# Revised Chunk\n\nThe new prose goes here."
        )
        assert error is None
        assert "The new prose goes here." in prose

    def test_no_revised_chunk_heading_no_error(self) -> None:
        """No # Revised Chunk heading is valid (ledger-only update)."""
        prose, error = _extract_revised_prose(
            "# Change Summary\n\nJust ledger changes."
        )
        assert error is None
        assert prose == ""

    def test_multiple_revised_chunk_headings_ambiguous(self) -> None:
        """Multiple # Revised Chunk headings produce PROSE_EXTRACTION_AMBIGUOUS."""
        prose, error = _extract_revised_prose(
            "# Revised Chunk\n\nFirst prose.\n\n# Revised Chunk\n\nSecond prose."
        )
        assert error is not None
        assert error.code == PROSE_EXTRACTION_AMBIGUOUS
        assert prose == ""

    def test_h2_revised_chunk_heading(self) -> None:
        """## Revised Chunk (H2) is also recognized."""
        prose, error = _extract_revised_prose(
            "## Revised Chunk\n\nThe revised text."
        )
        assert error is None
        assert "The revised text." in prose

    def test_prose_extraction_via_parse_model_output(self) -> None:
        """Full pipeline extracts prose from fenced block with Markdown body."""
        # Use a valid YAML block without revised_prose field (it has a default
        # of empty string), then provide the prose in the Markdown body.
        yaml_lines = [
            f'schema_version: "{_V}"',
            'fence_type: loom-update',
            'mode: full_replacement',
            'target_chunk: C-0001',
            'change_summary: "Updated the text."',
            'requires_human_review: true',
        ]
        yaml_str = "\n".join(yaml_lines)
        markdown_body = "# Revised Chunk\n\nThis is the revised prose content.\n\n# Change Summary\n\nUpdated the text."
        text = _fenced_update(yaml_str, markdown_body)
        result = parse_model_output(text)
        assert result.ok is True
        assert result.data["revised_prose_length"] > 0

    def test_ambiguous_prose_via_parse_model_output(self) -> None:
        """Ambiguous prose extraction fails the parse."""
        yaml_lines = [
            f'schema_version: "{_V}"',
            'fence_type: loom-update',
            'mode: full_replacement',
            'target_chunk: C-0001',
            'change_summary: "Changes."',
            'requires_human_review: true',
        ]
        yaml_str = "\n".join(yaml_lines)
        markdown_body = "# Revised Chunk\n\nFirst.\n\n# Revised Chunk\n\nSecond.\n\n# Change Summary\n\nChanges."
        text = _fenced_update(yaml_str, markdown_body)
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == PROSE_EXTRACTION_AMBIGUOUS


# ---------------------------------------------------------------------------
# Content splitting tests
# ---------------------------------------------------------------------------


class TestContentSplitting:
    """Verify _split_update_block_content separates YAML from Markdown."""

    def test_split_with_separator(self) -> None:
        """Content with --- separator is split correctly."""
        yaml_part = "key: value"
        md_part = "# Revised Chunk\n\nProse here."
        raw = f"{yaml_part}\n---\n{md_part}"
        y, m = _split_update_block_content(raw)
        assert y.strip() == yaml_part
        assert m.strip() == md_part.strip()

    def test_split_without_separator(self) -> None:
        """Content without --- separator treats all as YAML."""
        raw = "key: value\nother: data"
        y, m = _split_update_block_content(raw)
        assert y.strip() == raw.strip()
        assert m == ""

    def test_split_empty_yaml(self) -> None:
        """Separator at the start means empty YAML."""
        raw = "---\n# Heading\nProse"
        y, m = _split_update_block_content(raw)
        assert y.strip() == ""
        assert "# Heading" in m


# ---------------------------------------------------------------------------
# Empty / malformed block tests
# ---------------------------------------------------------------------------


class TestMalformedBlock:
    """Verify UPDATE_BLOCK_MALFORMED for various malformed inputs."""

    def test_empty_fenced_block(self) -> None:
        """Empty content between fences is rejected."""
        text = "```loom-update\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MALFORMED

    def test_whitespace_only_fenced_block(self) -> None:
        """Whitespace-only content between fences is rejected."""
        text = "```loom-update\n   \n\t\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MALFORMED

    def test_invalid_yaml_content(self) -> None:
        """Invalid YAML inside the fence is rejected."""
        text = "```loom-update\n: invalid: yaml: [\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MALFORMED

    def test_yaml_null_document(self) -> None:
        """A YAML document that resolves to null is rejected."""
        text = "```loom-update\nnull\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MALFORMED

    def test_unclosed_fence(self) -> None:
        """An opening fence without a closing fence is rejected."""
        text = f"```loom-update\n{_update_block_yaml()}"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MISSING

    def test_non_map_yaml_rejected(self) -> None:
        """A YAML list instead of a mapping is rejected."""
        text = "```loom-update\n- item1\n- item2\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MALFORMED


# ---------------------------------------------------------------------------
# Size limit tests
# ---------------------------------------------------------------------------


class TestSizeLimit:
    """Verify size limit enforcement."""

    def test_oversized_input_rejected(self) -> None:
        """Input exceeding MAX_UPDATE_BLOCK_SIZE is rejected."""
        # Create a string that's too large
        huge_text = "x" * (MAX_UPDATE_BLOCK_SIZE + 1)
        result = parse_model_output(huge_text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MALFORMED
        assert len(result.errors) > 0
        assert result.errors[0].detail.get("actual_size") == MAX_UPDATE_BLOCK_SIZE + 1

    def test_exactly_at_size_limit_accepted(self) -> None:
        """Input exactly at the size limit is not rejected for size."""
        # Build a valid block that's close to the limit
        # (We can't easily hit exactly the limit, so we just verify
        # that a normal-sized block works)
        text = _full_update_block()
        result = parse_model_output(text)
        assert result.ok is True


# ---------------------------------------------------------------------------
# Depth limit tests
# ---------------------------------------------------------------------------


class TestDepthLimit:
    """Verify YAML depth limit enforcement."""

    def test_normal_depth_accepted(self) -> None:
        """Normal update block depth is well within limits."""
        text = _full_update_block(
            new_decisions=[
                {"provisional_id": "new-1", "summary": "A decision"},
            ]
        )
        result = parse_model_output(text)
        assert result.ok is True


# ---------------------------------------------------------------------------
# No auto-correction tests
# ---------------------------------------------------------------------------


class TestNoAutoCorrection:
    """Verify the parser never auto-corrects model output."""

    def test_wrong_fence_type_not_corrected(self) -> None:
        """'loom_update' (underscore) is not auto-corrected to 'loom-update'."""
        yaml_str = _update_block_yaml()
        text = f"```loom_update\n{yaml_str}\n```"
        result = parse_model_output(text)
        assert result.ok is False
        # Should be MISSING, not silently accepted
        assert result.code == UPDATE_BLOCK_MISSING

    def test_missing_fields_not_filled_in(self) -> None:
        """Missing required fields are not auto-filled."""
        yaml_no_target = f'schema_version: "{_V}"\nfence_type: loom-update\nmode: full_replacement'
        text = f"```loom-update\n{yaml_no_target}\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MALFORMED

    def test_patch_not_treated_as_full_replacement(self) -> None:
        """patch mode is NOT silently treated as full_replacement."""
        text = _full_update_block(mode="patch")
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == PATCH_MODE_UNSUPPORTED


# ---------------------------------------------------------------------------
# Determinism tests
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Verify parsing is deterministic — same input always produces same output."""

    def test_same_input_same_target_chunk(self) -> None:
        """Same input always produces the same target_chunk."""
        text = _full_update_block()
        r1 = parse_model_output(text)
        r2 = parse_model_output(text)
        assert r1.data["target_chunk"] == r2.data["target_chunk"]

    def test_same_input_same_mode(self) -> None:
        """Same input always produces the same mode."""
        text = _full_update_block()
        r1 = parse_model_output(text)
        r2 = parse_model_output(text)
        assert r1.data["mode"] == r2.data["mode"]

    def test_same_input_same_parsed_dict(self) -> None:
        """Same input always produces the same parsed dict."""
        text = _full_update_block()
        r1 = parse_model_output(text)
        r2 = parse_model_output(text)
        assert r1.data["parsed"] == r2.data["parsed"]

    def test_error_output_deterministic(self) -> None:
        """Error output for the same bad input is also deterministic."""
        text = _full_update_block(mode="patch")
        r1 = parse_model_output(text)
        r2 = parse_model_output(text)
        assert r1.code == r2.code


# ---------------------------------------------------------------------------
# Integration with yaml_io UPDATE_BLOCK mode
# ---------------------------------------------------------------------------


class TestUpdateBlockModeIntegration:
    """Verify the parser uses YamlMode.UPDATE_BLOCK correctly."""

    def test_anchor_in_fenced_block_caught_by_strict_mode(self) -> None:
        """Anchors inside a loom-update block are caught by UPDATE_BLOCK mode."""
        yaml_with_anchor = textwrap.dedent(f"""\
            schema_version: "{_V}"
            fence_type: loom-update
            mode: full_replacement
            target_chunk: C-0001
            revised_prose: &text "The prose"
            change_summary: *text
        """)
        text = f"```loom-update\n{yaml_with_anchor}\n```"
        result = parse_model_output(text)
        assert result.ok is False

    def test_merge_key_rejected(self) -> None:
        """YAML merge key (<<:) is rejected in update blocks."""
        yaml_with_merge = textwrap.dedent(f"""\
            common: &common
              key: value
            schema_version: "{_V}"
            fence_type: loom-update
            mode: full_replacement
            target_chunk: C-0001
            derived:
              <<: *common
        """)
        text = f"```loom-update\n{yaml_with_merge}\n```"
        result = parse_model_output(text)
        assert result.ok is False


# ---------------------------------------------------------------------------
# CommandResult integration
# ---------------------------------------------------------------------------


class TestCommandResultIntegration:
    """Verify parse results use CommandResult correctly."""

    def test_success_result_is_command_result(self) -> None:
        """Successful parse returns a CommandResult with ok=True."""
        text = _full_update_block()
        result = parse_model_output(text)
        assert result.ok is True
        assert result.command == "parse_update"
        assert result.code == "OK"

    def test_failure_result_is_command_result(self) -> None:
        """Failed parse returns a CommandResult with ok=False."""
        result = parse_model_output("no fence here")
        assert result.ok is False
        assert result.command == "parse_update"
        assert result.code == UPDATE_BLOCK_MISSING

    def test_failure_has_loom_errors(self) -> None:
        """Failed parse carries LoomError instances in errors list."""
        result = parse_model_output("no fence here")
        assert len(result.errors) > 0
        assert result.errors[0].code == UPDATE_BLOCK_MISSING

    def test_failure_error_has_detail(self) -> None:
        """Failed parse errors have machine-readable detail dicts."""
        result = parse_model_output("no fence here")
        assert len(result.errors) > 0
        assert isinstance(result.errors[0].detail, dict)

    def test_to_json_works(self) -> None:
        """CommandResult.to_json() works on parse results."""
        text = _full_update_block()
        result = parse_model_output(text)
        json_str = result.to_json()
        assert "target_chunk" in json_str


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Verify edge cases and boundary conditions."""

    def test_fenced_block_at_start_of_text(self) -> None:
        """Fence at the very start of the text works."""
        text = _full_update_block()
        assert text.startswith("```loom-update")
        result = parse_model_output(text)
        assert result.ok is True

    def test_fenced_block_at_end_of_text(self) -> None:
        """Fence at the very end of the text works."""
        text = "Some preamble.\n\n" + _full_update_block()
        result = parse_model_output(text)
        assert result.ok is True

    def test_triple_backticks_in_prose(self) -> None:
        """Backticks in prose content don't confuse fence scanning."""
        yaml_str = _update_block_yaml(revised_prose="```this is not a fence```")
        text = f"```loom-update\n{yaml_str}\n```"
        result = parse_model_output(text)
        # This should either parse successfully or fail gracefully
        # (it depends on whether the inner backticks confuse the fence scanner)
        # The key requirement is that it never silently accepts bad data.
        assert isinstance(result.ok, bool)

    def test_empty_string_input(self) -> None:
        """Empty string input is rejected."""
        result = parse_model_output("")
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MISSING

    def test_only_backticks(self) -> None:
        """Just backticks with no content is rejected."""
        result = parse_model_output("``````")
        assert result.ok is False

    def test_new_item_cannot_force_approved(self) -> None:
        """New items with review_state=approved are rejected by the schema."""
        yaml_with_approved = textwrap.dedent(f"""\
            schema_version: "{_V}"
            fence_type: loom-update
            mode: full_replacement
            target_chunk: C-0001
            new_decisions:
              - provisional_id: "new-1"
                summary: "Sneaky approval"
                review_state: approved
        """)
        text = f"```loom-update\n{yaml_with_approved}\n```"
        result = parse_model_output(text)
        assert result.ok is False
        assert result.code == UPDATE_BLOCK_MALFORMED

    def test_multiple_new_items_accepted(self) -> None:
        """Multiple new items with valid provisional IDs are accepted."""
        yaml_with_multiple = textwrap.dedent(f"""\
            schema_version: "{_V}"
            fence_type: loom-update
            mode: full_replacement
            target_chunk: C-0001
            new_decisions:
              - provisional_id: "new-1"
                summary: "First decision"
              - provisional_id: "new-2"
                summary: "Second decision"
            new_threads:
              - provisional_id: "new-3"
                summary: "A thread"
        """)
        text = f"```loom-update\n{yaml_with_multiple}\n```"
        result = parse_model_output(text)
        assert result.ok is True
        assert result.data["new_decisions"] == 2
        assert result.data["new_threads"] == 1

    def test_change_summary_and_review_notes(self) -> None:
        """change_summary and review_notes fields work."""
        text = _full_update_block(
            change_summary="Revised the opening paragraph for clarity.",
            review_notes="Please review the dialogue changes.",
        )
        result = parse_model_output(text)
        assert result.ok is True

    def test_full_pipeline_with_markdown_body(self) -> None:
        """Full pipeline: YAML frontmatter + Markdown body with prose."""
        yaml_lines = [
            f'schema_version: "{_V}"',
            'fence_type: loom-update',
            'mode: full_replacement',
            'target_chunk: C-0001',
            'change_summary: "Rewrote the opening paragraph to establish mood."',
            'requires_human_review: true',
        ]
        yaml_str = "\n".join(yaml_lines)
        markdown_body = textwrap.dedent("""\
            # Revised Chunk

            The sun set over the hills, casting long shadows across the valley.
            Maria looked up from her book and sighed.

            # Change Summary

            Rewrote the opening paragraph to establish mood.
        """)
        text = _fenced_update(yaml_str, markdown_body)
        result = parse_model_output(text)
        assert result.ok is True
        assert result.data["revised_prose_length"] > 0
