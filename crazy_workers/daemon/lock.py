"""Single-owner locks for the reconcile daemon."""

import logging
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..core.recovery import RecoveryLock


logger = logging.getLogger('crazy_workers')


class FileDaemonLock:
  """Host-local daemon lock used by self-contained SQLite deployments."""

  def __init__(self, path):
    self._lock = RecoveryLock(path)
    self._held = False

  def acquire(self):
    self._held = self._lock.acquire()
    return self._held

  def is_held(self):
    return self._held

  def release(self):
    if self._held:
      self._lock.release()
      self._held = False


class PostgreSQLDaemonLock:
  """Session-level advisory lock shared by every container using a database.

  The dedicated SQLAlchemy connection stays open for the daemon lifetime. If
  that connection is lost, is_held fails and the reconciler exits at its next
  ownership check instead of reconnecting without ownership.
  """

  def __init__(self, engine, name):
    self.engine = engine
    self.name = name
    self.connection = None

  def acquire(self):
    connection = None
    try:
      connection = self.engine.connect()
      connection = connection.execution_options(isolation_level='AUTOCOMMIT')
      acquired = connection.execute(
        text("SELECT pg_try_advisory_lock(hashtextextended(current_database() || ':' || :lock_name, 0))"),
        {'lock_name': self.name},
      ).scalar()
    except SQLAlchemyError as exc:
      logger.error('Could not acquire PostgreSQL daemon lock %r: %s', self.name, exc)
      if connection is not None:
        connection.close()
      return False

    if not acquired:
      connection.close()
      return False

    self.connection = connection
    return True

  def is_held(self):
    if self.connection is None:
      return False
    try:
      self.connection.execute(text('SELECT 1')).scalar()
      return True
    except SQLAlchemyError as exc:
      logger.error('Lost PostgreSQL daemon lock %r: %s', self.name, exc)
      return False

  def release(self):
    if self.connection is None:
      return
    try:
      self.connection.execute(
        text("SELECT pg_advisory_unlock(hashtextextended(current_database() || ':' || :lock_name, 0))"),
        {'lock_name': self.name},
      )
    except SQLAlchemyError as exc:
      logger.warning('Could not explicitly release PostgreSQL daemon lock %r: %s', self.name, exc)
    finally:
      self.connection.close()
      self.connection = None


def create_daemon_lock(engine, file_path, name):
  """Return a database-wide lock for PostgreSQL, otherwise a local file lock."""
  if engine.dialect.name == 'postgresql':
    return PostgreSQLDaemonLock(engine, name)
  return FileDaemonLock(file_path)
