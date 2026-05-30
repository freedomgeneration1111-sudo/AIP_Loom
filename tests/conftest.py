"""Shared test fixtures for AIP_Loom.

This module provides reusable fixtures for all test modules.  Chunk 17
adds fixtures specifically designed for acceptance and chaos testing:
projects with multiple chunks and model output builders.

Helper functions (parse_json_output, build_model_output, etc.) live in
:mod:`tests.helpers` so they can be imported from any test module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aip_loom.cli import app
from aip_loom.git import configure_local_git
from aip_loom.init import init_project
from aip_loom.yaml_io import dump_yaml, load_yaml

from .helpers import _TS, build_model_output, commit_all, parse_json_output, write_chunk


# ---------------------------------------------------------------------------
# Basic fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner() -> CliRunner:
    """Return a CliRunner for invoking the AIP_Loom Typer app."""
    return CliRunner()


@pytest.fixture()
def invoke(runner: CliRunner) -> Any:
    """Return a helper that invokes the app with the given args."""
    def _invoke(args: list[str]) -> Any:
        return runner.invoke(app, args)
    return _invoke


# ---------------------------------------------------------------------------
# Project fixture: minimal project with 3 chunks
# ---------------------------------------------------------------------------


@pytest.fixture()
def project_with_chunks(tmp_path: Path) -> Path:
    """Create a project with 3 chunks (C-0001, C-0002, C-0003).

    The project is fully initialised with Git, has valid chunk order
    in the manifest, and a clean working tree.  This is the canonical
    fixture for acceptance and chaos testing.
    """
    root = tmp_path / "acceptance-project"
    root.mkdir()

    init_project(root=root, name="brick-kiln", project_type="academic")
    configure_local_git(root)

    # Create three chunks with distinct prose content
    write_chunk(root, "C-0001", "Introduction", "This is the introduction to the research paper. It covers the background and motivation for the study.")
    write_chunk(root, "C-0002", "Methodology", "This section describes the methodology used in the study. It includes the experimental design and data collection procedures.")
    write_chunk(root, "C-0003", "Results", "The results of the study are presented here. Key findings and statistical analyses are discussed in detail.")

    # Update manifest chunk order
    manifest_path = root / "aip_loom.yaml"
    manifest = load_yaml(manifest_path)
    manifest["chunks"]["order"] = ["C-0001", "C-0002", "C-0003"]
    dump_yaml(manifest, manifest_path)

    commit_all(root, "test: setup project with 3 chunks")

    return root


@pytest.fixture()
def project_with_thread(tmp_path: Path) -> Path:
    """Create a project with an open thread in the threads ledger.

    The project has one chunk (C-0001) and one open thread (T-0001)
    referencing that chunk.  Used for testing close_threads in reconcile.
    """
    root = tmp_path / "thread-project"
    root.mkdir()

    init_project(root=root, name="thread-test", project_type="novel")
    configure_local_git(root)

    write_chunk(root, "C-0001", "Chapter One", "The first chapter of the novel. A mysterious stranger arrives in town.")

    # Add an open thread to the threads ledger
    threads_path = root / "ledgers" / "threads.yaml"
    threads_data = load_yaml(threads_path)
    threads_data["entries"] = [
        {
            "id": "T-0001",
            "review_state": "approved",
            "created_at": _TS,
            "summary": "Who is the mysterious stranger?",
            "state": "open",
            "scope": "global",
            "chunk_id": "C-0001",
            "blocked_by": [],
        }
    ]
    dump_yaml(threads_data, threads_path)

    # Update manifest
    manifest_path = root / "aip_loom.yaml"
    manifest = load_yaml(manifest_path)
    manifest["chunks"]["order"] = ["C-0001"]
    dump_yaml(manifest, manifest_path)

    commit_all(root, "test: setup project with open thread")

    return root
