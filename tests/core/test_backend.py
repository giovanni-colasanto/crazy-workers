import io
import os
import pytest
import subprocess
from unittest.mock import MagicMock, patch

from crazy_workers.core.backend import SubprocessBackend


def test_invalid_log_destination_is_rejected():
  with pytest.raises(ValueError):
    SubprocessBackend(log_destination='syslog')


def test_default_environment_inherits_everything():
  backend = SubprocessBackend()
  with patch.dict(os.environ, {'CW_SECRET': 'visible'}, clear=True):
    child_env = backend._build_child_env(None)
  assert child_env['CW_SECRET'] == 'visible'


def test_allowlist_excludes_unlisted_application_secrets():
  backend = SubprocessBackend(inherited_env_keys={'DATABASE_URL'})
  with patch.dict(
    os.environ,
    {'PATH': '/bin', 'DATABASE_URL': 'postgresql://db', 'UNRELATED_SECRET': 'hidden'},
    clear=True,
  ):
    child_env = backend._build_child_env({'EXPLICIT': 'yes'})

  assert child_env['PATH'] == '/bin'
  assert child_env['DATABASE_URL'] == 'postgresql://db'
  assert child_env['EXPLICIT'] == 'yes'
  assert 'UNRELATED_SECRET' not in child_env


def test_spawn_with_stdout_starts_output_forwarder():
  backend = SubprocessBackend(log_destination='stdout')
  process = MagicMock(pid=321)

  with (
    patch('crazy_workers.core.backend.subprocess.Popen', return_value=process) as popen,
    patch.object(backend, '_start_output_forwarder') as start_output_forwarder,
    patch.object(backend, '_died_immediately', return_value=False),
  ):
    handle = backend.spawn(
      worker_key='disk_watcher',
      worker_type='disk_watcher',
      worker_path='/workers/disk_watcher.py',
      parameters={'interval': 60},
      env={},
      log_path='/unused/worker.log',
    )

  assert handle.pid == 321
  assert handle.process is process
  assert popen.call_args.kwargs['stdout'] is subprocess.PIPE
  assert popen.call_args.kwargs['stderr'] is subprocess.STDOUT
  start_output_forwarder.assert_called_once_with(process, 'disk_watcher')


def test_stdout_forwarder_prefixes_worker_logger():
  backend = SubprocessBackend(log_destination='stdout')
  process = MagicMock()
  process.stdout = io.StringIO('first line\nsecond line\n')

  with patch('crazy_workers.core.backend.logging.getLogger') as get_logger:
    thread = backend._start_output_forwarder(process, 'disk_watcher')
    thread.join(timeout=1)

  get_logger.assert_called_once_with('crazy_workers.worker.disk_watcher')
  worker_logger = get_logger.return_value
  worker_logger.info.assert_any_call('first line')
  worker_logger.info.assert_any_call('second line')
  assert process.stdout.closed


def test_stdout_forwarder_accepts_missing_pipe():
  backend = SubprocessBackend(log_destination='stdout')
  process = MagicMock()
  process.stdout = None

  thread = backend._start_output_forwarder(process, 'worker')
  thread.join(timeout=1)

  assert not thread.is_alive()
