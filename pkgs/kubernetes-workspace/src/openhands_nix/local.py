"""LocalNixEnvironment: local development workspace powered by Nix.

Wraps commands in `nix shell` so the configured packages are available
without containers. Great for local development and testing.

Example:
    workspace = LocalNixEnvironment(
        nix=NixEnvironment(packages=["nixpkgs#nodejs", "nixpkgs#ripgrep"]),
        working_dir="/home/user/project",
    )
    result = workspace.execute_command("node --version")
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from pydantic import Field
from openhands.sdk.git.models import GitChange, GitDiff
from openhands.sdk.workspace.base import BaseWorkspace
from openhands.sdk.workspace.models import CommandResult, FileOperationResult

from openhands_nix.workspace import NixEnvironment

logger = logging.getLogger(__name__)


class LocalNixEnvironment(BaseWorkspace):
    """Local workspace that runs commands inside a `nix shell` environment.

    No containers, no network overhead — just Nix providing the packages
    on the local machine. Commands are wrapped in `nix shell ... --command`
    so the right tools are on PATH.
    """

    nix: NixEnvironment = Field(
        default_factory=NixEnvironment,
        description="Nix environment configuration",
    )
    working_dir: str = Field(default=".")

    def _nix_shell_prefix(self) -> list[str]:
        """Build the `nix shell ...` prefix for wrapping commands."""
        if not self.nix.has_nix_config:
            return []

        shell_args = self.nix.to_nix_shell_args()
        if not shell_args:
            return []

        return [
            "nix", "shell",
            "--no-write-lock-file",
            *shell_args,
            "--command",
        ]

    def _run(
        self,
        command: str,
        cwd: str | Path | None = None,
        timeout: float = 30.0,
    ) -> subprocess.CompletedProcess:
        """Run a command, optionally wrapped in nix shell."""
        work_dir = str(cwd) if cwd else self.working_dir
        prefix = self._nix_shell_prefix()

        if prefix:
            full_cmd = prefix + ["bash", "-c", command]
        else:
            full_cmd = ["bash", "-c", command]

        logger.debug("Running: %s (cwd=%s)", " ".join(full_cmd), work_dir)
        return subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            cwd=work_dir,
            timeout=timeout,
        )

    def execute_command(
        self,
        command: str,
        cwd: str | Path | None = None,
        timeout: float = 30.0,
    ) -> CommandResult:
        try:
            result = self._run(command, cwd=cwd, timeout=timeout)
            return CommandResult(
                command=command,
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                timeout_occurred=False,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                command=command,
                exit_code=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                timeout_occurred=True,
            )

    def file_upload(
        self,
        source_path: str | Path,
        destination_path: str | Path,
    ) -> FileOperationResult:
        src = Path(source_path)
        dst = Path(destination_path)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return FileOperationResult(
                success=True,
                source_path=str(src),
                destination_path=str(dst),
                file_size=dst.stat().st_size,
                error=None,
            )
        except Exception as e:
            return FileOperationResult(
                success=False,
                source_path=str(src),
                destination_path=str(dst),
                file_size=None,
                error=str(e),
            )

    def file_download(
        self,
        source_path: str | Path,
        destination_path: str | Path,
    ) -> FileOperationResult:
        return self.file_upload(source_path, destination_path)

    def git_changes(self, path: str | Path) -> list[GitChange]:
        result = self._run(
            f"git -C {path} status --porcelain",
            timeout=10.0,
        )
        # Delegate to SDK's git utilities if available
        from openhands.sdk.workspace.local import LocalWorkspace

        local = LocalWorkspace(working_dir=self.working_dir)
        return local.git_changes(path)

    def git_diff(self, path: str | Path) -> GitDiff:
        from openhands.sdk.workspace.local import LocalWorkspace

        local = LocalWorkspace(working_dir=self.working_dir)
        return local.git_diff(path)

    def pause(self) -> None:
        logger.debug("LocalNixEnvironment.pause() is a no-op")

    def resume(self) -> None:
        logger.debug("LocalNixEnvironment.resume() is a no-op")
