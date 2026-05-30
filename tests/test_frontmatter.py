"""Tests for aip_loom.frontmatter — Markdown frontmatter parser + writer.

These tests prove:
- parse_frontmatter correctly splits and validates frontmatter
- split_frontmatter handles various edge cases
- write_frontmatter produces valid Markdown with frontmatter
- Round-trip: write → parse returns identical data
- Frontmatter ID is used, never filename inference
- Malformed frontmatter raises FrontmatterParseError
- Empty frontmatter is rejected
- Missing closing delimiter is rejected
- No frontmatter at all raises FrontmatterParseError
"""

from __future__ import annotations

import textwrap

import pytest

from aip_loom.errors import FIELD_MISSING
from aip_loom.frontmatter import (
    FrontmatterParseError,
    FrontmatterParseResult,
    parse_frontmatter,
    split_frontmatter,
    write_frontmatter,
)
from aip_loom.schemas import ChunkFrontmatter, SUPPORTED_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_V = SUPPORTED_SCHEMA_VERSION


def _valid_markdown(**fm_overrides: object) -> str:
    """Return a valid Markdown string with YAML frontmatter."""
    fm = {
        "schema_version": _V,
        "id": "C-0001",
        "title": "Chapter One",
        "word_count": 500,
        "prose_checksum": "abc123",
        "created_at": "2026-05-28T12:00:00Z",
        "updated_at": "2026-05-28T12:00:00Z",
    }
    fm.update(fm_overrides)

    lines = ["---"]
    for key, value in fm.items():
        if isinstance(value, str):
            lines.append(f"{key}: \"{value}\"")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append("This is the prose body of the chapter.")

    return "\n".join(lines)


def _valid_frontmatter(**overrides: object) -> ChunkFrontmatter:
    """Return a valid ChunkFrontmatter instance."""
    base = {
        "schema_version": _V,
        "id": "C-0001",
        "title": "Chapter One",
        "word_count": 500,
        "prose_checksum": "abc123",
        "created_at": "2026-05-28T12:00:00Z",
        "updated_at": "2026-05-28T12:00:00Z",
    }
    base.update(overrides)
    return ChunkFrontmatter(**base)


# ===========================================================================
# split_frontmatter
# ===========================================================================


class TestSplitFrontmatter:
    """Tests for raw text splitting (no YAML validation)."""

    def test_basic_split(self) -> None:
        text = "---\nkey: value\n---\nProse content."
        yaml_str, prose = split_frontmatter(text)
        assert yaml_str == "key: value"
        assert prose == "Prose content."

    def test_multiline_yaml(self) -> None:
        text = "---\nkey1: value1\nkey2: value2\n---\nProse."
        yaml_str, prose = split_frontmatter(text)
        assert "key1: value1" in yaml_str
        assert "key2: value2" in yaml_str
        assert prose == "Prose."

    def test_empty_prose_body(self) -> None:
        text = "---\nkey: value\n---\n"
        yaml_str, prose = split_frontmatter(text)
        assert yaml_str == "key: value"
        assert prose == ""

    def test_prose_with_multiple_paragraphs(self) -> None:
        text = "---\nkey: value\n---\n\nFirst paragraph.\n\nSecond paragraph."
        yaml_str, prose = split_frontmatter(text)
        assert "First paragraph." in prose
        assert "Second paragraph." in prose

    def test_missing_opening_delimiter(self) -> None:
        with pytest.raises(FrontmatterParseError) as exc_info:
            split_frontmatter("key: value\n---\nProse.")
        assert exc_info.value.loom_error.code == FIELD_MISSING

    def test_missing_closing_delimiter(self) -> None:
        with pytest.raises(FrontmatterParseError) as exc_info:
            split_frontmatter("---\nkey: value\nProse without closing.")
        assert exc_info.value.loom_error.code == FIELD_MISSING
        assert "no closing delimiter" in exc_info.value.loom_error.message.lower()

    def test_empty_frontmatter_rejected(self) -> None:
        """---\n--- is empty frontmatter and must be rejected."""
        with pytest.raises(FrontmatterParseError) as exc_info:
            split_frontmatter("---\n---\nProse.")
        assert exc_info.value.loom_error.code == FIELD_MISSING
        assert "empty" in exc_info.value.loom_error.message.lower()

    def test_whitespace_around_delimiters(self) -> None:
        """Delimiters with trailing whitespace should still be recognized."""
        text = "---  \nkey: value\n---  \nProse."
        yaml_str, prose = split_frontmatter(text)
        assert yaml_str == "key: value"
        assert prose == "Prose."


# ===========================================================================
# parse_frontmatter
# ===========================================================================


