"""Minimal Markdown concatenator (draft build) for AIP_Loom.

This module is the **single authority** for building draft output from
an AIP_Loom project.  It concatenates ordered chunk prose bodies into
a single Markdown file, stripping frontmatter and respecting the
canonical chunk order from :func:`resolve_chunk_order`.

Design principles (Chunk 16 description):

- **Reuse, don't re-implement**: Chunk ordering comes from
  ``ProjectState.chunk_order`` (itself from :mod:`chunk_order`).
  Frontmatter stripping uses :mod:`frontmatter`.  Project loading
  uses :func:`load_project`.  Validation uses :func:`validate_project`.
- **Deterministic output**: Given the same project state and chunk
  order, the output Markdown is byte-for-byte identical.
- **Fail cleanly on critical validation errors**: If
  :func:`validate_project` reports errors, the build aborts with a
  clear ``BUILD_VALIDATION_FAILED`` error rather than producing
  corrupt output.
- **Deliberately minimal**: Only ``--mode draft --format md`` is
  supported.  DOCX and PDF are explicitly unsupported and produce
  ``BUILD_FORMAT_UNSUPPORTED`` errors.
- **Build report**: Every build produces a report listing included
  chunks, skipped chunks, total word count, and any warnings.
- **No silent behaviour**: Missing chunks, empty projects, and
  fallback ordering all produce warnings that are surfaced to the
  caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import (
    BUILD_FORMAT_UNSUPPORTED,
    BUILD_MODE_UNSUPPORTED,
    BUILD_NO_CHUNKS,
    BUILD_VALIDATION_FAILED,
    BUILD_CHUNK_SKIPPED,
    FILE_WRITE_ERROR,
    LoomError,
    LoomWarning,
)
from .project import ProjectError, ProjectState, load_project, validate_project
from .results import CommandResult

__all__ = [
    "BuildError",
    "BuildResult",
    "build_draft_md",
    "run_build",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BuildError(Exception):
    """Raised when a build operation fails critically.

    Carries a :class:`LoomError` with a stable error code.
    """

    def __init__(self, loom_error: LoomError) -> None:
        self.loom_error = loom_error
        super().__init__(loom_error.message)

# ---------------------------------------------------------------------------
# Supported values
# ---------------------------------------------------------------------------

#: Build modes currently supported.
SUPPORTED_MODES = {"draft"}

#: Output formats currently supported.
SUPPORTED_FORMATS = {"md"}

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildResult:
    """The result of a draft build operation.

    Attributes
    ----------
    output_path:
        The path where the concatenated Markdown was written.
    included_chunks:
        The chunk IDs that were included in the output, in order.
    skipped_chunk_ids:
        The chunk IDs that were in the resolved order but could not
        be found in the project's loaded chunks (should be rare).
    total_word_count:
        The total word count across all included chunk prose bodies.
    total_char_count:
        The total character count of the output Markdown.
    used_manifest_order:
        Whether the manifest's ``chunks.order`` was used for ordering.
    """

    output_path: Path
    included_chunks: list[str]
    skipped_chunk_ids: list[str]
    total_word_count: int
    total_char_count: int
    used_manifest_order: bool


# ---------------------------------------------------------------------------
# Service logic — build_draft_md
# ---------------------------------------------------------------------------


def build_draft_md(
    state: ProjectState,
    output_path: Path,
) -> BuildResult:
    """Concatenate ordered chunk prose bodies into a single Markdown file.

    This function:

    1. Resolves the canonical chunk order from ``state.chunk_order``
       (which was produced by :func:`resolve_chunk_order` during
       :func:`load_project`).
    2. For each chunk ID in order, retrieves the prose body from
       ``state.chunks`` (frontmatter is already stripped by
       :func:`load_project` via :func:`parse_frontmatter`).
    3. Concatenates all prose bodies with a separator.
    4. Writes the result to *output_path*.
    5. Returns a :class:`BuildResult` with the build report.

    Parameters
    ----------
    state:
        The loaded project state from :func:`load_project`.
    output_path:
        The path to write the concatenated Markdown file to.

    Returns
    -------
    BuildResult
        A frozen dataclass with the build report.

    Raises
    ------
    LoomError
        If there are no chunks to build (``BUILD_NO_CHUNKS``).
    """
    # 1. Resolve chunk order
    ordered_ids: list[str] = []
    used_manifest_order = False

    if state.chunk_order is not None:
        ordered_ids = state.chunk_order.ordered_ids
        used_manifest_order = state.chunk_order.used_manifest_order
    else:
        # No chunk_order resolved (manifest was None) — use chunks dict keys
        # sorted naturally as a last resort.
        from .chunk_order import natural_sort_key

        ordered_ids = sorted(state.chunks.keys(), key=natural_sort_key)
        used_manifest_order = False

    # 2. Build prose sections
    included_chunks: list[str] = []
    skipped_chunk_ids: list[str] = []
    prose_sections: list[str] = []
    total_word_count = 0

    for chunk_id in ordered_ids:
        chunk_data = state.chunks.get(chunk_id)
        if chunk_data is None:
            # Chunk ID is in the order but not in loaded chunks.
            # This can happen if a chunk file is malformed or missing.
            skipped_chunk_ids.append(chunk_id)
            continue

        prose_body = chunk_data.prose_body

        # Strip leading/trailing whitespace from the prose body to ensure
        # clean concatenation, but preserve internal structure.
        prose_body = prose_body.strip()

        # Count words in the prose body
        word_count = len(prose_body.split())
        total_word_count += word_count

        # Add a section separator with the chunk ID as a comment
        section = f"<!-- {chunk_id} -->\n\n{prose_body}"
        prose_sections.append(section)
        included_chunks.append(chunk_id)

    # 3. Check for empty output
    if not included_chunks:
        raise BuildError(
            LoomError(
                code=BUILD_NO_CHUNKS,
                message="No chunks to build — the project has no loadable chunks",
                detail={"ordered_ids": ordered_ids, "skipped": skipped_chunk_ids},
            )
        )

    # 4. Concatenate with double-newline separator
    output_content = "\n\n".join(prose_sections)

    # Ensure the output ends with a trailing newline (deterministic)
    if not output_content.endswith("\n"):
        output_content += "\n"

    total_char_count = len(output_content)

    # 5. Write the output file
    # Ensure parent directory exists
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        output_path.write_text(output_content, encoding="utf-8")
    except OSError as exc:
        raise BuildError(
            LoomError(
                code=FILE_WRITE_ERROR,
                message=f"Cannot write build output to {output_path}: {exc}",
                detail={"path": str(output_path), "error": str(exc)},
            )
        ) from exc

    return BuildResult(
        output_path=output_path,
        included_chunks=included_chunks,
        skipped_chunk_ids=skipped_chunk_ids,
        total_word_count=total_word_count,
        total_char_count=total_char_count,
        used_manifest_order=used_manifest_order,
    )


# ---------------------------------------------------------------------------
# Top-level build runner (called by CLI)
# ---------------------------------------------------------------------------


def run_build(
    mode: str,
    fmt: str,
    output_path: str | None,
) -> CommandResult:
    """Run a build command and return a :class:`CommandResult`.

    This is the top-level entry point called by the thin CLI handler.
    It validates arguments, loads the project, runs validation, and
    delegates to the appropriate build function.

    Parameters
    ----------
    mode:
        The build mode (e.g. ``"draft"``).
    fmt:
        The output format (e.g. ``"md"``).
    output_path:
        The output file path, or ``None`` to use the default.

    Returns
    -------
    CommandResult
        The universal response envelope.
    """
    root = Path.cwd()

    # -- Validate mode -------------------------------------------------------
    if mode not in SUPPORTED_MODES:
        return CommandResult.failure(
            command="build",
            code=BUILD_MODE_UNSUPPORTED,
            message=(
                f"Build mode {mode!r} is not supported.  "
                f"Supported modes: {sorted(SUPPORTED_MODES)}"
            ),
            data={"requested_mode": mode, "supported_modes": sorted(SUPPORTED_MODES)},
        )

    # -- Validate format -----------------------------------------------------
    if fmt not in SUPPORTED_FORMATS:
        return CommandResult.failure(
            command="build",
            code=BUILD_FORMAT_UNSUPPORTED,
            message=(
                f"Output format {fmt!r} is not supported.  "
                f"Supported formats: {sorted(SUPPORTED_FORMATS)}.  "
                f"DOCX and PDF are explicitly unsupported — use an external "
                f"converter on the Markdown output."
            ),
            data={"requested_format": fmt, "supported_formats": sorted(SUPPORTED_FORMATS)},
        )

    # -- Load project --------------------------------------------------------
    try:
        state = load_project(root)
    except ProjectError as exc:
        return CommandResult.failure(
            command="build",
            code=exc.loom_error.code,
            message=exc.loom_error.message,
            errors=[exc.loom_error],
        )

    # -- Validate project ----------------------------------------------------
    validation = validate_project(state)

    if not validation.ok:
        # Critical validation errors — abort build
        return CommandResult.failure(
            command="build",
            code=BUILD_VALIDATION_FAILED,
            message=(
                f"Build aborted: project validation failed with "
                f"{len(validation.errors)} error(s).  Fix validation errors "
                f"before building."
            ),
            errors=list(validation.errors),
            warnings=list(validation.warnings),
        )

    # -- Collect warnings from project state and validation ------------------
    all_warnings: list[LoomWarning] = list(state.load_warnings)
    all_warnings.extend(validation.warnings)

    # -- Resolve output path -------------------------------------------------
    if output_path is not None:
        resolved_output = Path(output_path).resolve()
    else:
        # Default: <project_root>/build/draft.md
        resolved_output = state.layout.root / "build" / "draft.md"

    # -- Execute build -------------------------------------------------------
    try:
        result = build_draft_md(state, resolved_output)
    except BuildError as exc:
        return CommandResult.failure(
            command="build",
            code=exc.loom_error.code,
            message=exc.loom_error.message,
            errors=[exc.loom_error],
            warnings=all_warnings,
        )

    # -- Add skipped chunk warnings ------------------------------------------
    for chunk_id in result.skipped_chunk_ids:
        all_warnings.append(
            LoomWarning(
                code=BUILD_CHUNK_SKIPPED,
                message=(
                    f"Chunk {chunk_id!r} is in the resolved order but could "
                    f"not be loaded — skipped from build output"
                ),
                detail={"chunk_id": chunk_id},
            )
        )

    # -- Build report data ---------------------------------------------------
    data: dict[str, Any] = {
        "output_path": str(result.output_path),
        "mode": mode,
        "format": fmt,
        "included_chunks": result.included_chunks,
        "chunk_count": len(result.included_chunks),
        "skipped_chunks": result.skipped_chunk_ids,
        "total_word_count": result.total_word_count,
        "total_char_count": result.total_char_count,
        "used_manifest_order": result.used_manifest_order,
    }

    message = (
        f"Draft build complete: {len(result.included_chunks)} chunk(s), "
        f"~{result.total_word_count} words → {result.output_path}"
    )

    return CommandResult.success(
        command="build",
        message=message,
        data=data,
        warnings=all_warnings,
    )
