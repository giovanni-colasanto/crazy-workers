import argparse
import logging
import os
import signal

from ..core.backend import SubprocessBackend
from ..core.manager import WorkerManager
from .lock import create_daemon_lock
from .reconciler import Reconciler


logger = logging.getLogger('crazy_workers')


def main(argv=None):
  parser = argparse.ArgumentParser(prog='crazy_workers.daemon', description='Run the reconcile loop.')
  parser.add_argument('--workers-dir', required=True, help='Directory containing worker scripts')
  parser.add_argument(
    '--db-url',
    default=os.environ.get('CRAZY_WORKERS_DB_URL'),
    help='Shared DB URL (defaults to $CRAZY_WORKERS_DB_URL, else the local SQLite under .service/)',
  )
  parser.add_argument('--interval', type=float, default=2.0, help='Seconds between reconcile passes')
  parser.add_argument('--log-level', default=os.environ.get('CRAZY_WORKERS_LOG_LEVEL', 'INFO'))
  parser.add_argument(
    '--log-destination',
    choices=('file', 'stdout'),
    default=os.environ.get('CRAZY_WORKERS_LOG_DESTINATION', 'file'),
    help='Write worker output to per-worker files or forward it to daemon stdout',
  )
  parser.add_argument(
    '--worker-env-allow',
    default=os.environ.get('CRAZY_WORKERS_ENV_ALLOWLIST'),
    help='Comma-separated application environment variables workers may inherit (default: inherit all)',
  )
  parser.add_argument(
    '--lock-name',
    default=os.environ.get('CRAZY_WORKERS_LOCK_NAME'),
    help='Logical daemon namespace for the PostgreSQL advisory lock (defaults to workers-dir)',
  )
  parser.add_argument(
    '--stop-workers-on-exit',
    action='store_true',
    default=_env_flag('CRAZY_WORKERS_STOP_WORKERS_ON_EXIT'),
    help='Gracefully stop child processes on shutdown without changing desired state',
  )
  args = parser.parse_args(argv)

  logging.basicConfig(
    level=getattr(logging, args.log_level.upper(), logging.INFO),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
  )

  # Fail loudly on a misconfigured path rather than silently creating an empty
  # workers dir (and then never finding any worker script in it).
  if not os.path.isdir(args.workers_dir):
    logger.error('Workers directory %s does not exist', args.workers_dir)
    return 2

  # The daemon owns the host-local runtime area (.service, logs, and the SQLite
  # file in self-contained mode), so it materialises them (create_dir=True).
  # auto_recover is off: a reconcile pass already restarts dead RUNNING workers.
  manager = WorkerManager(
    args.workers_dir,
    create_dir=True,
    auto_boot=False,
    auto_recover=False,
    db_url=args.db_url,
    create_tables=args.db_url is None,
    backend=SubprocessBackend(
      log_destination=args.log_destination,
      inherited_env_keys=_parse_env_allowlist(args.worker_env_allow),
    ),
  )

  lock_name = args.lock_name or os.path.abspath(args.workers_dir)
  lock = create_daemon_lock(manager.storage.engine, os.path.join(manager.service_dir, 'daemon.lock'), lock_name)
  if not lock.acquire():
    logger.error(
      'Daemon ownership for %s was not acquired; another daemon may own it or lock setup failed. Exiting.', lock_name
    )
    manager.dispose()
    return 1

  reconciler = Reconciler(manager, interval=args.interval, owner_guard=lock.is_held)
  _install_signal_handlers(reconciler)

  try:
    reconciler.run_forever()
  finally:
    if args.stop_workers_on_exit:
      _stop_running_workers(manager)
    lock.release()
    manager.dispose()
  return 0


def _parse_env_allowlist(raw):
  if raw is None:
    return None
  return {key.strip().upper() for key in raw.split(',') if key.strip()}


def _env_flag(name):
  return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _stop_running_workers(manager):
  for worker in manager.list_workers():
    if worker['status'] != 'RUNNING':
      continue
    stopped, detail = manager.stop_worker(worker['worker_key'])
    if not stopped:
      logger.warning('Could not stop worker %s during daemon shutdown: %s', worker['worker_key'], detail)


def _install_signal_handlers(reconciler):
  def _handle(signum, _frame):
    logger.info('Received signal %s; shutting down.', signum)
    reconciler.stop()

  for signame in ('SIGTERM', 'SIGINT'):
    sig = getattr(signal, signame, None)
    if sig is not None:
      try:
        signal.signal(sig, _handle)
      except (ValueError, OSError):
        # Not the main thread, or unsupported on this platform — best effort.
        pass
