"""Register the Kubernetes sandbox service with the OpenHands app server.

This module is loaded at Python startup via a .pth file. It installs an
import hook that monkey-patches config_from_env() to recognize
RUNTIME=kubernetes, routing to KubernetesSandboxService.

All heavy imports are deferred until config_from_env() is actually called
to avoid circular imports at startup.
"""

import os
import importlib
import sys


class _KubernetesConfigHook:
    """Meta path finder that patches config_from_env after openhands.app_server.config loads."""

    _patched = False

    def find_module(self, fullname, path=None):
        if fullname == "openhands.app_server.config" and not self._patched:
            return self
        return None

    def load_module(self, fullname):
        # Remove ourselves temporarily to avoid recursion
        sys.meta_path.remove(self)
        try:
            module = importlib.import_module(fullname)
        finally:
            # Re-add if not yet patched (in case of import error)
            if not self._patched:
                sys.meta_path.append(self)

        self._patch(module)
        return module

    def _patch(self, config_module):
        self._patched = True
        _original = config_module.config_from_env

        def patched_config_from_env():
            from openhands_nix.kubernetes_sandbox import (
                KubernetesSandboxServiceInjector,
                KubernetesSandboxSpecServiceInjector,
            )

            config = _original()
            config.sandbox = KubernetesSandboxServiceInjector()
            config.sandbox_spec = KubernetesSandboxSpecServiceInjector()
            return config

        config_module.config_from_env = patched_config_from_env


def _register():
    if os.getenv("RUNTIME") != "kubernetes":
        return
    sys.meta_path.append(_KubernetesConfigHook())


_register()
