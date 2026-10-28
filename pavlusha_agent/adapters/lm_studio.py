"""LM Studio native lifecycle/context and backend error decoding."""
from __future__ import annotations
from contextlib import contextmanager
import copy
import http.client
import json
import sys
import math
import urllib.error
import urllib.request
from typing import Any
from ..core import AgentError, ProcessCleanupError, _trim
from ._errors import chat_error
from ._chat import ChatProvider


def provider_error(detail: str, *, status: int | None = None) -> AgentError:
    return chat_error(detail, status=status, unwrap_lm_studio=True)


class LMStudioAdapter(ChatProvider):
    backend_name = "lm-studio"
    decode_error = staticmethod(provider_error)

    def _validate_response_model(self, reported_model):
        if not isinstance(reported_model, str) or reported_model == self.model:
            return
        for model, instance in self._loaded_worker_instances():
            identities = (model.get("key"), instance.get("id"))
            if self.model in identities and reported_model in identities:
                return
        raise AgentError(f"LM Studio returned model {reported_model!r} instead of selected {self.model!r}; "
                         "response discarded")

    def _model_lifecycle_request(self, path: str, payload=None) -> dict[str, Any]:
        """LM Studio native API only; finite requests, no guessed unload controls."""
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise AgentError("Worker lifecycle requires a finite positive API timeout")
        root = self.base_url.removesuffix("/v1")
        request = urllib.request.Request(
            root + "/api/v1/models" + path, headers=self._headers(),
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            method="GET" if payload is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
            if not isinstance(body, dict):
                raise ValueError("expected an object")
            return body
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except (OSError, http.client.HTTPException):
                detail = str(exc)
            raise AgentError(f"Worker lifecycle API {path or '/'} failed: HTTP {exc.code}: "
                             f"{_trim(detail, 4000)}") from exc
        except (OSError, http.client.HTTPException, ValueError, TypeError) as exc:
            raise AgentError(f"Worker lifecycle API {path or '/'} failed: {exc}") from exc

    def _loaded_worker_instances(self):
        models = self._model_lifecycle_request("").get("models")
        if not isinstance(models, list):
            raise AgentError("Worker release requires LM Studio native /api/v1/models")
        instances = []
        for model in models:
            if not isinstance(model, dict) or model.get("type") != "llm":
                continue
            for instance in model.get("loaded_instances", []):
                if isinstance(instance, dict):
                    instances.append((model, instance))
        return instances

    @contextmanager
    def _released_worker(self, *, restore_guard=None):
        """Unload one unambiguous Worker, restore even after an uncertain unload.

        No inference/KV state is retained. Backend failure can prevent restoration;
        in that case raise rather than resume inference or rerun the shell command.
        """
        selected = self.resolve_model()
        matches = [(m, i) for m, i in self._loaded_worker_instances()
                   if selected in (m.get("key"), i.get("id"))]
        if len(matches) != 1:
            raise AgentError("Worker release requires exactly one matching loaded instance")
        model, instance = matches[0]
        key, instance_id, config = model.get("key"), instance.get("id"), instance.get("config")
        # Explicit fields in LM Studio 0.4.25's native model-load request schema.
        # Loaded-instance metadata can contain more; those fields use backend defaults.
        supported = {"context_length", "eval_batch_size", "flash_attention",
                     "num_experts", "offload_kv_cache_to_gpu", "physical_batch_size",
                     "parallel", "context_checkpoints", "reasoning_budget_message",
                     "speculative_draft_mtp", "speculative_draft_simple",
                     "speculative_draft_model", "speculative_draft_max_tokens",
                     "speculative_draft_min_tokens", "speculative_draft_min_continue_probability",
                     "prompt_template", "ttl_seconds"}
        if (not isinstance(key, str) or not key or not isinstance(instance_id, str)
                or not instance_id):
            raise AgentError("Worker release requires a model key and instance ID")
        config = config if isinstance(config, dict) else {}
        restore_config = {k: copy.deepcopy(v) for k, v in config.items() if k in supported}
        load = {"model": key, **restore_config, "echo_load_config": True}
        entered = False
        try:
            reply = self._model_lifecycle_request("/unload", {"instance_id": instance_id})
            if reply.get("instance_id") != instance_id:
                raise AgentError("Worker unload returned a different instance")
            if any(i.get("id") == instance_id for _, i in self._loaded_worker_instances()):
                raise AgentError("Worker instance remains loaded; shell was not launched")
            entered = True
            yield
        finally:
            if isinstance(sys.exc_info()[1], ProcessCleanupError):
                raise  # Owned work is not confirmed dead; keep Worker unloaded.
            if entered and restore_guard is not None:
                restore_guard()  # Failure deliberately keeps Worker unloaded; no blind reload.
            # A lost unload response may still mean the server unloaded the model.
            try:
                try:
                    remaining = [(m, i) for m, i in self._loaded_worker_instances()
                                 if m.get("key") == key]
                except AgentError:
                    # An unavailable status endpoint must not skip the reload attempt.
                    remaining = []
                if remaining:
                    if len(remaining) != 1 or remaining[0][1].get("id") != instance_id:
                        raise AgentError("Worker instance changed during release; cannot safely restore")
                    self.model = instance_id
                else:
                    reply = self._model_lifecycle_request("/load", load)
                    restored_id = reply.get("instance_id")
                    if (reply.get("status") != "loaded" or not isinstance(restored_id, str)
                            or not restored_id):
                        raise AgentError("Worker restore did not confirm a loaded instance")
                    restored = [(m, i) for m, i in self._loaded_worker_instances()
                                if m.get("key") == key]
                    if len(restored) != 1 or restored[0][1].get("id") != restored_id:
                        raise AgentError("Worker restore did not confirm the same model key")
                    self.model = restored_id
            except AgentError as exc:
                raise AgentError(f"Worker restoration failed; runtime stopped: {exc}") from exc

    def _discover_model(self) -> str:
        instances = self._loaded_worker_instances()
        if not instances:
            raise AgentError("LM Studio has no loaded Worker model; load a model first, "
                             "or pass --model and --worker-context-budget for explicit loading on demand")
        if len(instances) != 1:
            raise AgentError("LM Studio has multiple loaded Worker instances; pass --model explicitly")
        instance_id = instances[0][1].get("id")
        if not isinstance(instance_id, str) or not instance_id:
            raise AgentError("LM Studio returned no valid loaded instance ID")
        self.model = instance_id
        return instance_id

    def _discover_context_length(self) -> int:
        """Read only the selected loaded instance's actual context capacity."""
        model = self.resolve_model()
        matches = [(m, i) for m, i in self._loaded_worker_instances()
                   if model in (m.get("key"), i.get("id"))]
        if len(matches) != 1:
            raise AgentError(f"cannot unambiguously determine active LM Studio context length for {model!r}; "
                             "select one loaded instance or pass --worker-context-budget explicitly")
        config = matches[0][1].get("config")
        context = config.get("context_length") if isinstance(config, dict) else None
        if type(context) is not int or context <= 0:
            raise AgentError("LM Studio loaded instance has no valid context_length; "
                             "pass --worker-context-budget explicitly")
        return context
