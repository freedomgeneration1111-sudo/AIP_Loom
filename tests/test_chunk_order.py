"""Tests for aip_loom.chunk_order — chunk order resolver.

These tests prove:
- Manifest order is respected when chunks.order is non-empty
- Filename fallback with CHUNK_ORDER_FALLBACK_USED warning when no order
- Empty chunk IDs → empty result with no warnings
- Unmapped chunks (in filesystem but not in manifest order) are appended
- Natural sort produces correct ordering (C-2 before C-10)
- No silent fallback: warnings are always emitted when fallback is used
- used_manifest_order flag is correct
"""

from __future__ import annotations

import pytest

from aip_loom.chunk_order import (
    ChunkOrderResult,
    natural_sort_key,
    resolve_chunk_order,
)
from aip_loom.errors import (
    CHUNK_ORDER_FALLBACK_USED,
    CHUNK_ORDER_FALLBACK_WARNING,
    LoomWarning,
)
from aip_loom.schemas import ProjectManifest, SUPPORTED_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_V = SUPPORTED_SCHEMA_VERSION


def _manifest(**overrides: object) -> ProjectManifest:
    """Return a valid ProjectManifest, with overrides applied."""
    base = {
        "schema_version": _V,
        "name": "test-novel",
    }
    base.update(overrides)
    return ProjectManifest(**base)


# ===========================================================================
# natural_sort_key
# ===========================================================================


class TestNaturalSortKey:
    """Tests for natural (human-friendly) string sorting."""

    def test_simple_number(self) -> None:
        # natural_sort_key splits on digits; trailing empty string from regex is expected
        key = natural_sort_key("C-0002")
        assert key[0] == "C-"
        assert key[1] == 2

    def test_larger_number(self) -> None:
        key = natural_sort_key("C-0010")
        assert key[0] == "C-"
        assert key[1] == 10

    def test_natural_sort_order(self) -> None:
        """C-0002 comes before C-0010 in natural sort."""
        ids = ["C-0010", "C-0002", "C-0001"]
        assert sorted(ids, key=natural_sort_key) == ["C-0001", "C-0002", "C-0010"]

    def test_different_prefixes(self) -> None:
        ids = ["C-0010", "D-0001", "C-0002"]
        sorted_ids = sorted(ids, key=natural_sort_key)
        # C-0002 < C-0010 < D-0001 (lexicographic on prefix, then numeric)
        assert sorted_ids == ["C-0002", "C-0010", "D-0001"]


# ===========================================================================
# resolve_chunk_order — manifest order respected
# ===========================================================================


class TestManifestOrderRespected:
    """When chunks.order is non-empty, that order is canonical."""

    def test_manifest_order_used(self) -> None:
        manifest = _manifest(chunks={"order": ["C-0002", "C-0001", "C-0003"]})
        result = resolve_chunk_order(manifest, ["C-0001", "C-0002", "C-0003"])
        assert result.ordered_ids == ["C-0002", "C-0001", "C-0003"]
        assert result.used_manifest_order is True

    def test_manifest_order_no_warnings(self) -> None:
        manifest = _manifest(chunks={"order": ["C-0001", "C-0002"]})
        result = resolve_chunk_order(manifest, ["C-0001", "C-0002"])
        assert result.warnings == []

    def test_manifest_order_subset_of_filesystem(self) -> None:
        """Chunks in manifest order that don't exist on filesystem are
        simply omitted from the result (they're not present in chunk_ids)."""
        manifest = _manifest(chunks={"order": ["C-0001", "C-0002", "C-0099"]})
        result = resolve_chunk_order(manifest, ["C-0001", "C-0002"])
        assert result.ordered_ids == ["C-0001", "C-0002"]

    def test_manifest_order_single_chunk(self) -> None:
        manifest = _manifest(chunks={"order": ["C-0003"]})
        result = resolve_chunk_order(manifest, ["C-0003"])
        assert result.ordered_ids == ["C-0003"]
        assert result.used_manifest_order is True


# ===========================================================================
# resolve_chunk_order — unmapped chunks
# ===========================================================================


class TestUnmappedChunks:
    """Chunks in filesystem but not in manifest order are appended."""

    def test_unmapped_chunks_appended_with_warning(self) -> None:
        manifest = _manifest(chunks={"order": ["C-0001"]})
        result = resolve_chunk_order(manifest, ["C-0001", "C-0002", "C-0003"])
        assert result.ordered_ids == ["C-0001", "C-0002", "C-0003"]
        assert len(result.warnings) == 1
        assert result.warnings[0].code == CHUNK_ORDER_FALLBACK_WARNING
        assert "unmapped" in result.warnings[0].message.lower() or "not in manifest" in result.warnings[0].message.lower()

    def test_unmapped_chunks_sorted_naturally(self) -> None:
        """Unmapped chunks are appended in natural sort order."""
        manifest = _manifest(chunks={"order": ["C-0003"]})
        result = resolve_chunk_order(manifest, ["C-0010", "C-0002", "C-0003"])
        # C-0003 from manifest order first, then unmapped in natural sort
        assert result.ordered_ids == ["C-0003", "C-0002", "C-0010"]

    def test_all_chunks_unmapped(self) -> None:
        """If no chunks match manifest order, all are unmapped."""
        manifest = _manifest(chunks={"order": ["C-0099"]})
        result = resolve_chunk_order(manifest, ["C-0001", "C-0002"])
        # C-0099 not in chunk_ids, so all are unmapped, appended in natural sort
        assert result.ordered_ids == ["C-0001", "C-0002"]


