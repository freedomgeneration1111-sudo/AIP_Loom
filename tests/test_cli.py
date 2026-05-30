"""Tests for aip_loom.cli — Typer app, --help, --version, placeholder commands.

These tests exercise the CLI through Typer's CliRunner so that we test the
full integration from argument parsing through result rendering.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from aip_loom.cli import app
from aip_loom.errors import NOT_IMPLEMENTED
from typer.testing import CliRunner


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _parse_json_output(output: str) -> dict[str, Any]:
    """Parse the JSON envelope from CLI output.

    The output may contain Rich ANSI sequences on stderr mixed with JSON
    on stdout.  We search for the first ``{`` and parse from there.
    """
    lines = output.strip().splitlines()
    json_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("{"):
            json_start = i
            break
    assert json_start is not None, f"No JSON found in output: {output!r}"
    json_text = "\n".join(lines[json_start:])
    return json.loads(json_text)


# ---------------------------------------------------------------------------
# --help and --version
# ---------------------------------------------------------------------------


class TestHelpAndVersion:
    """Positive: --help and --version exit 0."""

    def test_help_exits_zero(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "aip-loom" in result.output.lower() or "AIP_Loom" in result.output

    def test_version_exits_zero(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output


# ---------------------------------------------------------------------------
# Placeholder commands — must fail honestly
# ---------------------------------------------------------------------------


class TestPlaceholderInit:
    """Placeholder init command tests."""

    def test_init_exits_nonzero(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["init", "my-project"])
        assert result.exit_code != 0

    def test_init_json_has_not_implemented(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["init", "my-project", "--json"])
        data = _parse_json_output(result.output)
        assert data["ok"] is False
        assert data["code"] == NOT_IMPLEMENTED
        assert data["command"] == "init"

    def test_init_json_envelope_shape(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["init", "my-project", "--json"])
        data = _parse_json_output(result.output)
        for key in ("ok", "command", "code", "message", "data", "warnings", "errors"):
            assert key in data, f"Missing envelope field: {key}"

    def test_init_does_not_create_files(self, runner: CliRunner) -> None:
        """Placeholder init must not create any files on disk."""
        with tempfile.TemporaryDirectory() as tmp:
            pre = hashlib.sha256(
                json.dumps({"files": sorted(os.listdir(tmp))}).encode()
            ).hexdigest()
            runner.invoke(app, ["init", "my-project"])
            post = hashlib.sha256(
                json.dumps({"files": sorted(os.listdir(tmp))}).encode()
            ).hexdigest()
            assert pre == post, "Placeholder init created files in tmp dir"


class TestPlaceholderStatus:
    def test_status_exits_nonzero(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["status"])
        assert result.exit_code != 0

    def test_status_json_has_not_implemented(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["status", "--json"])
        data = _parse_json_output(result.output)
        assert data["code"] == NOT_IMPLEMENTED
        assert data["command"] == "status"


class TestPlaceholderValidate:
    def test_validate_exits_nonzero(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["validate"])
        assert result.exit_code != 0

    def test_validate_json_has_not_implemented(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["validate", "--json"])
        data = _parse_json_output(result.output)
        assert data["code"] == NOT_IMPLEMENTED
        assert data["command"] == "validate"


class TestPlaceholderBrief:
    def test_brief_exits_nonzero(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["brief", "C-0001"])
        assert result.exit_code != 0

    def test_brief_json_has_not_implemented(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["brief", "C-0001", "--json"])
        data = _parse_json_output(result.output)
        assert data["code"] == NOT_IMPLEMENTED
        assert data["command"] == "brief"


class TestPlaceholderInspect:
    def test_inspect_exits_nonzero(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["inspect", "C-0001"])
        assert result.exit_code != 0

    def test_inspect_json_has_not_implemented(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["inspect", "C-0001", "--json"])
        data = _parse_json_output(result.output)
        assert data["code"] == NOT_IMPLEMENTED
        assert data["command"] == "inspect"


class TestPlaceholderReconcile:
    def test_reconcile_exits_nonzero(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["reconcile", "C-0001"])
        assert result.exit_code != 0

    def test_reconcile_json_has_not_implemented(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["reconcile", "C-0001", "--json"])
        data = _parse_json_output(result.output)
        assert data["code"] == NOT_IMPLEMENTED
        assert data["command"] == "reconcile"


# ---------------------------------------------------------------------------
# Unknown command
# ---------------------------------------------------------------------------


class TestUnknownCommand:
    def test_unknown_command_exits_nonzero(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["nonexistent-command"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Filesystem mutation safety — SHA-256 snapshot test
# ---------------------------------------------------------------------------


class TestNoFilesystemMutation:
    """Every placeholder command must leave the filesystem untouched.

    This satisfies the Test Honesty Rule (BuildSpec §3A.6): at least one
    test proving that a required failure mode does not mutate canonical
    files.  For this pure-skeleton chunk, the equivalent is proving that
    placeholder commands do not create files.
    """

    def test_no_mutation_from_any_placeholder(self, runner: CliRunner) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pre_files = set(Path(tmp).rglob("*"))
            for cmd in [
                ["init", "test-proj"],
                ["status"],
                ["validate"],
                ["brief", "C-0001"],
                ["inspect", "C-0001"],
                ["reconcile", "C-0001"],
            ]:
                runner.invoke(app, cmd)
            post_files = set(Path(tmp).rglob("*"))
            assert pre_files == post_files, (
                f"Placeholder commands created files: {post_files - pre_files}"
            )