class TestParseFrontmatter:
    """Tests for full frontmatter parsing with YAML validation."""

    def test_parse_valid_markdown(self) -> None:
        md = _valid_markdown()
        result = parse_frontmatter(md)
        assert isinstance(result, FrontmatterParseResult)
        assert result.frontmatter.id == "C-0001"
        assert result.frontmatter.title == "Chapter One"
        assert "prose body" in result.prose_body

    def test_parse_extracts_id_from_frontmatter(self) -> None:
        """The chunk ID comes from frontmatter, not filename inference."""
        md = _valid_markdown(id="C-0042")
        result = parse_frontmatter(md)
        assert result.frontmatter.id == "C-0042"

    def test_parse_preserves_prose_body(self) -> None:
        prose = "Line one.\n\nLine two.\n\nLine three."
        md = _valid_markdown()
        # Rebuild with specific prose
        fm_lines = md.split("---\n")[1].split("---")[0]
        full = f"---\n{fm_lines}---\n{prose}"
        result = parse_frontmatter(full)
        assert result.prose_body == prose

    def test_parse_empty_prose_body(self) -> None:
        md = _valid_markdown()
        # Rebuild with empty prose
        parts = md.split("---\n")
        fm_text = parts[1].split("---")[0]
        full = f"---\n{fm_text}---\n"
        result = parse_frontmatter(full)
        assert result.prose_body == ""

    def test_parse_invalid_yaml_rejected(self) -> None:
        md = "---\nkey: [unclosed\n---\nProse."
        with pytest.raises(FrontmatterParseError):
            parse_frontmatter(md)

    def test_parse_missing_required_field_rejected(self) -> None:
        """YAML that doesn't match ChunkFrontmatter schema is rejected."""
        md = "---\nschema_version: \"0.1.0\"\ntitle: No ID field\n---\nProse."
        with pytest.raises(FrontmatterParseError):
            parse_frontmatter(md)

    def test_parse_bad_schema_version_rejected(self) -> None:
        md = _valid_markdown(schema_version="99.0.0")
        with pytest.raises(FrontmatterParseError):
            parse_frontmatter(md)

    def test_parse_no_frontmatter_rejected(self) -> None:
        """A file with no frontmatter delimiters is rejected."""
        with pytest.raises(FrontmatterParseError):
            parse_frontmatter("Just plain prose content with no frontmatter.")


# ===========================================================================
# write_frontmatter
# ===========================================================================


class TestWriteFrontmatter:
    """Tests for composing Markdown with frontmatter."""

    def test_write_produces_valid_markdown(self) -> None:
        fm = _valid_frontmatter()
        prose = "Story content here."
        result = write_frontmatter(fm, prose)
        assert result.startswith("---\n")
        assert "---\n" in result[4:]  # Closing delimiter
        assert prose in result

    def test_write_includes_all_frontmatter_fields(self) -> None:
        fm = _valid_frontmatter()
        result = write_frontmatter(fm, "Prose.")
        assert "schema_version" in result
        assert "id" in result
        assert "title" in result
        assert "C-0001" in result

    def test_write_empty_prose(self) -> None:
        fm = _valid_frontmatter()
        result = write_frontmatter(fm, "")
        assert result.endswith("---\n")


# ===========================================================================
# Round-trip: write → parse
# ===========================================================================


class TestRoundTrip:
    """write_frontmatter → parse_frontmatter must preserve data."""

    def test_round_trip_preserves_id(self) -> None:
        fm = _valid_frontmatter(id="C-0042")
        md = write_frontmatter(fm, "Some prose.")
        result = parse_frontmatter(md)
        assert result.frontmatter.id == "C-0042"

    def test_round_trip_preserves_title(self) -> None:
        fm = _valid_frontmatter(title="The Great Chapter")
        md = write_frontmatter(fm, "Some prose.")
        result = parse_frontmatter(md)
        assert result.frontmatter.title == "The Great Chapter"

    def test_round_trip_preserves_prose(self) -> None:
        fm = _valid_frontmatter()
        prose = "First paragraph.\n\nSecond paragraph with **bold**."
        md = write_frontmatter(fm, prose)
        result = parse_frontmatter(md)
        assert result.prose_body == prose

    def test_round_trip_preserves_checksum(self) -> None:
        fm = _valid_frontmatter(prose_checksum="sha256abc123")
        md = write_frontmatter(fm, "Prose.")
        result = parse_frontmatter(md)
        assert result.frontmatter.prose_checksum == "sha256abc123"

    def test_round_trip_preserves_word_count(self) -> None:
        fm = _valid_frontmatter(word_count=1234)
        md = write_frontmatter(fm, "Prose.")
        result = parse_frontmatter(md)
        assert result.frontmatter.word_count == 1234

    def test_round_trip_preserves_status(self) -> None:
        fm = _valid_frontmatter(status="revised")
        md = write_frontmatter(fm, "Prose.")
        result = parse_frontmatter(md)
        assert result.frontmatter.status.value == "revised"


# ===========================================================================
# No filename inference
# ===========================================================================


class TestNoFilenameInference:
    """When frontmatter exists, ID comes from frontmatter only."""

    def test_id_always_from_frontmatter(self) -> None:
        """Even if the filename were 'C-0099.md', the frontmatter ID is
        the authoritative one."""
        md = _valid_markdown(id="C-0001")
        result = parse_frontmatter(md)
        # The parse function returns the frontmatter ID, regardless of
        # what the filename might suggest
        assert result.frontmatter.id == "C-0001"

    def test_frontmatter_result_carries_no_filename_info(self) -> None:
        """FrontmatterParseResult has no filename field — the ID is
        always from the parsed YAML."""
        md = _valid_markdown()
        result = parse_frontmatter(md)
        # FrontmatterParseResult only has .frontmatter and .prose_body
        # No filename or path attribute
        assert not hasattr(result, "filename")
        assert not hasattr(result, "path")
