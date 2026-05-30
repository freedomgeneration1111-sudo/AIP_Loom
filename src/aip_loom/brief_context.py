"""Shared brief context selection engine for AIP_Loom.

This module is the **single authority** for selecting the context that
``brief`` would assemble for a given chunk.  Both ``inspect`` and
``brief`` must call :func:`select_context` here — no other module may
implement its own context selection logic.  This prevents the primary
anti-pattern of parallel implementations diverging over time.

Design principles (BuildSpec §3A and Chunk 11 description):

- **Shared logic**: ``inspect`` and ``brief`` use the **same** context
  selection function.  ``inspect`` is a read-only preview of what
  ``brief`` would select; it must never duplicate the selection code.
- **Token budget aware**: Context selection respects a token budget.
  Sections that would overflow the budget are dropped and reported in
  ``dropped_sections``.  This ensures both commands agree on what fits.
- **Honest about gaps**: If a scoped ledger is missing or malformed,
  a warning is emitted — never silently skipped.  The user needs to
  know that context is incomplete.
- **Deterministic ordering**: Selected context items appear in a
  consistent, deterministic order: chunk frontmatter first, then
  decisions scoped to the chunk, then threads scoped to the chunk,
  then global decisions, then global threads, then distillate anchor
  summaries, then questions.
- **No file writes**: This module is pure computation.  It never writes
  to disk, even when used by ``brief`` (which writes the assembled
  brief in a separate step).

Context selection algorithm
---------------------------

Given a target chunk ID and a token budget:

1. **Target chunk**: Include the target chunk's frontmatter metadata
   and prose body.  This is always included (even if it alone exceeds
   the budget — the target chunk is mandatory).

2. **Scoped decisions**: Include all decision entries whose
   ``chunk_id`` matches the target.  These are the most relevant
   context for understanding what decisions were made about this chunk.

3. **Scoped threads**: Include all thread entries whose ``chunk_id``
   matches the target.  These represent open or closed continuity
   concerns directly related to this chunk.

4. **Distillate anchor**: If the distillate contains a node for the
   target chunk, include its summary and references.

5. **Global decisions**: Include decision entries with ``scope=global``
   (no chunk_id).  These provide project-wide context.

6. **Global threads**: Include thread entries with ``scope=global``.
   These provide project-wide continuity context.

7. **Adjacent chunk summaries**: If the target chunk has predecessors
   or successors in the chunk order, include their distillate summaries
   (if available) for continuity context.

8. **Questions**: Include unresolved questions as potential context
   for the model.

9. **Token budget enforcement**: After selecting all eligible sections,
   if the total estimated tokens exceed the budget, sections are trimmed
   from the bottom of the priority list (questions first, then global
   threads, then global decisions, etc.).  Dropped sections are reported
   in ``dropped_sections``.

Missing or malformed ledgers produce warnings, not errors.  The context
selection continues with whatever data is available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .errors import (
    BRIEF_BUDGET_OVERFLOW,
    BRIEF_DIRTY_CHUNK,
    BRIEF_ORPHAN_CHUNK,
    BRIEF_STALE_CHUNK,
    CHUNK_NOT_FOUND,
    TOKEN_COUNT_APPROXIMATE,
    LoomError,
    LoomWarning,
)
from .project import ChunkData, ProjectState
from .schemas import (
    DecisionEntry,
    DistillateNode,
    QuestionEntry,
    ReviewState,
    ThreadEntry,
    ThreadState,
)
from .tokens import TokenEstimate, estimate_text_tokens

__all__ = [
    "ContextSection",
    "SelectedContext",
    "select_context",
    "DEFAULT_TOKEN_BUDGET",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default token budget for context selection.  This is a generous
#: default that works for most LLM context windows.  The caller can
#: override it via CLI flags or configuration.
DEFAULT_TOKEN_BUDGET = 8000


# ---------------------------------------------------------------------------
# Context section
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextSection:
    """A single section of selected context.

    Attributes
    ----------
    section_type:
        The type of context section (e.g. ``"chunk_prose"``,
        ``"scoped_decision"``, ``"global_thread"``).
    source_id:
        The ID of the source item (e.g. ``"C-0001"``, ``"D-0003"``).
    content:
        The text content of this section.
    token_estimate:
        The token estimate for this section.
    priority:
        Priority level (lower = higher priority).  Sections with lower
        priority are dropped first when the budget is exceeded.
    """

    section_type: str
    source_id: str
    content: str
    token_estimate: TokenEstimate
    priority: int


# ---------------------------------------------------------------------------
# Selected context result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectedContext:
    """The result of selecting context for a target chunk.

    This is the shared data structure used by both ``inspect`` and
    ``brief``.  It contains everything that would go into a brief,
    plus metadata about what was selected, dropped, and any warnings.

    Attributes
    ----------
    target_chunk_id:
        The chunk ID that was inspected.
    target_chunk:
        The target chunk data, or ``None`` if the chunk was not found.
    sections:
        The selected context sections in priority order.
    dropped_sections:
        Sections that were eligible but dropped due to budget overflow.
    scoped_decisions:
        Decision entries scoped to the target chunk.
    scoped_threads:
        Thread entries scoped to the target chunk.
    global_decisions:
        Global (non-scoped) decision entries included.
    global_threads:
        Global (non-scoped) thread entries included.
    distillate_node:
        The distillate node for the target chunk, if any.
    adjacent_summaries:
        Distillate summaries of adjacent chunks (predecessor/successor).
    unresolved_questions:
        Unresolved question entries included.
    total_token_estimate:
        The total token estimate for all selected sections.
    token_budget:
        The token budget that was used.
    budget_exceeded:
        Whether the total exceeds the budget.
    errors:
        Errors encountered during context selection (e.g. chunk not found).
    warnings:
        Warnings encountered during context selection (e.g. missing ledger,
        approximate token count).
    """

    target_chunk_id: str
    target_chunk: ChunkData | None
    sections: tuple[ContextSection, ...]
    dropped_sections: tuple[ContextSection, ...]
    scoped_decisions: tuple[DecisionEntry, ...]
    scoped_threads: tuple[ThreadEntry, ...]
    global_decisions: tuple[DecisionEntry, ...]
    global_threads: tuple[ThreadEntry, ...]
    distillate_node: DistillateNode | None
    adjacent_summaries: tuple[DistillateNode, ...]
    unresolved_questions: tuple[QuestionEntry, ...]
    total_token_estimate: TokenEstimate
    token_budget: int
    budget_exceeded: bool
    errors: tuple[LoomError, ...]
    warnings: tuple[LoomWarning, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary of the selected context."""
        result: dict[str, Any] = {
            "target_chunk_id": self.target_chunk_id,
            "target_chunk_found": self.target_chunk is not None,
            "sections": [
                {
                    "type": s.section_type,
                    "source_id": s.source_id,
                    "tokens": s.token_estimate.token_count,
                    "priority": s.priority,
                }
                for s in self.sections
            ],
            "dropped_sections": [
                {
                    "type": s.section_type,
                    "source_id": s.source_id,
                    "tokens": s.token_estimate.token_count,
                    "priority": s.priority,
                }
                for s in self.dropped_sections
            ],
            "scoped_decisions": [e.id for e in self.scoped_decisions],
            "scoped_threads": [e.id for e in self.scoped_threads],
            "global_decisions": [e.id for e in self.global_decisions],
            "global_threads": [e.id for e in self.global_threads],
            "distillate_node": (
                {
                    "chunk_id": self.distillate_node.chunk_id,
                    "title": self.distillate_node.title,
                    "summary": self.distillate_node.summary,
                    "key_decisions": self.distillate_node.key_decisions,
                    "open_threads": self.distillate_node.open_threads,
                }
                if self.distillate_node
                else None
            ),
            "adjacent_summaries": [
                {"chunk_id": n.chunk_id, "title": n.title, "summary": n.summary}
                for n in self.adjacent_summaries
            ],
            "unresolved_questions": [e.id for e in self.unresolved_questions],
            "total_tokens": self.total_token_estimate.to_dict(),
            "token_budget": self.token_budget,
            "budget_exceeded": self.budget_exceeded,
            "errors": [
                {"code": e.code, "message": e.message, "detail": e.detail}
                for e in self.errors
            ],
            "warnings": [
                {"code": w.code, "message": w.message, "detail": w.detail}
                for w in self.warnings
            ],
        }
        return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_decision(entry: DecisionEntry) -> str:
    """Format a decision entry as context text."""
    lines = [
        f"Decision {entry.id}: {entry.summary}",
    ]
    if entry.rationale:
        lines.append(f"  Rationale: {entry.rationale}")
    if entry.chunk_id:
        lines.append(f"  Scoped to: {entry.chunk_id}")
    if entry.review_state != ReviewState.APPROVED:
        lines.append(f"  Review: {entry.review_state.value}")
    return "\n".join(lines)


