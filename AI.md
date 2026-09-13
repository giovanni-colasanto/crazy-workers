# AI Project Context & Rules

## Core Mandates

- **Clean & Talking Code**: Prioritize expressive, self-documenting code. Variables, functions, and classes should have clear, descriptive names that reveal their intent.
- **Modularity & Decomposition**: Keep files small and focused on a single responsibility. If a file becomes too large or complex, refactor it into a module (a directory with `__init__.py` and sub-modules).
- **Structured Testing**: Tests must be organized into a `tests/` directory that mirrors the package structure. Each source module should have a corresponding test module. Cross-cutting integration tests go in `tests/integration/`. Tests for `example_app/` go in `tests/app/`.
- **Responsibility Distribution**: Aim for a balanced distribution of responsibilities. Use design patterns where appropriate to reduce complexity.
- **Testing Integrity**: Never modify the codebase solely to facilitate a test. If a test is difficult to write, refactor for better observability or improve the test setup.
- **Linting & Formatting**: Always run `ruff check . --fix` and `ruff format .` after any code change.
- **Testing**: Always run `pytest` after changes.
- **Coverage**: Ensure total coverage stays above 90% (the pipeline threshold configured in `pyproject.toml`). New code must be covered by tests. Each individual file must maintain a minimum coverage of 75%.
- **Clean Tests**: Tests must be clean, readable, and strictly free of `print()` statements. Use assertions for verification.
- **No Silent Exceptions**: Avoid broad `except Exception: pass` blocks. Catch only specific, expected exception types. Each exception handler must either log the error, propagate it, or have an explicit comment explaining why silence is correct.
- **Documentation Maintenance**: After every development cycle, review and update `README.md`, `CLI.md`, and this file. Ensure all functional changes, new configurations, and architectural shifts are accurately reflected.
- **Style**: Follow `pyproject.toml` (2-space indent, single quotes). No `print` statements. Imports must be sorted alphabetically and organized into two blocks separated by a blank line:
  1. External dependencies: standard library and third-party packages.
  2. Project-internal imports: modules belonging to the project itself.

## Test Structure

```
tests/
  base.py                   # BaseTestCase with process leak detection
  test_params.py            # crazy_workers/params.py (worker-side parse_params)
  core/
    test_backend.py         # SubprocessBackend environment and stdout forwarding
    test_engine.py          # crazy_workers/core/engine.py
    test_recovery.py        # crazy_workers/core/recovery.py
    manager/
      test_init.py          # crazy_workers/core/manager/__init__.py
      test_lister.py        # crazy_workers/core/manager/lister.py
      test_recoverer.py     # crazy_workers/core/manager/recoverer.py
      test_starter.py       # crazy_workers/core/manager/starter.py
      test_stopper.py       # crazy_workers/core/manager/stopper.py
      test_boot_wiring.py   # start_worker -> automatic boot-restore wiring
      test_db_integration.py # shared engine, worker_env injection, recover-on-init
  boot/                     # crazy_workers/boot/ (automatic boot-restore)
    test_base.py            # crazy_workers/boot/base.py
    test_systemd.py         # crazy_workers/boot/systemd.py
    test_windows.py         # crazy_workers/boot/windows.py
    test_detect.py          # crazy_workers/boot/detect.py
    test_orchestrator.py    # crazy_workers/boot/orchestrator.py
    test_entry.py           # crazy_workers/boot/entry.py
  cli/
    test_main.py            # crazy_workers/cli/main.py
    test_discovery.py       # crazy_workers/cli/discovery.py
    commands/
      test_status.py        # crazy_workers/cli/commands/status.py
      test_starter.py       # crazy_workers/cli/commands/starter.py
      test_stopper.py       # crazy_workers/cli/commands/stopper.py
      test_params.py        # crazy_workers/cli/commands/params.py
  client/
    test_client.py          # crazy_workers/client.py (control-plane WorkerClient)
  daemon/
    test_main.py            # crazy_workers/daemon/__main__.py and runner.py
    test_lock.py            # local and PostgreSQL single-owner daemon locks
    test_reconciler.py      # crazy_workers/daemon/reconciler.py (incl. parameter-drift recycle)
  database/
    test_storage.py         # crazy_workers/database/storage.py
  testing/
    test_fake_backend.py    # crazy_workers/testing/backends.py (FakeBackend contract)
    test_polling.py         # crazy_workers/testing/polling.py
  integration/              # Full-stack tests with real processes
    test_resilience.py      # Kill/recovery/log/path-traversal scenarios
    test_nested_workers.py  # Parent-child worker scenarios
  app/                      # Tests for example_app/
    test_app.py             # example_app/app.py — routes, /events, end-to-end shared-DB demo
    workers/
      test_workers.py       # Smoke tests for each example worker
      test_db_writer.py     # example_app/workers/db_writer.py (uses injected DATABASE_URL)
```

## Commands

```bash
# Lint
ruff check .

# Lint + auto-fix
ruff check . --fix

# Format
ruff format .

# Test
pytest

# Test with coverage
coverage run -m pytest && coverage report

# Build for PyPI
python -m build

# Check distribution
twine check dist/*
```
