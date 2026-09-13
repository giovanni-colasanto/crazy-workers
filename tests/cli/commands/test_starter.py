import json
import os
from io import StringIO
from unittest.mock import MagicMock, patch

from crazy_workers import WorkerClient
from crazy_workers.cli.commands import start_worker
from crazy_workers.cli.main import main as cli_main
from tests.base import BaseTestCase


class TestCliCommandStarter(BaseTestCase):
  def setUp(self):
    super().setUp()
    self.env_patcher = patch.dict(os.environ, {'CRAZY_WORKERS_DIR': self.workers_path})
    self.env_patcher.start()

  def tearDown(self):
    self.env_patcher.stop()
    super().tearDown()

  def test_start_worker_interactive_no_files(self):
    client = MagicMock()
    with patch('os.listdir', return_value=[]):
      with patch('sys.stdout', new=StringIO()) as fake_out:
        res = start_worker(client, self.workers_path, None)
        self.assertFalse(res)
        self.assertIn('No worker scripts found', fake_out.getvalue())
    client.request_start.assert_not_called()

  def test_start_worker_interactive_selection(self):
    client = MagicMock()
    client.request_start.return_value = 'w1'
    with patch('os.listdir', return_value=['w1.py', 'w2.py', 'not_python.txt']):
      with patch('os.path.exists', return_value=True):  # pretend w1.py exists
        with patch('rich.prompt.IntPrompt.ask', return_value=1):
          with patch('sys.stdout', new=StringIO()):
            res = start_worker(client, self.workers_path, None)
            self.assertTrue(res)
            client.request_start.assert_called_with('w1', worker_key=None, parameters=None)

  def test_start_worker_error_reading_dir(self):
    client = MagicMock()
    with patch('os.listdir', side_effect=OSError('Permission denied')):
      with patch('sys.stderr', new=StringIO()) as fake_err:
        res = start_worker(client, self.workers_path, None)
        self.assertFalse(res)
        self.assertIn('Error reading workers directory', fake_err.getvalue())

  def test_start_worker_missing_script(self):
    client = MagicMock()
    with patch('sys.stderr', new=StringIO()) as fake_err:
      res = start_worker(client, self.workers_path, 'does_not_exist')
      self.assertFalse(res)
      self.assertIn('not found', fake_err.getvalue())
    client.request_start.assert_not_called()

  def test_cli_start_persists_request_without_spawning(self):
    params = {'test_key': 'test_val', 'num': 123}
    args = ['crazy-workers', 'start', 'example_worker', '--key', 'params_test', '--params', json.dumps(params)]
    with patch('sys.argv', args):
      with patch('sys.stdout', new=StringIO()) as fake_out:
        try:
          cli_main()
        except SystemExit as e:
          self.assertEqual(e.code, 0)
        self.assertIn('Requested', fake_out.getvalue())
        self.assertIn('params_test', fake_out.getvalue())

    sqlite = f'sqlite:///{os.path.join(self.workers_path, ".service", "workers.db")}'
    with WorkerClient(db_url=sqlite) as client:
      worker = client.get('params_test')
    self.assertEqual(worker['parameters'], params)
    self.assertEqual(worker['desired_status'], 'RUNNING')
    self.assertNotEqual(worker['status'], 'RUNNING')  # no daemon ran, so nothing started

  def test_cli_start_invalid_json_params(self):
    args = ['crazy-workers', 'start', 'example_worker', '--params', 'not-valid-json{']
    with patch('sys.argv', args):
      with patch('sys.stderr', new=StringIO()) as fake_err:
        with self.assertRaises(SystemExit) as cm:
          cli_main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn('Invalid JSON', fake_err.getvalue())

  def test_remote_explicit_start_does_not_require_local_worker_files(self):
    db_url = f'sqlite:///{os.path.join(self.test_dir, "remote.db")}'
    with WorkerClient(db_url=db_url, create_tables=True):
      pass

    with patch.dict(os.environ, {'CRAZY_WORKERS_DB_URL': db_url}, clear=True):
      with patch('sys.argv', ['crazy-workers', 'start', 'remote_worker', '--key', 'remote_1']):
        with patch('sys.stdout', new=StringIO()) as fake_out:
          cli_main()

    self.assertIn('Requested', fake_out.getvalue())
    with WorkerClient(db_url=db_url) as client:
      self.assertEqual(client.get('remote_1')['worker_type'], 'remote_worker')