def _format_thread(entry: ThreadEntry) -> str:
    """Format a thread entry as context text."""
    lines = [
        f"Thread {entry.id}: {entry.summary}",
    ]
    lines.append(f"  State: {entry.state.value}")
    if entry.chunk_id:
        lines.append(f"  Scoped to: {entry.chunk_id}")
    if entry.blocked_by:
        lines.append(f"  Blocked by: {', '.join(entry.blocked_by)}")
    if entry.review_state != ReviewState.APPROVED:
        lines.append(f"  Review: {entry.review_state.value}")
    return "\n".join(lines)


def _format_question(entry: QuestionEntry) -> str:
    """Format a question entry as context text."""
    lines = [
        f"Question {entry.id}: {entry.question}",
    ]
    if entry.answer:
        lines.append(f"  Answer: {entry.answer}")
    lines.append(f"  Resolved: {entry.resolved}")
    return "\n".join(lines)


def _format_distillate_node(node: DistillateNode) -> str:
    """Format a distillate node as context text."""
    lines = [
        f"Distillate [{node.chunk_id}]: {node.title}",
    ]
    if node.summary:
        lines.append(f"  Summary: {node.summary}")
    if node.key_decisions:
        lines.append(f"  Key decisions: {', '.join(node.key_decisions)}")
    if node.open_threads:
        lines.append(f"  Open threads: {', '.join(node.open_threads)}")
    return "\n".join(lines)


