from sqlalchemy.exc import SQLAlchemyError
from unittest.mock import MagicMock

from crazy_workers.daemon.lock import FileDaemonLock, PostgreSQLDaemonLock, create_daemon_lock


def _postgres_lock(acquired=True):
  engine = MagicMock()
  engine.dialect.name = 'postgresql'
  raw_connection = engine.connect.return_value
  connection = raw_connection.execution_options.return_value
  connection.execute.return_value.scalar.return_value = acquired
  return PostgreSQLDaemonLock(engine, 'lotec-workers'), connection


def test_postgresql_lock_acquires_checks_and_releases():
  lock, connection = _postgres_lock()

  assert lock.acquire()
  assert lock.is_held()
  lock.release()

  assert connection.execute.call_count == 3
  connection.close.assert_called_once()
  assert lock.connection is None


def test_postgresql_lock_closes_connection_when_already_owned():
  lock, connection = _postgres_lock(acquired=False)

  assert not lock.acquire()
  connection.close.assert_called_once()


def test_postgresql_lock_handles_acquire_error():
  lock, connection = _postgres_lock()
  connection.execute.side_effect = SQLAlchemyError('database unavailable')

  assert not lock.acquire()
  connection.close.assert_called_once()


def test_postgresql_lock_stops_reporting_ownership_after_connection_loss():
  lock, connection = _postgres_lock()
  assert lock.acquire()
  connection.execute.side_effect = SQLAlchemyError('connection lost')

  assert not lock.is_held()
  lock.release()
  connection.close.assert_called_once()


def test_release_without_connection_is_safe():
  lock, connection = _postgres_lock()

  lock.release()

  connection.close.assert_not_called()


def test_factory_uses_file_lock_for_sqlite(tmp_path):
  engine = MagicMock()
  engine.dialect.name = 'sqlite'
  path = tmp_path / 'daemon.lock'

  lock = create_daemon_lock(engine, str(path), 'workers')
  assert isinstance(lock, FileDaemonLock)
  assert lock.acquire()
  assert lock.is_held()
  lock.release()
  assert not path.exists()
  assert not lock.is_held()


def test_factory_uses_postgresql_advisory_lock():
  engine = MagicMock()
  engine.dialect.name = 'postgresql'

  lock = create_daemon_lock(engine, 'unused', 'workers')

  assert isinstance(lock, PostgreSQLDaemonLock)
