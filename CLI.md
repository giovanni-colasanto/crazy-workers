# Crazy Workers CLI

The `crazy-workers` library provides a command-line interface to manage worker processes directly from the terminal.

## Installation

```bash
pip install crazy-workers
```

## Global Options

| Option | Description |
|--------|-------------|
| `--workers-dir PATH` | Directory containing the worker scripts. Overrides all other discovery methods. |

## Worker Directory Discovery

The CLI resolves the workers directory through a tiered mechanism (highest priority first):

1. **`--workers-dir` flag** — must point to an existing directory, otherwise the CLI exits with an error.
2. **`CRAZY_WORKERS_DIR` environment variable** — can be set in your shell or in a local `.env` file.
3. **Interactive prompt** — if running in a TTY, asks for the path and saves it to `.env` for future use.
4. **Auto-detection** — checks for a folder named `workers/` in the current working directory.

If none of the above resolves to a valid directory, the CLI exits with an error. Unlike the library, the CLI **never creates** the workers directory.

## Commands

### `status`

The observability hub. Shows the boot-restore state for the workers directory
(enabled/not installed, the mechanism, and whether it runs at boot or at login),
followed by a table of the workers tracked in the database — status, PID, last
action timestamp, and parameters.

```bash
crazy-workers status
crazy-workers status --all
crazy-workers --workers-dir /path/to/workers status
```

| Option | Description |
|--------|-------------|
| `--all` | Show every stopped worker instead of the latest few. |
| `--json` | Output all workers as JSON to stdout (never truncated, includes `desired_status`). |

The `Status` column shows the **effective** state of each worker. When it
diverges from the requested state, the daemon still has work to do and the row
carries a pending note:

- `RUNNING (stop requested)` — a stop was requested but the process is still
  alive; if it never goes away, something is wrong with the stop.
- `STOPPED (start pending)` — a start was requested and the daemon has not
  spawned the process yet.
- `CRASHED (restart pending)` — the daemon will restart it with backoff.

Stopped workers are history, not activity: only the **2 most recently
stopped** are shown and the remainder is collapsed into an explicit caption
under the table (`… N more stopped workers hidden — use --all to show them`).
Nothing is deleted — the full history stays in the database and `--all` (or
`--json`) shows every row.

Status colors: `green` = RUNNING, `cyan` = NEVER_STARTED, `yellow` = STARTING, `dim` = STOPPED, `bold red` = CRASHED.

---

### `start`

Starts a worker process. If `worker_type` is omitted, presents an interactive list of available `.py` files.

```bash
# Start by type (key defaults to worker_type)
crazy-workers start example_worker

# With a custom key (allows multiple instances of the same type)
crazy-workers start example_worker --key job_1

# With parameters (must be a valid JSON string)
crazy-workers start example_worker --params '{"duration": 30, "mode": "fast"}'

# Remote daemon: the explicit type is trusted and validated by the daemon
CRAZY_WORKERS_DB_URL=postgresql://... crazy-workers start example_worker

# Interactive selection
crazy-workers start
```

| Option | Description |
|--------|-------------|
| `worker_type` | Filename of the worker script (without `.py`). Optional — interactive if omitted. |
| `--key KEY` | Unique key for this instance. Defaults to `worker_type`. |
| `--params JSON` | JSON string passed to the worker as `sys.argv[1]`. |

Re-running `start` on a worker that is already RUNNING updates its spec: if the
new `--params` differ from the ones the live process was spawned with, the
daemon recycles it (stop, then restart with the new parameters) within a couple
of reconcile passes. Identical parameters leave the process untouched.

When `CRAZY_WORKERS_DB_URL` is set and the worker type is explicit, `start`
does not require a local copy of the workers directory. This lets an operator
control a container or remote daemon through the shared database. Interactive
selection still requires the scripts locally; the process-owning daemon always
performs the authoritative file and name validation.

---

### `stop`

Stops a running worker by its key. If `worker_key` is omitted, presents an interactive list of running workers.

```bash
# Explicit key
crazy-workers stop job_1

# Interactive selection
crazy-workers stop
```

On stop, unmanaged child processes are also terminated. Independently managed nested workers (started via their own `WorkerManager`) are preserved.

---

### `params`

Displays the parameters a worker was started with, formatted as JSON.

```bash
# Explicit key
crazy-workers params job_1

# Interactive selection
crazy-workers params
```

---

> **No `restore` command.** In the recommended deployment, systemd or the
> container runtime restarts the reconcile daemon and desired state restores
> the workers. Embedded deployments can opt into the legacy OS boot hook with
> `WorkerManager(..., auto_boot=True)`.

### `daemon`

Runs the reconcile loop as a foreground process and owns all worker subprocesses
for one worker directory and database. Because `--workers-dir` is a global
option, place it before the `daemon` command:

```bash
crazy-workers --workers-dir /app/workers daemon \
  --db-url postgresql://user:password@database/app \
  --interval 2 \
  --log-level INFO \
  --log-destination stdout \
  --worker-env-allow DATABASE_URL,STATIC_FOLDER \
  --lock-name application-workers \
  --stop-workers-on-exit
```

| Option | Environment variable | Default |
|--------|----------------------|---------|
| `--workers-dir PATH` | `CRAZY_WORKERS_DIR` | Required existing directory. The flag takes precedence. |
| `--db-url URL` | `CRAZY_WORKERS_DB_URL` | Local SQLite database under `.service/`. |
| `--interval SECONDS` | — | `2.0`. |
| `--log-level LEVEL` | `CRAZY_WORKERS_LOG_LEVEL` | `INFO`. |
| `--log-destination file\|stdout` | `CRAZY_WORKERS_LOG_DESTINATION` | `file`. |
| `--worker-env-allow KEY1,KEY2` | `CRAZY_WORKERS_ENV_ALLOWLIST` | Inherit the complete daemon environment. |
| `--lock-name NAME` | `CRAZY_WORKERS_LOCK_NAME` | Absolute path of the workers directory. |
| `--stop-workers-on-exit` | `CRAZY_WORKERS_STOP_WORKERS_ON_EXIT` | Disabled. The environment value accepts `1`, `true`, `yes`, or `on`. |

Explicit value-taking flags override environment variables; omitted options
fall back to their environment defaults. `--stop-workers-on-exit` and its
environment variable can enable the shutdown behaviour. PostgreSQL-backed
daemon locks require PostgreSQL 11 or newer because they use
`hashtextextended`. On older servers lock acquisition fails and the daemon
exits.

When an environment allowlist is active, application variables are denied
unless named explicitly. In particular, add `CRAZY_WORKERS_DB_URL` if the
worker code itself reads it; the daemon using that variable does not implicitly
make it available to children. Portable runtime variables are always retained;
the complete list is documented in the README container section.

---

## Interactive Mode

Omitting `worker_type` (for `start`) or `worker_key` (for `stop` and `params`) activates interactive mode — a numbered list is shown and you select with a number. Requires a TTY.

## Configuration via `.env`

When the interactive prompt is used to set the workers directory, it is automatically saved to `.env` in the current working directory:

```text
CRAZY_WORKERS_DIR=/absolute/path/to/workers
```

The CLI reads this file at every invocation. You can also set it manually.

### Platform examples

```bash
# Linux / macOS
export CRAZY_WORKERS_DIR=/path/to/workers
crazy-workers status

# Windows (PowerShell)
$env:CRAZY_WORKERS_DIR = "C:\path\to\workers"
crazy-workers status

# Persist to .env (any platform)
echo "CRAZY_WORKERS_DIR=/path/to/workers" > .env
crazy-workers status
```
