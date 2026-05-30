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

try:
    from .status import HealthLevel, StatusReport
except ImportError:
    HealthLevel = None  # type: ignore[assignment,misc]
    StatusReport = None  # type: ignore[assignment,misc]

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

    # -- status command gets a dedicated dashboard renderer ------------------
    if result.command == "status" and result.data and "health" in result.data:
        _render_status_dashboard(result)
        return

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


# ---------------------------------------------------------------------------
# Status dashboard renderer
# ---------------------------------------------------------------------------


def _render_status_dashboard(result: CommandResult) -> None:
    """Render a status command result as a Rich dashboard.

    This produces a more readable, dashboard-style layout instead of the
    generic key-value table.  It shows:
    - Health badge (green/yellow/red)
    - Project info
    - Chunk progress
    - Ledger counts and pending reviews
    - Git state
    - Lock state
    - Recovery/recommendation items
    - Warnings and errors (if any)
    """
    data = result.data

    # -- Health badge --------------------------------------------------------
    health = data.get("health", "unknown")
    if health == "healthy":
        health_style = "bold green"
        border_style = "green"
    elif health == "degraded":
        health_style = "bold yellow"
        border_style = "yellow"
    else:
        health_style = "bold red"
        border_style = "red"

    console.print()
    console.print(
        Panel(
            f"[{health_style}]{health.upper()}[/]  {result.message}",
            title=f"aip-loom status",
            border_style=border_style,
        )
    )

    # -- Project info --------------------------------------------------------
    info_table = Table(title="Project", show_header=False)
    info_table.add_column("Field", style="bold")
    info_table.add_column("Value")
    info_table.add_row("Name", str(data.get("project_name", "<unknown>")))
    info_table.add_row("Type", str(data.get("project_type", "<unknown>")))
    info_table.add_row("Root", str(data.get("root", "")))
    console.print(info_table)

    # -- Chunk progress ------------------------------------------------------
    chunks = data.get("chunks", {})
    if chunks:
        chunk_table = Table(title="Chunks", show_header=False)
        chunk_table.add_column("Field", style="bold")
        chunk_table.add_column("Value")
        chunk_table.add_row("Total", str(chunks.get("total", 0)))
        chunk_table.add_row("Draft", str(chunks.get("draft", 0)))
        chunk_table.add_row("Revised", str(chunks.get("revised", 0)))
        chunk_table.add_row("Final", str(chunks.get("final", 0)))
        dirty = chunks.get("dirty_checksums", 0)
        if dirty > 0:
            chunk_table.add_row("Dirty checksums", f"[yellow]{dirty}[/]")
        console.print(chunk_table)

    # -- Ledgers -------------------------------------------------------------
    ledgers = data.get("ledgers", {})
    if ledgers:
        ledger_table = Table(title="Ledgers", show_header=True, header_style="bold")
        ledger_table.add_column("Type")
        ledger_table.add_column("Total")
        ledger_table.add_column("Pending Review")
        ledger_table.add_row(
            "Decisions",
            str(ledgers.get("decisions_total", 0)),
            str(ledgers.get("decisions_pending", 0)),
        )
        ledger_table.add_row(
            "Threads",
            str(ledgers.get("threads_total", 0)),
            str(ledgers.get("threads_pending", 0)),
        )
        ledger_table.add_row(
            "Questions",
            str(ledgers.get("questions_total", 0)),
            str(ledgers.get("questions_pending", 0)),
        )
        # Extra row for thread state
        open_threads = ledgers.get("threads_open", 0)
        blocked_threads = ledgers.get("threads_blocked", 0)
        if open_threads > 0 or blocked_threads > 0:
            state_parts = []
            if open_threads > 0:
                state_parts.append(f"open: {open_threads}")
            if blocked_threads > 0:
                state_parts.append(f"blocked: {blocked_threads}")
            ledger_table.add_row(
                "Thread states",
                ", ".join(state_parts),
                "",
            )
        console.print(ledger_table)

    # -- Git -----------------------------------------------------------------
    git = data.get("git", {})
    if git:
        git_table = Table(title="Git", show_header=False)
        git_table.add_column("Field", style="bold")
        git_table.add_column("Value")
        git_table.add_row("Repository", "yes" if git.get("is_repo") else "no")
        if git.get("is_repo"):
            git_table.add_row("Branch", str(git.get("branch", "")))
            git_table.add_row(
                "Working tree",
                "[green]clean[/]" if git.get("clean") else "[yellow]dirty[/]",
            )
            if not git.get("clean"):
                git_table.add_row(
                    "Staged / Unstaged / Untracked",
                    f"{git.get('staged_count', 0)} / "
                    f"{git.get('unstaged_count', 0)} / "
                    f"{git.get('untracked_count', 0)}",
                )
        console.print(git_table)

    # -- Lock ----------------------------------------------------------------
    lock = data.get("lock", {})
    if lock:
        lock_table = Table(title="Lock", show_header=False)
        lock_table.add_column("Field", style="bold")
        lock_table.add_column("Value")
        if lock.get("locked"):
            lock_table.add_row("Status", "[yellow]locked[/]")
            if lock.get("is_stale"):
                lock_table.add_row("Stale", "[red]yes — PID is dead[/]")
            if lock.get("pid"):
                lock_table.add_row("PID", str(lock.get("pid", "")))
            if lock.get("command"):
                lock_table.add_row("Command", str(lock.get("command", "")))
        else:
            lock_table.add_row("Status", "[green]unlocked[/]")
        console.print(lock_table)

    # -- Recovery indicator ---------------------------------------------------
    if data.get("recovery_file_exists"):
        console.print("[bold red]RECOVERY.md exists[/] — a previous reconcile may have failed.")

    # -- Next actions --------------------------------------------------------
    actions = data.get("next_actions", [])
    if actions:
        action_table = Table(title="Next Actions", show_header=False)
        action_table.add_column("Priority", style="bold")
        action_table.add_column("Action")
        for i, action in enumerate(actions, 1):
            action_table.add_row(str(i), action)
        console.print(action_table)

    # -- Warnings ------------------------------------------------------------
    if result.warnings:
        table = Table(title="Warnings", show_header=True, header_style="bold yellow")
        table.add_column("Code", style="yellow")
        table.add_column("Message")
        for w in result.warnings:
            table.add_row(w.code, w.message)
        console.print(table)

    # -- Errors --------------------------------------------------------------
    if result.errors:
        table = Table(title="Errors", show_header=True, header_style="bold red")
        table.add_column("Code", style="red")
        table.add_column("Message")
        for e in result.errors:
            table.add_row(e.code, e.message)
        console.print(table)
