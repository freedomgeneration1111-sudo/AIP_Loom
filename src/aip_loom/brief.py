"""Brief generation service for AIP_Loom.

This module is the **single authority** for assembling and writing session
briefs.  It calls :func:`select_context` from :mod:`aip_loom.brief_context`
as the **only** source of context selection logic — no other function in
this module may independently decide what goes into the brief.

Design principles (BuildSpec §3A and Chunk 12 description):

- **Zero duplication**: ``brief`` calls the **same** ``select_context()``
  that ``inspect`` uses.  The brief module only adds rendering and
  file-writing logic on top of the shared selection engine.
- **Protected sections**: Certain section types must NEVER be dropped
  from a brief, even if the token budget is exceeded.  If protected
  sections would be dropped, the brief fails with
  ``BRIEF_BUDGET_OVERFLOW`` rather than producing an incomplete brief.
  Protected priorities: 0 (chunk frontmatter), 1 (chunk prose),
  2 (distillate anchor), 3 (scoped decisions), 4 (scoped threads),
  6 (global decisions).
- **Dry-run safety**: When ``--dry-run`` is set, **nothing** is written
  to disk.  The brief content is assembled and returned but the file
  write step is skipped entirely.
- **Force with strong warning**: ``--force`` allows brief generation
  on dirty/stale/orphan chunks, but always emits a ``BRIEF_FORCE_USED``
  warning that is impossible to miss.
- **Deterministic**: Given the same project state, chunk ID, and task,
  the brief content is always identical.
- **Human-readable output**: The brief is a well-structured Markdown
  file with YAML frontmatter containing metadata.
- **Token consistency**: Token estimates in the brief match exactly
  what ``inspect`` would show for the same chunk and budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .brief_context import (
    DEFAULT_TOKEN_BUDGET,
    ContextSection,
    SelectedContext,
    select_context,
)
from .checksum import compute_prose_checksum
from .errors import (
    BRIEF_BUDGET_OVERFLOW,
    BRIEF_DIRTY_CHUNK,
    BRIEF_FORCE_USED,
    BRIEF_ORPHAN_CHUNK,
    BRIEF_STALE_CHUNK,
    CHUNK_NOT_FOUND,
    LoomError,
    LoomWarning,
)
from .fs import ensure_directory, safe_write_text
from .layout import ProjectLayout
from .project import ProjectError, ProjectState, load_project, validate_project
from .results import CommandResult
from .schemas import SUPPORTED_SCHEMA_VERSION
from .tokens import estimate_text_tokens

__all__ = [
    "BriefResult",
    "assemble_brief_content",
    "generate_brief",
    "PROTECTED_PRIORITIES",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Section priorities that must NEVER be dropped from a brief.
#: If any section with these priorities appears in ``dropped_sections``,
#: the brief generation fails with ``BRIEF_BUDGET_OVERFLOW``.
#:
#: - Priority 0: chunk frontmatter (mandatory — part of target chunk)
#: - Priority 1: chunk prose (mandatory — part of target chunk)
#: - Priority 2: distillate anchor (structural context)
#: - Priority 3: scoped decisions (chunk-relevant decisions)
#: - Priority 4: scoped threads (chunk-relevant continuity items)
#: - Priority 6: global decisions (project-wide decisions)
PROTECTED_PRIORITIES: frozenset[int] = frozenset({0, 1, 2, 3, 4, 6})


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BriefResult:
    """The result of generating a brief.

    Attributes
    ----------
    chunk_id:
        The target chunk ID.
    brief_path:
        The path where the brief was written, or ``None`` if dry-run.
    content:
        The assembled brief Markdown content.
    token_estimate:
        Token estimate for the brief content.
    token_budget:
        The token budget used.
    section_count:
        Number of sections included in the brief.
    dropped_count:
        Number of sections dropped due to budget.
    dry_run:
        Whether this was a dry-run (no file written).
    """

    chunk_id: str
    brief_path: Path | None
    content: str
    token_estimate: int
    token_budget: int
    section_count: int
    dropped_count: int
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        result: dict[str, Any] = {
            "chunk_id": self.chunk_id,
            "brief_path": str(self.brief_path) if self.brief_path else None,
            "token_estimate": self.token_estimate,
            "token_budget": self.token_budget,
            "section_count": self.section_count,
            "dropped_count": self.dropped_count,
            "dry_run": self.dry_run,
            "content_length": len(self.content),
        }
        return result


# ---------------------------------------------------------------------------
# Brief content assembler
# ---------------------------------------------------------------------------


def assemble_brief_content(
    context: SelectedContext,
    task: str = "",
) -> str:
    """Assemble the brief Markdown content from a SelectedContext.

    This is the **rendering** step — it takes the selected context and
    formats it into a human-readable Markdown brief.  It does NOT make
    any selection decisions; those are handled entirely by
    :func:`select_context`.

    Parameters
    ----------
    context:
        The selected context from :func:`select_context`.
    task:
        Optional task description to include in the brief.

    Returns
    -------
    str
        The assembled brief Markdown content with YAML frontmatter.
    """
    now = datetime.now(timezone.utc).isoformat()
    chunk_id = context.target_chunk_id
    token_count = context.total_token_estimate.token_count

    # Build frontmatter
    fm_lines = [
        f"chunk_id: {chunk_id!r}",
        f"generated_at: {now!r}",
        f"token_estimate: {token_count}",
        f"token_budget: {context.token_budget}",
        f"schema_version: {SUPPORTED_SCHEMA_VERSION!r}",
        f"section_count: {len(context.sections)}",
        f"dropped_count: {len(context.dropped_sections)}",
    ]
    if task:
        # Escape any quotes in the task string
        escaped_task = task.replace("'", "''")
        fm_lines.append(f"task: {escaped_task!r}")
    frontmatter = "---\n" + "\n".join(fm_lines) + "\n---"

    # Build body
    parts: list[str] = [frontmatter, ""]
    parts.append(f"# Session Brief: {chunk_id}")
    parts.append("")

    # Task description (if provided)
    if task:
        parts.append("## Task")
        parts.append("")
        parts.append(task)
        parts.append("")

    # Group sections by type for structured output
    sections_by_type: dict[str, list[ContextSection]] = {}
    for section in context.sections:
        sections_by_type.setdefault(section.section_type, []).append(section)

    # Target chunk
    parts.append("## Target Chunk")
    parts.append("")
    for section in context.sections:
        if section.section_type == "chunk_frontmatter":
            parts.append(section.content)
            parts.append("")
            break

    for section in context.sections:
        if section.section_type == "chunk_prose":
            parts.append("### Prose")
            parts.append("")
            parts.append(section.content)
            parts.append("")
            break

    # Distillate anchor
    if "distillate_node" in sections_by_type:
        parts.append("## Distillate Anchor")
        parts.append("")
        for section in sections_by_type["distillate_node"]:
            parts.append(section.content)
            parts.append("")

    # Scoped decisions
    if "scoped_decision" in sections_by_type:
        parts.append("## Scoped Decisions")
        parts.append("")
        for section in sections_by_type["scoped_decision"]:
            parts.append(section.content)
            parts.append("")

    # Scoped threads
    if "scoped_thread" in sections_by_type:
        parts.append("## Scoped Threads")
        parts.append("")
        for section in sections_by_type["scoped_thread"]:
            parts.append(section.content)
            parts.append("")

    # Adjacent summaries
    if "adjacent_summary" in sections_by_type:
        parts.append("## Adjacent Summaries")
        parts.append("")
        for section in sections_by_type["adjacent_summary"]:
            parts.append(section.content)
            parts.append("")

    # Global decisions
    if "global_decision" in sections_by_type:
        parts.append("## Global Decisions")
        parts.append("")
        for section in sections_by_type["global_decision"]:
            parts.append(section.content)
            parts.append("")

    # Global threads
    if "global_thread" in sections_by_type:
        parts.append("## Global Threads")
        parts.append("")
        for section in sections_by_type["global_thread"]:
            parts.append(section.content)
            parts.append("")

    # Unresolved questions
    if "unresolved_question" in sections_by_type:
        parts.append("## Unresolved Questions")
        parts.append("")
        for section in sections_by_type["unresolved_question"]:
            parts.append(section.content)
            parts.append("")

    # Footer with dropped sections info
    if context.dropped_sections:
        parts.append("---")
        parts.append("")
        dropped_types = [s.section_type for s in context.dropped_sections]
        parts.append(
            f"*Dropped {len(context.dropped_sections)} section(s) due to "
            f"budget: {', '.join(sorted(set(dropped_types)))}*"
        )
        parts.append("")

    # Token summary footer
    parts.append("---")
    parts.append("")
    budget_status = "within budget" if not context.budget_exceeded else "EXCEEDED"
    parts.append(
        f"*Token estimate: ~{token_count} / {context.token_budget} "
        f"({budget_status})*"
    )
    parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_chunk_dirty(
    state: ProjectState,
    chunk_id: str,
) -> LoomError | None:
    """Check if a chunk has a dirty checksum (prose edited without update).

    Returns a ``BRIEF_DIRTY_CHUNK`` error if dirty, or ``None`` if clean.
    """
    chunk_data = state.chunks.get(chunk_id)
    if chunk_data is None:
        return None

    actual = compute_prose_checksum(chunk_data.prose_body)
    recorded = chunk_data.frontmatter.prose_checksum

    if actual != recorded:
        return LoomError(
            code=BRIEF_DIRTY_CHUNK,
            message=(
                f"Chunk {chunk_id} has a dirty checksum — the prose has been "
                f"edited without updating the frontmatter.  Run 'aip-loom "
                f"reconcile' first, or use --force to override."
            ),
            detail={
                "chunk_id": chunk_id,
                "recorded_checksum": recorded[:12] + "...",
                "actual_checksum": actual[:12] + "...",
            },
        )
    return None


def _check_chunk_orphan(
    state: ProjectState,
    chunk_id: str,
) -> LoomError | None:
    """Check if a chunk is not in the manifest's chunk order.

    Returns a ``BRIEF_STALE_CHUNK`` error if orphan, or ``None`` if listed.
    """
    if state.manifest is None:
        return None

    ordered_ids = set(state.manifest.chunks.order)
    if ordered_ids and chunk_id not in ordered_ids:
        return LoomError(
            code=BRIEF_STALE_CHUNK,
            message=(
                f"Chunk {chunk_id} is not in the manifest's chunk order — "
                f"it may be an untracked or orphan chunk.  Use --force to "
                f"override."
            ),
            detail={
                "chunk_id": chunk_id,
                "manifest_order": list(ordered_ids)[:10],
            },
        )
    return None


def _check_protected_sections_dropped(
    context: SelectedContext,
) -> LoomError | None:
    """Check if any protected section was dropped due to budget.

    Also checks if the budget is exceeded by mandatory/protected sections
    even if none were technically "dropped" (because they are mandatory
    in ``select_context`` and thus always kept, but the budget is still
    exceeded).

    Returns a ``BRIEF_BUDGET_OVERFLOW`` error if protected sections
    were dropped or the budget is exceeded, or ``None`` if all
    protected sections are intact and within budget.
    """
    dropped_protected = [
        s for s in context.dropped_sections
        if s.priority in PROTECTED_PRIORITIES
    ]

    if dropped_protected:
        dropped_types = sorted(set(s.section_type for s in dropped_protected))
        dropped_ids = [s.source_id for s in dropped_protected[:10]]

        return LoomError(
            code=BRIEF_BUDGET_OVERFLOW,
            message=(
                f"Cannot generate brief: {len(dropped_protected)} protected "
                f"section(s) would be dropped due to token budget.  Protected "
                f"sections include the full target chunk, distillate anchor, "
                f"scoped decisions/threads, and global decisions.  Increase "
                f"the token budget or reduce context."
            ),
            detail={
                "chunk_id": context.target_chunk_id,
                "budget": context.token_budget,
                "protected_tokens": sum(
                    s.token_estimate.token_count for s in context.sections
                    if s.priority in PROTECTED_PRIORITIES
                ),
                "dropped_protected_types": dropped_types,
                "dropped_protected_ids": dropped_ids,
            },
        )

    # Also check if mandatory/protected sections exceed the budget.
    # In this case, select_context keeps them (because they are mandatory)
    # but the budget is still exceeded — the brief would be misleading.
    if context.budget_exceeded:
        protected_tokens = sum(
            s.token_estimate.token_count for s in context.sections
            if s.priority in PROTECTED_PRIORITIES
        )
        return LoomError(
            code=BRIEF_BUDGET_OVERFLOW,
            message=(
                f"Cannot generate brief: protected context for "
                f"{context.target_chunk_id} ({protected_tokens} tokens) "
                f"exceeds the token budget ({context.token_budget} tokens).  "
                f"Increase the token budget or reduce context size."
            ),
            detail={
                "chunk_id": context.target_chunk_id,
                "budget": context.token_budget,
                "protected_tokens": protected_tokens,
                "dropped_protected_types": [],
                "dropped_protected_ids": [],
            },
        )

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_brief(
    root: Path,
    chunk_id: str,
    task: str = "",
    dry_run: bool = False,
    force: bool = False,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> CommandResult:
    """Generate a deterministic session brief for a chunk.

    This is the **single entry point** for brief generation.  It:

    1. Loads the project via :func:`load_project`.
    2. Selects context via :func:`select_context` (shared with inspect).
    3. Checks for dirty/orphan chunks (fails unless ``--force``).
    4. Checks that no protected sections were dropped.
    5. Assembles the brief Markdown content.
    6. Writes the brief file (unless ``--dry-run``).

    Parameters
    ----------
    root:
        The project root directory.
    chunk_id:
        The target chunk ID.
    task:
        Optional task description to include in the brief.
    dry_run:
        If ``True``, assemble the brief but do not write to disk.
    force:
        If ``True``, allow brief generation on dirty/stale/orphan chunks
        with a strong ``BRIEF_FORCE_USED`` warning.
    token_budget:
        The token budget for context selection.

    Returns
    -------
    CommandResult
        The result of the brief generation.
    """
    root = Path(root).resolve()

    # ---------------------------------------------------------------
    # 1. Load project
    # ---------------------------------------------------------------
    try:
        state = load_project(root)
    except ProjectError as exc:
        return CommandResult.failure(
            command="brief",
            code=exc.loom_error.code,
            message=exc.loom_error.message,
            errors=[exc.loom_error],
        )

    errors: list[LoomError] = []
    warnings: list[LoomWarning] = list(state.load_warnings)

    # ---------------------------------------------------------------
    # 2. Select context using the SHARED engine
    # ---------------------------------------------------------------
    context = select_context(state, chunk_id=chunk_id, token_budget=token_budget)

    # Propagate context errors and warnings
    errors.extend(context.errors)
    warnings.extend(context.warnings)

    # Check if chunk was found
    if context.target_chunk is None:
        data = context.to_dict()
        return CommandResult.failure(
            command="brief",
            code=CHUNK_NOT_FOUND,
            message=f"Chunk {chunk_id!r} not found in project",
            errors=errors if errors else None,
            data=data,
            warnings=warnings,
        )

    # ---------------------------------------------------------------
    # 3. Check for dirty / orphan chunks
    # ---------------------------------------------------------------
    dirty_error = _check_chunk_dirty(state, chunk_id)
    orphan_error = _check_chunk_orphan(state, chunk_id)

    chunk_issues: list[LoomError] = []
    if dirty_error is not None:
        chunk_issues.append(dirty_error)
    if orphan_error is not None:
        chunk_issues.append(orphan_error)

    if chunk_issues and not force:
        # Fail — chunk has issues and --force was not used
        errors.extend(chunk_issues)
        data = context.to_dict()
        data["chunk_issues"] = [e.code for e in chunk_issues]
        return CommandResult.failure(
            command="brief",
            code=chunk_issues[0].code,
            message=chunk_issues[0].message,
            errors=errors,
            data=data,
            warnings=warnings,
        )

    if chunk_issues and force:
        # Proceed with strong warning
        for err in chunk_issues:
            warnings.append(
                LoomWarning(
                    code=BRIEF_FORCE_USED,
                    message=(
                        f"FORCE OVERRIDE: {err.message}  "
                        f"Generating brief anyway — the resulting brief may "
                        f"contain stale or incomplete context."
                    ),
                    detail={
                        "overridden_code": err.code,
                        "overridden_message": err.message,
                        "chunk_id": chunk_id,
                    },
                )
            )

    # ---------------------------------------------------------------
    # 4. Check protected sections
    # ---------------------------------------------------------------
    protected_error = _check_protected_sections_dropped(context)
    if protected_error is not None:
        errors.append(protected_error)
        data = context.to_dict()
        data["protected_dropped"] = True
        return CommandResult.failure(
            command="brief",
            code=BRIEF_BUDGET_OVERFLOW,
            message=protected_error.message,
            errors=errors,
            data=data,
            warnings=warnings,
        )

    # ---------------------------------------------------------------
    # 5. Assemble brief content
    # ---------------------------------------------------------------
    content = assemble_brief_content(context, task=task)

    # ---------------------------------------------------------------
    # 6. Write brief file (unless dry-run)
    # ---------------------------------------------------------------
    layout = state.layout
    brief_dir = layout.aip_loom_dir / "briefs"
    brief_path = brief_dir / f"{chunk_id}.md"

    if not dry_run:
        try:
            ensure_directory(brief_dir)
            safe_write_text(brief_path, content, layout)
        except Exception as exc:
            errors.append(
                LoomError(
                    code="FILE_WRITE_ERROR",
                    message=f"Failed to write brief file: {exc}",
                    detail={
                        "path": str(brief_path),
                        "error": str(exc),
                    },
                )
            )
            return CommandResult.failure(
                command="brief",
                code="FILE_WRITE_ERROR",
                message=f"Failed to write brief file: {exc}",
                errors=errors,
                warnings=warnings,
            )

    # ---------------------------------------------------------------
    # 7. Build result
    # ---------------------------------------------------------------
    brief_result = BriefResult(
        chunk_id=chunk_id,
        brief_path=brief_path if not dry_run else None,
        content=content,
        token_estimate=context.total_token_estimate.token_count,
        token_budget=token_budget,
        section_count=len(context.sections),
        dropped_count=len(context.dropped_sections),
        dry_run=dry_run,
    )

    data = brief_result.to_dict()
    # Also include context selection details for --json
    data["selected_context"] = context.to_dict()

    # Build message
    if dry_run:
        message = (
            f"Dry-run brief for {chunk_id}: "
            f"{brief_result.section_count} section(s), "
            f"~{brief_result.token_estimate} tokens"
        )
    else:
        message = (
            f"Brief generated for {chunk_id}: "
            f"{brief_result.section_count} section(s), "
            f"~{brief_result.token_estimate} tokens → {brief_path}"
        )

    if brief_result.dropped_count > 0:
        message += f" ({brief_result.dropped_count} dropped)"

    return CommandResult.success(
        command="brief",
        message=message,
        data=data,
        warnings=warnings,
    )
