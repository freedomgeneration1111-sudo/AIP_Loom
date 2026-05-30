"""CLI entry point for AIP_Loom.

This module wires the Typer application, Rich console, and ``--json`` flag.
CLI handlers are deliberately thin: they parse arguments, delegate to a
service-layer function, and render the resulting :class:`CommandResult`.

Placeholder commands return ``NOT_IMPLEMENTED`` with a nonzero exit.  They
must never pretend to succeed.
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from pathlib import Path

from . import __version__
from .brief import generate_brief
from .brief_context import DEFAULT_TOKEN_BUDGET, select_context
from .errors import (
    CHUNK_NOT_FOUND,
    FILE_NOT_FOUND,
    FILE_READ_ERROR,
    NOT_IMPLEMENTED,
    PROJECT_MALFORMED,
    PROJECT_NOT_FOUND,
    RECONCILE_PRE_VALIDATION_FAILED,
    RECOVERY_FILE_EXISTS,
    STALE_LOCK_DETECTED,
    LoomError,
    LoomWarning,
)
from .init import InitError, init_project
from .output import render_result
from .project import ProjectError, ValidationResult, load_project, validate_project
from .reconcile_apply import apply_reconcile_plan
from .reconcile_plan import ReconcilePlan, build_reconcile_plan
from .results import CommandResult
from .status import HealthLevel, StatusReport, compute_status
from .update_parser import parse_model_output

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


def _run_init(name: str, project_type: str, project_dir: str | None) -> CommandResult:
    """Real init service — delegates to :func:`init_project`."""
    # Determine project root: use explicit dir or current working directory
    if project_dir:
        root = Path(project_dir).resolve()
    else:
        root = Path.cwd()

    try:
        result = init_project(root=root, name=name, project_type=project_type)
    except InitError as exc:
        return CommandResult.failure(
            command="init",
            code=exc.loom_error.code,
            message=exc.loom_error.message,
            errors=[exc.loom_error],
        )

    data = {
        "root": str(result.root),
        "git_initialized": result.git_initialized,
        "git_commit_created": result.git_commit_created,
    }
    return CommandResult.success(
        command="init",
        message=f"Project '{name}' initialised at {result.root}",
        data=data,
        warnings=list(result.warnings),
    )


def _run_status() -> CommandResult:
    """Real status service — delegates to :func:`compute_status`."""
    root = Path.cwd()
    report = compute_status(root)

    # Build data payload from the StatusReport
    data = report.to_dict()

    # Collect errors and warnings from the report
    all_errors: list[LoomError] = list(report.load_errors)
    all_warnings: list[LoomWarning] = list(report.load_warnings)
    if report.validation is not None:
        all_errors.extend(report.validation.errors)
        all_warnings.extend(report.validation.warnings)

    # Add recovery file warning if present
    if report.recovery_file_exists:
        all_warnings.append(
            LoomWarning(
                code=RECOVERY_FILE_EXISTS,
                message="RECOVERY.md exists — a previous reconcile may have failed.",
                detail={"file": str(Path(root) / "RECOVERY.md")},
            )
        )

    # Add stale lock warning if present
    if report.lock.is_stale:
        all_warnings.append(
            LoomWarning(
                code=STALE_LOCK_DETECTED,
                message=(
                    f"Stale lock detected: PID {report.lock.lock_info.pid} "
                    f"(command: {report.lock.lock_info.command!r}) is dead."
                ),
                detail={
                    "pid": report.lock.lock_info.pid,
                    "command": report.lock.lock_info.command,
                },
            )
        )

    # Determine success/failure based on health
    if report.health == HealthLevel.HEALTHY:
        message = f"Project '{report.project_name}' is healthy ({report.chunks.total} chunks)"
        return CommandResult.success(
            command="status",
            message=message,
            data=data,
            warnings=all_warnings,
        )
    elif report.health == HealthLevel.DEGRADED:
        message = (
            f"Project '{report.project_name}' is degraded "
            f"({report.warning_count} warnings, {report.chunks.total} chunks)"
        )
        return CommandResult.success(
            command="status",
            message=message,
            data=data,
            warnings=all_warnings,
        )
    else:
        # BLOCKED
        message = (
            f"Project '{report.project_name}' is blocked "
            f"({report.error_count} errors, {report.warning_count} warnings)"
        )
        return CommandResult.failure(
            command="status",
            code=PROJECT_MALFORMED,
            message=message,
            errors=all_errors if all_errors else None,
            data=data,
            warnings=all_warnings,
        )


def _run_validate(chunk: str | None) -> CommandResult:
    """Real validate service — delegates to load_project + validate_project."""
    root = Path.cwd()

    try:
        state = load_project(root)
    except ProjectError as exc:
        return CommandResult.failure(
            command="validate",
            code=exc.loom_error.code,
            message=exc.loom_error.message,
            errors=[exc.loom_error],
        )

    result = validate_project(state, chunk_scope=chunk)

    # Build data payload
    chunk_count = len(state.chunks)
    ledger_counts = {
        "decisions": len(state.decisions_ledger.entries) if state.decisions_ledger else 0,
        "threads": len(state.threads_ledger.entries) if state.threads_ledger else 0,
        "questions": len(state.questions_ledger.entries) if state.questions_ledger else 0,
    }

    data: dict[str, Any] = {
        "root": str(root),
        "chunks": chunk_count,
        "ledgers": ledger_counts,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
    }

    if chunk:
        data["chunk_scope"] = chunk

    all_warnings = list(result.warnings)
    all_errors = list(result.errors)

    if result.ok:
        return CommandResult.success(
            command="validate",
            message=f"Validation passed ({chunk_count} chunks, {len(all_warnings)} warnings)",
            data=data,
            warnings=all_warnings,
        )
    else:
        return CommandResult.failure(
            command="validate",
            code=result.errors[0].code if result.errors else PROJECT_MALFORMED,
            message=f"Validation failed with {len(all_errors)} error(s)",
            errors=all_errors,
            data=data,
            warnings=all_warnings,
        )


def _run_brief(
    chunk: str,
    task: str,
    dry_run: bool,
    force: bool,
) -> CommandResult:
    """Real brief service — delegates to :func:`generate_brief`.

    This function calls the shared context selection engine
    (:func:`select_context`) via :func:`generate_brief` — it never
    duplicates selection logic.
    """
    root = Path.cwd()
    return generate_brief(
        root=root,
        chunk_id=chunk,
        task=task,
        dry_run=dry_run,
        force=force,
        token_budget=DEFAULT_TOKEN_BUDGET,
    )


def _run_inspect(chunk: str) -> CommandResult:
    """Real inspect service — delegates to load_project + select_context.

    Inspect is a read-only command that shows what context ``brief``
    would select for a given chunk, without writing any brief file.
    It uses the **same** context selection logic as ``brief`` via
    :func:`select_context` from :mod:`aip_loom.brief_context`.
    """
    root = Path.cwd()

    # 1. Load project
    try:
        state = load_project(root)
    except ProjectError as exc:
        return CommandResult.failure(
            command="inspect",
            code=exc.loom_error.code,
            message=exc.loom_error.message,
            errors=[exc.loom_error],
        )

    # 2. Select context using the shared engine
    context = select_context(state, chunk_id=chunk)

    # 3. Build result
    all_warnings: list[LoomWarning] = list(context.warnings)
    all_errors: list[LoomError] = list(context.errors)

    # Also include load warnings/errors from project state
    all_warnings.extend(state.load_warnings)
    all_errors.extend(state.load_errors)

    data = context.to_dict()

    if context.target_chunk is None:
        # Chunk not found — this is a failure
        return CommandResult.failure(
            command="inspect",
            code=CHUNK_NOT_FOUND,
            message=f"Chunk {chunk!r} not found in project",
            errors=all_errors if all_errors else None,
            data=data,
            warnings=all_warnings,
        )

    # Success — context was selected (may have warnings)
    section_count = len(context.sections)
    dropped_count = len(context.dropped_sections)
    token_count = context.total_token_estimate.token_count

    message = (
        f"Context for {chunk}: {section_count} section(s), "
        f"~{token_count} tokens"
    )
    if dropped_count > 0:
        message += f" ({dropped_count} dropped due to budget)"

    return CommandResult.success(
        command="inspect",
        message=message,
        data=data,
        warnings=all_warnings,
    )


def _run_reconcile(
    chunk: str,
    output_path: str | None,
    preview: bool,
    allow_dirty_git: bool = False,
) -> CommandResult:
    """Reconcile service — delegates to build_reconcile_plan.

    For ``--preview`` mode, the plan is built and displayed but no
    canonical files are written.  This is the "dry run" path.

    For non-preview mode (Chunk 15), the plan will be applied to
    canonical state.  Currently returns NOT_IMPLEMENTED for apply.
    """
    root = Path.cwd()

    # 1. Load project state
    try:
        state = load_project(root)
    except ProjectError as exc:
        return CommandResult.failure(
            command="reconcile",
            code=exc.loom_error.code,
            message=exc.loom_error.message,
            errors=[exc.loom_error],
        )

    # 2. Read model output file
    if output_path is None:
        return CommandResult.failure(
            command="reconcile",
            code=FILE_NOT_FOUND,
            message=(
                "No model output file specified.  Use --output to "
                "provide the path to the file containing model output."
            ),
        )

    model_output_path = Path(output_path).resolve()
    if not model_output_path.is_file():
        return CommandResult.failure(
            command="reconcile",
            code=FILE_NOT_FOUND,
            message=f"Model output file not found: {model_output_path}",
            detail={"path": str(model_output_path)},
        )

    try:
        model_output_text = model_output_path.read_text(encoding="utf-8")
    except OSError as exc:
        return CommandResult.failure(
            command="reconcile",
            code=FILE_READ_ERROR,
            message=f"Cannot read model output file: {exc}",
            detail={"path": str(model_output_path), "error": str(exc)},
        )

    # 3. Parse model output using the strict update parser
    parse_result = parse_model_output(model_output_text)
    if not parse_result.ok:
        # Parser failed — surface the error
        return CommandResult.failure(
            command="reconcile",
            code=parse_result.code,
            message=f"Model output parse failed: {parse_result.message}",
            errors=parse_result.errors,
            data=parse_result.data,
            warnings=parse_result.warnings,
        )

    # 4. Extract the ParsedUpdateBlock object
    parsed_block_obj = parse_result.data.get("_parsed_block")
    if parsed_block_obj is None:
        return CommandResult.failure(
            command="reconcile",
            code=RECONCILE_PRE_VALIDATION_FAILED,
            message="Parser succeeded but no parsed block object in result data",
        )

    # 5. Validate that the CLI chunk argument matches the model output target
    model_target = parsed_block_obj.update_block.target_chunk
    if chunk != model_target:
        return CommandResult.failure(
            command="reconcile",
            code=RECONCILE_PRE_VALIDATION_FAILED,
            message=(
                f"Chunk mismatch: CLI argument specifies {chunk!r} but "
                f"model output targets {model_target!r}.  The chunk ID "
                f"on the command line must match the target_chunk in the "
                f"model output."
            ),
            detail={"cli_chunk": chunk, "model_target": model_target},
        )

    # 6. Build the reconcile plan
    plan = build_reconcile_plan(parsed_block_obj, state)

    # 7. Handle preview mode vs apply mode
    if preview:
        # Preview mode: return the plan without any canonical writes
        plan_data = plan.to_dict()
        plan_data["model_output_file"] = str(model_output_path)
        plan_data["preview"] = True

        all_warnings = list(plan.warnings)
        all_warnings.extend(state.load_warnings)

        if plan.plan_ok:
            message = (
                f"Reconcile plan for chunk {plan.target_chunk}: "
                f"{len(plan.ledger_changes)} ledger change(s), "
                f"{len(plan.file_changes)} file(s) affected"
            )
            if plan.conflicts:
                message += f", {len(plan.conflicts)} conflict(s)"
            return CommandResult.success(
                command="reconcile",
                message=message,
                data=plan_data,
                warnings=all_warnings,
            )
        else:
            # Plan has conflicts — still return data for inspection
            # but mark as failure
            conflict_codes = [c.code for c in plan.conflicts]
            return CommandResult.failure(
                command="reconcile",
                code=conflict_codes[0] if conflict_codes else RECONCILE_PRE_VALIDATION_FAILED,
                message=(
                    f"Reconcile plan has {len(plan.conflicts)} conflict(s) "
                    f"for chunk {plan.target_chunk}"
                ),
                errors=list(plan.conflicts),
                data=plan_data,
                warnings=all_warnings,
            )
    else:
        # Apply mode — delegate to the transactional apply engine
        return apply_reconcile_plan(
            plan=plan,
            model_output_text=model_output_text,
            root=root,
            allow_dirty_git=allow_dirty_git,
        )


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------


@app.command()
def init(
    name: str = typer.Argument(..., help="Project name."),
    type: str = typer.Option("novel", "--type", "-t", help="Project type (novel, technical, academic, general)."),
    directory: Optional[str] = typer.Option(None, "--dir", "-d", help="Project directory (defaults to current directory)."),
    json_output: bool = JsonFlag,
) -> None:
    """Initialise a new AIP_Loom project."""
    result = _run_init(name=name, project_type=type, project_dir=directory)
    render_result(result, use_json=json_output)
    raise typer.Exit(code=result.exit_code)


@app.command()
def status(
    json_output: bool = JsonFlag,
) -> None:
    """Show project status dashboard."""
    result = _run_status()
    render_result(result, use_json=json_output)
    raise typer.Exit(code=result.exit_code)


@app.command()
def validate(
    chunk: Optional[str] = typer.Option(None, "--chunk", "-c", help="Validate a specific chunk."),
    json_output: bool = JsonFlag,
) -> None:
    """Validate project structure and data integrity."""
    result = _run_validate(chunk=chunk)
    render_result(result, use_json=json_output)
    raise typer.Exit(code=result.exit_code)


@app.command()
def brief(
    chunk: str = typer.Argument(..., help="Target chunk ID."),
    task: str = typer.Option("", "--task", "-t", help="Task description to include in the brief."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview brief without writing."),
    force: bool = typer.Option(False, "--force", help="Force brief generation for dirty/stale chunks."),
    json_output: bool = JsonFlag,
) -> None:
    """Generate a deterministic session brief for a chunk."""
    result = _run_brief(chunk=chunk, task=task, dry_run=dry_run, force=force)
    render_result(result, use_json=json_output)
    raise typer.Exit(code=result.exit_code)


@app.command()
def inspect(
    chunk: str = typer.Argument(..., help="Chunk ID to inspect."),
    json_output: bool = JsonFlag,
) -> None:
    """Inspect chunk context without writing a brief."""
    result = _run_inspect(chunk=chunk)
    render_result(result, use_json=json_output)
    raise typer.Exit(code=result.exit_code)


@app.command()
def reconcile(
    chunk: str = typer.Argument(..., help="Target chunk ID."),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Path to model output file."),
    preview: bool = typer.Option(False, "--preview", help="Preview changes without applying."),
    allow_dirty_git: bool = typer.Option(False, "--allow-dirty-git", help="Allow reconcile when Git working tree is dirty."),
    json_output: bool = JsonFlag,
) -> None:
    """Reconcile model output with project state."""
    result = _run_reconcile(chunk=chunk, output_path=output, preview=preview, allow_dirty_git=allow_dirty_git)
    render_result(result, use_json=json_output)
    raise typer.Exit(code=result.exit_code)
