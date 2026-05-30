# AIP_Loom

**Local-first Python CLI workbench for longform AI document continuity.**

AIP_Loom gives you a disciplined, file-based workflow for writing long-form documents (novels, technical manuals, academic papers) with an AI collaborator — without losing your mind, your data, or your narrative thread.

## The Problem

When you use an AI to help write a 50,000+ word document across many sessions, you face three hard problems:

1. **Context drift**: The AI forgets what it decided three sessions ago. Decisions get silently reversed. Threads get dropped.
2. **No rollback**: If an AI rewrite goes wrong, you have no way to undo just that rewrite without losing everything else.
3. **No audit trail**: You can't answer "why did Chapter 7 change?" because there's no record of what the AI proposed, what you accepted, or what you rejected.

AIP_Loom solves all three with a **chunk-based, ledger-driven, transactional** workflow.

## How It Works

AIP_Loom breaks your document into **chunks** (C-0001, C-0002, ...) stored as standalone Markdown files. Each chunk carries structured frontmatter and is tracked by a set of canonical ledgers:

- **Decisions Ledger** (`D-0001`, `D-0002`, ...): Records every narrative or structural decision, with scope (global or chunk-scoped) and review state.
- **Threads Ledger** (`T-0001`, `T-0002`, ...): Tracks open continuity threads, blocked relationships, and resolved strands.
- **Questions Ledger** (`Q-0001`, `Q-0002`, ...): Captures open questions that need resolution before moving forward.
- **Distillate**: A compact structural index — per-chunk summaries, key decisions, and open threads — used by the brief engine.
- **Session Log** (`S-0001`, ...): Every brief-and-reconcile cycle is recorded for full auditability.

The core loop is:

```
init → validate → brief → [AI writes] → reconcile --preview → reconcile (apply) → build
```

Every mutation goes through a **transactional reconcile** that snapshots files before modifying them, validates the result, and rolls back on failure. If the Git commit fails after a successful apply, a `RECOVERY.md` file is written with exact recovery commands — your data is never silently lost.

## Project Structure

```
my-novel/
├── aip_loom.yaml          # Project manifest (name, type, chunk order)
├── chunks/
│   ├── C-0001.md          # Chunk files (frontmatter + prose)
│   ├── C-0002.md
│   └── ...
├── ledgers/
│   ├── decisions.yaml     # Decision ledger
│   ├── threads.yaml       # Thread/strand ledger
│   └── questions.yaml     # Question/open-issue ledger
├── distillate.yaml        # Structural index
├── sessions.yaml          # Session log
├── comments.yaml          # Review comments
├── archive/               # Archived pre-reconcile evidence
├── build/                 # Build output (draft.md)
└── .aip-loom/
    ├── lock               # Exclusive lock file
    ├── briefs/            # Generated briefs
    └── staging/           # Staging area for reconcile
```

## Installation

```bash
# From source
git clone https://github.com/freedomgeneration1111-sudo/AIP_Loom.git
cd AIP_Loom
pip install -e .

# With optional token counting support
pip install -e ".[tokens]"

# With development dependencies
pip install -e ".[dev]"
```

Requires Python 3.11 or later.

## Quick Start

### 1. Initialize a project

```bash
mkdir my-novel && cd my-novel
aip-loom init "My Novel" --type novel
```

This creates the full project directory tree with empty ledgers, a manifest, and (if Git is available) an initial commit.

### 2. Add chunks

Create chunk files in `chunks/` with structured frontmatter:

```markdown
---
schema_version: "0.1.0"
id: C-0001
title: "The Beginning"
status: draft
word_count: 2500
prose_checksum: "sha256:abc123..."
distillate_anchor: ""
created_at: "2025-01-15T10:00:00Z"
updated_at: "2025-01-15T10:00:00Z"
---

It was a dark and stormy night...
```

Update the manifest's `chunks.order` to define the reading order.

### 3. Validate

```bash
aip-loom validate              # Validate entire project
aip-loom validate --chunk C-0001  # Validate a specific chunk
```

Validation checks for: missing files, schema violations, duplicate IDs, broken references, checksum mismatches, chunk order mismatches, and pending review items.

### 4. Check status

```bash
aip-loom status
```

Shows a health dashboard: chunk count, validation state, lock status, recovery file presence.

### 5. Inspect context for a chunk

```bash
aip-loom inspect C-0001
```

Shows what context the brief engine would select for a chunk — without writing any files.

### 6. Generate a brief

```bash
aip-loom brief C-0001 --task "Revise opening for stronger hook"
```

Generates a deterministic session brief that includes the target chunk, relevant distillate nodes, open threads, recent decisions, and the task description. The brief is written to `.aip-loom/briefs/C-0001.md`.

