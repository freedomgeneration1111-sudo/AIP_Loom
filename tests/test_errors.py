"""Tests for aip_loom.errors — error taxonomy, dataclasses, immutability."""

from __future__ import annotations

import pytest

from aip_loom.errors import (
    LoomError,
    LoomWarning,
    NOT_IMPLEMENTED,
    LOCK_HELD,
    CHUNK_NOT_FOUND,
    CHUNK_ORDER_FALLBACK_WARNING,
)


class TestLoomError:
    """Tests for the LoomError frozen dataclass."""

    def test_construction_minimal(self) -> None:
        err = LoomError(code=NOT_IMPLEMENTED, message="not built yet")
        assert err.code == NOT_IMPLEMENTED
        assert err.message == "not built yet"
        assert err.detail == {}

    def test_construction_with_detail(self) -> None:
        err = LoomError(
            code=LOCK_HELD,
            message="lock held",
            detail={"pid": 12345, "command": "reconcile"},
        )
        assert err.detail["pid"] == 12345

    def test_frozen_immutability(self) -> None:
        err = LoomError(code=NOT_IMPLEMENTED, message="x")
        with pytest.raises(AttributeError):
            err.code = "MUTATED"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = LoomError(code=NOT_IMPLEMENTED, message="x")
        b = LoomError(code=NOT_IMPLEMENTED, message="x")
        assert a == b

    def test_inequality_different_code(self) -> None:
        a = LoomError(code=NOT_IMPLEMENTED, message="x")
        b = LoomError(code=LOCK_HELD, message="x")
        assert a != b


class TestLoomWarning:
    """Tests for the LoomWarning frozen dataclass."""

    def test_construction_minimal(self) -> None:
        w = LoomWarning(code=CHUNK_ORDER_FALLBACK_WARNING, message="falling back")
        assert w.code == CHUNK_ORDER_FALLBACK_WARNING
        assert w.message == "falling back"
        assert w.detail == {}

    def test_frozen_immutability(self) -> None:
        w = LoomWarning(code=CHUNK_ORDER_FALLBACK_WARNING, message="x")
        with pytest.raises(AttributeError):
            w.code = "MUTATED"  # type: ignore[misc]


class TestErrorCodeConstants:
    """Verify that error codes are stable strings (not auto-generated)."""

    def test_not_implemented_is_stable(self) -> None:
        assert NOT_IMPLEMENTED == "NOT_IMPLEMENTED"

    def test_lock_held_is_stable(self) -> None:
        assert LOCK_HELD == "LOCK_HELD"

    def test_chunk_not_found_is_stable(self) -> None:
        assert CHUNK_NOT_FOUND == "CHUNK_NOT_FOUND"

    def test_warning_code_is_stable(self) -> None:
        assert CHUNK_ORDER_FALLBACK_WARNING == "CHUNK_ORDER_FALLBACK_WARNING"
