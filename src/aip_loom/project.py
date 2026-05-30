"""Project loader and validation engine for AIP_Loom.

This module is the **single authority** for loading an entire AIP_Loom
project into memory and validating its structural and semantic integrity.
No other module may read and assemble the project state independently —
it must delegate to :func:`load_project` here.

Design principles (BuildSpec §3A and Chunk 09 description):

- **Single loading authority**: :func:`load_project` is the only function
  that reads all canonical files, parses them, and assembles a coherent
  :class:`ProjectState`.  Every downstream command (status, brief,
  reconcile, inspect) uses this single loader.
- **Honest partial loading**: If some files are malformed or missing,
  :func:`load_project` still returns a :class:`ProjectState` with
  ``load_errors`` populated.  It never fabricates default state to
  hide failures.  A missing ledger is an error, not an empty ledger.
- **Validation is pure**: :func:`validate_project` performs side-effect-
  free integrity checks.  It **never** repairs, auto-fixes, or writes
  files.  Dirty checksums are reported, not corrected.  Broken
  references are flagged, not patched.
- **Validation passes**: Validation checks are organised into passes
  that detect duplicate IDs, broken references, schema violations,
  checksum mismatches, missing required files, and chunk order issues.
- **Chunk scoping**: Validation supports ``--chunk`` scoping so that
  a single chunk can be checked in isolation.
- **Pending review reporting**: Ledger entries with ``review_state=pending``
  are reported as warnings — they are not errors, but they require
  human attention.
- **Uses existing modules**: Loading and validation use
  :mod:`aip_loom.yaml_io`, :mod:`aip_loom.schemas`,
  :mod:`aip_loom.layout`, :mod:`aip_loom.checksum`,
  :mod:`aip_loom.frontmatter`, :mod:`aip_loom.ids`, and
  :mod:`aip_loom.chunk_order`.  No ad-hoc YAML parsing, checksum
  computation, or ID extraction elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .checksum import compute_prose_checksum
from .chunk_order import ChunkOrderResult, resolve_chunk_order
from .errors import (
    CHECKSUM_MISMATCH,
    CHUNK_NOT_FOUND,
    FIELD_MISSING,
    ID_DUPLICATE,
    PROJECT_MALFORMED,
    PROJECT_NOT_FOUND,
    SCHEMA_VALIDATION_FAILED,
    VALIDATION_BROKEN_REFERENCE,
    VALIDATION_CHUNK_ORDER_MISMATCH,
    VALIDATION_DIRTY_CHECKSUM,
    VALIDATION_DUPLICATE_ID,
    VALIDATION_MISSING_FILE,
    VALIDATION_PENDING_REVIEW,
    YAML_PARSE_ERROR,
    LoomError,
    LoomWarning,
    REVIEW_STATE_PENDING,
    CHECKSUM_DIRTY,
)
from .frontmatter import FrontmatterParseError, FrontmatterParseResult, parse_frontmatter
from .ids import extract_id_number
from .layout import LayoutError, ProjectLayout
from .schemas import (
    ChunkFrontmatter,
    CommentLog,
    DecisionLedger,
    Distillate,
    ProjectManifest,
    QuestionLedger,
    ReviewState,
    SessionLog,
    ThreadLedger,
)
from .yaml_io import YamlLoadError, load_yaml_as

__all__ = [
    "ProjectError",
    "ChunkData",
    "ProjectState",
    "ValidationResult",
    "load_project",
    "validate_project",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProjectError(Exception):
    """Raised when project loading fails critically.

    Carries a :class:`LoomError` with a stable error code.  This is only
    raised for fundamental problems (project not found, manifest missing).
    Per-file load failures are captured in :attr:`ProjectState.load_errors`
    rather than raising.
    """

    def __init__(self, loom_error: LoomError) -> None:
        self.loom_error = loom_error
        super().__init__(loom_error.message)


# ---------------------------------------------------------------------------
# Per-chunk data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkData:
    """Parsed data for a single chunk file.

    Attributes
    ----------
    file_path:
        The absolute path to the chunk Markdown file.
    frontmatter:
        The validated frontmatter model instance.
    prose_body:
        The prose body text below the frontmatter delimiters.
    """

    file_path: Path
    frontmatter: ChunkFrontmatter
    prose_body: str


# ---------------------------------------------------------------------------
# Project state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectState:
    """The loaded state of an AIP_Loom project.

    This is the single, comprehensive data structure that every downstream
    command uses.  It contains all loaded canonical files, all parsed
    chunks, and any errors or warnings encountered during loading.

    Attributes
    ----------
    layout:
        The project layout (path authority).
    manifest:
        The loaded project manifest, or ``None`` if it could not be parsed.
    decisions_ledger:
        The loaded decisions ledger, or ``None`` if it could not be parsed.
    threads_ledger:
        The loaded threads ledger, or ``None`` if it could not be parsed.
    questions_ledger:
        The loaded questions ledger, or ``None`` if it could not be parsed.
    distillate:
        The loaded distillate, or ``None`` if it could not be parsed.
    sessions:
        The loaded session log, or ``None`` if it could not be parsed.
    comments:
        The loaded comment log, or ``None`` if it could not be parsed.
    chunks:
        Mapping of chunk ID to :class:`ChunkData`.  Only chunks that
        were successfully parsed are included.
    chunk_order:
        The resolved chunk order (from manifest or fallback).
    load_errors:
        Errors encountered during loading (malformed YAML, missing files,
        schema violations, etc.).
    load_warnings:
        Warnings encountered during loading.
    """

    layout: ProjectLayout
    manifest: ProjectManifest | None
    decisions_ledger: DecisionLedger | None
    threads_ledger: ThreadLedger | None
    questions_ledger: QuestionLedger | None
    distillate: Distillate | None
    sessions: SessionLog | None
    comments: CommentLog | None
    chunks: dict[str, ChunkData]
    chunk_order: ChunkOrderResult | None
    load_errors: tuple[LoomError, ...] = ()
    load_warnings: tuple[LoomWarning, ...] = ()


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    """The result of validating a project.

    Attributes
    ----------
    errors:
        All errors found during validation (schema violations, duplicate
        IDs, broken references, missing files, etc.).
    warnings:
        All warnings found during validation (dirty checksums, pending
        review items, chunk order fallbacks, etc.).
    ok:
        Whether validation passed with zero errors.
    """

    errors: tuple[LoomError, ...]
    warnings: tuple[LoomWarning, ...]
    ok: bool

    @classmethod
    def from_findings(
        cls,
        errors: Sequence[LoomError],
        warnings: Sequence[LoomWarning],
    ) -> ValidationResult:
        """Create a ValidationResult from lists of errors and warnings."""
        err_tuple = tuple(errors)
        warn_tuple = tuple(warnings)
        return cls(
            errors=err_tuple,
            warnings=warn_tuple,
            ok=len(err_tuple) == 0,
        )


# ---------------------------------------------------------------------------
# Internal helpers — safe loaders
# ---------------------------------------------------------------------------


def _safe_load_yaml(
    path: Path,
    model_type: type,
    label: str,
    errors: list[LoomError],
) -> Any | None:
    """Load a YAML file and validate it against a Pydantic model.

    On failure, appends a :class:`LoomError` to *errors* and returns
    ``None`` rather than raising.  This allows partial loading to
    continue.

    Parameters
    ----------
    path:
        The file path to load.
    model_type:
        The Pydantic model class to validate against.
    label:
        A human-readable label for error messages.
    errors:
        The list to append errors to.

    Returns
    -------
    Any | None
        The validated model instance, or ``None`` on failure.
    """
    try:
        return load_yaml_as(path, model_type)
    except YamlLoadError as exc:
        errors.append(
            LoomError(
                code=exc.loom_error.code,
                message=f"Cannot load {label}: {exc.loom_error.message}",
                detail={
                    "file": str(path),
                    "label": label,
                    "original_code": exc.loom_error.code,
                    **exc.loom_error.detail,
                },
            )
        )
        return None


def _discover_chunks(
    layout: ProjectLayout,
    errors: list[LoomError],
    warnings: list[LoomWarning],
) -> dict[str, ChunkData]:
    """Discover and parse all chunk files in the project.

    Scans the ``chunks/`` directory for ``*.md`` files, parses each
    one's frontmatter, and returns a mapping of chunk ID to
    :class:`ChunkData`.

    Malformed chunk files are recorded in *errors* and skipped.  They
    are **not** silently dropped — the caller always sees an error for
    each unparsable chunk.

    Parameters
    ----------
    layout:
        The project layout.
    errors:
        The list to append errors to.
    warnings:
        The list to append warnings to.

    Returns
    -------
    dict[str, ChunkData]
        Mapping of chunk ID to parsed chunk data.
    """
    chunks: dict[str, ChunkData] = {}

    if not layout.chunks_dir.is_dir():
        # No chunks directory is not an error for an empty project.
        return chunks

    for md_file in sorted(layout.chunks_dir.glob("*.md")):
        try:
            raw_text = md_file.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(
                LoomError(
                    code=YAML_PARSE_ERROR,
                    message=f"Cannot read chunk file: {md_file}: {exc}",
                    detail={"file": str(md_file), "error": str(exc)},
                )
            )
            continue

        try:
            parse_result = parse_frontmatter(raw_text)
        except FrontmatterParseError as exc:
            errors.append(
                LoomError(
                    code=exc.loom_error.code,
                    message=f"Malformed frontmatter in {md_file.name}: {exc.loom_error.message}",
                    detail={
                        "file": str(md_file),
                        "original_code": exc.loom_error.code,
                        **exc.loom_error.detail,
                    },
                )
            )
            continue

        chunk_id = parse_result.frontmatter.id
        chunk_data = ChunkData(
            file_path=md_file,
            frontmatter=parse_result.frontmatter,
            prose_body=parse_result.prose_body,
        )
        chunks[chunk_id] = chunk_data

    return chunks


# ---------------------------------------------------------------------------
# Public API — load
# ---------------------------------------------------------------------------


def load_project(root: Path) -> ProjectState:
    """Load an entire AIP_Loom project into memory.

    This is the **single entry point** for reading all project files.
    It loads the manifest, ledgers, distillate, session log, comment
    log, and all chunk files, assembling them into a :class:`ProjectState`.

    If some files are missing or malformed, they are recorded in
    ``load_errors`` and the corresponding fields are set to ``None``.
    The function only raises :class:`ProjectError` for fundamental
    problems (project root does not exist, manifest is absent).

    Parameters
    ----------
    root:
        The project root directory.

    Returns
    -------
    ProjectState
        The loaded project state with any errors or warnings.

    Raises
    ------
    ProjectError
        If the project root does not exist or has no manifest.
    """
    root = Path(root).resolve()

    # 1. Construct layout (validates root exists)
    try:
        layout = ProjectLayout(root=root)
    except LayoutError as exc:
        raise ProjectError(
            LoomError(
                code=PROJECT_NOT_FOUND,
                message=f"Project root does not exist: {root}",
                detail={"root": str(root)},
            )
        ) from exc

    # 2. Check manifest existence (fundamental requirement)
    if not layout.manifest_path.is_file():
        raise ProjectError(
            LoomError(
                code=PROJECT_NOT_FOUND,
                message=(
                    f"No project manifest found at {layout.manifest_path}.  "
                    "Run 'aip-loom init' to create a project."
                ),
                detail={"root": str(root), "manifest": str(layout.manifest_path)},
            )
        )

    errors: list[LoomError] = []
    warnings: list[LoomWarning] = []

    # 3. Load all canonical YAML files (best-effort per file)
    manifest = _safe_load_yaml(
        layout.manifest_path, ProjectManifest, "project manifest", errors,
    )
    decisions_ledger = _safe_load_yaml(
        layout.decisions_ledger_path, DecisionLedger, "decisions ledger", errors,
    )
    threads_ledger = _safe_load_yaml(
        layout.threads_ledger_path, ThreadLedger, "threads ledger", errors,
    )
    questions_ledger = _safe_load_yaml(
        layout.questions_ledger_path, QuestionLedger, "questions ledger", errors,
    )
    distillate = _safe_load_yaml(
        layout.distillate_path, Distillate, "distillate", errors,
    )
    sessions = _safe_load_yaml(
        layout.sessions_path, SessionLog, "session log", errors,
    )
    comments = _safe_load_yaml(
        layout.comments_path, CommentLog, "comment log", errors,
    )

    # 4. Discover and parse chunk files
    chunks = _discover_chunks(layout, errors, warnings)

    # 5. Resolve chunk order (requires manifest)
    chunk_order: ChunkOrderResult | None = None
    if manifest is not None:
        chunk_ids = list(chunks.keys())
        chunk_order = resolve_chunk_order(manifest, chunk_ids)
        warnings.extend(chunk_order.warnings)

    return ProjectState(
        layout=layout,
        manifest=manifest,
        decisions_ledger=decisions_ledger,
        threads_ledger=threads_ledger,
        questions_ledger=questions_ledger,
        distillate=distillate,
        sessions=sessions,
        comments=comments,
        chunks=chunks,
        chunk_order=chunk_order,
        load_errors=tuple(errors),
        load_warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Public API — validate
# ---------------------------------------------------------------------------


def validate_project(
    state: ProjectState,
    chunk_scope: str | None = None,
) -> ValidationResult:
    """Validate the integrity of a loaded project.

    Performs the following validation passes:

    1. **Missing required files** — checks that all canonical files exist.
    2. **Schema violations** — checks that load errors exist (already
       captured during loading).
    3. **Duplicate IDs** — detects duplicate chunk IDs and duplicate
       ledger entry IDs.
    4. **Broken references** — checks that chunk_id references in ledger
       entries and distillate nodes point to existing chunks.
    5. **Checksum mismatches** — verifies that ``prose_checksum`` in each
       chunk's frontmatter matches the computed checksum of the prose body.
    6. **Chunk order issues** — checks that the manifest's chunk order
       matches the chunks on disk.
    7. **Pending review items** — reports ledger entries with
       ``review_state=pending``.

    Validation is **pure**: no files are modified, no repairs are made,
    no auto-fixing occurs.

    Parameters
    ----------
    state:
        The loaded project state from :func:`load_project`.
    chunk_scope:
        If provided, limit validation to this specific chunk ID.
        Only the specified chunk and its references are checked.

    Returns
    -------
    ValidationResult
        A frozen result with all errors and warnings found.
    """
    errors: list[LoomError] = []
    warnings: list[LoomWarning] = []

    # Promote load errors into validation errors
    errors.extend(state.load_errors)
    warnings.extend(state.load_warnings)

    layout = state.layout

    # -- Pass 1: Missing required files --------------------------------------
    _check_missing_files(layout, errors)

    # -- Pass 2: Schema violations are already captured in load_errors --------
    # (No additional work needed — _safe_load_yaml captured them.)

    # -- Pass 3: Duplicate IDs -----------------------------------------------
    _check_duplicate_ids(state, errors)

    # -- Pass 4: Broken references --------------------------------------------
    _check_broken_references(state, errors, warnings, chunk_scope)

    # -- Pass 5: Checksum mismatches ------------------------------------------
    _check_checksums(state, warnings, chunk_scope)

    # -- Pass 6: Chunk order issues -------------------------------------------
    _check_chunk_order(state, errors, warnings)

    # -- Pass 7: Pending review items -----------------------------------------
    _check_pending_reviews(state, warnings, chunk_scope)

    return ValidationResult.from_findings(errors, warnings)


# ---------------------------------------------------------------------------
# Validation pass implementations
# ---------------------------------------------------------------------------


def _check_missing_files(
    layout: ProjectLayout,
    errors: list[LoomError],
) -> None:
    """Check that all required canonical files exist.

    The following files are required for a valid project:
    - aip_loom.yaml (manifest)
    - ledgers/decisions.yaml
    - ledgers/threads.yaml
    - ledgers/questions.yaml
    - distillate.yaml
    - sessions.yaml
    - comments.yaml

    Required directories:
    - chunks/
    - archive/
    - .aip-loom/
    """
    required_files: list[tuple[Path, str]] = [
        (layout.manifest_path, "project manifest"),
        (layout.decisions_ledger_path, "decisions ledger"),
        (layout.threads_ledger_path, "threads ledger"),
        (layout.questions_ledger_path, "questions ledger"),
        (layout.distillate_path, "distillate"),
        (layout.sessions_path, "session log"),
        (layout.comments_path, "comment log"),
    ]

    required_dirs: list[tuple[Path, str]] = [
        (layout.chunks_dir, "chunks directory"),
        (layout.archive_dir, "archive directory"),
        (layout.aip_loom_dir, ".aip-loom directory"),
    ]

    for path, label in required_files:
        if not path.is_file():
            errors.append(
                LoomError(
                    code=VALIDATION_MISSING_FILE,
                    message=f"Missing required file: {label} ({path.name})",
                    detail={"file": str(path), "label": label, "kind": "file"},
                )
            )

    for path, label in required_dirs:
        if not path.is_dir():
            errors.append(
                LoomError(
                    code=VALIDATION_MISSING_FILE,
                    message=f"Missing required directory: {label} ({path.name})",
                    detail={"file": str(path), "label": label, "kind": "directory"},
                )
            )


def _check_duplicate_ids(
    state: ProjectState,
    errors: list[LoomError],
) -> None:
    """Check for duplicate IDs across chunks and ledger entries.

    Duplicate chunk IDs are detected (e.g. two chunks with id C-0001).
    Duplicate ledger entry IDs are also detected within each ledger.
    Cross-ledger duplicates are not checked (D-0001 and T-0001 are
    allowed — they have different prefixes).
    """
    # -- Duplicate chunk IDs -------------------------------------------------
    # The chunks dict is keyed by ID, so if two files had the same ID
    # the second would overwrite the first. We need to check the actual
    # files on disk for this. However, _discover_chunks already dedupes
    # by ID. If two .md files have the same frontmatter ID, the second
    # silently replaces the first in the dict — which is itself a bug.
    # We need a more careful scan.
    chunk_id_to_files: dict[str, list[Path]] = {}
    if state.layout.chunks_dir.is_dir():
        for md_file in state.layout.chunks_dir.glob("*.md"):
            if md_file.name in chunk_id_to_files.get(md_file.stem, []):
                continue  # Already processed
            # Quick check: does this chunk appear in our loaded data?
            # We need to re-scan to find ALL files with same ID
            chunk_data = state.chunks.get(md_file.stem) if md_file.stem in state.chunks else None
            # Actually we need to check by frontmatter ID, not filename.
            # Let's just use the loaded chunks and check file count.
            pass

    # Better approach: scan loaded chunks for ID that came from
    # multiple files. Since _discover_chunks uses a dict keyed by ID,
    # duplicates are silently dropped. Let's re-discover more carefully.
    seen_chunk_ids: dict[str, Path] = {}
    if state.layout.chunks_dir.is_dir():
        for md_file in sorted(state.layout.chunks_dir.glob("*.md")):
            try:
                raw_text = md_file.read_text(encoding="utf-8")
                parse_result = parse_frontmatter(raw_text)
                chunk_id = parse_result.frontmatter.id
            except (OSError, FrontmatterParseError):
                continue  # Already captured as load error

            if chunk_id in seen_chunk_ids:
                errors.append(
                    LoomError(
                        code=VALIDATION_DUPLICATE_ID,
                        message=(
                            f"Duplicate chunk ID {chunk_id!r}: "
                            f"found in both {seen_chunk_ids[chunk_id].name} "
                            f"and {md_file.name}"
                        ),
                        detail={
                            "id": chunk_id,
                            "files": [
                                str(seen_chunk_ids[chunk_id]),
                                str(md_file),
                            ],
                        },
                    )
                )
            else:
                seen_chunk_ids[chunk_id] = md_file

    # -- Duplicate ledger entry IDs ------------------------------------------
    for ledger_label, ledger_entries in [
        ("decisions", state.decisions_ledger.entries if state.decisions_ledger else []),
        ("threads", state.threads_ledger.entries if state.threads_ledger else []),
        ("questions", state.questions_ledger.entries if state.questions_ledger else []),
    ]:
        seen_ids: dict[str, int] = {}
        for entry in ledger_entries:
            if entry.id in seen_ids:
                seen_ids[entry.id] += 1
            else:
                seen_ids[entry.id] = 1
        for eid, count in seen_ids.items():
            if count > 1:
                errors.append(
                    LoomError(
                        code=VALIDATION_DUPLICATE_ID,
                        message=(
                            f"Duplicate {ledger_label} ledger ID {eid!r} "
                            f"(appears {count} times)"
                        ),
                        detail={
                            "id": eid,
                            "ledger": ledger_label,
                            "count": count,
                        },
                    )
                )


def _check_broken_references(
    state: ProjectState,
    errors: list[LoomError],
    warnings: list[LoomWarning],
    chunk_scope: str | None,
) -> None:
    """Check that chunk_id references point to existing chunks.

    Checks references in:
    - Decision entries (chunk_id field)
    - Thread entries (chunk_id field)
    - Distillate nodes (chunk_id field)
    - Distillate node key_decisions and open_threads (referenced IDs
      should exist in their respective ledgers)
    - Thread blocked_by references
    """
    # Build the set of known chunk IDs
    chunk_ids = set(state.chunks.keys())

    # Build the set of known ledger entry IDs
    decision_ids: set[str] = set()
    thread_ids: set[str] = set()
    question_ids: set[str] = set()

    if state.decisions_ledger:
        decision_ids = {e.id for e in state.decisions_ledger.entries}
    if state.threads_ledger:
        thread_ids = {e.id for e in state.threads_ledger.entries}
    if state.questions_ledger:
        question_ids = {e.id for e in state.questions_ledger.entries}

    # -- Decision entries: chunk_id references --------------------------------
    if state.decisions_ledger:
        for entry in state.decisions_ledger.entries:
            if not entry.chunk_id:
                continue  # Global scope, no chunk reference
            if chunk_scope and entry.chunk_id != chunk_scope:
                continue  # Out of scope
            if entry.chunk_id not in chunk_ids:
                errors.append(
                    LoomError(
                        code=VALIDATION_BROKEN_REFERENCE,
                        message=(
                            f"Decision {entry.id} references chunk "
                            f"{entry.chunk_id!r} which does not exist"
                        ),
                        detail={
                            "source_type": "decision",
                            "source_id": entry.id,
                            "target_type": "chunk",
                            "target_id": entry.chunk_id,
                        },
                    )
                )

    # -- Thread entries: chunk_id references ----------------------------------
    if state.threads_ledger:
        for entry in state.threads_ledger.entries:
            # chunk_id reference
            if entry.chunk_id:
                if chunk_scope and entry.chunk_id != chunk_scope:
                    pass  # Out of scope
                elif entry.chunk_id not in chunk_ids:
                    errors.append(
                        LoomError(
                            code=VALIDATION_BROKEN_REFERENCE,
                            message=(
                                f"Thread {entry.id} references chunk "
                                f"{entry.chunk_id!r} which does not exist"
                            ),
                            detail={
                                "source_type": "thread",
                                "source_id": entry.id,
                                "target_type": "chunk",
                                "target_id": entry.chunk_id,
                            },
                        )
                    )

            # blocked_by references
            for blocked_id in entry.blocked_by:
                if blocked_id not in thread_ids:
                    errors.append(
                        LoomError(
                            code=VALIDATION_BROKEN_REFERENCE,
                            message=(
                                f"Thread {entry.id} is blocked by "
                                f"{blocked_id!r} which does not exist in "
                                f"the threads ledger"
                            ),
                            detail={
                                "source_type": "thread",
                                "source_id": entry.id,
                                "target_type": "thread",
                                "target_id": blocked_id,
                            },
                        )
                    )

    # -- Distillate nodes: chunk_id references --------------------------------
    if state.distillate:
        for node in state.distillate.nodes:
            if chunk_scope and node.chunk_id != chunk_scope:
                continue  # Out of scope
            if node.chunk_id not in chunk_ids:
                errors.append(
                    LoomError(
                        code=VALIDATION_BROKEN_REFERENCE,
                        message=(
                            f"Distillate node references chunk "
                            f"{node.chunk_id!r} which does not exist"
                        ),
                        detail={
                            "source_type": "distillate_node",
                            "target_type": "chunk",
                            "target_id": node.chunk_id,
                        },
                    )
                )

            # key_decisions references
            for dec_id in node.key_decisions:
                if dec_id not in decision_ids:
                    errors.append(
                        LoomError(
                            code=VALIDATION_BROKEN_REFERENCE,
                            message=(
                                f"Distillate node for {node.chunk_id} references "
                                f"decision {dec_id!r} which does not exist"
                            ),
                            detail={
                                "source_type": "distillate_node",
                                "source_chunk": node.chunk_id,
                                "target_type": "decision",
                                "target_id": dec_id,
                            },
                        )
                    )

            # open_threads references
            for thread_id in node.open_threads:
                if thread_id not in thread_ids:
                    errors.append(
                        LoomError(
                            code=VALIDATION_BROKEN_REFERENCE,
                            message=(
                                f"Distillate node for {node.chunk_id} references "
                                f"thread {thread_id!r} which does not exist"
                            ),
                            detail={
                                "source_type": "distillate_node",
                                "source_chunk": node.chunk_id,
                                "target_type": "thread",
                                "target_id": thread_id,
                            },
                        )
                    )


def _check_checksums(
    state: ProjectState,
    warnings: list[LoomWarning],
    chunk_scope: str | None,
) -> None:
    """Check that prose checksums match the frontmatter-recorded values.

    A mismatch means the prose body has been edited without updating
    the frontmatter checksum.  This is a **warning**, not an error,
    because the user may have intentionally edited the file and not
    yet run reconcile.

    Checksum mismatches are **reported but never auto-fixed**.
    """
    for chunk_id, chunk_data in state.chunks.items():
        if chunk_scope and chunk_id != chunk_scope:
            continue  # Out of scope

        actual_checksum = compute_prose_checksum(chunk_data.prose_body)
        recorded_checksum = chunk_data.frontmatter.prose_checksum

        if actual_checksum != recorded_checksum:
            warnings.append(
                LoomWarning(
                    code=VALIDATION_DIRTY_CHECKSUM,
                    message=(
                        f"Chunk {chunk_id} has a dirty checksum: "
                        f"frontmatter records {recorded_checksum[:12]}... "
                        f"but actual prose hashes to {actual_checksum[:12]}..."
                    ),
                    detail={
                        "chunk_id": chunk_id,
                        "recorded_checksum": recorded_checksum,
                        "actual_checksum": actual_checksum,
                        "file": str(chunk_data.file_path),
                    },
                )
            )


def _check_chunk_order(
    state: ProjectState,
    errors: list[LoomError],
    warnings: list[LoomWarning],
) -> None:
    """Check that the manifest's chunk order matches chunks on disk.

    Detects:
    - Chunks listed in manifest order but not present on disk
    - Chunks on disk but not listed in manifest order
    """
    if state.manifest is None:
        return  # Can't check order without manifest

    manifest_order = set(state.manifest.chunks.order)
    disk_chunks = set(state.chunks.keys())

    # Chunks in manifest but not on disk
    missing_from_disk = manifest_order - disk_chunks
    for chunk_id in sorted(missing_from_disk):
        errors.append(
            LoomError(
                code=VALIDATION_CHUNK_ORDER_MISMATCH,
                message=(
                    f"Chunk {chunk_id!r} is listed in manifest order "
                    f"but no corresponding file exists on disk"
                ),
                detail={
                    "chunk_id": chunk_id,
                    "location": "manifest_order",
                    "problem": "missing_from_disk",
                },
            )
        )

    # Chunks on disk but not in manifest order
    # This is only a problem if the manifest has a non-empty order.
    # If the manifest order is empty, the fallback is expected and
    # already warned by chunk_order.py.
    if state.manifest.chunks.order:
        not_in_manifest = disk_chunks - manifest_order
        for chunk_id in sorted(not_in_manifest):
            warnings.append(
                LoomWarning(
                    code=VALIDATION_CHUNK_ORDER_MISMATCH,
                    message=(
                        f"Chunk {chunk_id!r} exists on disk but is not "
                        f"listed in manifest order"
                    ),
                    detail={
                        "chunk_id": chunk_id,
                        "location": "disk",
                        "problem": "not_in_manifest_order",
                    },
                )
            )


def _check_pending_reviews(
    state: ProjectState,
    warnings: list[LoomWarning],
    chunk_scope: str | None,
) -> None:
    """Report ledger entries with review_state=pending.

    Pending review items are **warnings**, not errors.  They indicate
    that human attention is needed but the data is structurally valid.
    """
    pending_items: list[dict[str, str]] = []

    if state.decisions_ledger:
        for entry in state.decisions_ledger.entries:
            if entry.review_state == ReviewState.PENDING:
                if chunk_scope and entry.chunk_id and entry.chunk_id != chunk_scope:
                    continue
                pending_items.append({
                    "type": "decision",
                    "id": entry.id,
                    "chunk_id": entry.chunk_id,
                })

    if state.threads_ledger:
        for entry in state.threads_ledger.entries:
            if entry.review_state == ReviewState.PENDING:
                if chunk_scope and entry.chunk_id and entry.chunk_id != chunk_scope:
                    continue
                pending_items.append({
                    "type": "thread",
                    "id": entry.id,
                    "chunk_id": entry.chunk_id,
                })

    if state.questions_ledger:
        for entry in state.questions_ledger.entries:
            if entry.review_state == ReviewState.PENDING:
                pending_items.append({
                    "type": "question",
                    "id": entry.id,
                })

    if pending_items:
        ids = [f"{item['type']}:{item['id']}" for item in pending_items]
        warnings.append(
            LoomWarning(
                code=VALIDATION_PENDING_REVIEW,
                message=(
                    f"{len(pending_items)} ledger entries pending review: "
                    f"{', '.join(ids[:10])}"
                    + ("..." if len(ids) > 10 else "")
                ),
                detail={
                    "count": len(pending_items),
                    "items": pending_items[:20],
                },
            )
        )