### 7. Reconcile (AI output → canonical state)

After the AI produces output containing a `loom-update` fenced block:

```bash
# Preview what would change
aip-loom reconcile C-0001 --output model-output.md --preview

# Apply the changes
aip-loom reconcile C-0001 --output model-output.md
```

The reconcile pipeline:
1. Acquires an exclusive lock
2. Pre-validates the project
3. Checks Git cleanliness
4. Parses and validates the model output
5. Builds a reconcile plan
6. Snapshots all files that will be modified
7. Writes pre-archive evidence
8. Applies changes to staged state
9. Replaces canonical files (with rollback on failure)
10. Post-apply validation
11. Completes archive + session log
12. Git add/commit (writes `RECOVERY.md` if Git fails)
13. Releases lock

### 8. Build draft output

```bash
aip-loom build                    # Build draft.md
aip-loom build --output my-book.md  # Custom output path
```

Concatenates ordered chunk prose bodies into a single Markdown file, stripping frontmatter and respecting the canonical chunk order.

## CLI Reference

| Command | Description |
|---------|-------------|
| `aip-loom init NAME` | Initialize a new project |
| `aip-loom status` | Show project health dashboard |
| `aip-loom validate` | Validate project integrity |
| `aip-loom inspect CHUNK` | Show context for a chunk (read-only) |
| `aip-loom brief CHUNK` | Generate a session brief |
| `aip-loom reconcile CHUNK -o FILE` | Reconcile model output |
| `aip-loom build` | Build draft output |
| `aip-loom --version` | Show version |
| `aip-loom --help` | Show help |

All commands support `--json` for machine-readable output.

## Key Design Principles

### Single Authority Modules

Every concern has exactly one module that owns it. No other module may duplicate that logic:

- **`layout.py`**: The only module that resolves file paths. No ad-hoc `Path` construction elsewhere.
- **`project.py`**: The only module that loads and validates the full project state.
- **`errors.py`**: The only source of error and warning codes. No ad-hoc error strings.
- **`results.py`**: The only result envelope shape (`CommandResult`).
- **`update_parser.py`**: The only parser for model output. The security boundary between untrusted AI output and canonical state.
- **`transaction.py`**: The only module for snapshot/restore file operations.
- **`lock.py`**: The only module for exclusive project locking.
- **`schemas.py`**: The only source of Pydantic models.
- **`yaml_io.py`**: The only module that reads or writes YAML.

### Honest Failure

Every failure produces a `CommandResult` with a stable error code from `errors.py`, a human-readable message, and machine-readable detail. No failure is silent. No malformed input is accepted.

### No Auto-Fix

Validation reports problems but never repairs them. Dirty checksums are warnings, not corrections. Broken references are errors, not patches. The user must explicitly reconcile to fix issues.

### Transactional Safety

The reconcile apply follows a strict 14-step protocol. If anything fails before canonical replacement completes, all files are restored from snapshots. If Git commit fails after successful replacement, a `RECOVERY.md` file is written with exact manual recovery commands — your writer data is preserved.

## Architecture

```
src/aip_loom/
├── __init__.py          # Package root (version, product name)
├── cli.py               # Typer CLI wiring (thin handlers → services)
├── init.py              # Project initialization (init_project)
├── project.py           # Project loader + validation engine
├── layout.py            # Canonical path resolution (ProjectLayout)
├── schemas.py           # Pydantic v2 models (all schema definitions)
├── errors.py            # Stable error/warning code taxonomy
├── results.py           # CommandResult envelope
├── output.py            # Rich console renderer
├── brief.py             # Brief generation service
├── brief_context.py     # Context selection engine (shared by brief + inspect)
├── inspect.py           # (via brief_context) Read-only chunk inspection
├── reconcile_plan.py    # Reconcile planner (plan builder)
├── reconcile_apply.py   # Transactional reconcile apply (14-step protocol)
├── update_parser.py     # Strict model-output parser (security boundary)
├── build.py             # Draft Markdown concatenator
├── transaction.py       # Transaction workspace (snapshot/restore)
├── lock.py              # Exclusive project locking (O_CREAT|O_EXCL)
├── status.py            # Health dashboard computation
├── checksum.py          # Prose checksum computation
├── frontmatter.py       # YAML frontmatter parser/writer
├── ids.py               # ID allocation (C-NNNN, D-NNNN, etc.)
├── chunk_order.py       # Chunk order resolution (manifest vs fallback)
├── tokens.py            # Token counting (tiktoken optional)
├── git.py               # Git operations (init, add, commit, clean check)
├── fs.py                # Safe file writes (atomic, path-validated)
└── yaml_io.py           # YAML load/dump with strict modes
```

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=aip_loom --cov-report=term-missing

