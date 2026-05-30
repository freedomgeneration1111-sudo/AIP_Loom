"""Tests for aip_loom.checksum — prose-body checksum calculator.

These tests prove:
- Deterministic: same prose → same checksum
- LF normalization: CRLF and bare CR produce same checksum as LF
- Trailing newline stripped: prose with/without trailing newline → same checksum
- Empty prose produces a valid checksum
- Unicode content produces a valid checksum
- Prose body only: frontmatter text is not included in checksum
- Different content → different checksum
"""

from __future__ import annotations

import hashlib

from aip_loom.checksum import compute_prose_checksum, CHECKSUM_ALGORITHM


# ===========================================================================
# Determinism
# ===========================================================================


class TestDeterminism:
    """Same prose must always produce the same checksum."""

    def test_same_prose_same_checksum(self) -> None:
        prose = "The quick brown fox jumps over the lazy dog."
        assert compute_prose_checksum(prose) == compute_prose_checksum(prose)

    def test_deterministic_known_value(self) -> None:
        """Verify against a manually computed SHA-256."""
        prose = "Hello world"
        # Manually: sha256("Hello world") in hex
        expected = hashlib.sha256("Hello world".encode("utf-8")).hexdigest()
        assert compute_prose_checksum(prose) == expected


# ===========================================================================
# LF normalization
# ===========================================================================


class TestLFNormalization:
    """CRLF and bare CR must be normalized to LF before hashing."""

    def test_crlf_normalized_to_lf(self) -> None:
        lf_prose = "line one\nline two\nline three"
        crlf_prose = "line one\r\nline two\r\nline three"
        assert compute_prose_checksum(lf_prose) == compute_prose_checksum(crlf_prose)

    def test_bare_cr_normalized_to_lf(self) -> None:
        lf_prose = "line one\nline two"
        cr_prose = "line one\rline two"
        assert compute_prose_checksum(lf_prose) == compute_prose_checksum(cr_prose)

    def test_mixed_endings_normalized(self) -> None:
        """A mix of CRLF, LF, and bare CR all produce the same checksum."""
        lf_only = "a\nb\nc"
        crlf_only = "a\r\nb\r\nc"
        mixed = "a\r\nb\nc"
        assert compute_prose_checksum(lf_only) == compute_prose_checksum(crlf_only)
        assert compute_prose_checksum(lf_only) == compute_prose_checksum(mixed)


# ===========================================================================
# Trailing newline stripping
# ===========================================================================


class TestTrailingNewlineStripping:
    """A single trailing newline is stripped before hashing."""

    def test_trailing_newline_stripped(self) -> None:
        prose_without = "Hello world"
        prose_with = "Hello world\n"
        assert compute_prose_checksum(prose_without) == compute_prose_checksum(prose_with)

    def test_multiple_trailing_newlines_only_last_stripped(self) -> None:
        """Only one trailing newline is stripped; the rest remain."""
        prose = "Hello world\n\n"  # Two trailing newlines → stripped to "Hello world\n"
        prose_single = "Hello world\n"  # One trailing → stripped to "Hello world"
        # After stripping one trailing \n: "Hello world\n" vs "Hello world"
        # These should be DIFFERENT because only one newline is stripped
        assert compute_prose_checksum(prose) != compute_prose_checksum(prose_single)

    def test_no_trailing_newline_unchanged(self) -> None:
        prose = "Hello world"
        # Compute manually without any trailing newline
        expected = hashlib.sha256("Hello world".encode("utf-8")).hexdigest()
        assert compute_prose_checksum(prose) == expected


# ===========================================================================
# Empty and edge cases
# ===========================================================================


class TestEdgeCases:
    """Edge cases for checksum computation."""

    def test_empty_prose(self) -> None:
        """Empty prose produces a valid (non-empty) checksum."""
        result = compute_prose_checksum("")
        assert isinstance(result, str)
        assert len(result) == 64  # SHA-256 hex digest is 64 chars

    def test_empty_prose_known_value(self) -> None:
        """Empty string checksum matches SHA-256 of empty string."""
        expected = hashlib.sha256(b"").hexdigest()
        assert compute_prose_checksum("") == expected

    def test_single_character(self) -> None:
        result = compute_prose_checksum("A")
        expected = hashlib.sha256("A".encode("utf-8")).hexdigest()
        assert result == expected

    def test_whitespace_only(self) -> None:
        """Whitespace-only prose still produces a checksum."""
        result = compute_prose_checksum("   \n   ")
        assert isinstance(result, str)
        assert len(result) == 64

    def test_unicode_content(self) -> None:
        """Unicode content is hashed as UTF-8."""
        prose = "日本語テスト 🌍"
        result = compute_prose_checksum(prose)
        expected = hashlib.sha256(prose.encode("utf-8")).hexdigest()
        assert result == expected

    def test_very_long_prose(self) -> None:
        """Long prose produces a valid checksum."""
        prose = "word " * 100000
        result = compute_prose_checksum(prose)
        assert isinstance(result, str)
        assert len(result) == 64


# ===========================================================================
# Different content → different checksum
# ===========================================================================


class TestDifferentContentDifferentChecksum:
    """Different prose content must produce different checksums."""

    def test_different_text_different_checksum(self) -> None:
        assert compute_prose_checksum("Hello") != compute_prose_checksum("World")

    def test_extra_space_different_checksum(self) -> None:
        assert compute_prose_checksum("Hello world") != compute_prose_checksum("Hello  world")

    def test_case_sensitive(self) -> None:
        assert compute_prose_checksum("hello") != compute_prose_checksum("Hello")


# ===========================================================================
# Prose body only (frontmatter exclusion)
# ===========================================================================


class TestProseBodyOnly:
    """The checksum must only cover the prose body, not frontmatter.

    These tests verify the contract that callers are responsible for
    passing only prose body text (not frontmatter) to
    compute_prose_checksum.  The function itself has no knowledge of
    frontmatter — it simply hashes what it receives."""

    def test_checksum_on_prose_only(self) -> None:
        """When given only the prose body (after split), the checksum
        is computed correctly."""
        prose = "The actual story content goes here."
        result = compute_prose_checksum(prose)
        expected = hashlib.sha256(prose.encode("utf-8")).hexdigest()
        assert result == expected

    def test_including_frontmatter_would_change_checksum(self) -> None:
        """If frontmatter were accidentally included, the checksum
        would differ — proving the caller must strip it first."""
        prose_only = "Story content."
        with_frontmatter = "---\nid: C-0001\n---\nStory content."
        assert compute_prose_checksum(prose_only) != compute_prose_checksum(with_frontmatter)


# ===========================================================================
# Algorithm constant
# ===========================================================================


class TestAlgorithmConstant:
    def test_algorithm_is_sha256(self) -> None:
        assert CHECKSUM_ALGORITHM == "sha-256"
