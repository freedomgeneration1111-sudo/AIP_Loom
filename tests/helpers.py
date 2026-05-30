"""Shared test helpers for AIP_Loom acceptance and chaos tests.

This module provides reusable helper functions that are used by multiple
test modules.  It is intentionally NOT named conftest.py because
conftest is a special pytest file that cannot be imported as a regular
module.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from aip_loom.checksum import compute_prose_checksum
from aip_loom.schemas import SUPPORTED_SCHEMA_VERSION
from aip_loom.yaml_io import dump_yaml_string


# ---------------------------------------------------------------------------
# Schema version constant
# ---------------------------------------------------------------------------

_V = SUPPORTED_SCHEMA_VERSION
_TS = "2026-05-30T12:00:00Z"


# ---------------------------------------------------------------------------
# JSON output parsing
# ---------------------------------------------------------------------------


def parse_json_output(output: str) -> dict[str, Any]:
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
# Model output builder
# ---------------------------------------------------------------------------


def build_model_output(
    target_chunk: str = "C-0002",
    revised_prose: str = "This is the revised methodology. The approach has been updated to include new analytical techniques.",
    new_decisions: list[dict[str, str]] | None = None,
    new_threads: list[dict[str, str]] | None = None,
    close_threads: list[str] | None = None,
    requires_human_review: bool = True,
) -> str:
    """Build a valid model output string with a loom-update fence.

    This helper constructs well-formed model output for testing the
    reconcile pipeline end-to-end.
    """
    lines = [
        "```loom-update",
        f'schema_version: "{_V}"',
        "fence_type: loom-update",
        "mode: full_replacement",
        f"target_chunk: {target_chunk}",
        f'revised_prose: "{revised_prose}"',
        f'change_summary: "Updated {target_chunk}."',
        f"requires_human_review: {str(requires_human_review).lower()}",
    ]

    if new_decisions:
        lines.append("new_decisions:")
        for d in new_decisions:
            lines.append(f"  - provisional_id: {d['provisional_id']}")
            lines.append(f'    summary: "{d["summary"]}"')

    if new_threads:
        lines.append("new_threads:")
        for t in new_threads:
            lines.append(f"  - provisional_id: {t['provisional_id']}")
            lines.append(f'    summary: "{t["summary"]}"')

    if close_threads:
        lines.append("close_threads:")
        for tid in close_threads:
            lines.append(f"  - {tid}")

    lines.append("---")
    lines.append("# Revised Chunk")
    lines.append("")
    lines.append(revised_prose)
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project setup helpers
# ---------------------------------------------------------------------------


def write_chunk(root: Path, chunk_id: str, title: str, prose: str) -> None:
    """Write a chunk file with valid frontmatter and prose body."""
    chunks_dir = root / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    chunk_path = chunks_dir / f"{chunk_id}.md"

    checksum = compute_prose_checksum(prose)
    word_count = len(prose.split())

    frontmatter = {
        "schema_version": _V,
        "id": chunk_id,
        "title": title,
        "status": "draft",
        "word_count": word_count,
        "prose_checksum": checksum,
        "distillate_anchor": "",
        "created_at": _TS,
        "updated_at": _TS,
    }

    yaml_str = dump_yaml_string(frontmatter).rstrip("\n")
    chunk_content = f"---\n{yaml_str}\n---\n{prose}"
    chunk_path.write_text(chunk_content, encoding="utf-8")


def commit_all(root: Path, message: str = "test commit") -> None:
    """Git add -A and commit everything in the project root."""
    (root / ".gitignore").write_text(".aip-loom/\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", "-A"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", message, "--allow-empty"],
        check=False,
        capture_output=True,
    )


def write_model_output(root: Path, filename: str, content: str) -> Path:
    """Write model output text to a file under the project's outputs dir."""
    output_path = root / "outputs" / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path
