"""llama.cpp context discovery and controller-owned server lifecycle."""
from __future__ import annotations
from contextlib import contextmanager
import json
import urllib.request
from ..core import AgentError
from ..model_backend import ProviderUnsupported
from ._chat import ChatProvider


class LlamaCppAdapter(ChatProvider):
    backend_name = "llama-cpp"

    def _validate_response_model(self, reported_model):
        if isinstance(reported_model, str) and reported_model != self.model:
            raise AgentError(f"llama.cpp returned model {reported_model!r} instead of selected {self.model!r}; "
                             "response discarded; select the server's actual model alias")

    def _discover_context_length(self) -> int:
        # /props reports the effective slot capacity, not the training context
        # or the sum of capacities across parallel slots.
        model = self.resolve_model()
        request = urllib.request.Request(self.base_url + "/models", headers=self._headers())
        endpoint = self.base_url.removesuffix("/v1") + "/props"
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                ids = [item["id"] for item in json.load(response)["data"]]
            if ids != [model]:
                raise AgentError("llama.cpp context discovery requires exactly the selected model; "
                                 "pass --worker-context-budget explicitly for a router")
            request = urllib.request.Request(endpoint, headers=self._headers())
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                context = json.load(response)["default_generation_settings"]["n_ctx"]
            if type(context) is not int or context <= 0:
                raise ValueError("expected a positive integer slot n_ctx")
            return context
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AgentError(f"cannot discover llama.cpp context length from {endpoint}: {exc}; "
                             "pass --worker-context-budget explicitly") from exc

    @contextmanager
    def _released_worker(self, *, restore_guard=None):
        if self.llama_server is None:
            raise ProviderUnsupported("Worker release requires the controller-owned llama-server; "
                                      "use --llama-server-config; attached servers are not stopped")
        if self.llama_server.base_url != self.base_url:
            raise AgentError("Managed llama-server endpoint does not match Worker endpoint")
        with self.llama_server.released_worker(self.resolve_model(), **({"restore_guard": restore_guard} if restore_guard is not None else {})):
            yield
