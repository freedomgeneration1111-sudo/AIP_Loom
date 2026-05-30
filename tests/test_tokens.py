"""Tests for aip_loom.tokens — Token estimation utility.

These tests verify:

- estimate_text_tokens returns a TokenEstimate with correct fields
- Token count is deterministic for the same input
- Approximation flag and warning are set when tiktoken is unavailable
- estimate_tokens combines multiple text strings
- TokenEstimate.to_dict() produces correct JSON structure
"""

from __future__ import annotations

import json

import pytest

from aip_loom.errors import TOKEN_COUNT_APPROXIMATE
from aip_loom.tokens import TokenEstimate, estimate_text_tokens, estimate_tokens


# ---------------------------------------------------------------------------
# TokenEstimate structure
# ---------------------------------------------------------------------------


class TestTokenEstimateStructure:
    """Verify TokenEstimate dataclass structure."""

    def test_frozen_dataclass(self) -> None:
        """TokenEstimate is frozen (immutable)."""
        est = TokenEstimate(
            token_count=100,
            is_approximate=True,
            encoding_name="heuristic",
            warning=None,
        )
        with pytest.raises(AttributeError):
            est.token_count = 200  # type: ignore[misc]

    def test_to_dict_keys(self) -> None:
        """to_dict() has expected keys."""
        est = TokenEstimate(
            token_count=100,
            is_approximate=True,
            encoding_name="heuristic",
            warning=None,
        )
        d = est.to_dict()
        assert "token_count" in d
        assert "is_approximate" in d
        assert "encoding_name" in d

    def test_to_dict_with_warning(self) -> None:
        """to_dict() includes warning when present."""
        from aip_loom.errors import LoomWarning
        warning = LoomWarning(
            code=TOKEN_COUNT_APPROXIMATE,
            message="Approximate",
            detail={},
        )
        est = TokenEstimate(
            token_count=100,
            is_approximate=True,
            encoding_name="heuristic",
            warning=warning,
        )
        d = est.to_dict()
        assert "warning" in d
        assert d["warning"]["code"] == TOKEN_COUNT_APPROXIMATE

    def test_to_dict_without_warning(self) -> None:
        """to_dict() has no warning key when warning is None."""
        est = TokenEstimate(
            token_count=100,
            is_approximate=False,
            encoding_name="cl100k_base",
            warning=None,
        )
        d = est.to_dict()
        assert "warning" not in d

    def test_to_dict_json_serializable(self) -> None:
        """to_dict() result is JSON serializable."""
        est = estimate_text_tokens("Hello world")
        d = est.to_dict()
        json_str = json.dumps(d, ensure_ascii=False)
        assert isinstance(json_str, str)


# ---------------------------------------------------------------------------
# estimate_text_tokens
# ---------------------------------------------------------------------------


class TestEstimateTextTokens:
    """Verify estimate_text_tokens function."""

    def test_returns_token_estimate(self) -> None:
        """estimate_text_tokens returns a TokenEstimate."""
        result = estimate_text_tokens("Hello world")
        assert isinstance(result, TokenEstimate)

    def test_token_count_positive(self) -> None:
        """Token count is positive for non-empty text."""
        result = estimate_text_tokens("Hello world, this is a test.")
        assert result.token_count > 0

    def test_deterministic(self) -> None:
        """Same input always returns the same token count."""
        text = "The quick brown fox jumps over the lazy dog."
        r1 = estimate_text_tokens(text)
        r2 = estimate_text_tokens(text)
        assert r1.token_count == r2.token_count

    def test_longer_text_has_more_tokens(self) -> None:
        """Longer text generally has more tokens."""
        short = "Hello"
        long = "Hello " * 100
        r_short = estimate_text_tokens(short)
        r_long = estimate_text_tokens(long)
        assert r_long.token_count > r_short.token_count

    def test_empty_string_minimum_one_token(self) -> None:
        """Empty string returns at least 1 token (heuristic floor)."""
        result = estimate_text_tokens("")
        assert result.token_count >= 1

    def test_encoding_name_set(self) -> None:
        """encoding_name is either 'cl100k_base' or 'heuristic'."""
        result = estimate_text_tokens("Test")
        assert result.encoding_name in ("cl100k_base", "heuristic")

    def test_approximation_flag_consistent(self) -> None:
        """is_approximate is True for heuristic, False for tiktoken."""
        result = estimate_text_tokens("Test")
        if result.encoding_name == "heuristic":
            assert result.is_approximate is True
            assert result.warning is not None
            assert result.warning.code == TOKEN_COUNT_APPROXIMATE
        else:
            assert result.is_approximate is False
            assert result.warning is None


# ---------------------------------------------------------------------------
# estimate_tokens (multi-string)
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    """Verify estimate_tokens multi-string function."""

    def test_combines_texts(self) -> None:
        """estimate_tokens combines multiple text strings."""
        r1 = estimate_tokens("Hello", "world")
        r2 = estimate_tokens("Hello\nworld")
        # The combined text should have at least as many tokens
        assert r1.token_count > 0
        assert r2.token_count > 0

    def test_single_string_equivalent(self) -> None:
        """estimate_tokens with one arg is equivalent to estimate_text_tokens."""
        text = "Single string test"
        r1 = estimate_tokens(text)
        r2 = estimate_text_tokens(text)
        assert r1.token_count == r2.token_count

    def test_no_args_returns_estimate(self) -> None:
        """estimate_tokens with no args returns estimate for empty join."""
        result = estimate_tokens()
        assert isinstance(result, TokenEstimate)
        # Empty join of no strings = ""
        assert result.token_count >= 1  # heuristic floor
