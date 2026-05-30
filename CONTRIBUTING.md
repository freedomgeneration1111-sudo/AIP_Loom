# Contributing to AIP_Loom

Thank you for your interest in contributing to AIP_Loom! This document provides guidelines and instructions for contributing.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/freedomgeneration1111-sudo/AIP_Loom.git
cd AIP_Loom

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install with development dependencies
pip install -e ".[dev]"

# Install with optional token counting
pip install -e ".[tokens]"
```

## Running Tests

```bash
# Run the full test suite
pytest

# Run with verbose output
pytest -v

# Run with coverage
pytest --cov=aip_loom --cov-report=term-missing

# Run a specific test module
pytest tests/test_reconcile_apply.py

# Run a specific test
pytest tests/test_cli.py::test_init_command
```

All tests must pass before submitting a change. The test suite includes unit tests, acceptance tests, and chaos/failure-injection tests.

## Code Style and Conventions

### Architecture Principles

1. **Single Authority Modules**: Every concern has exactly one owning module. No other module may duplicate that logic. The key authorities are:
   - `layout.py` — path resolution
   - `project.py` — project loading and validation
   - `errors.py` — error and warning codes
   - `results.py` — result envelope (`CommandResult`)
   - `update_parser.py` — model output parsing (security boundary)
   - `transaction.py` — file snapshot/restore
   - `lock.py` — exclusive locking
   - `schemas.py` — Pydantic models
   - `yaml_io.py` — YAML read/write

2. **Honest Failure**: Every failure must produce a `CommandResult` with a stable error code, human-readable message, and machine-readable detail. No silent failures. No ad-hoc error strings.

3. **No Auto-Fix**: Validation reports problems but never repairs them. The user must explicitly act.

4. **Transactional Safety**: Mutations go through `TransactionWorkspace` with snapshot-before-modify and rollback-on-failure.

5. **Frozen Dataclasses**: Result types and configuration objects are frozen (immutable after construction).

### Code Conventions

- Python 3.11+ (use `from __future__ import annotations`)
- Type hints on all function signatures
- Docstrings on all public functions and classes (NumPy style)
- Pydantic v2 models with `extra="forbid"`
- Error codes from `errors.py` — never invent ad-hoc strings
- File writes through `fs.py` (`safe_write_text`, `safe_write_bytes`) — never raw `write()`
- YAML through `yaml_io.py` — never direct `ruamel.yaml` usage
- Paths through `ProjectLayout` — never ad-hoc `Path` construction
- CLI handlers are thin: parse arguments, delegate to service, render result

### Testing Conventions

- All tests use `tmp_path` for isolation — no shared state
- Configure local Git user in test repos (avoid global gitconfig dependency)
- Tests exercise the public CLI and service APIs, not internal functions
- Deterministic tests only — no timing-dependent assertions, no network calls
- Chaos tests use the `FailureInjector` protocol to inject failures at specific stages
- Every new error code must have at least one test that triggers it

## Pull Request Process

1. Create a feature branch from `main`
2. Make your changes with tests
3. Ensure all tests pass: `pytest`
4. Ensure no lint errors
5. Submit a pull request with a clear description of the change

## Reporting Issues

When reporting bugs, please include:

- The exact command you ran
- The full output (especially with `--json` flag)
- The error code from the output
- The project state (or a minimal reproducer)
