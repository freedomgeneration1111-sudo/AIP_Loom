"""Prose-body checksum calculator for AIP_Loom.

This module is the **single authority** for computing checksums over chunk
prose bodies.  No other module may compute its own checksum — it must
call :func:`compute_prose_checksum` here.

Design principles (BuildSpec §3A and Chunk 04 description):

- **Prose body only**: The checksum covers only the prose content below
  the YAML frontmatter, not the frontmatter itself.  This ensures that
  metadata changes (e.g. updating ``word_count`` or ``updated_at``) do
  not falsely trigger checksum mismatches.
- **LF normalization**: All line endings are normalized to LF (``\\n``)
  before hashing.  This ensures that the same prose content produces
  the same checksum regardless of whether the file was edited on
  Windows (CRLF) or Unix (LF).
- **Trailing newline stripped**: A single trailing newline (if present)
  is stripped before hashing so that editors that add/remove a final
  newline do not cause spurious mismatches.
- **No silent updates**: Checksums are computed on demand and returned
  as strings.  This module never writes to files or updates schemas.
  The caller is responsible for storing the result, and only during
  explicit write operations.
- **SHA-256**: The hash algorithm is SHA-256, providing a good balance
  of collision resistance and performance for text content.
"""

from __future__ import annotations

import hashlib

__all__ = [
    "compute_prose_checksum",
    "CHECKSUM_ALGORITHM",
]

#: The hash algorithm used for prose checksums.
CHECKSUM_ALGORITHM = "sha-256"


def compute_prose_checksum(prose: str) -> str:
    """Compute a deterministic checksum over the prose body of a chunk.

    The checksum is computed over the prose body **only**, after the
    following normalizations:

    1. All line endings are normalized to LF (``\\n``).  CRLF (``\\r\\n``)
       and bare CR (``\\r``) are replaced with LF.
    2. A single trailing newline (if present after normalization) is
       stripped so that editors that add/remove a final newline do not
       cause spurious mismatches.

    Parameters
    ----------
    prose:
        The prose body text (everything below the YAML frontmatter).
        Must **not** include the frontmatter or its delimiters.

    Returns
    -------
    str
        The hex-encoded SHA-256 digest of the normalized prose.

    Examples
    --------
    >>> compute_prose_checksum("Hello world")
    '64ec88ca00b268e5ba1a35678a1b5316d210f2f81ede36fd8ef4a1c7...'
    >>> # CRLF is normalized to LF — same result as LF
    >>> compute_prose_checksum("Hello\\r\\nworld") == compute_prose_checksum("Hello\\nworld")
    True
    """
    # Step 1: Normalize line endings to LF
    normalized = prose.replace("\r\n", "\n").replace("\r", "\n")

    # Step 2: Strip a single trailing newline if present
    if normalized.endswith("\n"):
        normalized = normalized[:-1]

    # Step 3: Compute SHA-256
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest
