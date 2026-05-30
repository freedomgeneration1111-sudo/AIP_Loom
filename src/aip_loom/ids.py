"""Canonical ID allocator for AIP_Loom.

This module is the **single authority** for allocating new sequential IDs
across all prefixes (C-, D-, T-, Q-, S-, CM-).  No other module may
compute or guess the next available ID — it must call
:func:`allocate_next_id` here.

Design principles (BuildSpec §3A and Chunk 04 description):

- **Canonical-only**: The allocator reads *only* from canonical ledger
  state (validated Pydantic model instances).  It never reads from
  staged, archive, or ad-hoc sources.  This prevents ID collisions
  from uncommitted or rolled-back state.
- **No gap-filling**: If IDs D-0001 and D-0003 exist, the next ID is
  D-0004, not D-0002.  Gap-filling introduces ordering confusion and
  is not worth the complexity.
- **Prefix-scoped**: Each prefix has its own sequence.  D-0001 and
  T-0001 are independent.
- **Honest on empty**: When no entries exist for a prefix, the first
  ID is ``{prefix}-0001``.
- **Rejects duplicates**: If the caller passes entries containing
  duplicate IDs for the same prefix, :class:`DuplicateIdError` is
  raised rather than silently proceeding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .errors import CHUNK_ID_INVALID, ID_DUPLICATE, LoomError

# Re-export for downstream code
__all__ = [
    "DuplicateIdError",
    "allocate_next_id",
    "extract_id_number",
    "KNOWN_PREFIXES",
]

# ---------------------------------------------------------------------------
# Known ID prefixes and their patterns
# ---------------------------------------------------------------------------

#: Canonical prefixes and the regex patterns they must match.
#: Each prefix maps to a compiled regex that validates the full ID string.
KNOWN_PREFIXES: dict[str, re.Pattern[str]] = {
    "C": re.compile(r"^C-\d{4,}$"),
    "CH": re.compile(r"^CH-\d{4,}$"),
    "D": re.compile(r"^D-\d{4,}$"),
    "T": re.compile(r"^T-\d{4,}$"),
    "Q": re.compile(r"^Q-\d{4,}$"),
    "S": re.compile(r"^S-\d{4,}$"),
    "CM": re.compile(r"^CM-\d{4,}$"),
}

#: Reverse map: from a full ID string, determine the prefix and number.
_ID_RE = re.compile(r"^([A-Z]{1,3})-(\d{4,})$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DuplicateIdError(Exception):
    """Raised when duplicate IDs are detected in the entry list.

    Carries a :class:`LoomError` with the ``ID_DUPLICATE`` stable code.
    """

    def __init__(self, loom_error: LoomError) -> None:
        self.loom_error = loom_error
        super().__init__(loom_error.message)


class InvalidIdError(Exception):
    """Raised when an ID does not match the expected pattern.

    Carries a :class:`LoomError` with the ``CHUNK_ID_INVALID`` stable code.
    """

    def __init__(self, loom_error: LoomError) -> None:
        self.loom_error = loom_error
        super().__init__(loom_error.message)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_id_number(id_str: str) -> tuple[str, int]:
    """Parse a canonical ID string into its prefix and numeric part.

    Parameters
    ----------
    id_str:
        A canonical ID like ``"D-0001"`` or ``"CM-0012"``.

    Returns
    -------
    tuple[str, int]
        The prefix (e.g. ``"D"``) and the numeric value (e.g. ``1``).

    Raises
    ------
    InvalidIdError
        If *id_str* does not match the canonical ID pattern.
    """
    m = _ID_RE.match(id_str)
    if not m:
        raise InvalidIdError(
            LoomError(
                code=CHUNK_ID_INVALID,
                message=f"Invalid canonical ID: {id_str!r}",
                detail={"id": id_str},
            )
        )
    return m.group(1), int(m.group(2))


def allocate_next_id(
    prefix: str,
    entries: Sequence[Any],
    id_attr: str = "id",
) -> str:
    """Allocate the next sequential ID for the given prefix.

    This function scans the ``id_attr`` field of each entry in *entries*,
    finds the highest numeric ID for *prefix*, and returns the next one.
    It performs several integrity checks:

    1. *prefix* must be a known prefix (in :data:`KNOWN_PREFIXES`).
    2. Every entry's ID matching *prefix* must conform to the prefix pattern.
    3. No duplicate IDs are allowed for the same prefix.
    4. Entries with other prefixes are silently ignored (they are scoped
       independently).

    Parameters
    ----------
    prefix:
        The ID prefix to allocate for (e.g. ``"D"``, ``"T"``, ``"C"``).
    entries:
        A sequence of Pydantic model instances (or any objects with an
        ``id`` attribute).  **Must come from canonical state only** —
        never from staged, archive, or unvalidated sources.
    id_attr:
        The attribute name on each entry that holds the ID string.
        Defaults to ``"id"``.

    Returns
    -------
    str
        The next available canonical ID (e.g. ``"D-0002"``).

    Raises
    ------
    DuplicateIdError
        If duplicate IDs for *prefix* are found in *entries*.
    InvalidIdError
        If an ID matching *prefix* does not conform to the expected pattern.

    Examples
    --------
    >>> from aip_loom.schemas import DecisionEntry
    >>> entries = [
    ...     DecisionEntry(id="D-0001", review_state="approved",
    ...                   created_at="2026-01-01T00:00:00Z", summary="X"),
    ... ]
    >>> allocate_next_id("D", entries)
    'D-0002'
    """
    if prefix not in KNOWN_PREFIXES:
        raise InvalidIdError(
            LoomError(
                code=CHUNK_ID_INVALID,
                message=f"Unknown ID prefix: {prefix!r}",
                detail={"prefix": prefix, "known": sorted(KNOWN_PREFIXES)},
            )
        )

    pattern = KNOWN_PREFIXES[prefix]
    seen_ids: dict[str, int] = {}
    max_number = 0

    for entry in entries:
        entry_id = getattr(entry, id_attr, None)
        if entry_id is None:
            continue

        # Only consider IDs matching our prefix
        if not entry_id.startswith(f"{prefix}-"):
            continue

        # Validate against the full pattern
        if not pattern.match(entry_id):
            raise InvalidIdError(
                LoomError(
                    code=CHUNK_ID_INVALID,
                    message=(
                        f"ID {entry_id!r} starts with prefix {prefix!r} "
                        f"but does not match pattern {pattern.pattern!r}"
                    ),
                    detail={"id": entry_id, "prefix": prefix},
                )
            )

        # Check for duplicates
        if entry_id in seen_ids:
            raise DuplicateIdError(
                LoomError(
                    code=ID_DUPLICATE,
                    message=f"Duplicate ID {entry_id!r} found in entries",
                    detail={"id": entry_id, "prefix": prefix},
                )
            )
        seen_ids[entry_id] = 1

        # Track the maximum numeric value
        _, number = extract_id_number(entry_id)
        if number > max_number:
            max_number = number

    next_number = max_number + 1
    return f"{prefix}-{next_number:04d}"
