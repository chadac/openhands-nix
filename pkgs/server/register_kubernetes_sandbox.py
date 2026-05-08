"""Register the Kubernetes sandbox service with the OpenHands app server.

This module is loaded at Python startup via a .pth file. It installs
an import hook that monkey-patches config_from_env() to route
RUNTIME=kubernetes to KubernetesSandboxService.

All heavy imports are deferred until the hooked functions are actually
called to avoid circular imports at startup.
"""

import logging
import os
import importlib
import sys

logger = logging.getLogger(__name__)


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
                AGENT_SERVER_INTERNAL,
                KubernetesSandboxServiceInjector,
                KubernetesSandboxSpecServiceInjector,
            )
            from openhands.app_server.sandbox.sandbox_models import AGENT_SERVER
            from openhands.app_server.app_conversation.live_status_app_conversation_service import (
                LiveStatusAppConversationService,
            )

            # Monkey-patch _get_agent_server_url to prefer the internal
            # cluster URL over the external (ALB) URL for server-to-sandbox
            # API calls. The external URL goes through the ALB which may
            # require OIDC auth or have latency; the internal URL stays
            # within the cluster.
            _orig_get_url = LiveStatusAppConversationService._get_agent_server_url

            def _patched_get_agent_server_url(self, sandbox):
                exposed_urls = sandbox.exposed_urls
                assert exposed_urls is not None
                # Prefer AGENT_SERVER_INTERNAL (cluster-local), fall back to AGENT_SERVER
                for exposed_url in exposed_urls:
                    if exposed_url.name == AGENT_SERVER_INTERNAL:
                        return exposed_url.url
                return _orig_get_url(self, sandbox)

            LiveStatusAppConversationService._get_agent_server_url = _patched_get_agent_server_url

            config = _original()
            config.sandbox = KubernetesSandboxServiceInjector()
            config.sandbox_spec = KubernetesSandboxSpecServiceInjector()
            return config

        config_module.config_from_env = patched_config_from_env


def _register():
    if os.getenv("RUNTIME") not in ("kubernetes", "openhands_nix.kubenix_runtime.KubenixRuntime"):
        return
    sys.meta_path.append(_KubernetesConfigHook())


_register()
