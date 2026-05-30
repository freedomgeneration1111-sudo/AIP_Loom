# AIP_Loom Pattern Registry

This registry is the single source of truth for reusable implementation
patterns in AIP_Loom.  Every chunk after Chunk 01 must read this registry
before implementation and must not create duplicate implementations of
patterns registered here.

## Error and Result Handling
- Shared envelope: src/aip_loom/results.py *(implemented — Chunk 01)*
- Error taxonomy: src/aip_loom/errors.py *(implemented — Chunk 01)*
- Renderer: src/aip_loom/output.py *(implemented — Chunk 01)*

## YAML and Schema Handling
- **YAML IO: src/aip_loom/yaml_io.py** *(implemented — Chunk 03)*
  - This is the **single, exclusive gateway** for all YAML reading and writing
    in AIP_Loom.  No other module may import or call ``ruamel.yaml`` (or any
    YAML library such as PyYAML) directly.  Direct ruamel/PyYAML imports
    elsewhere are a **spec violation** unless explicitly justified and
    registered here.
  - Provides: ``load_yaml()``, ``load_yaml_string()``, ``load_yaml_as()``,
    ``load_yaml_string_as()``, ``dump_yaml()``, ``dump_yaml_string()``.
  - Two strictness modes via ``YamlMode``: ``PROJECT`` (lenient on anchors)
    and ``UPDATE_BLOCK`` (strict: anchors/aliases/tags are hard errors).
  - Duplicate key detection via ruamel.yaml's ``DuplicateKeyError``.
  - Anchor/alias/tag scanning with ``_has_real_anchor()``, ``_scan_for_anchors_aliases()``,
    ``_scan_for_tags()`` — only real anchors (``anchor.value is not None``)
    are flagged, not the default ``Anchor(None)`` ruamel.yaml attaches.
  - Pydantic bridge: ``load_yaml_as()`` converts ruamel.yaml types to plain
    Python via ``_convert_to_plain()`` (handles CommentedMap, CommentedSeq,
    TaggedScalar, ScalarString) then validates against a Pydantic model.
  - All failures raise ``YamlLoadError`` with a ``LoomError`` carrying stable
    error codes (YAML_PARSE_ERROR, YAML_DUPLICATE_KEYS, YAML_ANCHORS_ALIASES,
    YAML_TAGS_REJECTED, SCHEMA_VALIDATION_FAILED, FILE_NOT_FOUND, etc.).
  - **Honest failure guarantee**: malformed/empty YAML never returns empty
    dict, empty list, or fabricated default state.
  - Round-trip preservation: comments, blank lines, and key order are preserved
    on human-editable files via ruamel.yaml round-trip mode.
- **Pydantic schemas: src/aip_loom/schemas.py** *(implemented — Chunk 02)*
  - This is the **single owner** of all Pydantic models. No other module may
    define private schema variants. If a command needs a new shape, it must
    be added here and tests must cover it. Duplicate private Pydantic models
    elsewhere are a spec violation.
  - Contains: enums (ReviewState, ProjectType, ChunkStatus, UpdateMode,
    ThreadState), schema version validation, ChunkFrontmatter, LedgerEntryBase
    + DecisionEntry/ThreadEntry/QuestionEntry, Ledger files (DecisionLedger,
    ThreadLedger, QuestionLedger), DistillateNode + Distillate, SessionEntry +
    SessionLog, CommentEntry + CommentLog, ProjectManifest, UpdateBlock +
    update item models.

## Filesystem and Transactions

**Mandatory rule (Chunk 05):** All filesystem path construction, file
writing, and locking **must** go through these three modules only.
Any ad-hoc ``pathlib`` composition from IDs, direct ``open(..., 'w')``,
or custom locking elsewhere in the codebase is a **spec violation**.