# Run specific test files
pytest tests/test_acceptance.py
pytest tests/test_chaos.py

# Run a specific test
pytest tests/test_reconcile_apply.py::test_apply_happy_path
```

The test suite includes:

- **Unit tests** for every module (30 test files, 10,000+ lines)
- **Acceptance tests** (`test_acceptance.py`): Full end-to-end happy path exercising every CLI command
- **Chaos tests** (`test_chaos.py`): Failure injection scenarios — crash during staged write, crash during canonical replacement, git commit failure, dirty git tree, hostile model output, stale locks, malformed YAML, missing fences, duplicate IDs, concurrent reconcile attempts, and build on invalid projects

All tests use `tmp_path` for isolation and local Git user config for test repos. Tests are deterministic — no flaky timing-dependent assertions.

## Error Code Reference

| Code | Category | Description |
|------|----------|-------------|
| `PROJECT_NOT_FOUND` | Project | Project root or manifest missing |
| `PROJECT_ALREADY_EXISTS` | Project | Attempt to init over existing project |
| `PROJECT_MALFORMED` | Project | Structural or schema violation |
| `SCHEMA_VALIDATION_FAILED` | Schema | Pydantic model validation failure |
| `YAML_PARSE_ERROR` | YAML | YAML syntax error |
| `YAML_DUPLICATE_KEYS` | YAML | Duplicate keys in YAML mapping |
| `UPDATE_BLOCK_MISSING` | Update | No `loom-update` fence in model output |
| `UPDATE_BLOCK_MALFORMED` | Update | Malformed update block content |
| `UPDATE_BLOCK_LEGACY_FENCE` | Update | Legacy `thread-update` fence used |
| `MODEL_ASSIGNED_ID` | Update | Model attempted to assign canonical IDs |
| `PATCH_MODE_UNSUPPORTED` | Update | PATCH mode requested (Phase 1: full_replacement only) |
| `LOCK_HELD` | Lock | Lock held by another live process |
| `LOCK_STALE` | Lock | Lock held by dead process |
| `GIT_DIRTY` | Git | Uncommitted changes in working tree |
| `GIT_COMMIT_FAILED` | Git | Git commit failed after reconcile apply |
| `RECONCILE_PRE_VALIDATION_FAILED` | Reconcile | Pre-apply validation failed |
| `RECONCILE_APPLIED_BUT_GIT_FAILED` | Reconcile | Apply succeeded but Git commit failed |
| `RECONCILE_RESTORED_AFTER_FAILURE` | Reconcile | Files restored from snapshots after failure |
| `VALIDATION_DUPLICATE_ID` | Validation | Duplicate chunk or ledger IDs |
| `VALIDATION_BROKEN_REFERENCE` | Validation | Reference to non-existent chunk or entry |
| `BUILD_VALIDATION_FAILED` | Build | Build aborted due to validation errors |

See `src/aip_loom/errors.py` for the complete taxonomy.

## Recovery Protocol

If a reconcile apply succeeds but the Git commit fails:

1. A `RECOVERY.md` file is written to the project root with exact manual recovery commands.
2. The canonical files have been modified — your writer data is preserved.
3. Either follow the commands in `RECOVERY.md` to complete the commit, or `git checkout -- .` to undo.
4. After resolution, delete `RECOVERY.md`.
5. Future reconcile attempts are blocked while `RECOVERY.md` exists.

## Model Output Format

The AI must produce output containing a `loom-update` fenced block:

````
```loom-update
schema_version: "0.1.0"
fence_type: loom-update
mode: full_replacement
target_chunk: C-0001
new_decisions:
  - provisional_id: new-1
    summary: "Switch to first-person narration"
    scope: chunk
    chunk_id: C-0001
new_threads:
  - provisional_id: new-2
    summary: "Resolve the mystery of the lighthouse"
    scope: global
---
# Revised Chunk

The revised prose goes here...

# Change Summary

Summary of what changed and why.
```
````

Key rules:
- Exactly one `loom-update` block per response
- New items must use provisional IDs (`new-1`, `new-2`) — canonical IDs are allocated by AIP_Loom
- `mode` must be `full_replacement` (no patch/diff mode in Phase 1)
- No YAML anchors, aliases, tags, or duplicate keys
- All fields are validated against strict Pydantic schemas with `extra="forbid"`

## Dependencies

| Package | Purpose |
|---------|---------|
| `typer>=0.12` | CLI framework |
| `rich>=13` | Terminal formatting |
| `pydantic>=2` | Schema validation |
| `ruamel.yaml>=0.18` | YAML parsing with round-trip preservation |
| `tiktoken>=0.7` | Token counting (optional) |

## License

MIT License. See [LICENSE](LICENSE) for details.