def _format_chunk_frontmatter(chunk: ChunkData) -> str:
    """Format chunk frontmatter metadata as context text."""
    fm = chunk.frontmatter
    lines = [
        f"Chunk {fm.id}: {fm.title}",
        f"  Status: {fm.status.value}",
        f"  Word count: {fm.word_count}",
        f"  Created: {fm.created_at}",
        f"  Updated: {fm.updated_at}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def select_context(
    state: ProjectState,
    chunk_id: str,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> SelectedContext:
    """Select the context that ``brief`` would assemble for a target chunk.

    This is the **single entry point** for context selection, shared by
    both ``inspect`` and ``brief``.  No other function may independently
    decide what context to include for a chunk.

    The function is **pure computation** — it never writes to disk.
    ``inspect`` uses it as a read-only preview; ``brief`` will use it
    to assemble the brief file.

    Parameters
    ----------
    state:
        The loaded project state from :func:`load_project`.
    chunk_id:
        The target chunk ID to select context for.
    token_budget:
        The maximum number of tokens to include.  Sections that would
        overflow the budget are dropped and reported in
        ``dropped_sections``.

    Returns
    -------
    SelectedContext
        A frozen dataclass with all selected context, dropped sections,
        token estimates, and any warnings or errors.
    """
    errors: list[LoomError] = []
    warnings: list[LoomWarning] = []

    # ---------------------------------------------------------------
    # 1. Locate target chunk
    # ---------------------------------------------------------------
    target_chunk = state.chunks.get(chunk_id)
    if target_chunk is None:
        errors.append(
            LoomError(
                code=CHUNK_NOT_FOUND,
                message=f"Chunk {chunk_id!r} not found in project",
                detail={"chunk_id": chunk_id, "available": list(state.chunks.keys())},
            )
        )
        # Return early with empty context — can't select without target
        return SelectedContext(
            target_chunk_id=chunk_id,
            target_chunk=None,
            sections=(),
            dropped_sections=(),
            scoped_decisions=(),
            scoped_threads=(),
            global_decisions=(),
            global_threads=(),
            distillate_node=None,
            adjacent_summaries=(),
            unresolved_questions=(),
            total_token_estimate=estimate_text_tokens(""),
            token_budget=token_budget,
            budget_exceeded=False,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )

    # ---------------------------------------------------------------
    # 2. Collect scoped ledger entries
    # ---------------------------------------------------------------
    scoped_decisions: list[DecisionEntry] = []
    scoped_threads: list[ThreadEntry] = []
    global_decisions: list[DecisionEntry] = []
    global_threads: list[ThreadEntry] = []
    unresolved_questions: list[QuestionEntry] = []

    # Decisions
    if state.decisions_ledger is not None:
        for entry in state.decisions_ledger.entries:
            if entry.chunk_id == chunk_id:
                scoped_decisions.append(entry)
            elif entry.scope == "global" and not entry.chunk_id:
                global_decisions.append(entry)
    else:
        warnings.append(
            LoomWarning(
                code=BRIEF_ORPHAN_CHUNK,
                message=(
                    f"Decisions ledger is missing or malformed — "
                    f"cannot retrieve scoped decisions for {chunk_id}"
                ),
                detail={"chunk_id": chunk_id, "ledger": "decisions"},
            )
        )

    # Threads
    if state.threads_ledger is not None:
        for entry in state.threads_ledger.entries:
            if entry.chunk_id == chunk_id:
                scoped_threads.append(entry)
            elif entry.scope == "global" and not entry.chunk_id:
                global_threads.append(entry)
    else:
        warnings.append(
            LoomWarning(
                code=BRIEF_ORPHAN_CHUNK,
                message=(
                    f"Threads ledger is missing or malformed — "
                    f"cannot retrieve scoped threads for {chunk_id}"
                ),
                detail={"chunk_id": chunk_id, "ledger": "threads"},
            )
        )

    # Questions
    if state.questions_ledger is not None:
        for entry in state.questions_ledger.entries:
            if not entry.resolved:
                unresolved_questions.append(entry)
    else:
        warnings.append(
            LoomWarning(
                code=BRIEF_ORPHAN_CHUNK,
                message=(
                    f"Questions ledger is missing or malformed — "
                    f"cannot retrieve unresolved questions"
                ),
                detail={"chunk_id": chunk_id, "ledger": "questions"},
            )
        )

    # ---------------------------------------------------------------
    # 3. Find distillate node for target chunk
    # ---------------------------------------------------------------
    distillate_node: DistillateNode | None = None
    if state.distillate is not None:
        for node in state.distillate.nodes:
            if node.chunk_id == chunk_id:
                distillate_node = node
                break

    # ---------------------------------------------------------------
    # 4. Find adjacent chunk distillate summaries
    # ---------------------------------------------------------------
    adjacent_summaries: list[DistillateNode] = []
    if state.distillate is not None and state.chunk_order is not None:
        ordered_ids = state.chunk_order.ordered_ids
        try:
            idx = ordered_ids.index(chunk_id)
            # Predecessor
            if idx > 0:
                prev_id = ordered_ids[idx - 1]
                for node in state.distillate.nodes:
                    if node.chunk_id == prev_id:
                        adjacent_summaries.append(node)
                        break
            # Successor
            if idx < len(ordered_ids) - 1:
                next_id = ordered_ids[idx + 1]
                for node in state.distillate.nodes:
                    if node.chunk_id == next_id:
                        adjacent_summaries.append(node)
                        break
        except ValueError:
            # Chunk not in ordered list — skip adjacent lookup
            pass

    # ---------------------------------------------------------------
    # 5. Build context sections with priority ordering
    # ---------------------------------------------------------------
    # Priority levels (lower = higher priority, dropped last):
    #   0 = target chunk frontmatter (always kept)
    #   1 = target chunk prose (always kept)
    #   2 = distillate node for target chunk
    #   3 = scoped decisions
    #   4 = scoped threads
    #   5 = adjacent distillate summaries
    #   6 = global decisions
    #   7 = global threads
    #   8 = unresolved questions
    sections: list[ContextSection] = []

    # Priority 0: Target chunk frontmatter
    fm_text = _format_chunk_frontmatter(target_chunk)
    sections.append(
        ContextSection(
            section_type="chunk_frontmatter",
            source_id=chunk_id,
            content=fm_text,
            token_estimate=estimate_text_tokens(fm_text),
            priority=0,
        )
    )

    # Priority 1: Target chunk prose
    prose_text = target_chunk.prose_body
    sections.append(
        ContextSection(
            section_type="chunk_prose",
            source_id=chunk_id,
            content=prose_text,
            token_estimate=estimate_text_tokens(prose_text),
            priority=1,
        )
    )

    # Priority 2: Distillate node
    if distillate_node is not None:
        dist_text = _format_distillate_node(distillate_node)
        sections.append(
            ContextSection(
                section_type="distillate_node",
                source_id=chunk_id,
                content=dist_text,
                token_estimate=estimate_text_tokens(dist_text),
                priority=2,
            )
        )

    # Priority 3: Scoped decisions
    for entry in scoped_decisions:
        text = _format_decision(entry)
        sections.append(
            ContextSection(
                section_type="scoped_decision",
                source_id=entry.id,
                content=text,
                token_estimate=estimate_text_tokens(text),
                priority=3,
            )
        )

    # Priority 4: Scoped threads
    for entry in scoped_threads:
        text = _format_thread(entry)
        sections.append(
            ContextSection(
                section_type="scoped_thread",
                source_id=entry.id,
                content=text,
                token_estimate=estimate_text_tokens(text),
                priority=4,
            )
        )

    # Priority 5: Adjacent distillate summaries
    for node in adjacent_summaries:
        text = _format_distillate_node(node)
        sections.append(
            ContextSection(
                section_type="adjacent_summary",
                source_id=node.chunk_id,
                content=text,
                token_estimate=estimate_text_tokens(text),
                priority=5,
            )
        )

    # Priority 6: Global decisions
    for entry in global_decisions:
        text = _format_decision(entry)
        sections.append(
            ContextSection(
                section_type="global_decision",
                source_id=entry.id,
                content=text,
                token_estimate=estimate_text_tokens(text),
                priority=6,
            )
        )

    # Priority 7: Global threads
    for entry in global_threads:
        text = _format_thread(entry)
        sections.append(
            ContextSection(
                section_type="global_thread",
                source_id=entry.id,
                content=text,
                token_estimate=estimate_text_tokens(text),
                priority=7,
            )
        )

    # Priority 8: Unresolved questions
    for entry in unresolved_questions:
        text = _format_question(entry)
        sections.append(
            ContextSection(
                section_type="unresolved_question",
                source_id=entry.id,
                content=text,
                token_estimate=estimate_text_tokens(text),
                priority=8,
            )
        )

    # ---------------------------------------------------------------
    # 6. Apply token budget
    # ---------------------------------------------------------------
    # Mandatory sections (priority 0 and 1) are always kept.
    # Other sections are kept in priority order until the budget is
    # exceeded; remaining sections are dropped.
    mandatory_sections: list[ContextSection] = []
    optional_sections: list[ContextSection] = []

    for section in sections:
        if section.priority <= 1:
            mandatory_sections.append(section)
        else:
            optional_sections.append(section)

    # Calculate mandatory token cost
    mandatory_tokens = sum(s.token_estimate.token_count for s in mandatory_sections)

    # Add optional sections in priority order until budget is exceeded
    kept_optional: list[ContextSection] = []
    dropped: list[ContextSection] = []
    running_total = mandatory_tokens

    for section in sorted(optional_sections, key=lambda s: s.priority):
        section_tokens = section.token_estimate.token_count
        if running_total + section_tokens <= token_budget:
            kept_optional.append(section)
            running_total += section_tokens
        else:
            dropped.append(section)

    # Final sections: mandatory + kept optional
    final_sections = mandatory_sections + kept_optional

    # Check if even mandatory sections exceed budget
    budget_exceeded = mandatory_tokens > token_budget
    if budget_exceeded:
        warnings.append(
            LoomWarning(
                code=BRIEF_BUDGET_OVERFLOW,
                message=(
                    f"Mandatory context for {chunk_id} "
                    f"({mandatory_tokens} tokens) exceeds budget "
                    f"({token_budget} tokens)"
                ),
                detail={
                    "chunk_id": chunk_id,
                    "mandatory_tokens": mandatory_tokens,
                    "budget": token_budget,
                },
            )
        )

    # Collect token estimation warning if heuristic was used
    if final_sections:
        # Use the first section's estimate to check for approximation
        # (all sections use the same method)
        token_warning = final_sections[0].token_estimate.warning
        if token_warning is not None:
            # Only add the approximation warning once
            has_approx_warning = any(
                w.code == TOKEN_COUNT_APPROXIMATE for w in warnings
            )
            if not has_approx_warning:
                warnings.append(token_warning)

    # Calculate total token estimate
    total_text = "\n".join(s.content for s in final_sections)
    total_estimate = estimate_text_tokens(total_text)

    return SelectedContext(
        target_chunk_id=chunk_id,
        target_chunk=target_chunk,
        sections=tuple(final_sections),
        dropped_sections=tuple(dropped),
        scoped_decisions=tuple(scoped_decisions),
        scoped_threads=tuple(scoped_threads),
        global_decisions=tuple(global_decisions),
        global_threads=tuple(global_threads),
        distillate_node=distillate_node,
        adjacent_summaries=tuple(adjacent_summaries),
        unresolved_questions=tuple(unresolved_questions),
        total_token_estimate=total_estimate,
        token_budget=token_budget,
        budget_exceeded=budget_exceeded,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
