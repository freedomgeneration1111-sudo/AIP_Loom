"""Result renderer for AIP_Loom CLI output.

This module owns the rendering of :class:`CommandResult` to the terminal.
It supports two modes:

* **Rich mode** (default) — human-friendly coloured output via Rich.
* **JSON mode** (``--json``) — machine-readable JSON to stdout.

No other module may print command results directly.  All output rendering
flows through :func:`render_result`.
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .results import CommandResult

# ---------------------------------------------------------------------------
# Shared console instance
# ---------------------------------------------------------------------------

# The console is created without stderr redirection so that ``--json`` can
# write to stdout while Rich diagnostic output goes to stderr when needed.
console = Console(stderr=False)


def render_result(result: CommandResult, *, use_json: bool = False) -> None:
    """Render a :class:`CommandResult` to the terminal.

    Parameters
    ----------
    result:
        The command result to render.
    use_json:
        When ``True``, emit the envelope as JSON to stdout.  When
        ``False``, render a human-friendly Rich panel.
    """
    if use_json:
        sys.stdout.write(result.to_json())
        sys.stdout.write("\n")
        return

    _render_rich(result)


def _render_rich(result: CommandResult) -> None:
    """Render a result as a Rich panel with optional warning/error tables."""

    # -- summary line -------------------------------------------------------
    if result.ok:
        console.print(
            Panel(
                f"[bold green]OK[/]  {result.message}",
                title=f"aip-loom {result.command}",
                border_style="green",
            )
        )
    else:
        console.print(
            Panel(
                f"[bold red]FAIL[/]  {result.message}",
                title=f"aip-loom {result.command}",
                subtitle=f"code: {result.code}",
                border_style="red",
            )
        )

    # -- data ---------------------------------------------------------------
    if result.data:
        table = Table(title="Data", show_header=True, header_style="bold")
        table.add_column("Key")
        table.add_column("Value")
        for key, value in result.data.items():
            table.add_row(str(key), str(value))
        console.print(table)

    # -- warnings -----------------------------------------------------------
    if result.warnings:
        table = Table(title="Warnings", show_header=True, header_style="bold yellow")
        table.add_column("Code", style="yellow")
        table.add_column("Message")
        for w in result.warnings:
            table.add_row(w.code, w.message)
        console.print(table)

    # -- errors -------------------------------------------------------------
    if result.errors:
        table = Table(title="Errors", show_header=True, header_style="bold red")
        table.add_column("Code", style="red")
        table.add_column("Message")
        table.add_column("Detail")
        for e in result.errors:
            detail_str = ", ".join(f"{k}={v}" for k, v in e.detail.items()) if e.detail else ""
            table.add_row(e.code, e.message, detail_str)
        console.print(table)