# ===========================================================================
# resolve_chunk_order — filename fallback
# ===========================================================================


class TestFilenameFallback:
    """When chunks.order is empty, fall back to filename sort with warning."""

    def test_empty_order_falls_back(self) -> None:
        manifest = _manifest(chunks={"order": []})
        result = resolve_chunk_order(manifest, ["C-0010", "C-0002", "C-0001"])
        assert result.ordered_ids == ["C-0001", "C-0002", "C-0010"]
        assert result.used_manifest_order is False

    def test_empty_order_emits_fallback_warning(self) -> None:
        manifest = _manifest(chunks={"order": []})
        result = resolve_chunk_order(manifest, ["C-0001", "C-0002"])
        assert len(result.warnings) == 1
        assert result.warnings[0].code == CHUNK_ORDER_FALLBACK_USED

    def test_no_chunks_key_falls_back(self) -> None:
        """If the manifest has no chunks key at all (default ChunkOrder
        has empty order list), fallback is used."""
        manifest = _manifest()
        result = resolve_chunk_order(manifest, ["C-0002", "C-0001"])
        assert result.ordered_ids == ["C-0001", "C-0002"]
        assert result.used_manifest_order is False

    def test_no_chunks_key_emits_warning(self) -> None:
        manifest = _manifest()
        result = resolve_chunk_order(manifest, ["C-0001"])
        assert len(result.warnings) == 1
        assert result.warnings[0].code == CHUNK_ORDER_FALLBACK_USED

    def test_fallback_warning_message_suggests_manifest(self) -> None:
        manifest = _manifest()
        result = resolve_chunk_order(manifest, ["C-0001"])
        assert "manifest" in result.warnings[0].message.lower()


# ===========================================================================
# resolve_chunk_order — empty chunk IDs
# ===========================================================================


class TestEmptyChunkIds:
    """No chunk IDs → empty result."""

    def test_empty_chunk_ids_returns_empty(self) -> None:
        manifest = _manifest(chunks={"order": ["C-0001"]})
        result = resolve_chunk_order(manifest, [])
        assert result.ordered_ids == []

    def test_empty_chunk_ids_no_warnings(self) -> None:
        manifest = _manifest()
        result = resolve_chunk_order(manifest, [])
        assert result.warnings == []


# ===========================================================================
# resolve_chunk_order — no silent fallback
# ===========================================================================


class TestNoSilentFallback:
    """Warnings must always be emitted when fallback is used."""

    def test_fallback_always_warns(self) -> None:
        """Every fallback usage produces at least one warning."""
        manifest = _manifest()
        for chunk_ids in [["C-0001"], ["C-0001", "C-0002"], ["C-0010"]]:
            result = resolve_chunk_order(manifest, chunk_ids)
            if chunk_ids:
                assert len(result.warnings) >= 1
                assert result.warnings[0].code == CHUNK_ORDER_FALLBACK_USED

    def test_manifest_order_no_false_warnings(self) -> None:
        """When manifest order is used and all chunks are mapped,
        no fallback warning is emitted."""
        manifest = _manifest(chunks={"order": ["C-0001", "C-0002"]})
        result = resolve_chunk_order(manifest, ["C-0001", "C-0002"])
        fallback_warnings = [w for w in result.warnings if w.code == CHUNK_ORDER_FALLBACK_USED]
        assert len(fallback_warnings) == 0


# ===========================================================================
# resolve_chunk_order — used_manifest_order flag
# ===========================================================================


class TestUsedManifestOrderFlag:
    """The used_manifest_order flag correctly reports the source."""

    def test_manifest_order_true(self) -> None:
        manifest = _manifest(chunks={"order": ["C-0001"]})
        result = resolve_chunk_order(manifest, ["C-0001"])
        assert result.used_manifest_order is True

    def test_fallback_false(self) -> None:
        manifest = _manifest()
        result = resolve_chunk_order(manifest, ["C-0001"])
        assert result.used_manifest_order is False

    def test_empty_chunks_with_manifest_true(self) -> None:
        """Even with empty chunk_ids, if manifest has an order,
        used_manifest_order is True."""
        manifest = _manifest(chunks={"order": ["C-0001"]})
        result = resolve_chunk_order(manifest, [])
        assert result.used_manifest_order is True


# ===========================================================================
# ChunkOrderResult frozen
# ===========================================================================


class TestChunkOrderResultFrozen:
    """ChunkOrderResult must be immutable."""

    def test_result_is_frozen(self) -> None:
        result = ChunkOrderResult(
            ordered_ids=["C-0001"],
            warnings=[],
            used_manifest_order=True,
        )
        with pytest.raises(AttributeError):
            result.ordered_ids = ["C-0099"]  # type: ignore[misc]
