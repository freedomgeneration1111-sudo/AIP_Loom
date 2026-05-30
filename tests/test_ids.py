"""Tests for aip_loom.ids — canonical ID allocator.

These tests prove:
- Empty ledger → first ID is {prefix}-0001
- Existing entries → next sequential ID after the maximum
- No gap-filling (D-0001, D-0003 → D-0004, not D-0002)
- Prefix-scoped independence (D-0001 and T-0001 don't interfere)
- Unknown prefix is rejected
- Duplicate IDs for the same prefix raise DuplicateIdError
- Malformed IDs matching the prefix but not the pattern raise InvalidIdError
- extract_id_number correctly parses prefix and numeric parts
- Non-matching prefixes are silently ignored during allocation
"""

from __future__ import annotations

import pytest

from aip_loom.errors import CHUNK_ID_INVALID, ID_DUPLICATE
from aip_loom.ids import (
    DuplicateIdError,
    InvalidIdError,
    allocate_next_id,
    extract_id_number,
    KNOWN_PREFIXES,
)
from aip_loom.schemas import (
    DecisionEntry,
    ThreadEntry,
    QuestionEntry,
    SessionEntry,
    CommentEntry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _d_entry(id_str: str) -> DecisionEntry:
    """Create a DecisionEntry with the given ID."""
    return DecisionEntry(
        id=id_str,
        review_state="approved",
        created_at="2026-05-28T12:00:00Z",
        summary=f"Decision {id_str}",
    )


def _t_entry(id_str: str) -> ThreadEntry:
    """Create a ThreadEntry with the given ID."""
    return ThreadEntry(
        id=id_str,
        review_state="approved",
        created_at="2026-05-28T12:00:00Z",
        summary=f"Thread {id_str}",
    )


def _q_entry(id_str: str) -> QuestionEntry:
    """Create a QuestionEntry with the given ID."""
    return QuestionEntry(
        id=id_str,
        review_state="approved",
        created_at="2026-05-28T12:00:00Z",
        question=f"Question {id_str}",
    )


# ===========================================================================
# extract_id_number
# ===========================================================================


class TestExtractIdNumber:
    """Tests for parsing canonical ID strings."""

    def test_parse_simple_prefix(self) -> None:
        prefix, number = extract_id_number("D-0001")
        assert prefix == "D"
        assert number == 1

    def test_parse_two_char_prefix(self) -> None:
        prefix, number = extract_id_number("CH-0012")
        assert prefix == "CH"
        assert number == 12

    def test_parse_cm_prefix(self) -> None:
        prefix, number = extract_id_number("CM-0003")
        assert prefix == "CM"
        assert number == 3

    def test_parse_large_number(self) -> None:
        prefix, number = extract_id_number("D-9999")
        assert prefix == "D"
        assert number == 9999

    def test_parse_five_digit_number(self) -> None:
        prefix, number = extract_id_number("D-10000")
        assert prefix == "D"
        assert number == 10000

    def test_invalid_id_raises(self) -> None:
        with pytest.raises(InvalidIdError) as exc_info:
            extract_id_number("bad-id")
        assert exc_info.value.loom_error.code == CHUNK_ID_INVALID

    def test_invalid_id_no_prefix(self) -> None:
        with pytest.raises(InvalidIdError):
            extract_id_number("0001")

    def test_invalid_id_lowercase_prefix(self) -> None:
        with pytest.raises(InvalidIdError):
            extract_id_number("d-0001")

    def test_invalid_id_short_number(self) -> None:
        """IDs must have at least 4 digits (e.g. D-001, not D-1)."""
        with pytest.raises(InvalidIdError):
            extract_id_number("D-001")


# ===========================================================================
# allocate_next_id — basic positive cases
# ===========================================================================


class TestAllocateNextIdBasic:
    """Basic ID allocation tests."""

    def test_empty_entries_returns_first_id(self) -> None:
        result = allocate_next_id("D", [])
        assert result == "D-0001"

    def test_single_entry_returns_next(self) -> None:
        entries = [_d_entry("D-0001")]
        result = allocate_next_id("D", entries)
        assert result == "D-0002"

    def test_multiple_entries_returns_next_after_max(self) -> None:
        entries = [_d_entry("D-0001"), _d_entry("D-0002"), _d_entry("D-0003")]
        result = allocate_next_id("D", entries)
        assert result == "D-0004"

    def test_no_gap_filling(self) -> None:
        """D-0001 and D-0003 exist → next is D-0004, not D-0002."""
        entries = [_d_entry("D-0001"), _d_entry("D-0003")]
        result = allocate_next_id("D", entries)
        assert result == "D-0004"

    def test_thread_prefix_independent(self) -> None:
        """T- entries don't affect D- allocation."""
        entries = [_d_entry("D-0001"), _t_entry("T-0001")]
        result = allocate_next_id("D", entries)
        assert result == "D-0002"

    def test_mixed_prefixes_scoped(self) -> None:
        """Each prefix has its own independent sequence."""
        entries = [
            _d_entry("D-0001"),
            _d_entry("D-0005"),
            _t_entry("T-0001"),
            _t_entry("T-0003"),
        ]
        assert allocate_next_id("D", entries) == "D-0006"
        assert allocate_next_id("T", entries) == "T-0004"


# ===========================================================================
# allocate_next_id — all known prefixes
# ===========================================================================


class TestAllocateNextIdAllPrefixes:
    """Verify allocation works for all known prefixes."""

    @pytest.mark.parametrize("prefix", sorted(KNOWN_PREFIXES.keys()))
    def test_first_id_for_each_prefix(self, prefix: str) -> None:
        result = allocate_next_id(prefix, [])
        expected = f"{prefix}-0001"
        assert result == expected

    def test_c_prefix(self) -> None:
        """Chunk prefix C works."""
        from aip_loom.schemas import ChunkFrontmatter
        fm = ChunkFrontmatter(
            schema_version="0.1.0",
            id="C-0001",
            title="Ch1",
            word_count=100,
            prose_checksum="abc",
            created_at="2026-05-28T12:00:00Z",
            updated_at="2026-05-28T12:00:00Z",
        )
        result = allocate_next_id("C", [fm])
        assert result == "C-0002"


# ===========================================================================
# allocate_next_id — negative / error cases
# ===========================================================================


class TestAllocateNextIdErrors:
    """Error cases for ID allocation."""

    def test_unknown_prefix_rejected(self) -> None:
        with pytest.raises(InvalidIdError) as exc_info:
            allocate_next_id("X", [])
        assert exc_info.value.loom_error.code == CHUNK_ID_INVALID
        assert "Unknown ID prefix" in exc_info.value.loom_error.message

    def test_duplicate_id_raises(self) -> None:
        """Duplicate IDs for the same prefix must raise DuplicateIdError."""
        entries = [_d_entry("D-0001"), _d_entry("D-0001")]
        with pytest.raises(DuplicateIdError) as exc_info:
            allocate_next_id("D", entries)
        assert exc_info.value.loom_error.code == ID_DUPLICATE

    def test_malformed_id_with_correct_prefix_raises(self) -> None:
        """An ID starting with D- but not matching the full pattern is
        caught as InvalidIdError."""
        from dataclasses import dataclass

        @dataclass
        class FakeEntry:
            id: str

        entries = [FakeEntry(id="D-1")]  # Too few digits
        with pytest.raises(InvalidIdError) as exc_info:
            allocate_next_id("D", entries)
        assert exc_info.value.loom_error.code == CHUNK_ID_INVALID

    def test_entries_without_id_attr_ignored(self) -> None:
        """Entries that don't have the id_attr are silently ignored."""
        entries = [object(), object()]
        result = allocate_next_id("D", entries)
        assert result == "D-0001"


# ===========================================================================
# allocate_next_id — custom id_attr
# ===========================================================================


class TestAllocateNextIdCustomAttr:
    """Test allocation with a custom id attribute name."""

    def test_custom_id_attr(self) -> None:
        """When entries use a different attribute name for the ID."""
        from dataclasses import dataclass

        @dataclass
        class SessionLike:
            session_id: str

        entries = [SessionLike(session_id="S-0001")]
        result = allocate_next_id("S", entries, id_attr="session_id")
        assert result == "S-0002"


# ===========================================================================
# allocate_next_id — canonical-only honesty
# ===========================================================================


class TestAllocateNextIdCanonicalOnly:
    """Tests proving the allocator only uses provided canonical entries,
    not staged or archive state.

    Since the allocator accepts only explicit entry sequences, these
    tests verify that the function does not have hidden state, file
    system access, or side effects."""

    def test_no_hidden_state(self) -> None:
        """Two calls with the same entries produce the same result."""
        entries = [_d_entry("D-0001")]
        result1 = allocate_next_id("D", entries)
        result2 = allocate_next_id("D", entries)
        assert result1 == result2 == "D-0002"

    def test_no_side_effects_on_entries(self) -> None:
        """Allocation must not mutate the input entries."""
        entries = [_d_entry("D-0001")]
        original_id = entries[0].id
        allocate_next_id("D", entries)
        assert entries[0].id == original_id

    def test_allocating_does_not_change_subsequent_calls(self) -> None:
        """Allocation is pure: calling it doesn't register the new ID."""
        entries = [_d_entry("D-0001")]
        result1 = allocate_next_id("D", entries)
        # If the allocator had side effects, this would return D-0003
        result2 = allocate_next_id("D", entries)
        assert result1 == "D-0002"
        assert result2 == "D-0002"  # Same — no side effects
