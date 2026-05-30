"""Tests for aip_loom.results — CommandResult envelope."""

from __future__ import annotations

import json

import pytest

from aip_loom.errors import LoomError, LoomWarning, NOT_IMPLEMENTED, LOCK_HELD
from aip_loom.results import CommandResult


class TestCommandResultConstruction:
    """Direct construction tests."""

    def test_success_factory(self) -> None:
        r = CommandResult.success(command="test", message="all good")
        assert r.ok is True
        assert r.code == "OK"
        assert r.command == "test"
        assert r.message == "all good"
        assert r.errors == []
        assert r.warnings == []

    def test_success_with_data_and_warnings(self) -> None:
        w = LoomWarning(code="CHUNK_ORDER_FALLBACK_WARNING", message="fallback")
        r = CommandResult.success(
            command="status",
            message="done",
            data={"chunks": 5},
            warnings=[w],
        )
        assert r.data == {"chunks": 5}
        assert len(r.warnings) == 1

    def test_failure_factory_synthesises_error(self) -> None:
        r = CommandResult.failure(
            command="init",
            code=NOT_IMPLEMENTED,
            message="not built",
        )
        assert r.ok is False
        assert r.code == NOT_IMPLEMENTED
        assert len(r.errors) == 1
        assert r.errors[0].code == NOT_IMPLEMENTED

    def test_failure_factory_with_explicit_errors(self) -> None:
        errs = [
            LoomError(code=LOCK_HELD, message="locked"),
            LoomError(code=NOT_IMPLEMENTED, message="stub"),
        ]
        r = CommandResult.failure(
            command="reconcile",
            code=LOCK_HELD,
            message="cannot proceed",
            errors=errs,
        )
        assert len(r.errors) == 2


class TestCommandResultImmutability:
    """Frozen dataclass enforcement."""

    def test_frozen(self) -> None:
        r = CommandResult.success(command="test", message="ok")
        with pytest.raises(AttributeError):
            r.ok = False  # type: ignore[misc]

    def test_frozen_data_dict(self) -> None:
        """The dict inside data is mutable; the field reference is not."""
        r = CommandResult.success(command="test", message="ok", data={"k": "v"})
        # You can mutate the dict contents (Python doesn't deep-freeze),
        # but you cannot reassign the field.
        with pytest.raises(AttributeError):
            r.data = {}  # type: ignore[misc]


class TestCommandResultSerialization:
    """to_dict / to_json tests."""

    def test_to_dict_success(self) -> None:
        r = CommandResult.success(command="init", message="created")
        d = r.to_dict()
        assert d["ok"] is True
        assert d["code"] == "OK"
        assert d["command"] == "init"
        assert d["warnings"] == []
        assert d["errors"] == []

    def test_to_dict_failure(self) -> None:
        r = CommandResult.failure(
            command="status",
            code=NOT_IMPLEMENTED,
            message="stub",
        )
        d = r.to_dict()
        assert d["ok"] is False
        assert d["code"] == NOT_IMPLEMENTED
        assert len(d["errors"]) == 1
        assert d["errors"][0]["code"] == NOT_IMPLEMENTED

    def test_to_json_roundtrip(self) -> None:
        r = CommandResult.failure(
            command="validate",
            code=NOT_IMPLEMENTED,
            message="stub",
            warnings=[
                LoomWarning(code="CHUNK_ORDER_FALLBACK_WARNING", message="fallback")
            ],
        )
        payload = r.to_json()
        parsed = json.loads(payload)
        assert parsed["ok"] is False
        assert len(parsed["warnings"]) == 1
        assert parsed["warnings"][0]["code"] == "CHUNK_ORDER_FALLBACK_WARNING"

    def test_to_json_preserves_unicode(self) -> None:
        r = CommandResult.success(command="test", message="données")
        parsed = json.loads(r.to_json())
        assert parsed["message"] == "données"


class TestCommandResultExitCode:
    """exit_code property tests."""

    def test_success_exit_code(self) -> None:
        r = CommandResult.success(command="test", message="ok")
        assert r.exit_code == 0

    def test_failure_exit_code(self) -> None:
        r = CommandResult.failure(
            command="test",
            code=NOT_IMPLEMENTED,
            message="fail",
        )
        assert r.exit_code == 1
