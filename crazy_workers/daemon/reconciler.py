import logging
import time
from datetime import datetime, timedelta, timezone

from ..database.schema import DesiredStatus, Worker, WorkerStatus


logger = logging.getLogger('crazy_workers')

_BACKOFF_BASE_SECONDS = 1
_BACKOFF_MAX_SECONDS = 60
# Cap the exponent so a long-crashed worker doesn't compute an astronomically
# large intermediate before min() clamps it.
_BACKOFF_MAX_EXPONENT = 16


class Reconciler:
  """Single-owner loop: drives actual worker state toward desired state.

  Owns every worker process for one workers_dir/DB. Clients never spawn; they
  only set desired_status in the shared DB and this loop makes it so.

  | desired  | alive | observed        | action                                     |
  |----------|-------|-----------------|--------------------------------------------|
  | RUNNING  | no    | RUNNING         | record crash (mark CRASHED, +restart_count)|
  | RUNNING  | no    | other           | start (skipped while in backoff)           |
  | RUNNING  | yes   | RUNNING         | noop (reset backoff once proven healthy)   |
  | RUNNING* | yes   | RUNNING         | *params diverged: recycle (stop; next pass |
  |          |       |                 | restarts with the parameters now in the DB)|
  | RUNNING  | yes   | other           | heal observed status to RUNNING            |
  | STOPPED  | yes   | -               | stop                                       |
  | STOPPED  | no    | RUNNING/CRASHED | heal stale observed status to STOPPED      |
  | STOPPED  | no    | STOPPED         | noop                                       |

  Crash detection is split from the restart on purpose. A worker that dies the
  instant it starts comes back from ``start_worker`` marked RUNNING; if the same
  pass that noticed the dead PID respawned it, its status would flip to RUNNING
  again before the backoff branch ever saw CRASHED, and an instantly-crashing
  worker would respawn at full loop speed forever. Instead the first pass records
  the death (CRASHED + a bumped restart_count), and a later pass restarts it once
  the exponential backoff has elapsed.

  Parameter drift gets the same two-pass treatment. A client can rewrite a
  worker's ``parameters`` while it is RUNNING; the process keeps its spawn-time
  argv and would silently run with the old behaviour forever while ``status``
  shows the new spec. Every pass therefore asks the backend what the live
  process was actually spawned with and, when that diverges from the DB spec,
  stops the worker — desired stays RUNNING, so the next pass restarts it with
  the current parameters. A backend that cannot tell (unreadable command line)
  answers None, which is never treated as divergence: recycling must act on
  evidence, not on a guess.
  """

  def __init__(self, manager, interval=2.0, owner_guard=None):
    self.manager = manager
    self.interval = interval
    self.owner_guard = owner_guard
    self._stop = False

  def run_forever(self):
    logger.info('Reconciler started (interval=%ss)', self.interval)
    while not self._stop:
      if self.owner_guard is not None and not self.owner_guard():
        logger.error('Daemon ownership was lost; stopping the reconciler.')
        self.stop()
        break
      try:
        self.reconcile_once()
      except Exception:
        logger.exception('Reconcile pass failed; continuing.')
      # Sleep in small slices so a SIGTERM-triggered stop is honoured promptly
      # instead of after a full interval.
      self._interruptible_sleep(self.interval)
    logger.info('Reconciler stopped.')

  def stop(self):
    self._stop = True

  def _interruptible_sleep(self, seconds):
    deadline = time.monotonic() + seconds
    while not self._stop:
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        break
      time.sleep(min(0.2, remaining))

  def reconcile_once(self):
    """One pass over every worker. Returns the actions taken (for tests/observability)."""
    actions = []
    for row in self._load_snapshot():
      action = self._reconcile_worker(row)
      if action:
        actions.append((row['worker_key'], action))
    return actions

  def _load_snapshot(self):
    # Read everything we need into plain dicts and release the session before
    # touching processes — start/stop open their own short-lived sessions.
    with self.manager.storage.session_scope() as session:
      return [
        {
          'worker_key': w.worker_key,
          'worker_type': w.worker_type,
          'parameters': w.parameters,
          'pid': w.pid,
          'desired': w.desired_status,
          'status': w.status,
          'restart_count': w.restart_count,
          'last_exit_at': w.last_exit_at,
        }
        for w in session.query(Worker).all()
      ]

  def _reconcile_worker(self, row):
    alive = self.manager.backend.is_alive(pid=row['pid'], worker_key=row['worker_key'])

    if row['desired'] == DesiredStatus.RUNNING and not alive:
      if row['status'] == WorkerStatus.RUNNING:
        # We last saw it RUNNING but the process is gone: it crashed. Record the
        # death now and let a later pass restart it — see the class docstring for
        # why detection and restart are split across passes.
        logger.warning('Reconcile: worker %s died; recording crash', row['worker_key'])
        self._record_crash(row['worker_key'])
        return 'crashed'
      if self._in_backoff(row):
        return None
      logger.info('Reconcile: starting %s', row['worker_key'])
      # reset_backoff=False: an automatic restart must not wipe the crash history
      # a fast crash-loop is accumulating, or the backoff could never escalate.
      self.manager.start_worker(row['worker_type'], row['worker_key'], row['parameters'], reset_backoff=False)
      return 'start'
    if row['desired'] == DesiredStatus.STOPPED:
      if alive:
        logger.info('Reconcile: stopping %s', row['worker_key'])
        self.manager.stop_worker(row['worker_key'])
        return 'stop'
      if row['status'] not in (WorkerStatus.STOPPED, WorkerStatus.NEVER_STARTED):
        # Desired down and the process is already gone, but the observed status is
        # stale — RUNNING left by the last spawn, or CRASHED. Nothing kills the
        # process (it is dead) and stop_worker only handles a RUNNING worker, so
        # converge the observed status here, or the table shows a phantom
        # RUNNING/CRASHED with a dead PID forever.
        self._mark_stopped(row['worker_key'])
        return 'mark_stopped'
      return None
    if row['desired'] == DesiredStatus.RUNNING and alive:
      if row['status'] != WorkerStatus.RUNNING:
        # Process is up but the observed status drifted (e.g. left STARTING). Heal it.
        self._mark_running(row['worker_key'])
        return 'mark_running'
      if self._parameters_diverged(row):
        # Stop only: desired stays RUNNING, so the next pass restarts the worker
        # with the parameters now in the DB (see the class docstring).
        logger.info('Reconcile: parameters changed for %s; recycling', row['worker_key'])
        self.manager.stop_worker(row['worker_key'])
        return 'recycle'
      if row['restart_count']:
        # Alive a full pass after its last spawn — proven healthy. Clear the crash
        # backoff so a future crash starts counting (and backing off) from zero.
        self._reset_backoff(row['worker_key'])
        return 'reset_backoff'
    return None

  def _parameters_diverged(self, row):
    spawned = self.manager.backend.spawned_parameters(pid=row['pid'], worker_key=row['worker_key'])
    return spawned is not None and spawned != (row['parameters'] or {})

  def _in_backoff(self, row):
    if not row['last_exit_at'] or row['status'] != WorkerStatus.CRASHED:
      return False
    exponent = min(row['restart_count'], _BACKOFF_MAX_EXPONENT)
    delay = min(_BACKOFF_BASE_SECONDS * (2**exponent), _BACKOFF_MAX_SECONDS)
    last_exit = row['last_exit_at']
    # last_exit_at is a timezone-aware column (TIMESTAMPTZ) so Postgres hands
    # back an aware value. SQLite has no tz type and returns naive UTC, so coerce
    # those to aware UTC — the comparison must never mix naive and aware.
    if last_exit.tzinfo is None:
      last_exit = last_exit.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < last_exit + timedelta(seconds=delay)

  def _mark_running(self, worker_key):
    with self.manager.storage.session_scope() as session:
      worker = session.query(Worker).filter_by(worker_key=worker_key).first()
      if worker:
        worker.status = WorkerStatus.RUNNING

  def _record_crash(self, worker_key):
    # Persist the death so the next pass's backoff gate can see it: mark CRASHED,
    # bump the restart counter and stamp the exit time in UTC (Python-side, so the
    # backoff math does not depend on the DB dialect's now()/timezone semantics).
    # pid is cleared so we stop probing a dead (and potentially reused) PID.
    with self.manager.storage.session_scope() as session:
      worker = session.query(Worker).filter_by(worker_key=worker_key).first()
      if worker:
        worker.status = WorkerStatus.CRASHED
        worker.last_exit_at = datetime.now(timezone.utc)
        worker.restart_count = (worker.restart_count or 0) + 1
        worker.pid = None

  def _reset_backoff(self, worker_key):
    with self.manager.storage.session_scope() as session:
      worker = session.query(Worker).filter_by(worker_key=worker_key).first()
      if worker:
        worker.restart_count = 0

  def _mark_stopped(self, worker_key):
    with self.manager.storage.session_scope() as session:
      worker = session.query(Worker).filter_by(worker_key=worker_key).first()
      if worker:
        worker.status = WorkerStatus.STOPPED
        worker.pid = None
        worker.last_stopped_at = datetime.now(timezone.utc)
