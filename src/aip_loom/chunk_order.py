"""Chunk order resolver for AIP_Loom.

This module is the **single authority** for determining the canonical
ordering of chunks in a project.  No other module may sort chunks
independently — it must call :func:`resolve_chunk_order` here.

Design principles (BuildSpec §3A and Chunk 04 description):

- **Manifest-respected**: If the project manifest contains
  ``chunks.order``, that order is canonical and is returned directly.
- **Filename fallback with warning**: If ``chunks.order`` is empty or
  missing, chunks are sorted by filename (natural sort).  When this
  fallback is used, a :class:`LoomWarning` with code
  ``CHUNK_ORDER_FALLBACK_USED`` is emitted — this is a signal to the
  human that the manifest should be updated.
- **No silent ordering**: The caller always receives both the ordered
  list of chunk IDs and any warnings that were generated.  Silent
  fallback is forbidden.
- **Chunk IDs from frontmatter**: The chunk IDs passed to this module
  should come from parsed frontmatter (via :mod:`aip_loom.frontmatter`),
  not from filename inference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .errors import (
    CHUNK_ORDER_FALLBACK_USED,
    CHUNK_ORDER_FALLBACK_WARNING,
    LoomWarning,
)
from .schemas import ProjectManifest

__all__ = [
    "ChunkOrderResult",
    "resolve_chunk_order",
    "natural_sort_key",
]

# ---------------------------------------------------------------------------
# Natural sort helper
# ---------------------------------------------------------------------------

#: Regex to split a string into numeric and non-numeric segments for
#: natural (human-friendly) sorting.
_NATURAL_SORT_RE = re.compile(r"(\d+)")


def natural_sort_key(s: str) -> list[str | int]:
    """Generate a sort key for natural (human-friendly) string ordering.

    This function splits a string into alternating non-numeric and numeric
    segments, converting numeric segments to integers.  This produces
    a sort order where ``C-2`` comes before ``C-10`` (unlike pure
    lexicographic sort).

    Parameters
    ----------
    s:
        The string to generate a sort key for.

    Returns
    -------
    list[str | int]
        A list of string and integer segments for comparison.

    Examples
    --------
    >>> natural_sort_key("C-0002")
    ['C-', 2]
    >>> natural_sort_key("C-0010")
    ['C-', 10]
    >>> sorted(["C-0010", "C-0002"], key=natural_sort_key)
    ['C-0002', 'C-0010']
    """
    parts = _NATURAL_SORT_RE.split(s)
    result: list[str | int] = []
    for part in parts:
        if part.isdigit():
            result.append(int(part))
        else:
            result.append(part)
    return result


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkOrderResult:
    """The result of resolving chunk order.

    Attributes
    ----------
    ordered_ids:
        The chunk IDs in canonical order.
    warnings:
        Any warnings generated during resolution (e.g. fallback used).
    used_manifest_order:
        Whether the manifest's ``chunks.order`` was used (True) or the
        filename-based fallback was applied (False).
    """

    ordered_ids: list[str]
    warnings: list[LoomWarning]
    used_manifest_order: bool


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_chunk_order(
    manifest: ProjectManifest,
    chunk_ids: Sequence[str],
) -> ChunkOrderResult:
    """Resolve the canonical ordering of chunks in a project.

    The resolution logic follows these rules:

    1. If the manifest has a non-empty ``chunks.order``, that order is
       canonical.  Only chunk IDs that appear in both the manifest order
       and the provided *chunk_ids* are included in the result, and they
       appear in manifest order.  Any chunk IDs in *chunk_ids* that are
       not in the manifest order are appended at the end in natural sort
       order (with a warning about unmapped chunks).
    2. If ``chunks.order`` is empty or missing, all provided *chunk_ids*
       are sorted by natural sort order.  A
       ``CHUNK_ORDER_FALLBACK_USED`` warning is emitted.
    3. If no chunk IDs are provided at all, an empty list is returned
       with no warnings.

    Parameters
    ----------
    manifest:
        The validated project manifest (from :mod:`aip_loom.yaml_io`).
    chunk_ids:
        The sequence of chunk IDs discovered from the filesystem.  These
        should come from parsed frontmatter, not filename inference.

    Returns
    -------
    ChunkOrderResult
        A frozen dataclass with the ordered chunk IDs, any warnings,
        and a flag indicating whether manifest order was used.

    Examples
    --------
    >>> from aip_loom.schemas import ProjectManifest, SUPPORTED_SCHEMA_VERSION
    >>> manifest = ProjectManifest(
    ...     schema_version=SUPPORTED_SCHEMA_VERSION,
    ...     name="test",
    ...     chunks={"order": ["C-0002", "C-0001"]},
    ... )
    >>> result = resolve_chunk_order(manifest, ["C-0001", "C-0002"])
    >>> result.ordered_ids
    ['C-0002', 'C-0001']
    >>> result.used_manifest_order
    True
    """
    warnings: list[LoomWarning] = []
    manifest_order = manifest.chunks.order
    id_set = set(chunk_ids)

    # No chunk IDs at all — nothing to order
    if not chunk_ids:
        return ChunkOrderResult(
            ordered_ids=[],
            warnings=[],
            used_manifest_order=bool(manifest_order),
        )

    # Case 1: Manifest has an explicit order
    if manifest_order:
        ordered: list[str] = []
        seen: set[str] = set()

        # First, include chunks in manifest order (only those that exist)
        for cid in manifest_order:
            if cid in id_set:
                ordered.append(cid)
                seen.add(cid)

        # Any chunks not in the manifest order get appended in natural sort
        unmapped = sorted(
            [cid for cid in chunk_ids if cid not in seen],
            key=natural_sort_key,
        )
        if unmapped:
            warnings.append(
                LoomWarning(
                    code=CHUNK_ORDER_FALLBACK_WARNING,
                    message=(
                        f"Chunk IDs not in manifest order, appended at end "
                        f"in filename sort: {unmapped}"
                    ),
                    detail={"unmapped_ids": unmapped},
                )
            )
            ordered.extend(unmapped)

        return ChunkOrderResult(
            ordered_ids=ordered,
            warnings=warnings,
            used_manifest_order=True,
        )

    # Case 2: No manifest order — fall back to natural filename sort
    warnings.append(
        LoomWarning(
            code=CHUNK_ORDER_FALLBACK_USED,
            message=(
                "No chunks.order in manifest; falling back to filename sort. "
                "Consider adding an explicit chunk order to the manifest."
            ),
            detail={"fallback": "filename_natural_sort"},
        )
    )

    ordered = sorted(chunk_ids, key=natural_sort_key)
    return ChunkOrderResult(
        ordered_ids=ordered,
        warnings=warnings,
        used_manifest_order=False,
    )
