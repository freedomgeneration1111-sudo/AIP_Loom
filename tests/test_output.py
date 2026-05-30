"""Tests for aip_loom.output — result renderer."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

from aip_loom.errors import LoomError, LoomWarning, NOT_IMPLEMENTED
from aip_loom.output import render_result
from aip_loom.results import CommandResult


class TestRenderResultJson:
    """JSON mode rendering tests."""

    def test_json_success(self) -> None:
        r = CommandResult.success(command="init", message="created", data={"path": "/tmp/x"})
        buf = StringIO()
        with patch("sys.stdout", buf):
            render_result(r, use_json=True)
        parsed = json.loads(buf.getvalue())
        assert parsed["ok"] is True
        assert parsed["data"]["path"] == "/tmp/x"

    def test_json_failure(self) -> None:
        r = CommandResult.failure(
            command="status",
            code=NOT_IMPLEMENTED,
            message="not built",
        )
        buf = StringIO()
        with patch("sys.stdout", buf):
            render_result(r, use_json=True)
        parsed = json.loads(buf.getvalue())
        assert parsed["ok"] is False
        assert parsed["code"] == NOT_IMPLEMENTED

    def test_json_includes_warnings(self) -> None:
        w = LoomWarning(code="CHUNK_ORDER_FALLBACK_WARNING", message="fallback")
        r = CommandResult.success(command="test", message="ok", warnings=[w])
        buf = StringIO()
        with patch("sys.stdout", buf):
            render_result(r, use_json=True)
        parsed = json.loads(buf.getvalue())
        assert len(parsed["warnings"]) == 1

    def test_json_includes_errors(self) -> None:
        r = CommandResult.failure(
            command="test",
            code=NOT_IMPLEMENTED,
            message="fail",
            errors=[
                LoomError(code=NOT_IMPLEMENTED, message="not built"),
            ],
        )
        buf = StringIO()
        with patch("sys.stdout", buf):
            render_result(r, use_json=True)
        parsed = json.loads(buf.getvalue())
        assert len(parsed["errors"]) == 1
        assert parsed["errors"][0]["code"] == NOT_IMPLEMENTED


class TestRenderResultRich:
    """Rich mode rendering tests — just verify no exceptions are raised."""

    def test_rich_success_no_exception(self) -> None:
        r = CommandResult.success(command="init", message="created")
        # Rich writes to its own Console; we just ensure no crash.
        render_result(r, use_json=False)

    def test_rich_failure_no_exception(self) -> None:
        r = CommandResult.failure(
            command="status",
            code=NOT_IMPLEMENTED,
            message="not built",
        )
        render_result(r, use_json=False)

    def test_rich_with_warnings_and_errors(self) -> None:
        r = CommandResult.failure(
            command="test",
            code=NOT_IMPLEMENTED,
            message="fail",
            warnings=[LoomWarning(code="CHUNK_ORDER_FALLBACK_WARNING", message="w")],
            errors=[LoomError(code=NOT_IMPLEMENTED, message="e")],
        )
        render_result(r, use_json=False)

    def test_rich_with_data(self) -> None:
        r = CommandResult.success(
            command="status",
            message="ok",
            data={"chunks": 3, "pending": 1},
        )
        render_result(r, use_json=False)
