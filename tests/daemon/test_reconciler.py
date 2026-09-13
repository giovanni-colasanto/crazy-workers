import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from crazy_workers import WorkerClient, WorkerManager
from crazy_workers.daemon.reconciler import Reconciler
from crazy_workers.database.schema import Worker, WorkerStatus


class _ReconcilerTestBase(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.mkdtemp(prefix='cw_reconciler_')
    self.workers_dir = os.path.join(self.tmp, 'workers')
    os.makedirs(self.workers_dir)
    # start_worker requires the script to exist, even though FakeBackend never runs it.
    with open(os.path.join(self.workers_dir, 'example_worker.py'), 'w') as f:
      f.write('pass\n')

    self.manager = WorkerManager.for_testing(self.workers_dir)
    self.backend = self.manager.test
    self.client = WorkerClient(db_url=f'sqlite:///{self.manager.db_path}', create_tables=False)
    self.reconciler = Reconciler(self.manager, interval=0.01)

  def tearDown(self):
    self.client.dispose()
    self.manager.dispose()
    shutil.rmtree(self.tmp, ignore_errors=True)

  def _set(self, worker_key, **fields):
    with self.manager.storage.session_scope() as session:
      worker = session.query(Worker).filter_by(worker_key=worker_key).first()
      for key, value in fields.items():
        setattr(worker, key, value)

  def _status(self, worker_key):
    return self.client.get(worker_key)['status']

  def _restart_count(self, worker_key):
    return self.client.get(worker_key)['restart_count']


class TestReconcileTable(_ReconcilerTestBase):
  def test_run_forever_stops_before_reconciling_when_ownership_is_lost(self):
    reconciler = Reconciler(self.manager, interval=0.01, owner_guard=lambda: False)
    self.client.request_start('example_worker', worker_key='w1')

    reconciler.run_forever()

    self.assertFalse(self.backend.is_running('w1'))

  def test_running_and_dead_starts(self):
    self.client.request_start('example_worker', worker_key='w1')
    actions = self.reconciler.reconcile_once()

    self.assertIn(('w1', 'start'), actions)
    self.assertTrue(self.backend.is_running('w1'))
    self.assertEqual(self._status('w1'), 'RUNNING')

  def test_running_and_alive_noop(self):
    self.client.request_start('example_worker', worker_key='w1')
    self.reconciler.reconcile_once()  # starts it
    actions = self.reconciler.reconcile_once()  # already alive

    self.assertEqual(actions, [])
    self.assertEqual(self.backend.start_count('w1'), 1)

  def test_stopped_and_alive_stops(self):
    self.client.request_start('example_worker', worker_key='w1')
    self.reconciler.reconcile_once()
    self.assertTrue(self.backend.is_running('w1'))

    self.client.request_stop('w1')
    actions = self.reconciler.reconcile_once()

    self.assertIn(('w1', 'stop'), actions)
    self.assertFalse(self.backend.is_running('w1'))

  def test_stopped_and_dead_noop(self):
    # Worker exists but was never started and is not desired.
    self.client.request_start('example_worker', worker_key='w1')
    self.client.request_stop('w1')
    actions = self.reconciler.reconcile_once()

    self.assertEqual(actions, [])
    self.assertEqual(self.backend.start_count('w1'), 0)

  def test_stopped_and_dead_heals_stale_running_status(self):
    # A worker whose process died while it was desired STOPPED keeps a stale
    # RUNNING status (the CLI reads it verbatim). The reconciler converges it.
    self.client.request_start('example_worker', worker_key='w1')
    self.reconciler.reconcile_once()  # RUNNING + alive
    self.client.request_stop('w1')
    self.backend.crash('w1')  # process gone, but status still RUNNING in DB
    self._set('w1', status=WorkerStatus.RUNNING)

    actions = self.reconciler.reconcile_once()

    self.assertIn(('w1', 'mark_stopped'), actions)
    self.assertEqual(self._status('w1'), 'STOPPED')

  def test_stopped_and_dead_heals_stale_crashed_status(self):
    self.client.request_start('example_worker', worker_key='w1')
    self.client.request_stop('w1')
    self._set('w1', status=WorkerStatus.CRASHED, pid=None)

    actions = self.reconciler.reconcile_once()

    self.assertIn(('w1', 'mark_stopped'), actions)
    self.assertEqual(self._status('w1'), 'STOPPED')

  def test_mark_running_heals_status_drift(self):
    self.client.request_start('example_worker', worker_key='w1')
    self.reconciler.reconcile_once()  # RUNNING + alive
    self._set('w1', status=WorkerStatus.STARTING)  # observed status drifts

    actions = self.reconciler.reconcile_once()

    self.assertIn(('w1', 'mark_running'), actions)
    self.assertEqual(self._status('w1'), 'RUNNING')

  def test_in_process_end_to_end(self):
    self.client.request_start('example_worker', worker_key='w1')
    self.reconciler.reconcile_once()
    self.assertTrue(self.backend.is_running('w1'))

    self.client.request_stop('w1')
    self.reconciler.reconcile_once()
    self.assertFalse(self.backend.is_running('w1'))


class TestReconcileRecovery(_ReconcilerTestBase):
  def test_crashed_running_worker_is_recorded_then_restarted(self):
    # Recovery of a dead RUNNING worker is a two-phase process: one pass records
    # the crash (so backoff can gate it), a later pass restarts it once backoff
    # has elapsed.
    self.client.request_start('example_worker', worker_key='w1')
    self.reconciler.reconcile_once()
    self.backend.crash('w1')  # process dies unexpectedly; DB still says RUNNING

    # Phase 1: crash recorded, NOT restarted yet.
    actions = self.reconciler.reconcile_once()
    self.assertIn(('w1', 'crashed'), actions)
    self.assertFalse(self.backend.is_running('w1'))
    self.assertEqual(self.backend.start_count('w1'), 1)
    self.assertEqual(self._status('w1'), 'CRASHED')
    self.assertEqual(self._restart_count('w1'), 1)

    # Phase 2: once the backoff window has elapsed, it is restarted.
    self._set('w1', last_exit_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    self.reconciler.reconcile_once()
    self.assertTrue(self.backend.is_running('w1'))
    self.assertEqual(self.backend.start_count('w1'), 2)

  def test_alive_worker_is_readopted_not_restarted(self):
    self.client.request_start('example_worker', worker_key='w1')
    self.reconciler.reconcile_once()

    # A second pass sees it alive (by pid/token) and leaves it alone.
    self.reconciler.reconcile_once()
    self.assertEqual(self.backend.start_count('w1'), 1)


class TestReconcileBackoff(_ReconcilerTestBase):
  def test_recent_crash_is_in_backoff(self):
    self.client.request_start('example_worker', worker_key='w1')
    self._set(
      'w1',
      status=WorkerStatus.CRASHED,
      restart_count=5,
      last_exit_at=datetime.now(timezone.utc),
      pid=None,
    )
    self.reconciler.reconcile_once()
    self.assertEqual(self.backend.start_count('w1'), 0)

  def test_naive_last_exit_at_is_treated_as_utc(self):
    self.client.request_start('example_worker', worker_key='w1')
    self._set(
      'w1',
      status=WorkerStatus.CRASHED,
      restart_count=5,
      last_exit_at=datetime.utcnow(),  # naive, as a DB round-trip would return
      pid=None,
    )
    self.reconciler.reconcile_once()
    self.assertEqual(self.backend.start_count('w1'), 0)

  def test_expired_backoff_restarts(self):
    self.client.request_start('example_worker', worker_key='w1')
    self._set(
      'w1',
      status=WorkerStatus.CRASHED,
      restart_count=1,
      last_exit_at=datetime.now(timezone.utc) - timedelta(seconds=120),
      pid=None,
    )
    self.reconciler.reconcile_once()
    self.assertEqual(self.backend.start_count('w1'), 1)

  def test_instant_crash_is_not_hot_restarted(self):
    # Regression: a worker that dies the instant it starts must not respawn on
    # every pass. The first pass after the death records the crash; the immediate
    # next pass is still inside the backoff window and must NOT restart.
    self.client.request_start('example_worker', worker_key='w1')
    self.reconciler.reconcile_once()  # start #1
    self.backend.crash('w1')

    self.assertIn(('w1', 'crashed'), self.reconciler.reconcile_once())
    self.assertEqual(self.backend.start_count('w1'), 1)  # crash recorded, not restarted

    self.assertEqual(self.reconciler.reconcile_once(), [])  # still in backoff
    self.assertEqual(self.backend.start_count('w1'), 1)

  def test_repeated_instant_crashes_escalate_restart_count(self):
    # Regression: previously every respawn reset restart_count to 0, so the
    # exponential backoff was pinned at its first level forever. Across restart
    # cycles the counter must accumulate.
    self.client.request_start('example_worker', worker_key='w1')
    self.reconciler.reconcile_once()  # start #1, restart_count 0

    counts = []
    for _ in range(4):
      self.backend.crash('w1')
      self.reconciler.reconcile_once()  # record crash (restart_count += 1)
      counts.append(self._restart_count('w1'))
      # Pretend the (escalating) backoff window has elapsed so the next pass restarts.
      self._set('w1', last_exit_at=datetime.now(timezone.utc) - timedelta(seconds=3600))
      self.reconciler.reconcile_once()  # restart, reset_backoff=False -> count preserved

    self.assertEqual(counts, [1, 2, 3, 4])

  def test_backoff_resets_once_worker_proves_healthy(self):
    # A worker that is observed alive a full pass after its spawn has proven
    # healthy; its accumulated crash backoff must be cleared.
    self.client.request_start('example_worker', worker_key='w1')
    self.reconciler.reconcile_once()  # start, alive
    self._set('w1', restart_count=3)  # pretend it had crashed a few times before

    actions = self.reconciler.reconcile_once()  # alive + RUNNING + count>0 -> reset
    self.assertIn(('w1', 'reset_backoff'), actions)
    self.assertEqual(self._restart_count('w1'), 0)


class TestReconcileParameterDrift(_ReconcilerTestBase):
  def test_changed_params_recycle_then_restart_with_new_params(self):
    # Regression: request_start with new params on a RUNNING worker used to
    # update the DB spec only — the process kept its spawn-time argv while
    # status showed the new parameters.
    self.client.request_start('example_worker', worker_key='w1', parameters={'mode': 'old'})
    self.reconciler.reconcile_once()
    self.assertEqual(self.backend.parameters_for('w1'), {'mode': 'old'})

    self.client.request_start('example_worker', worker_key='w1', parameters={'mode': 'new'})

    # Phase 1: divergence detected, worker stopped (desired stays RUNNING).
    actions = self.reconciler.reconcile_once()
    self.assertIn(('w1', 'recycle'), actions)
    self.assertFalse(self.backend.is_running('w1'))
    self.assertEqual(self._status('w1'), 'STOPPED')

    # Phase 2: restarted with the parameters now in the DB.
    actions = self.reconciler.reconcile_once()
    self.assertIn(('w1', 'start'), actions)
    self.assertTrue(self.backend.is_running('w1'))
    self.assertEqual(self.backend.parameters_for('w1'), {'mode': 'new'})

  def test_unchanged_params_do_not_recycle(self):
    self.client.request_start('example_worker', worker_key='w1', parameters={'mode': 'same'})
    self.reconciler.reconcile_once()

    self.client.request_start('example_worker', worker_key='w1', parameters={'mode': 'same'})
    actions = self.reconciler.reconcile_once()

    self.assertEqual(actions, [])
    self.assertEqual(self.backend.start_count('w1'), 1)

  def test_unknown_spawn_parameters_never_recycle(self):
    # A backend that cannot read the live command line answers None; that must
    # not be mistaken for divergence, or healthy workers get recycled on a guess.
    self.client.request_start('example_worker', worker_key='w1', parameters={'mode': 'old'})
    self.reconciler.reconcile_once()
    self.backend.spawned_parameters = lambda **_: None

    self.client.request_start('example_worker', worker_key='w1', parameters={'mode': 'new'})
    actions = self.reconciler.reconcile_once()

    self.assertEqual(actions, [])
    self.assertTrue(self.backend.is_running('w1'))
    self.assertEqual(self.backend.start_count('w1'), 1)


class TestReconcilerLoop(_ReconcilerTestBase):
  def test_interruptible_sleep_never_uses_negative_duration(self):
    slept = []

    def record_sleep(seconds):
      self.assertGreaterEqual(seconds, 0)
      slept.append(seconds)

    with (
      patch(
        'crazy_workers.daemon.reconciler.time.monotonic',
        side_effect=[10.0, 10.05, 10.2],
      ),
      patch(
        'crazy_workers.daemon.reconciler.time.sleep',
        side_effect=record_sleep,
      ),
    ):
      self.reconciler._interruptible_sleep(0.1)

    self.assertEqual(len(slept), 1)
    self.assertAlmostEqual(slept[0], 0.05)

  def test_run_forever_runs_until_stopped(self):
    calls = []

    def fake_once():
      calls.append(1)
      if len(calls) >= 2:
        self.reconciler.stop()

    self.reconciler.reconcile_once = fake_once
    self.reconciler.run_forever()
    self.assertGreaterEqual(len(calls), 2)

  def test_run_forever_survives_reconcile_error(self):
    calls = []

    def boom():
      calls.append(1)
      if len(calls) == 1:
        raise RuntimeError('transient')
      self.reconciler.stop()

    self.reconciler.reconcile_once = boom
    self.reconciler.run_forever()  # must not propagate
    self.assertEqual(len(calls), 2)