- **Project layout: src/aip_loom/layout.py** *(implemented — Chunk 05)*
  - This is the **single authority** for resolving every canonical path
    in an AIP_Loom project.  No other module may construct paths from
    IDs, filenames, or model-provided strings.
  - Provides: ``ProjectLayout(root)`` (frozen dataclass), ``LayoutError``.
  - Properties: ``manifest_path``, ``distillate_path``, ``sessions_path``,
    ``comments_path``, ``chunks_dir``, ``ledgers_dir``, ``archive_dir``,
    ``aip_loom_dir``, ``staging_dir``, ``decisions_ledger_path``,
    ``threads_ledger_path``, ``questions_ledger_path``, ``lock_path``.
  - Methods: ``chunk_path(chunk_id)`` (validates ID against schema regex
    before path construction), ``archive_chunk_path(chunk_id)``,
    ``validate_path(path)`` (rejects ``..``, symlinks escaping root,
    resolved paths outside root), ``is_project_initialized()``.
  - **ID validation before path construction**: ``chunk_path()`` validates
    the chunk ID against ``_CHUNK_ID_RE`` before building any path.
    This prevents path traversal through malformed IDs.
  - **Path safety**: ``validate_path()`` checks resolved paths stay within
    root, rejects ``..`` components, and checks symlink targets.
  - **Root must exist**: Constructor requires an existing directory.

