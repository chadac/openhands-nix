"""Nix-based runtime for OpenHands.

Extends LocalRuntime to wrap the action execution server in a Nix shell
environment. Packages specified via NIX_PACKAGES env var or sandbox config
are made available to the agent's commands.

Usage:
    RUNTIME=nix NIX_PACKAGES="python3 nodejs jq" openhands-server
"""

import logging
import os
import subprocess
import sys
import tempfile
import threading

import openhands
from openhands.core.config import OpenHandsConfig
from openhands.runtime.impl.docker.docker_runtime import (
    APP_PORT_RANGE_1,
    APP_PORT_RANGE_2,
    EXECUTION_SERVER_PORT_RANGE,
    VSCODE_PORT_RANGE,
)
from openhands.runtime.impl.local.local_runtime import (
    ActionExecutionServerInfo,
    LocalRuntime,
    _RUNNING_SERVERS,
    _python_bin_path,
    get_user_info,
)
from openhands.runtime.plugins import PluginRequirement
from openhands.runtime.utils import find_available_tcp_port
from openhands.runtime.utils.command import get_action_execution_server_startup_command

logger = logging.getLogger(__name__)


def _get_nix_packages() -> list[str]:
    """Get Nix packages from env var. Returns flake references like 'nixpkgs#python3'."""
    raw = os.environ.get('NIX_PACKAGES', '').strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split() if p.strip()]


def _create_nix_server(
    config: OpenHandsConfig,
    plugins: list[PluginRequirement],
    workspace_prefix: str,
    nix_packages: list[str] | None = None,
) -> tuple[ActionExecutionServerInfo, str]:
    """Create an action execution server inside a nix shell.

    If nix_packages is provided and non-empty, the server subprocess is
    launched via `nix shell <packages> --command ...` so all specified
    packages are available on PATH.
    """
    logger.info('Creating a Nix server')

    # Set up workspace directory
    temp_workspace = tempfile.mkdtemp(
        prefix=f'openhands_workspace_{workspace_prefix}',
    )
    workspace_mount_path = temp_workspace

    # Find available ports
    execution_server_port = find_available_tcp_port(*EXECUTION_SERVER_PORT_RANGE)
    vscode_port = int(
        os.getenv('VSCODE_PORT') or str(find_available_tcp_port(*VSCODE_PORT_RANGE))
    )
    app_ports = [
        int(
            os.getenv('WORK_PORT_1')
            or os.getenv('APP_PORT_1')
            or str(find_available_tcp_port(*APP_PORT_RANGE_1))
        ),
        int(
            os.getenv('WORK_PORT_2')
            or os.getenv('APP_PORT_2')
            or str(find_available_tcp_port(*APP_PORT_RANGE_2))
        ),
    ]

    # Get user info
    user_id, username = get_user_info()

    # Build the action execution server command
    server_cmd = get_action_execution_server_startup_command(
        server_port=execution_server_port,
        plugins=plugins,
        app_config=config,
        python_prefix=[],
        python_executable=sys.executable,
        override_user_id=user_id,
        override_username=username,
    )

    # Wrap in nix shell if packages are specified
    if nix_packages:
        nix_cmd = ['nix', 'shell'] + nix_packages + ['--command'] + server_cmd
        logger.info(f'Starting Nix server with packages: {nix_packages}')
    else:
        nix_cmd = server_cmd
        logger.info('Starting Nix server without extra packages')

    logger.info(f'Server command: {nix_cmd}')

    env = os.environ.copy()
    code_repo_path = os.path.dirname(os.path.dirname(openhands.__file__))
    env['PYTHONPATH'] = os.pathsep.join([code_repo_path, env.get('PYTHONPATH', '')])
    env['OPENHANDS_REPO_PATH'] = code_repo_path
    env['LOCAL_RUNTIME_MODE'] = '1'
    env['VSCODE_PORT'] = str(vscode_port)
    env['PATH'] = f'{_python_bin_path()}{os.pathsep}{env.get("PATH", "")}'

    server_process = subprocess.Popen(  # noqa: S603
        nix_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
        env=env,
        cwd=code_repo_path,
    )

    log_thread_exit_event = threading.Event()

    def log_output() -> None:
        if not server_process or not server_process.stdout:
            logger.error('server process or stdout not available for logging.')
            return
        try:
            while server_process.poll() is None:
                if log_thread_exit_event.is_set():
                    break
                line = server_process.stdout.readline()
                if not line:
                    break
                logger.info(f'nix-server: {line.strip()}')
            if not log_thread_exit_event.is_set():
                for line in server_process.stdout:
                    if log_thread_exit_event.is_set():
                        break
                    logger.info(f'nix-server (remaining): {line.strip()}')
        except Exception as e:
            logger.error(f'Error reading nix-server output: {e}')
        finally:
            logger.info('nix-server log output thread finished.')

    log_thread = threading.Thread(target=log_output, daemon=True)
    log_thread.start()

    server_info = ActionExecutionServerInfo(
        process=server_process,
        execution_server_port=execution_server_port,
        vscode_port=vscode_port,
        app_ports=app_ports,
        log_thread=log_thread,
        log_thread_exit_event=log_thread_exit_event,
        temp_workspace=temp_workspace,
        workspace_mount_path=workspace_mount_path,
    )

    api_url = f'{config.sandbox.local_runtime_url}:{execution_server_port}'
    return server_info, api_url


class NixRuntime(LocalRuntime):
    """Runtime that wraps the action execution server in a Nix shell.

    Packages are specified via the NIX_PACKAGES environment variable
    (space-separated flake references like "nixpkgs#python3 nixpkgs#jq").

    All agent commands executed in the sandbox will have access to the
    specified Nix packages on their PATH.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._nix_packages = _get_nix_packages()

    async def connect(self) -> None:
        """Connect to or create a Nix-wrapped action execution server.

        This is a modified version of LocalRuntime.connect() that uses
        _create_nix_server instead of _create_server.
        """
        from openhands.runtime.base import RuntimeStatus

        self.set_runtime_status(RuntimeStatus.STARTING_RUNTIME)

        if self.attach_to_existing and self.sid in _RUNNING_SERVERS:
            server_info, api_url = _RUNNING_SERVERS[self.sid]
            logger.info(f'Reusing existing Nix server for session {self.sid}')
            self._server_info = server_info
            self.api_url = api_url
        else:
            server_info, api_url = _create_nix_server(
                config=self.config,
                plugins=self.plugins,
                workspace_prefix=self.sid,
                nix_packages=self._nix_packages,
            )
            self._server_info = server_info
            self.api_url = api_url
            _RUNNING_SERVERS[self.sid] = (server_info, api_url)

        logger.info(
            f'Nix server started on {self.api_url} '
            f'(packages: {self._nix_packages or "none"})'
        )

        self._wait_until_alive()
        await self.setup_initial_env()
        self._runtime_initialized = True
        self.set_runtime_status(RuntimeStatus.READY)
