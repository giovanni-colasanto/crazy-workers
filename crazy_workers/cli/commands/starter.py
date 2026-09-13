import os
from rich.prompt import IntPrompt

from ..ui import console, err_console


def start_worker(client, workers_dir, worker_type, worker_key=None, parameters=None):
  """Request a worker to run. The daemon performs the actual spawn."""
  if not worker_type:
    # Interactive mode: list .py files in workers_dir
    try:
      files = [f[:-3] for f in os.listdir(workers_dir) if f.endswith('.py') and f != '__init__.py']
    except Exception as e:
      err_console().print(f'[bold red]Error reading workers directory:[/bold red] {e}')
      return False

    if not files:
      console().print(f'[yellow]No worker scripts found in {workers_dir}[/yellow]')
      return False

    console().print('\n[bold cyan]Select a worker type to start:[/bold cyan]')
    for i, f in enumerate(files, 1):
      console().print(f'  [bold]{i})[/bold] {f}')

    choice = IntPrompt.ask('Enter the number', choices=[str(i) for i in range(1, len(files) + 1)])
    worker_type = files[choice - 1]

  # Surface a typo here rather than as a daemon CRASHED/retry loop later.
  if workers_dir is not None and not os.path.exists(os.path.join(workers_dir, f'{worker_type}.py')):
    err_console().print(f'[bold red]Error:[/bold red] Worker file {worker_type}.py not found in {workers_dir}')
    return False

  key = client.request_start(worker_type, worker_key=worker_key, parameters=parameters)
  console().print('[bold green]Requested:[/bold green] worker set to RUNNING (the daemon will start it)')
  console().print(f'  [bold]Key:[/bold] {key}')
  return True
