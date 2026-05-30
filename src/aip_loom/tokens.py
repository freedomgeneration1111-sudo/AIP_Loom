"""Token estimation utility for AIP_Loom.

This module is the **single authority** for estimating token counts across
all AIP_Loom commands.  Both ``inspect`` and ``brief`` must use
:func:`estimate_tokens` here — no other module may implement its own
token counting logic.

Design principles (BuildSpec §3A and Chunk 11 description):

- **Consistent estimation**: Every command that needs token counts uses
  the same estimation function.  If tiktoken is available, it is used
  for precise counting; otherwise a deterministic heuristic is applied.
- **Deterministic**: The heuristic (``len(text) // 4``) is deterministic
  and does not depend on external state.  Given the same input, it
  always returns the same estimate.
- **Approximation flag**: When the heuristic is used instead of tiktoken,
  a :class:`LoomWarning` with code ``TOKEN_COUNT_APPROXIMATE`` is
  included so that callers (and users) know the count is imprecise.
- **No silent switching**: The caller always knows whether tiktoken was
  used or the heuristic was applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import TOKEN_COUNT_APPROXIMATE, LoomWarning

__all__ = [
    "TokenEstimate",
    "estimate_tokens",
    "estimate_text_tokens",
]

# ---------------------------------------------------------------------------
# Tiktoken availability check
# ---------------------------------------------------------------------------

_tiktoken_available: bool | None = None


def _is_tiktoken_available() -> bool:
    """Check whether tiktoken is importable."""
    global _tiktoken_available
    if _tiktoken_available is None:
        try:
            import tiktoken  # noqa: F401
            _tiktoken_available = True
        except ImportError:
            _tiktoken_available = False
    return _tiktoken_available


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenEstimate:
    """The result of estimating token counts for context selection.

    Attributes
    ----------
    token_count:
        Estimated number of tokens.
    is_approximate:
        Whether the estimate uses a heuristic (True) or tiktoken (False).
    encoding_name:
        Name of the encoding used (e.g. ``"cl100k_base"``) or
        ``"heuristic"`` if tiktoken is unavailable.
    warning:
        A :class:`LoomWarning` with code ``TOKEN_COUNT_APPROXIMATE``
        if the heuristic was used, or ``None`` if tiktoken was used.
    """

    token_count: int
    is_approximate: bool
    encoding_name: str
    warning: LoomWarning | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        result: dict[str, Any] = {
            "token_count": self.token_count,
            "is_approximate": self.is_approximate,
            "encoding_name": self.encoding_name,
        }
        if self.warning is not None:
            result["warning"] = {
                "code": self.warning.code,
                "message": self.warning.message,
            }
        return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def estimate_text_tokens(text: str) -> TokenEstimate:
    """Estimate the token count for a single text string.

    Uses tiktoken with ``cl100k_base`` encoding if available, otherwise
    falls back to the ``len(text) // 4`` heuristic.

    Parameters
    ----------
    text:
        The text to estimate tokens for.

    Returns
    -------
    TokenEstimate
        The token estimate with approximation metadata.
    """
    if _is_tiktoken_available():
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        count = len(enc.encode(text))
        return TokenEstimate(
            token_count=count,
            is_approximate=False,
            encoding_name="cl100k_base",
            warning=None,
        )

    # Heuristic fallback: ~4 characters per token for English text
    count = max(1, len(text) // 4)
    warning = LoomWarning(
        code=TOKEN_COUNT_APPROXIMATE,
        message=(
            "Token count is approximate (tiktoken not available). "
            "Install with: pip install aip-loom[tokens]"
        ),
        detail={"method": "heuristic", "chars_per_token": 4},
    )
    return TokenEstimate(
        token_count=count,
        is_approximate=True,
        encoding_name="heuristic",
        warning=warning,
    )


def estimate_tokens(*texts: str) -> TokenEstimate:
    """Estimate the combined token count for multiple text strings.

    This is a convenience wrapper that concatenates all texts and
    estimates the total token count.

    Parameters
    ----------
    *texts:
        One or more text strings to estimate tokens for.

    Returns
    -------
    TokenEstimate
        The combined token estimate.
    """
    combined = "\n".join(texts)
    return estimate_text_tokens(combined)
