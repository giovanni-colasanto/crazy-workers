"""Process backends: the seam between the worker state machine and the OS.

WorkerManager never spawns or signals processes directly — it goes through a
ProcessBackend. SubprocessBackend is the default and launches real OS
processes. Alternative backends (e.g. for testing) live in
crazy_workers.testing and let consumer projects exercise their orchestration
logic without launching anything.
"""

import json
import logging
import os
import subprocess
import sys
import threading

from .engine import is_worker_process, read_spawned_parameters, terminate_process, worker_key_token


logger = logging.getLogger('crazy_workers')

# How long to wait after spawning before declaring the worker started. This is
# only an *immediate-failure* guard: it catches scripts that die on launch
# (bad import, syntax error, missing module). Keep it small so spawn stays
# responsive.
_STARTUP_GRACE_SECONDS = 0.05

_ESSENTIAL_ENV_KEYS = {
  'COMSPEC',
  'HOME',
  'LANG',
  'LC_ALL',
  'PATH',
  'PATHEXT',
  'PYTHONPATH',
  'SYSTEMROOT',
  'TEMP',
  'TMP',
  'TZ',
  'USERPROFILE',
  'VIRTUAL_ENV',
  'WINDIR',
}


class WorkerHandle:
  """Opaque reference to a launched worker, returned by ProcessBackend.spawn.

  `process` is backend-specific (a Popen for SubprocessBackend, None for
  backends that do not run real processes).
  """

  def __init__(self, pid, process=None):
    self.pid = pid
    self.process = process


class ProcessBackend:
  """Interface every backend implements. See SubprocessBackend for semantics."""

  def spawn(self, *, worker_key, worker_type, worker_path, parameters, env, log_path):
    """Launch a worker. Return a WorkerHandle, or None if it died immediately."""
    raise NotImplementedError

  def is_alive(self, *, pid, worker_key):
    """True only if `pid` is alive AND still belongs to `worker_key` (PID-reuse safe)."""
    raise NotImplementedError

  def spawned_parameters(self, *, pid, worker_key):
    """Parameters the live worker was spawned with, or None if unknowable."""
    raise NotImplementedError

  def terminate(self, *, pid, worker_key, handle=None, exclude_pids=None):
    """Stop the worker process, sparing any independently-managed children."""
    raise NotImplementedError


class SubprocessBackend(ProcessBackend):
  """Default backend: each worker runs as an independent OS process."""

  def __init__(self, *, log_destination='file', inherited_env_keys=None):
    if log_destination not in ('file', 'stdout'):
      raise ValueError("log_destination must be 'file' or 'stdout'")
    self.log_destination = log_destination
    self.inherited_env_keys = None if inherited_env_keys is None else {key.upper() for key in inherited_env_keys}

  def spawn(self, *, worker_key, worker_type, worker_path, parameters, env, log_path):
    child_env = self._build_child_env(env)

    log_fh = self._open_log(worker_key, log_path) if self.log_destination == 'file' else None
    # log_fh ownership is transferred to Popen; Popen duplicates it via os.dup2,
    # so we close our copy right after spawning.
    dest = subprocess.PIPE if self.log_destination == 'stdout' else (log_fh if log_fh else subprocess.DEVNULL)

    process = subprocess.Popen(
      [
        sys.executable,
        '-u',
        '-m',
        'crazy_workers._bootstrap',
        worker_key_token(worker_key),
        worker_path,
        json.dumps(parameters),
      ],
      stdout=dest,
      stderr=subprocess.STDOUT if self.log_destination == 'stdout' else dest,
      text=True,
      bufsize=1,
      env=child_env,
    )
    if log_fh:
      log_fh.close()

    if self.log_destination == 'stdout':
      self._start_output_forwarder(process, worker_key)

    if self._died_immediately(process, worker_key):
      return None

    logger.info(f'Worker {worker_key} started with PID {process.pid}')
    return WorkerHandle(process.pid, process)

  def _build_child_env(self, explicit_env):
    if self.inherited_env_keys is None:
      child_env = os.environ.copy()
    else:
      inherited = _ESSENTIAL_ENV_KEYS | self.inherited_env_keys
      child_env = {key: value for key, value in os.environ.items() if key.upper() in inherited}
    child_env.update(explicit_env or {})
    return child_env

  def _start_output_forwarder(self, process, worker_key):
    worker_logger = logging.getLogger(f'crazy_workers.worker.{worker_key}')

    def forward_output():
      if process.stdout is None:
        return
      try:
        for line in process.stdout:
          worker_logger.info(line.rstrip('\r\n'))
      finally:
        process.stdout.close()

    thread = threading.Thread(target=forward_output, name=f'crazy-workers-log-{worker_key}', daemon=True)
    thread.start()
    return thread

  def is_alive(self, *, pid, worker_key):
    return is_worker_process(pid, worker_key)

  def spawned_parameters(self, *, pid, worker_key):
    return read_spawned_parameters(pid, worker_key)

  def terminate(self, *, pid, worker_key, handle=None, exclude_pids=None):
    popen = handle.process if handle else None
    terminate_process(pid, popen_process=popen, exclude_pids=exclude_pids)

  def _open_log(self, worker_key, log_path):
    try:
      fh = open(log_path, 'a')
      logger.info(f'Worker {worker_key} logging to {log_path}')
      return fh
    except Exception as e:
      logger.error(f'Failed to open log file for worker {worker_key}: {e}')
      return None

  def _died_immediately(self, process, worker_key):
    try:
      process.wait(timeout=_STARTUP_GRACE_SECONDS)
      logger.error(f'Worker {worker_key} failed to start immediately (exit code: {process.returncode})')
      return True
    except subprocess.TimeoutExpired:
      # Expected case: still running after the grace period.
      return False
