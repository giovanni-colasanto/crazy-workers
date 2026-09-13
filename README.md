# Crazy Workers

A Python library for managing background worker processes with persistent state, automatic crash recovery, and a built-in CLI.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Features

- **Persistent State** — SQLite database tracks worker status, PIDs, and parameters across restarts.
- **Backend Integration** — Co-locate crazy_workers' tables in your project's database (pass a SQLAlchemy engine or URL), inject a shared `DATABASE_URL` into every worker, and recover workers automatically when the backend boots. See [Backend integration](#backend-integration).
- **Process Management** — Start, stop, and monitor background Python scripts as independent OS processes.
- **Automatic Recovery** — Detects crashed workers and restarts them on application boot.
- **Reconcile Daemon** — A single-owner loop (`python -m crazy_workers.daemon`) drives actual state toward the desired state that clients (`WorkerClient`, CLI) write to the shared database: it starts and stops workers, restarts crashes with exponential backoff, and recycles a running worker whose parameters changed in the database — the process is never left silently running with a stale spec.
- **Optional Boot-Restore** — Embedded deployments can opt into a per-user OS hook (a systemd user unit / a logon Scheduled Task). Daemon and container deployments leave restart ownership to systemd or the container runtime. See [Automatic boot-restore](#automatic-boot-restore).
- **Child Process Control** — On stop, terminates unmanaged subprocesses while preserving independently-managed nested workers.
- **CLI Interface** — Manage workers from the terminal with interactive prompts and auto-discovery (see [CLI.md](https://github.com/Vanni-broUser/crazy-workers/blob/main/CLI.md)).
- **Security** — Worker types and keys are restricted to a safe identifier charset (`A-Z a-z 0-9 _ -`), with a defence-in-depth check that the resolved script path stays inside the workers directory. This blocks path traversal on both Unix and Windows (including drive-relative names like `c:evil`).
- **Observability** — Per-worker file logging; all service files (DB, lock, logs) live in a `.service/` folder inside your workers directory.
- **Zombie Protection** — Distinguishes active processes from zombies using `psutil`.
- **PID-Reuse Safe** — Each worker is tagged with an identity token on its command line; recovery and stop confirm a PID still belongs to the worker before acting, so a recycled PID is never mistaken for (or worse, killed as) a live worker. Works on both Unix and Windows.
- **Gunicorn-safe** — File-based lock prevents concurrent recovery runs across multiple workers.
- **Testable** — Drive your orchestration with a `FakeBackend` (no real processes) and import polling helpers for the few genuine end-to-end tests. See [Testing your app](#testing-your-app).

## Installation

```bash
pip install crazy-workers
```

Or from source:

```bash
git clone https://github.com/Vanni-broUser/crazy-workers
cd crazy-workers
pip install .
```

## Quick Start

### 1. Create a worker script

```python
# workers/my_worker.py
import time

from crazy_workers import parse_params

params = parse_params()
duration = params.get('duration', 60)

for _ in range(duration):
  time.sleep(1)
```

### 2. Manage it from Python

```python
from crazy_workers import WorkerManager

manager = WorkerManager('workers')

# Start
success, result = manager.start_worker(
  'my_worker',
  worker_key='job_1',
  parameters={'duration': 30},
)
print(result['pid'])  # OS process ID
print(result['status'])  # 'RUNNING'

# List
for w in manager.list_workers():
  print(w['worker_key'], w['status'])

# Stop
manager.stop_worker('job_1')

# Recover crashed workers (call on app startup)
restarted = manager.recover_workers()

manager.dispose()  # releases DB connection; does NOT kill workers
```

### 3. Or from the CLI

```bash
crazy-workers status
crazy-workers start my_worker --key job_1 --params '{"duration": 30}'
crazy-workers stop job_1
```

See [CLI.md](https://github.com/Vanni-broUser/crazy-workers/blob/main/CLI.md) for full CLI documentation.

## API Reference

### `WorkerManager(workers_dir, create_dir=True, ...)`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workers_dir` | `str` | `'workers'` | Directory containing worker `.py` scripts |
| `create_dir` | `bool` | `True` | Create `workers_dir` and `.service/` if they don't exist |
| `backend` | `ProcessBackend` | `None` | Process backend; the default spawns real subprocesses (tests inject a fake) |
| `auto_boot` | `bool` | `False` | Opt into the legacy per-user OS boot-restore hook — see [Automatic boot-restore](#automatic-boot-restore) |
| `boot_provider` | `BootProvider` | `None` | Override the boot-restore mechanism (mainly a test seam) |
| `db_url` | `str` | `None` | SQLAlchemy URL for worker state; defaults to SQLite under `.service/` |
| `engine` | `Engine` | `None` | Reuse an existing SQLAlchemy engine so the tables live in your database; **not** disposed by crazy_workers |
| `worker_env` | `dict` | `None` | Environment variables injected into **every** spawned worker (e.g. `DATABASE_URL`) |
| `auto_recover` | `bool` | `True` | Recover dead-but-`RUNNING` workers when the manager is constructed |
| `create_tables` | `bool` | `True` | Create crazy_workers' own tables on init; set `False` when the host owns the schema via its migrations |

See [Backend integration](#backend-integration) for `db_url` / `engine` / `worker_env` / `auto_recover` / `create_tables`.

### `start_worker(worker_type, worker_key=None, parameters=None, env=None)`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `worker_type` | `str` | — | Filename (without `.py`) of the worker script |
| `worker_key` | `str` | `worker_type` | Unique identifier; allows multiple instances of the same type |
| `parameters` | `dict` | `{}` | JSON-serializable dict passed as `sys.argv[1]` to the worker |
| `env` | `dict` | `None` | Extra environment variables injected into the worker process |

Returns `(bool, dict | str)` — `(True, worker_dict)` on success, `(False, error_message)` on failure.

> **Note on `RUNNING`:** success means the worker was *spawned* and survived a brief startup grace period that catches immediate launch failures (bad import, missing module). It does **not** guarantee the worker will run to completion — a worker that fails later is still reported `RUNNING` until the next `list_workers()` / `recover_workers()` reconciles its state.

### `stop_worker(worker_key)`

Gracefully terminates the worker (SIGTERM → SIGKILL after timeout). Returns `(bool, str)`.

### `list_workers()`

Returns a list of worker dicts including RUNNING, STOPPED, CRASHED, and NEVER_STARTED (filesystem-discovered) workers.

### `recover_workers()`

Restarts any worker whose DB status is RUNNING but whose process is dead. Uses a file lock to prevent concurrent recovery. Returns a list of restarted keys.

> You rarely call this directly: it runs automatically when the manager is constructed (`auto_recover=True`) and after a machine reboot via the boot hook. It remains available as an explicit, idempotent trigger.

### `dispose()`

Closes the database connection and clears internal process references. Does **not** kill background workers — they continue running independently.

## Worker Script Contract

A worker receives its parameters as a JSON string in `sys.argv[1]`. Use
`parse_params` instead of decoding it by hand:

```python
from crazy_workers import parse_params

params = parse_params()
# ... do work ...
```

Without arguments it is lenient: a worker launched with no parameters gets
`{}`. Pass `required=` to abort before any real work when a parameter the
worker cannot run without is missing or empty — the worker exits with code 1
and a message on stderr:

```python
params = parse_params(required=('device_id', 'output_dir'))
```

`parse_params(argv=...)` accepts an explicit argv list, which makes the
parsing trivially testable without patching `sys.argv`.

A worker is a separate process, so it cannot be handed a live object (e.g. a DB
connection). Pass **configuration** instead: the manager's `worker_env` (and any
per-call `env`) is injected as environment variables, so a worker reads, say,
`os.environ['DATABASE_URL']` and opens its own connection. See
[`example_app/workers/db_writer.py`](https://github.com/Vanni-broUser/crazy-workers/blob/main/example_app/workers/db_writer.py).

## Testing your app

Code that uses `WorkerManager` has two kinds of logic worth testing:
**orchestration** (which workers start, pairing, rollback, recovery) and the
**work itself** (does the worker actually do its job). `crazy_workers.testing`
makes both fast and non-flaky.

### Orchestration — without launching a single process

`WorkerManager.for_testing()` wires a **FakeBackend**: spawning and termination
are faked, but the state machine (SQLite, recovery, validation) stays **real**
and runs in-process. The backend is exposed as `manager.test` for assertions.

```python
from crazy_workers import WorkerManager


def test_starts_recorder_and_renamer():
  manager = WorkerManager.for_testing('workers')  # FakeBackend, no processes

  manager.start_worker('recorder', worker_key='cam1', parameters={'device': 'cam1'})
  manager.start_worker('renamer', worker_key='renamer_cam1', parameters={'output_dir': '/data/cam1'})

  assert manager.test.started_types == ['recorder', 'renamer']
  assert manager.test.is_running('cam1')
  assert manager.test.parameters_for('renamer_cam1') == {'output_dir': '/data/cam1'}
  manager.dispose()


def test_recovery_restarts_a_crash():
  manager = WorkerManager.for_testing('workers')
  manager.start_worker('recorder', worker_key='cam1')

  manager.test.crash('cam1')  # simulate an unexpected death
  manager.recover_workers()  # the real recovery path runs in-process

  assert manager.test.start_count('cam1') == 2
  assert manager.test.is_running('cam1')
  manager.dispose()
```

`workers_dir` must still contain the `<type>.py` files (`start_worker` checks
the script exists), but the fake backend never executes them — empty files are
enough.

`manager.test` exposes:

| Member | Returns |
|---|---|
| `started_types` / `started_keys` | every spawn, in order (a restart appears twice) |
| `running_keys` | keys whose latest process is still "alive" |
| `is_running(key)` | bool |
| `start_count(key)` | how many times the key was (re)started |
| `parameters_for(key)` | parameters of the most recent spawn |
| `crash(key)` | simulate an unexpected death (without a stop) |

### The real thing — polling helpers, not `sleep`

The few tests that *must* launch real processes should wait on conditions,
never fixed sleeps. `crazy_workers.testing` exposes the helpers used by the
library's own suite:

```python
from crazy_workers.testing import wait_for_worker_status, wait_for_log, wait_for_pid_dead

manager = WorkerManager('workers')
ok, res = manager.start_worker('recorder', worker_key='cam1')

wait_for_worker_status(manager, 'cam1', 'RUNNING')
wait_for_log('workers/.service/logs/cam1.log', 'recording started')
# ... assert the worker actually did its job ...
manager.stop_worker('cam1')
wait_for_pid_dead(res['pid'])
```

Available: `wait_for`, `wait_for_file`, `wait_for_log`, `wait_for_worker_status`,
`wait_for_worker_in_db`, `wait_for_worker_pid`, `wait_for_pid_dead`. Each raises
`AssertionError` with a useful message on timeout.

> **What the fake covers — and what it doesn't.** `for_testing`/FakeBackend
> tests *orchestration*, not "the worker actually records / sends / converts".
> Keep a small number of real end-to-end tests for that, made stable with the
> polling helpers above, and move everything else — the bulk — into the fast,
> deterministic fake world.

## Project Structure

```
crazy_workers/       # Library package
  core/              # WorkerManager, process engine, recovery lock
  boot/              # Automatic per-user boot-restore (systemd user unit / scheduled task)
  cli/               # CLI entry point, commands, discovery
  database/          # SQLAlchemy schema and pluggable storage (SQLite, or a shared engine/URL)
  testing/           # FakeBackend + polling helpers for consumer test suites
example_app/         # Flask demo application
  app.py
  workers/           # Example worker scripts
tests/
  core/              # Unit tests for core modules
  cli/               # Unit tests for CLI modules
  database/          # Unit tests for storage layer
  testing/           # Tests for the FakeBackend and polling helpers
  integration/       # Full-stack integration tests (real processes)
  app/               # Tests for the example Flask app
```

## Flask Integration

> ⚠️ **Security:** `start_worker()` runs the worker script named by the caller. Exposing it over HTTP makes it a **privileged operation** — anyone who can reach the route can launch any script in your workers directory. Put such routes behind authentication, and prefer validating `worker_type` against a known allow-list of expected workers. The example below is a minimal demo with **no auth**.

```python
from crazy_workers import WorkerManager


def create_app(db_engine, db_url):
  app = Flask(__name__)

  manager = WorkerManager(
    'workers',
    engine=db_engine,  # crazy_workers' tables live in YOUR database
    worker_env={'DATABASE_URL': db_url},  # injected into every worker
    # auto_recover=True (default): when the app boots, workers left RUNNING
    # with a dead PID are restored automatically — no explicit call needed.
  )

  @app.route('/workers/start', methods=['POST'])
  def start():
    data = request.json
    success, result = manager.start_worker(
      data['worker_type'],
      worker_key=data.get('worker_key'),
      parameters=data.get('parameters', {}),
    )
    return (jsonify(result), 200) if success else (jsonify({'error': result}), 400)

  return app
```

See [`example_app/app.py`](https://github.com/Vanni-broUser/crazy-workers/blob/main/example_app/app.py) for a complete example.

## Backend integration

When crazy_workers runs inside a backend, three options let it cooperate with
the project instead of living off to the side:

- **Co-locate its tables in your database.** Pass an existing SQLAlchemy
  `engine` (or a `db_url`) to `WorkerManager`. crazy_workers creates its own
  `workers` table inside your database and inherits its persistence and backups
  — so state survives even if the process/container is recreated. A shared
  engine is never disposed by crazy_workers; its owner manages it.
- **Let your migrations own the schema.** If your project tracks its schema with
  a migration tool (Alembic, etc.), pass `create_tables=False` so crazy_workers
  issues no DDL: the `workers` table becomes a normal migration in your history,
  with a single source of truth and no create-on-import side effect. You own the
  ordering — the table must exist before the manager queries it, so build the
  manager *after* your migrations run (and keep `auto_recover=False` until then,
  since recovery reads that table). See the `workers` schema in
  [`crazy_workers/database/schema.py`](https://github.com/Vanni-broUser/crazy-workers/blob/main/crazy_workers/database/schema.py)
  for the columns your migration must create.
- **Give workers the connection they need.** A worker is a separate process, so
  it can't receive a live DB connection — pass the *configuration* instead.
  `worker_env={'DATABASE_URL': ...}` is injected into every spawned worker
  (overridable per call via `start_worker(..., env=...)`); the worker opens its
  own connection from it.
- **Recovery on construction.** `auto_recover=True` (default) restores
  dead-but-`RUNNING` workers when the manager is built, so a restarting backend
  brings its workers back with no explicit call. The CLI and boot entrypoint
  set it to `False` (management/one-shot, not supervision).

## Gunicorn / Multi-Process Servers

When using a pre-fork server like Gunicorn:

- **Recovery is atomic** — a file lock (`.service/workers.db.recovery.lock`) ensures `recover_workers()` runs once even when multiple workers boot simultaneously.
- **Workers outlive their parent** — if a Gunicorn worker is recycled, background processes keep running. The next recovery cycle re-attaches or restarts them.

## Container daemon

Run one daemon process as the container entrypoint and let it supervise every
worker subprocess in that container:

~~~bash
python -m crazy_workers.daemon \
  --workers-dir /app/workers \
  --db-url "$DATABASE_URL" \
  --lock-name my-application-workers \
  --log-destination stdout \
  --stop-workers-on-exit \
  --worker-env-allow DATABASE_URL,STATIC_FOLDER
~~~

With PostgreSQL, the daemon holds a database advisory lock keyed by the current
database and `--lock-name`. A second container for the same logical worker
group exits instead of racing the first. PostgreSQL 11 or newer is required
because the lock key uses `hashtextextended`; on an older server lock
acquisition fails and the daemon exits. SQLite and other self-contained
deployments retain the host-local PID lock.

Ownership is checked once per reconcile pass. If the PostgreSQL lock connection
is lost, the old daemon exits at its next check instead of reconnecting without
ownership. A replacement can acquire the released lock immediately, so during
failover there can be a brief overlap (normally up to the reconcile interval,
2 seconds by default, plus the current pass) before the old daemon stops its
workers. The lock prevents steady-state split brain; it is not a zero-overlap
handover protocol.

`--log-destination stdout` forwards every worker line through a logger named
`crazy_workers.worker.<worker_key>`, making `docker logs` a single,
filterable stream. File logging remains the default for compatibility.

By default workers inherit the daemon environment. Supplying
`--worker-env-allow` switches to least-privilege inheritance: only portable
runtime variables plus the named application variables reach worker
subprocesses. The always-retained runtime keys are `COMSPEC`, `HOME`, `LANG`,
`LC_ALL`, `PATH`, `PATHEXT`, `PYTHONPATH`, `SYSTEMROOT`, `TEMP`, `TMP`, `TZ`,
`USERPROFILE`, `VIRTUAL_ENV`, and `WINDIR`. Application variables are not
implicit: for example, a worker that reads `CRAZY_WORKERS_DB_URL` must name it
in the allowlist. Values explicitly passed through `WorkerManager.worker_env`
or per-start `env` are always included.

`--stop-workers-on-exit` gracefully terminates the supervised subprocesses
when the daemon receives SIGTERM while leaving their desired state RUNNING.
The replacement container therefore restarts them cleanly. It is opt-in
because host daemons may intentionally leave workers alive across a daemon
upgrade.

## Automatic boot-restore

Embedded deployments may opt into a **per-user OS hook** for their workers
directory by constructing `WorkerManager(..., auto_boot=True)`. The hook calls
the internal entrypoint `python -m crazy_workers.boot
--workers-dir <dir>`, which runs `recover_workers()`. The install is
best-effort and happens at most once per directory (a marker lives in
`.service/boot.json`); a failure never blocks the worker from starting and is
reported by `crazy-workers status`.

| Platform | Mechanism | When it runs |
|----------|-----------|--------------|
| Linux | systemd **user** unit (`~/.config/systemd/user`) | at user login — or at true boot if **lingering** is enabled |
| Windows | logon Scheduled Task (`schtasks /SC ONLOGON`) | at user logon |

**Unattended boot — the honest caveat.** The recovery itself is durable across
consecutive reboots (a worker left `RUNNING` with a dead PID is restarted, and
this is PID-reuse safe). But running it *without any login* depends on the OS:

- **Linux:** enable lingering once, as root — `loginctl enable-linger <user>` —
  so the user's systemd starts at boot (the same model as the Docker daemon
  restarting containers). Without it, restore runs at the next login.
- **Windows:** a user task fires at logon; true pre-logon start needs autologon
  or an administrator-installed task.

`crazy-workers status` always reports whether the hook runs **at boot** or
**at login**, so this is never silently wrong.

Boot hooks are disabled by default. Even when `auto_boot=True`, setting
`CRAZY_WORKERS_NO_BOOT` disables installation entirely.

## Logs

With the default `file` log destination, each worker's stdout/stderr is appended
to `.service/logs/<worker_key>.log`. These files are written directly by the
worker process, so the library does **not** rotate them — they grow until you
act. For long-lived deployments, rotate them externally (e.g. `logrotate` with
`copytruncate`) or have your worker script configure its own
`logging.handlers.RotatingFileHandler` instead of writing to stdout/stderr.

With `--log-destination stdout` (or
`CRAZY_WORKERS_LOG_DESTINATION=stdout`), no per-worker file is created. Worker
output is forwarded to the daemon logger as described in
[Container daemon](#container-daemon), so the process supervisor or container
runtime owns collection and rotation.

## Development

### Setup

```bash
git clone https://github.com/Vanni-broUser/crazy-workers
cd crazy-workers
pip install -e .[dev]
```

### Commands

```bash
# Lint and format
ruff check . --fix && ruff format .

# Run tests
pytest

# Run tests with coverage
coverage run -m pytest && coverage report
```

### Standards

See [AI.md](https://github.com/Vanni-broUser/crazy-workers/blob/main/AI.md) for the full coding and testing standards used in this project.

## Support the project ❤️

crazy-workers is free and open-source (MIT). If it saves you time or powers your work,
consider supporting its development:

- **GitHub Sponsors** — recurring or one-time, 0% platform fee: https://github.com/sponsors/Vanni-broUser
- **Stripe** — one-time card donation via secure checkout: https://buy.stripe.com/dRmeVdbiw2bD1HN5jscZa00
- **⭐ Star the repo** — free, and it really helps visibility.

> The Stripe link is a [Payment Link](https://stripe.com/docs/payment-links); the same URL powers the
> **Sponsor** button on GitHub via [`.github/FUNDING.yml`](.github/FUNDING.yml).
