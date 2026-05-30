"""Shared test fixtures for AIP_Loom."""

from __future__ import annotations

from typer.testing import CliRunner

import pytest

from aip_loom.cli import app


@pytest.fixture()
def runner() -> CliRunner:
    """Return a CliRunner for invoking the AIP_Loom Typer app."""
    return CliRunner()


@pytest.fixture()
def invoke(runner: CliRunner) -> ...:
    """Return a helper that invokes the app with the given args."""
    def _invoke(args: list[str]) -> ...:
        return runner.invoke(app, args)
    return _invoke
