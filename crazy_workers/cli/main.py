import argparse
import json
import os
import sys
from rich.panel import Panel

from ..client import WorkerClient
from .commands import show_params, show_status, start_worker, stop_worker
from .discovery import resolve_workers_dir
from .ui import console, err_console


def _db_url():
  return os.environ.get('CRAZY_WORKERS_DB_URL')


def _build_client(workers_dir):
  """A control-plane client over the shared DB, or the local self-contained SQLite.

  With CRAZY_WORKERS_DB_URL set the CLI talks to the same DB as the daemon and
  issues no DDL (the daemon or host owns the schema). Otherwise it falls back to
  the local ``.service/workers.db``, the self-contained mode.
  """
  db_url = _db_url()
  if db_url:
    return WorkerClient(db_url=db_url, create_tables=False)
  service_dir = os.path.join(workers_dir, '.service')
  os.makedirs(service_dir, exist_ok=True)
  sqlite_path = os.path.join(service_dir, 'workers.db')
  return WorkerClient(db_url=f'sqlite:///{sqlite_path}', create_tables=True)


def _build_parser():
  def formatter(prog):
    return argparse.HelpFormatter(prog, max_help_position=32)

  parser = argparse.ArgumentParser(description='Crazy Workers CLI', formatter_class=formatter)
  parser.add_argument('--workers-dir', help='Directory containing worker scripts')

  subparsers = parser.add_subparsers(dest='command', help='Commands')

  status_parser = subparsers.add_parser(
    'status', help='Show workers (effective status; older stopped rows truncated) and the target DB'
  )
  status_parser.add_argument('--json', action='store_true', help='Output all workers as JSON to stdout')
  status_parser.add_argument('--all', action='store_true', help='Show every stopped worker instead of the latest few')

  start_parser = subparsers.add_parser('start', help='Request a worker to run (interactive if type missing)')
  start_parser.add_argument('worker_type', nargs='?', help='The type (filename) of worker to start')
  start_parser.add_argument('--key', help='Optional custom key for the worker')
  start_parser.add_argument('--params', help='JSON string of parameters for the worker')

  stop_parser = subparsers.add_parser('stop', help='Request a worker to stop (interactive if key missing)')
  stop_parser.add_argument('worker_key', nargs='?', help='The key of the worker to stop')

  params_parser = subparsers.add_parser('params', help='Show parameters for a worker')
  params_parser.add_argument('worker_key', nargs='?', help='The key of the worker')

  daemon_parser = subparsers.add_parser('daemon', help='Run the reconcile loop (owns the worker processes)')
  daemon_parser.add_argument(
    '--db-url',
    help='Shared DB URL (defaults to CRAZY_WORKERS_DB_URL, else local SQLite)',
  )
  daemon_parser.add_argument('--interval', type=float, default=2.0, help='Seconds between reconcile passes')
  daemon_parser.add_argument('--log-level', help='Daemon log level (defaults to CRAZY_WORKERS_LOG_LEVEL or INFO)')
  daemon_parser.add_argument(
    '--log-destination',
    choices=('file', 'stdout'),
    help='Write worker output to files or daemon stdout (defaults to CRAZY_WORKERS_LOG_DESTINATION or file)',
  )
  daemon_parser.add_argument(
    '--worker-env-allow',
    help='Comma-separated application environment variables workers may inherit',
  )
  daemon_parser.add_argument(
    '--lock-name',
    help='PostgreSQL advisory-lock namespace (defaults to CRAZY_WORKERS_LOCK_NAME or the absolute workers-dir)',
  )
  daemon_parser.add_argument(
    '--stop-workers-on-exit',
    action='store_true',
    help='Stop child processes on daemon shutdown without changing desired state',
  )

  return parser


def main():
  parser = _build_parser()
  args = parser.parse_args()

  if not args.command:
    console().print(
      Panel.fit(
        '[bold cyan]Crazy Workers CLI[/bold cyan]\n[dim]Manage your background processes with ease[/dim]',
        border_style='cyan',
      )
    )
    parser.print_help()
    sys.exit(1)

  # Only `start` (lists/validates the worker scripts) and the daemon (owns them)
  # truly need the workers dir. With a shared DB, status/stop/params work without
  # it, so we must not block on the interactive prompt (see CRAZY_WORKERS_DB_URL).
  remote_explicit_start = args.command == 'start' and args.worker_type and _db_url() is not None
  needs_dir = args.command == 'daemon' or _db_url() is None or (args.command == 'start' and not remote_explicit_start)
  workers_dir = resolve_workers_dir(args.workers_dir, required=needs_dir)

  # The daemon is the process owner, not a client — it builds its own manager.
  if args.command == 'daemon':
    from ..daemon.runner import main as daemon_main

    argv = ['--workers-dir', workers_dir, '--interval', str(args.interval)]
    if args.db_url is not None:
      argv += ['--db-url', args.db_url]
    if args.log_level is not None:
      argv += ['--log-level', args.log_level]
    if args.log_destination is not None:
      argv += ['--log-destination', args.log_destination]
    if args.worker_env_allow is not None:
      argv += ['--worker-env-allow', args.worker_env_allow]
    if args.lock_name:
      argv += ['--lock-name', args.lock_name]
    if args.stop_workers_on_exit:
      argv.append('--stop-workers-on-exit')
    sys.exit(daemon_main(argv))

  try:
    with _build_client(workers_dir) as client:
      if args.command == 'status':
        show_status(client, workers_dir, json_mode=getattr(args, 'json', False), show_all=getattr(args, 'all', False))
      elif args.command == 'start':
        params = _parse_params(args.params)
        if not start_worker(client, workers_dir, args.worker_type, worker_key=args.key, parameters=params):
          sys.exit(1)
      elif args.command == 'stop':
        if not stop_worker(client, args.worker_key):
          sys.exit(1)
      elif args.command == 'params':
        if not show_params(client, args.worker_key):
          sys.exit(1)
  except ValueError as e:
    err_console().print(f'[bold red]Error:[/bold red] {e}')
    sys.exit(1)


def _parse_params(raw):
  if not raw:
    return None
  try:
    return json.loads(raw)
  except json.JSONDecodeError:
    err_console().print('[bold red]Error:[/bold red] Invalid JSON in --params')
    sys.exit(1)


if __name__ == '__main__':
  main()