- **Atomic write: src/aip_loom/fs.py** *(implemented — Chunk 05)*
  - This is the **single authority** for all file writing.  No other
    module may perform direct file writes (``open(..., 'w')`` or
    ``pathlib.Path.write_text()``).  All writes must go through here.
  - Provides: ``atomic_write(target, layout)`` (context manager),
    ``safe_write_text(target, content, layout)``,
    ``safe_write_bytes(target, content, layout)``,
    ``ensure_directory(path)``, ``AtomicWriteError``.
  - **Atomic write protocol**: write to temp file in same directory →
    fsync temp file → fsync parent directory → ``os.replace`` →
    fsync parent directory again.  Same-directory temp file ensures
    ``os.replace`` is atomic (same filesystem).
  - **Cleanup on failure**: If writing or replacing fails, the temp file
    is removed and the original file is left untouched.
  - **Path safety**: All write functions validate the target path against
    the project layout before writing.
  - **fsync discipline**: Both file and directory are fsynced.  Directory
    fsync is best-effort (some filesystems don't support it).

- **Locking: src/aip_loom/lock.py** *(implemented — Chunk 05)*
  - This is the **single authority** for exclusive project locking.
    No other module may implement its own locking mechanism.
  - Provides: ``ProjectLock(layout, command)`` (context manager),
    ``acquire_lock(layout, command)`` (convenience context manager),
    ``LockError``, ``LockInfo`` (frozen dataclass).
  - **Exclusive create**: Uses ``O_CREAT | O_EXCL`` for atomic lock
    file creation.  If the file already exists, ``LOCK_HELD`` is raised.
  - **PID liveness**: On POSIX, uses ``os.kill(pid, 0)`` to check if
    the lock-holding process is alive.  On other platforms, returns
    ``None`` (uncertain) and treats the lock as potentially held.
  - **Stale lock detection**: Reports PID, command, age, and liveness
    status.  Includes recovery instructions in the error message.
    Stale locks emit ``STALE_LOCK_DETECTED`` warning and raise
    ``LOCK_STALE`` error code.
  - **No silent deletion**: Stale locks are never deleted automatically.
    The caller must explicitly call ``force_release()`` after reviewing
    diagnostics.
  - **Lock file format**: ``<pid>:<command>`` (e.g. ``12345:reconcile``).

- **Transaction workspace: src/aip_loom/transaction.py** *(implemented — Chunk 07)*
  - This is the **single authority** for transactional file operations —
    staging, snapshotting, restoring, and cleaning up.  Reconcile (Chunk 15)
    and any other operation that modifies canonical state **must** use these
    primitives rather than implementing local snapshot/restore behaviour.
  - **Mandatory rule**: Any module that needs to modify canonical files with
    rollback capability **must** use ``TransactionWorkspace`` for snapshots
    and restores.  Implementing ad-hoc backup/restore logic elsewhere is a
    **spec violation**.
  - Provides: ``TransactionWorkspace(layout, failure_injector)``,
    ``TransactionError``, ``TransactionStatus`` (enum),
    ``SnapshotEntry`` (frozen dataclass), ``TransactionManifest`` (frozen dataclass),
    ``FailureInjector`` (protocol), ``NoopFailureInjector``.
  - **Semantics-agnostic**: Only stages, snapshots, restores, and cleans up.
    Does **not** know about reconcile, update blocks, model output, or
    chunk semantics.
  - **Snapshot before modify**: ``snapshot_file(canonical_path)`` copies the
    file to the workspace ``snapshots/`` directory and records a SHA-256
    hash for restore verification.
  - **Hash verification on restore**: ``restore()`` verifies the snapshot
    content hash matches the recorded hash.  Mismatches produce
    ``TX_HASH_MISMATCH`` and abort the restore.
  - **Non-existent file tracking**: If a file does not exist at snapshot
    time, ``existed=False`` is recorded.  On restore, such files are
    deleted (if created during the transaction).
  - **No evidence destruction**: On restore failure, the workspace is
    **not** deleted.  Status is set to ``FAILED`` and the workspace is
    preserved for forensic analysis.
  - **Failure injection**: ``FailureInjector`` protocol allows tests to
    inject failures at snapshot, stage, restore, and commit stages.
    ``NoopFailureInjector`` is the production default.
  - **Workspace layout**: ``.aip-loom/tmp/<txid>/`` with ``manifest.json``,
    ``staged/``, and ``snapshots/`` subdirectories.
  - **Manifest**: ``manifest.json`` records ``tx_id``, ``created_at``,
    ``status``, and ``snapshots`` (list of entries).  Written on every
    status change.
  - **Idempotent snapshot**: Calling ``snapshot_file()`` on the same path
    twice returns the existing entry without re-copying.
  - Error codes used: ``TX_ALREADY_ACTIVE``, ``TX_NOT_ACTIVE``,
    ``TX_SNAPSHOT_FAILED``, ``TX_RESTORE_FAILED``,
    ``TX_FILE_NOT_SNAPSHOTTED``, ``TX_HASH_MISMATCH``,
    ``PATH_UNSAFE``, ``CHECKSUM_MISMATCH``.

## Git

**Mandatory rule (Chunk 06):** All Git operations in AIP_Loom **must**
go through this module only.  Direct ``subprocess.run(['git', ...])``
calls or GitPython usage elsewhere in the codebase is a **spec
violation** unless explicitly justified and registered here.

- **Git wrapper: src/aip_loom/git.py** *(implemented — Chunk 06)*
  - This is the **single authority** for all Git operations.  No other
    module may call ``subprocess.run(['git', ...])`` directly or use
    GitPython.
  - Provides: ``is_git_repo(root)``, ``git_status(root)`` → ``GitStatus``,
    ``is_git_clean(root)``, ``git_add(root, paths)``,
    ``git_commit(root, message, allow_empty=False)``,
    ``configure_local_git(root, user_name, user_email)``, ``GitError``,
    ``GitStatus`` (frozen dataclass).
  - **subprocess only**: Uses ``subprocess.run`` exclusively.  No GitPython,
    no libgit2 bindings.
  - **Capture both streams**: Every Git invocation captures both stdout
    and stderr.  Stderr is never silently discarded.
  - **Never hide errors**: Non-zero exit codes raise ``GitError`` with
    full stderr content.  Pre-commit hook failures are not suppressed.
  - **Binary check**: ``_find_git_binary()`` verifies ``git`` is on PATH
    before any command.  Missing binary produces ``GIT_BINARY_MISSING``.
  - **GitStatus structured result**: ``git_status()`` returns a frozen
    dataclass with ``is_repo``, ``clean``, ``staged``, ``unstaged``,
    ``untracked``, and ``raw`` fields.  No raw string parsing needed
    by callers.
  - **Commit failure is real failure**: ``git_commit()`` surfaces stderr
    on failure.  Does not auto-restore writer data or treat failed
    commits as no-ops.
  - **Local test config**: ``configure_local_git()`` sets ``user.name``
    and ``user.email`` in the repo-local config so tests do not depend
    on global ``~/.gitconfig``.
  - **Porcelain parsing**: ``_parse_porcelain()`` parses ``git status
    --porcelain`` output into staged/unstaged/untracked categories.
  - Error codes used: ``GIT_NOT_REPO``, ``GIT_DIRTY``, ``GIT_COMMIT_FAILED``,
    ``GIT_BINARY_MISSING`` (all defined in ``errors.py`` since Chunk 01).

## Validation

**Mandatory rule (Chunk 09):** Project loading and validation **must** go
through `project.py` only.  No other module may read and assemble the
project state independently.  Validation must be side-effect-free.

- **Project loader: src/aip_loom/project.py** *(implemented — Chunk 09)*
  - This is the **single authority** for loading an entire AIP_Loom
    project into memory and validating its structural and semantic
    integrity.  No other module may read and assemble the project state
    independently — it must delegate to :func:`load_project` here.
  - Provides: ``load_project(root)`` → ``ProjectState``,
    ``validate_project(state, chunk_scope)`` → ``ValidationResult``,
    ``ProjectError``, ``ProjectState``, ``ChunkData``,
    ``ValidationResult``.
  - **Single loading authority**: ``load_project`` is the only function
    that reads all canonical files, parses them, and assembles a
    coherent ``ProjectState``.  Every downstream command (status, brief,
    reconcile, inspect) uses this single loader.
  - **Honest partial loading**: If some files are malformed or missing,
    ``load_project`` still returns a ``ProjectState`` with
    ``load_errors`` populated.  It never fabricates default state to
    hide failures.  A missing ledger is an error (``None`` field), not
    an empty ledger with zero entries.
  - **Validation is pure**: ``validate_project`` performs side-effect-free
    integrity checks.  It **never** repairs, auto-fixes, or writes
    files.  Dirty checksums are reported, not corrected.  Broken
    references are flagged, not patched.
  - **Validation passes**: (1) missing required files, (2) schema
    violations (captured during loading), (3) duplicate IDs across
    chunks and ledgers, (4) broken references (chunk_id, blocked_by,
    distillate node references), (5) checksum mismatches (dirty files),
    (6) chunk order issues (manifest vs disk), (7) pending review items.
  - **Chunk scoping**: Validation supports ``chunk_scope`` parameter so
    that a single chunk can be checked in isolation.
  - **Pending review reporting**: Ledger entries with
    ``review_state=pending`` are reported as warnings (not errors).
  - **Uses existing modules**: Loading and validation use
    ``yaml_io.py``, ``schemas.py``, ``layout.py``, ``checksum.py``,
    ``frontmatter.py``, ``ids.py``, and ``chunk_order.py``.  No ad-hoc
    YAML parsing, checksum computation, or ID extraction elsewhere.
  - Error codes used: ``VALIDATION_DUPLICATE_ID``,
    ``VALIDATION_BROKEN_REFERENCE``, ``VALIDATION_MISSING_FILE``,
    ``VALIDATION_CHUNK_ORDER_MISMATCH``, ``PROJECT_NOT_FOUND``,
    ``YAML_PARSE_ERROR``, ``SCHEMA_VALIDATION_FAILED``,
    ``FILE_NOT_FOUND``, ``ID_DUPLICATE``.
  - Warning codes used: ``VALIDATION_DIRTY_CHECKSUM``,
    ``VALIDATION_PENDING_REVIEW``,
    ``CHUNK_ORDER_FALLBACK_WARNING``, ``CHUNK_ORDER_FALLBACK_USED``.
  - **CLI integration**: The ``validate`` CLI command delegates to
    ``load_project`` + ``validate_project`` via ``_run_validate()``.
    Supports ``--chunk`` (scope) and ``--json`` output.

## IDs, Checksums, and Chunk Order

**Mandatory rule (Chunk 04):** ID allocation, checksum calculation,
frontmatter parsing, and chunk order resolution **must** go through
these four modules only.  Any ad-hoc ID computation, checksum hashing,
frontmatter regex, or chunk sorting elsewhere in the codebase is a
**spec violation**.

- **ID allocator: src/aip_loom/ids.py** *(implemented — Chunk 04)*
  - This is the **single authority** for allocating new sequential IDs.
    No other module may compute or guess the next available ID.
  - Provides: ``allocate_next_id(prefix, entries, id_attr="id")``,
    ``extract_id_number(id_str)``, ``KNOWN_PREFIXES``.
  - **Canonical-only rule**: The allocator reads *only* from validated
    Pydantic model instances (canonical ledger state).  It must **never**
    read from staged, archive, or unvalidated sources.  Violating this
    rule can produce ID collisions from uncommitted or rolled-back state.
  - **No gap-filling**: If D-0001 and D-0003 exist, the next ID is
    D-0004, not D-0002.  Gap-filling introduces ordering confusion.
  - **Prefix-scoped**: Each prefix (C, CH, D, T, Q, S, CM) has its own
    independent sequence.
  - Raises ``DuplicateIdError`` (code ``ID_DUPLICATE``) if duplicate IDs
    are found for the same prefix.
  - Raises ``InvalidIdError`` (code ``CHUNK_ID_INVALID``) if an ID
    starts with the expected prefix but doesn't match the full pattern,
    or if an unknown prefix is used.
  - Empty entry list → first ID is ``{prefix}-0001``.

- **Checksum: src/aip_loom/checksum.py** *(implemented — Chunk 04)*
  - This is the **single authority** for computing prose-body checksums.
    No other module may compute its own checksum.
  - Provides: ``compute_prose_checksum(prose)``, ``CHECKSUM_ALGORITHM``.
  - **Prose body only**: The checksum covers only the prose content
    below the YAML frontmatter.  Frontmatter changes must not trigger
    checksum mismatches.
  - **LF normalization**: CRLF and bare CR are replaced with LF before
    hashing.  Same prose → same checksum regardless of platform.
  - **Trailing newline stripped**: A single trailing newline (if present)
    is removed before hashing to avoid spurious mismatches from editor
    newline handling.
  - **No silent updates**: This module never writes to files or updates
    schemas.  Checksums are computed on demand and returned as hex
    strings.  Only explicit write operations may store them.
  - Algorithm: SHA-256.

- **Frontmatter: src/aip_loom/frontmatter.py** *(implemented — Chunk 04)*
  - This is the **single authority** for parsing and writing Markdown
    YAML frontmatter.  No other module may use ad-hoc regex or string
    splitting to extract frontmatter.
  - Provides: ``parse_frontmatter(text)`` → ``FrontmatterParseResult``,
    ``write_frontmatter(frontmatter, prose_body)`` → ``str``,
    ``split_frontmatter(text)`` → ``(yaml_str, prose_body)``.
  - **No filename inference**: When frontmatter exists, the chunk ID is
    taken from the frontmatter, never inferred from the filename.  This
    module explicitly returns the frontmatter-parsed ID via
    ``FrontmatterParseResult.frontmatter.id``.
  - Uses ``yaml_io.load_yaml_string_as`` for YAML parsing (single-gateway
    principle) and ``yaml_io.dump_yaml_string`` for YAML serialization.
  - Raises ``FrontmatterParseError`` (carrying ``LoomError``) on:
    missing opening delimiter, missing closing delimiter, empty
    frontmatter, or YAML validation failure.

- **Chunk order: src/aip_loom/chunk_order.py** *(implemented — Chunk 04)*
  - This is the **single authority** for determining the canonical
    ordering of chunks.  No other module may sort chunks independently.
  - Provides: ``resolve_chunk_order(manifest, chunk_ids)``
    → ``ChunkOrderResult``, ``natural_sort_key(s)``.
  - **Manifest-respected**: If ``chunks.order`` is non-empty, that order
    is canonical.  Chunks not in the manifest order are appended at the
    end in natural sort order (with a warning).
  - **Filename fallback with warning**: If ``chunks.order`` is empty or
    missing, chunks are sorted by natural sort and a
    ``CHUNK_ORDER_FALLBACK_USED`` warning is emitted.
  - **No silent ordering**: The caller always receives both the ordered
    list and any warnings.  Silent fallback is forbidden.
  - ``ChunkOrderResult`` is frozen and includes ``used_manifest_order``
    flag for downstream logic.

## Status

**Mandatory rule (Chunk 10):** Project status computation **must** go
through `status.py` only.  No other module may independently compute
and present project status.  Status must be honest — never fabricate
zero counts or hide problems.

- **Status service: src/aip_loom/status.py** *(implemented — Chunk 10)*
  - This is the **single authority** for computing a project's overall
    status.  No other module may independently compute and present
    project status — it must delegate to :func:`compute_status` here.
  - Provides: ``compute_status(root)`` → ``StatusReport``,
    ``HealthLevel`` (enum), ``StatusReport`` (frozen dataclass),
    ``ChunkStatusSummary``, ``LedgerStatusSummary``,
    ``GitStatusSummary``, ``LockStatusSummary``.
  - **Honest above all**: Status never fabricates zero counts or hides
    problems.  If the project cannot be loaded, the report reflects
    that failure rather than pretending everything is fine.
  - **Single data structure**: ``StatusReport`` is the same structure
    used for Rich terminal output and JSON output.  No divergent logic.
  - **Compose, don't duplicate**: Status reuses ``load_project()``,
    ``validate_project()``, ``git_status()``, and lock detection
    rather than computing state independently.
  - **Health classification**: ``HealthLevel`` enum with three levels:
    ``HEALTHY`` (no errors, no warnings requiring action),
    ``DEGRADED`` (warnings but no errors — e.g. pending reviews,
    dirty checksums), ``BLOCKED`` (structural errors — missing files,
    broken references, load failures, stale locks, recovery files).
  - **Recovery awareness**: If ``RECOVERY.md`` exists (left after a
    Git commit failure during reconcile), it is surfaced as a
    ``RECOVERY_FILE_EXISTS`` warning and the health is ``BLOCKED``.
  - **Stale lock detection**: Stale locks (dead PID) are reported
    with ``STALE_LOCK_DETECTED`` warning and the health is ``BLOCKED``.
  - **Next actions**: The report includes suggested next actions
    based on the current state, ordered by priority.
  - **Lock state**: Uses ``_read_lock_file()`` from ``lock.py`` to
    detect and parse the lock file.  Stale lock detection uses PID
    liveness checking.
  - **Git branch**: Reports current branch name via ``git branch
    --show-current``.
  - **to_dict()**: ``StatusReport.to_dict()`` produces a complete
    JSON-serialisable dictionary used by the ``--json`` CLI output.
  - **CLI integration**: The ``status`` CLI command delegates to
    ``compute_status()`` via ``_run_status()``.  Supports ``--json``
    output.  HEALTHY and DEGRADED return exit code 0; BLOCKED returns
    exit code 1.
  - Error codes used: ``PROJECT_MALFORMED`` (for blocked status).
  - Warning codes used: ``RECOVERY_FILE_EXISTS``, ``STALE_LOCK_DETECTED``
    (plus all validation warning codes from the ``ValidationResult``).

## Brief / Context Selection

**Mandatory rule (Chunk 11):** Context selection for both `inspect` and
`brief` **must** go through `brief_context.py` only.  No other module may
implement its own context selection logic.  Token estimation **must** go
through `tokens.py` only.  Duplicating selection or estimation code
elsewhere is a **spec violation**.

- **Brief context engine: src/aip_loom/brief_context.py** *(implemented — Chunk 11)*
  - This is the **single authority** for selecting the context that
    ``brief`` would assemble for a given chunk.  Both ``inspect`` and
    ``brief`` must call :func:`select_context` here — no other module
    may implement its own context selection logic.  This prevents the
    primary anti-pattern of parallel implementations diverging over time.
  - Provides: ``select_context(state, chunk_id, token_budget)``
    → ``SelectedContext``, ``ContextSection``, ``SelectedContext``,
    ``DEFAULT_TOKEN_BUDGET``.
  - **Shared logic**: ``inspect`` and ``brief`` use the **same** context
    selection function.  ``inspect`` is a read-only preview of what
    ``brief`` would select; it must never duplicate the selection code.
  - **Priority-based selection**: Sections are ordered by priority:
    0 = chunk frontmatter (mandatory), 1 = chunk prose (mandatory),
    2 = distillate node, 3 = scoped decisions, 4 = scoped threads,
    5 = adjacent summaries, 6 = global decisions, 7 = global threads,
    8 = unresolved questions.  Mandatory sections are never dropped.
  - **Token budget aware**: Sections that would overflow the budget are
    dropped from lowest priority upward.  Dropped sections are reported
    in ``dropped_sections``.  Budget overflow emits a
    ``BRIEF_BUDGET_OVERFLOW`` warning.
  - **Honest about gaps**: If a scoped ledger is missing or malformed,
    a ``BRIEF_ORPHAN_CHUNK`` warning is emitted — never silently skipped.
  - **No file writes**: This module is pure computation.  It never writes
    to disk, even when used by ``brief``.
  - **Deterministic**: Given the same project state and chunk ID, the
    output is always the same.
  - **to_dict()**: ``SelectedContext.to_dict()`` produces a complete
    JSON-serialisable dictionary used by the ``--json`` CLI output.
  - Error codes used: ``CHUNK_NOT_FOUND``.
  - Warning codes used: ``BRIEF_BUDGET_OVERFLOW``, ``BRIEF_ORPHAN_CHUNK``,
    ``TOKEN_COUNT_APPROXIMATE``.

- **Token estimation: src/aip_loom/tokens.py** *(implemented — Chunk 11)*
  - This is the **single authority** for estimating token counts across
    all AIP_Loom commands.  Both ``inspect`` and ``brief`` must use
    :func:`estimate_text_tokens` here — no other module may implement
    its own token counting logic.
  - Provides: ``estimate_text_tokens(text)`` → ``TokenEstimate``,
    ``estimate_tokens(*texts)`` → ``TokenEstimate``, ``TokenEstimate``
    (frozen dataclass).
  - **Consistent estimation**: Uses tiktoken with ``cl100k_base`` encoding
    if available, otherwise falls back to the ``len(text) // 4`` heuristic.
  - **Approximation flag**: When the heuristic is used, a
    ``TOKEN_COUNT_APPROXIMATE`` warning is included in the result.
  - **Deterministic**: Given the same input, always returns the same count.
  - **to_dict()**: ``TokenEstimate.to_dict()`` produces a JSON-serialisable
    dictionary.
  - Warning code used: ``TOKEN_COUNT_APPROXIMATE``.

- **CLI integration (inspect)**: The ``inspect`` CLI command delegates to
  ``load_project()`` + ``select_context()`` via ``_run_inspect()``.
  Supports ``--json`` output.  Unknown chunk returns exit code 1.
  Rich dashboard shows: token budget, selected sections, dropped sections,
  ledger references, distillate node, adjacent summaries, warnings/errors.

## Brief Generation

**Mandatory rule (Chunk 12):** Brief generation **must** go through
`brief.py` only.  No other module may independently assemble or write
brief files.  Brief generation **must** call `select_context()` from
`brief_context.py` as the **only** source of context selection — no
duplication of selection logic is permitted.

- **Brief service: src/aip_loom/brief.py** *(implemented — Chunk 12)*
  - This is the **single authority** for assembling and writing session
    briefs.  It adds rendering and file-writing logic on top of the
    shared context selection engine — it never duplicates selection logic.
  - Provides: ``generate_brief(root, chunk_id, task, dry_run, force,
    token_budget)`` → ``CommandResult``, ``assemble_brief_content(context,
    task)`` → ``str``, ``BriefResult`` (frozen dataclass),
    ``PROTECTED_PRIORITIES`` (frozenset).
  - **Zero duplication**: ``generate_brief`` calls ``select_context()``
    from ``brief_context.py`` as the single source of truth.  No other
    function in this module independently decides what context to include.
  - **Protected sections**: Sections with priorities in
    ``PROTECTED_PRIORITIES`` ({0, 1, 2, 3, 4, 6}) are never dropped
    from a brief.  If any protected section would be dropped due to
    budget, or if the budget is exceeded by mandatory sections, the
    brief fails with ``BRIEF_BUDGET_OVERFLOW`` rather than producing an
    incomplete brief.  Only priorities 5 (adjacent summaries), 7
    (global threads), and 8 (unresolved questions) can be dropped.
  - **Dirty/orphan chunk guards**: Brief fails for chunks with dirty
    checksums (``BRIEF_DIRTY_CHUNK``) or chunks not in the manifest
    order (``BRIEF_STALE_CHUNK``) unless ``--force`` is used.
  - **Force with unmistakable warning**: ``--force`` allows brief
    generation on dirty/stale/orphan chunks, but always emits a
    ``BRIEF_FORCE_USED`` warning containing ``FORCE OVERRIDE`` in
    the message.
  - **Dry-run safety**: When ``dry_run=True``, **nothing** is written
    to disk.  The brief content is assembled and returned but the file
    write step is skipped entirely.
  - **Deterministic**: Given the same project state, chunk ID, task,
    and token budget, the brief content is always identical.
  - **Human-readable output**: The brief is a well-structured Markdown
    file with YAML frontmatter containing metadata (chunk_id,
    generated_at, token_estimate, token_budget, schema_version,
    section_count, dropped_count, task).
  - **Brief file location**: ``.aip-loom/briefs/<chunk_id>.md`` — written
    using ``safe_write_text()`` from ``fs.py`` for atomic writes.
  - **Token consistency**: Token estimates in the brief match exactly
    what ``inspect`` would show for the same chunk and budget, because
    both commands use the same ``select_context()`` engine.
  - **to_dict()**: ``BriefResult.to_dict()`` produces a JSON-serialisable
    dictionary including chunk_id, brief_path, token_estimate,
    token_budget, section_count, dropped_count, dry_run, and content_length.
  - **CLI integration**: The ``brief`` CLI command delegates to
    ``generate_brief()`` via ``_run_brief()``.  Supports ``--task``,
    ``--dry-run``, ``--force``, and ``--json`` flags.
  - Error codes used: ``CHUNK_NOT_FOUND``, ``BRIEF_BUDGET_OVERFLOW``,
    ``BRIEF_DIRTY_CHUNK``, ``BRIEF_STALE_CHUNK``, ``FILE_WRITE_ERROR``.
  - Warning codes used: ``BRIEF_FORCE_USED``, plus all warnings from
    ``SelectedContext`` and ``ProjectState``.

## Reconcile
- Update parser: src/aip_loom/update_parser.py *(not yet implemented — Chunk 13)*
- Reconcile planner: src/aip_loom/reconcile_plan.py *(not yet implemented — Chunk 14)*
- Reconcile apply: src/aip_loom/reconcile_apply.py *(not yet implemented — Chunk 15)*

## CLI and Output
- CLI entry point: src/aip_loom/cli.py *(implemented — Chunk 01, init wired in Chunk 08, validate wired in Chunk 09, status wired in Chunk 10, inspect wired in Chunk 11, brief wired in Chunk 12)*
- Result rendering: src/aip_loom/output.py *(implemented — Chunk 01)*

## Project Initialisation

**Mandatory rule (Chunk 08):** Project creation and scaffolding **must**
go through the init service only.  No other module may create the project
directory tree, manifest, ledgers, or initial files directly.

- **Init service: src/aip_loom/init.py** *(implemented — Chunk 08)*
  - This is the **single authority** for creating a new AIP_Loom project.
    No other module may create the project directory tree or initial files.
  - Provides: ``init_project(root, name, project_type)``, ``InitError``,
    ``InitResult`` (frozen dataclass).
  - **Create-or-fail semantics**: If any step fails, all partial artefacts
    are cleaned up.  The target directory is left in the same state as
    before the call (or removed entirely if we created it).
  - **No fake approved content**: The distillate placeholder is created with
    an empty ``nodes`` list.  No fabricated summaries, decisions, or
    approval states are ever written.
  - **Schema-valid output**: Every file written during init validates against
    the corresponding Pydantic model in ``schemas.py``.
  - **Path safety**: All path construction goes through ``ProjectLayout``.
    All file writes go through ``fs.safe_write_text()``.
  - **YAML gateway**: All YAML serialisation goes through ``yaml_io.dump_yaml_string()``.
    Model instances are constructed and validated before serialisation.
  - **Transaction safety**: ``TransactionWorkspace`` is used to snapshot files
    that might be overwritten.  On failure, ``restore()`` is called to roll
    back any partial writes.
  - **Git best-effort**: Git initialisation (``git init``, ``git add``, ``git commit``)
    is attempted but never fatal.  If Git is unavailable or fails, a
    ``GIT_INIT_SKIPPED`` warning is returned but the project is still
    considered successfully initialised.
  - **Project type validation**: The ``project_type`` parameter is validated
    against the ``ProjectType`` enum before any files are created.  Invalid
    types raise ``InitError`` with ``FIELD_INVALID``.
  - **Existing project detection**: If ``aip_loom.yaml`` already exists in
    the target directory, init fails with ``PROJECT_ALREADY_EXISTS``.
    Existing manifests are never modified.
  - **Created structure**:
    - ``aip_loom.yaml`` — project manifest (``ProjectManifest``)
    - ``chunks/`` — empty directory for chunk Markdown files
    - ``ledgers/decisions.yaml`` — empty decision ledger (``DecisionLedger``)
    - ``ledgers/threads.yaml`` — empty thread ledger (``ThreadLedger``)
    - ``ledgers/questions.yaml`` — empty question ledger (``QuestionLedger``)
    - ``distillate.yaml`` — empty distillate (``Distillate``)
    - ``sessions.yaml`` — empty session log (``SessionLog``)
    - ``comments.yaml`` — empty comment log (``CommentLog``)
    - ``archive/`` — empty directory for archived chunks
    - ``.aip-loom/`` — control directory (with ``staging/`` subdirectory)
  - **CLI integration**: The ``init`` CLI command delegates to ``init_project()``
    via ``_run_init()``.  Supports ``--type`` (project type) and ``--dir``
    (project directory) options.  ``--json`` output includes root path,
    git_initialised, and git_commit_created.
  - Error codes used: ``PROJECT_ALREADY_EXISTS``, ``FIELD_INVALID``,
    ``FILE_WRITE_ERROR``.  Warning code: ``GIT_INIT_SKIPPED``.
