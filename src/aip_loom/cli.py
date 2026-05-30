"""CLI entry point for AIP_Loom.

This module wires the Typer application, Rich console, and ``--json`` flag.
CLI handlers are deliberately thin: they parse arguments, delegate to a
service-layer function, and render the resulting :class:`CommandResult`.

Placeholder commands return ``NOT_IMPLEMENTED`` with a nonzero exit.  They
must never pretend to succeed.
"""

from __future__ import annotations

from typing import Optional

import typer

from . import __version__
from .errors import NOT_IMPLEMENTED
from .output import render_result
from .results import CommandResult

# ---------------------------------------------------------------------------
# Typer application
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="aip-loom",
    help="AIP_Loom — local-first CLI workbench for longform AI document continuity.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        typer.echo(f"aip-loom {__version__}")
        raise typer.Exit(code=0)


@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """AIP_Loom — local-first CLI workbench for longform AI document continuity."""
    # The callback exists only to host the --version flag.
    # No other global logic belongs here.


# ---------------------------------------------------------------------------
# Shared JSON-flag type for all subcommands
# ---------------------------------------------------------------------------

JsonFlag = typer.Option(False, "--json", help="Output result as JSON.")


# ---------------------------------------------------------------------------
# Placeholder service stubs
# ---------------------------------------------------------------------------
# Each stub returns a CommandResult with NOT_IMPLEMENTED.  When a real
# service implementation is added (in a later chunk), the stub is replaced
# and the CLI handler stays unchanged.


def _stub_init(name: str, project_type: str) -> CommandResult:
    """Placeholder: will be implemented in Chunk 08."""
    return CommandResult.failure(
        command="init",
        code=NOT_IMPLEMENTED,
        message=f"The 'init' command is not yet implemented. (name={name!r}, type={project_type!r})",
    )


def _stub_status() -> CommandResult:
    """Placeholder: will be implemented in Chunk 10."""
    return CommandResult.failure(
        command="status",
        code=NOT_IMPLEMENTED,
        message="The 'status' command is not yet implemented.",
    )


def _stub_validate(chunk: str | None) -> CommandResult:
    """Placeholder: will be implemented in Chunk 09."""
    return CommandResult.failure(
        command="validate",
        code=NOT_IMPLEMENTED,
        message=f"The 'validate' command is not yet implemented. (chunk={chunk!r})",
    )


def _stub_brief(
    chunk: str,
    dry_run: bool,
    force: bool,
) -> CommandResult:
    """Placeholder: will be implemented in Chunk 12."""
    return CommandResult.failure(
        command="brief",
        code=NOT_IMPLEMENTED,
        message=f"The 'brief' command is not yet implemented. (chunk={chunk!r})",
    )


def _stub_inspect(chunk: str) -> CommandResult:
    """Placeholder: will be implemented in Chunk 11."""
    return CommandResult.failure(
        command="inspect",
        code=NOT_IMPLEMENTED,
        message=f"The 'inspect' command is not yet implemented. (chunk={chunk!r})",
    )


def _stub_reconcile(
    chunk: str,
    output_path: str | None,
    preview: bool,
) -> CommandResult:
    """Placeholder: will be implemented in Chunks 14/15."""
    return CommandResult.failure(
        command="reconcile",
        code=NOT_IMPLEMENTED,
        message=f"The 'reconcile' command is not yet implemented. (chunk={chunk!r})",
    )


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------


@app.command()
def init(
    name: str = typer.Argument(..., help="Project name."),
    type: str = typer.Option("novel", "--type", "-t", help="Project type (novel, technical, etc.)."),
    json_output: bool = JsonFlag,
) -> None:
    """Initialise a new AIP_Loom project."""
    result = _stub_init(name=name, project_type=type)
    render_result(result, use_json=json_output)
    raise typer.Exit(code=result.exit_code)


@app.command()
def status(
    json_output: bool = JsonFlag,
) -> None:
    """Show project status dashboard."""
    result = _stub_status()
    render_result(result, use_json=json_output)
    raise typer.Exit(code=result.exit_code)


@app.command()
def validate(
    chunk: Optional[str] = typer.Option(None, "--chunk", "-c", help="Validate a specific chunk."),
    json_output: bool = JsonFlag,
) -> None:
    """Validate project structure and data integrity."""
    result = _stub_validate(chunk=chunk)
    render_result(result, use_json=json_output)
    raise typer.Exit(code=result.exit_code)


@app.command()
def brief(
    chunk: str = typer.Argument(..., help="Target chunk ID."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview brief without writing."),
    force: bool = typer.Option(False, "--force", help="Force brief generation for dirty/stale chunks."),
    json_output: bool = JsonFlag,
) -> None:
    """Generate a deterministic session brief for a chunk."""
    result = _stub_brief(chunk=chunk, dry_run=dry_run, force=force)
    render_result(result, use_json=json_output)
    raise typer.Exit(code=result.exit_code)


@app.command()
def inspect(
    chunk: str = typer.Argument(..., help="Chunk ID to inspect."),
    json_output: bool = JsonFlag,
) -> None:
    """Inspect chunk context without writing a brief."""
    result = _stub_inspect(chunk=chunk)
    render_result(result, use_json=json_output)
    raise typer.Exit(code=result.exit_code)


@app.command()
def reconcile(
    chunk: str = typer.Argument(..., help="Target chunk ID."),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Path to model output file."),
    preview: bool = typer.Option(False, "--preview", help="Preview changes without applying."),
    json_output: bool = JsonFlag,
) -> None:
    """Reconcile model output with project state."""
    result = _stub_reconcile(chunk=chunk, output_path=output, preview=preview)
    render_result(result, use_json=json_output)
    raise typer.Exit(code=result.exit_code)
