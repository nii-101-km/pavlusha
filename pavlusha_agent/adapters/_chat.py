"""Shared OpenAI-compatible wire transport, private to model adapters."""
from __future__ import annotations
import copy
import http.client
import json
import urllib.error
import urllib.request
from typing import Any
from ..core import AgentError, ProviderTurn
from ._errors import chat_error
from ..model_backend import ProviderUnsupported, WorkerStreamInterrupted


class ChatProvider:
    """Stable chat entry point; constructing it selects a concrete backend.

    The auto default is LM Studio, or llama.cpp with an owned server.
    No server probing or fallback to a different backend occurs.
    Expert alone may request the generic chat-completions transport.
    """
    backend_name = "chat-completions"

    def __new__(cls, *args, backend="auto", **kwargs):
        if cls is ChatProvider:
            from . import adapter_class
            owned = kwargs.get("llama_server")
            if owned is None and len(args) > 6:
                owned = args[6]
            target = adapter_class(backend, owned=owned is not None)
            return object.__new__(target)
        return object.__new__(cls)

    def __init__(
        self,
        base_url: str,
        model: str | None,
        api_key: str,
        timeout: float,
        temperature: float,
        max_tokens: int,
        llama_server=None,
        *, backend="auto",
    ) -> None:
        if backend != "auto" and backend != self.backend_name:
            raise AgentError(f"Adapter {self.backend_name!r} cannot use backend {backend!r}")
        if llama_server is not None and self.backend_name != "llama-cpp":
            raise AgentError("Managed llama-server requires --backend llama-cpp")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.llama_server = llama_server

    def worker_completion(
        self, messages: list[dict[str, Any]], *, on_delta=None, **options: Any,
    ) -> ProviderTurn:
        """Send the authoritative Worker prompt, optionally preserving streaming."""
        if on_delta is not None:
            return self.complete_turn_stream(messages, on_delta=on_delta, **options)
        return self.complete_turn(messages, **options)

    def stateless_completion(
        self, messages: list[dict[str, Any]], **options: Any,
    ) -> ProviderTurn:
        """Independent textual request; no inherited or exported conversation state.

        Backend prefix caching may still apply. This method does not promise
        physical KV isolation or add unverified cache-control flags.
        """
        return self.complete_turn(copy.deepcopy(messages), **options)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def discover_context_length(self) -> int:
        return self._discover_context_length()

    def _discover_context_length(self) -> int:
        raise ProviderUnsupported("Context discovery is unavailable; pass --worker-context-budget explicitly")

    def released_worker(self, *, restore_guard=None):
        if restore_guard is None:
            return self._released_worker()
        return self._released_worker(restore_guard=restore_guard)

    def _released_worker(self, *, restore_guard=None):
        raise ProviderUnsupported("Worker release is unavailable for this backend")

    def _model_lifecycle_request(self, path, payload=None):
        raise ProviderUnsupported("LM Studio lifecycle API is unavailable for this backend")

    decode_error = staticmethod(chat_error)

    def _validate_response_model(self, reported_model):
        """Generic remote services may legitimately return a versioned model ID."""

    def resolve_model(self) -> str:
        if self.model:
            return self.model
        return self._discover_model()

    def _discover_model(self) -> str:
        request = urllib.request.Request(self.base_url + "/models", headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
            models = body.get("data", [])
            ids = [item.get("id") for item in models if isinstance(item, dict) and item.get("id")]
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise AgentError(f"cannot discover model from {self.base_url}/models: {exc}") from exc
        if len(ids) != 1:
            raise AgentError("provider requires exactly one model; pass --model explicitly")
        self.model = ids[0]
        return self.model

    def complete_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> ProviderTurn:
        model = self.resolve_model()
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
            self._validate_response_model(body.get("model"))
            choice = body["choices"][0]
            message = choice["message"]
            content = message.get("content")
            reasoning = message.get("reasoning_content")
            usage = body.get("usage") if isinstance(body, dict) else None
            usage = usage if isinstance(usage, dict) else {}
            completion_details = usage.get("completion_tokens_details")
            completion_details = completion_details if isinstance(completion_details, dict) else {}

            def usage_int(value: Any) -> int | None:
                return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                tool_calls = []
            return ProviderTurn(
                content=content if isinstance(content, str) else "",
                reasoning_content=reasoning if isinstance(reasoning, str) else "",
                finish_reason=choice.get("finish_reason"),
                prompt_tokens=usage_int(usage.get("prompt_tokens")),
                completion_tokens=usage_int(usage.get("completion_tokens")),
                reasoning_tokens=usage_int(completion_details.get("reasoning_tokens")),
                tool_calls=[item for item in tool_calls if isinstance(item, dict)],
            )
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            raise self.decode_error(detail, status=exc.code) from exc
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise AgentError(f"provider error: {type(exc).__name__}: {exc}") from exc

    def complete_turn_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        on_delta,
        should_cancel=None,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> ProviderTurn:
        """Stream one turn while reconstructing the same ProviderTurn contract.

        `on_delta(kind, text)` observes `reasoning` or `content`; exactly False requests
        interruption. The response is closed before WorkerStreamInterrupted is raised.
        Closing HTTP stops local consumption; remote computation cancellation is not guaranteed.
        The provider request asks for final usage in the SSE stream when supported.
        """
        model = self.resolve_model()
        payload: dict[str, Any] = {
            "model": model, "messages": messages, "temperature": self.temperature,
            "max_tokens": self.max_tokens, "stream": True,
            "stream_options": {"include_usage": True},
        }
        if response_format is not None: payload["response_format"] = response_format
        if reasoning_effort is not None: payload["reasoning_effort"] = reasoning_effort
        if tools is not None: payload["tools"] = tools
        if tool_choice is not None: payload["tool_choice"] = tool_choice
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(), method="POST",
        )
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        indexed_calls: dict[int, dict[str, Any]] = {}
        finish_reason = None
        usage: dict[str, Any] = {}
        interrupted = False
        completed = False
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw_line in response:
                    # Check every SSE line, including tool-only deltas, usage and DONE.
                    if should_cancel is not None and should_cancel():
                        interrupted = True
                        break
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        completed = True
                        break
                    chunk = json.loads(data)
                    if not isinstance(chunk, dict):
                        raise ValueError("expected an SSE object")
                    self._validate_response_model(chunk.get("model"))
                    if isinstance(chunk, dict) and "error" in chunk:
                        raise self.decode_error(data)
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0]
                    if choice.get("finish_reason") is not None:
                        finish_reason = choice.get("finish_reason")
                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        continue
                    reasoning = delta.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        reasoning_parts.append(reasoning)
                        if on_delta("reasoning", reasoning) is False:
                            interrupted = True
                            break
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        content_parts.append(content)
                        if on_delta("content", content) is False:
                            interrupted = True
                            break
                    calls = delta.get("tool_calls")
                    if isinstance(calls, list):
                        for item in calls:
                            if not isinstance(item, dict):
                                continue
                            index = item.get("index")
                            if type(index) is not int or index < 0:
                                # Preserve legacy unindexed diagnostics; never guess
                                # which parallel call owns an arguments fragment.
                                tool_calls.append(copy.deepcopy(item))
                                continue
                            if index not in indexed_calls:
                                indexed_calls[index] = {}
                                tool_calls.append(indexed_calls[index])
                            call = indexed_calls[index]
                            for field in ("id", "type"):
                                if item.get(field) is not None:
                                    call[field] = item[field]
                            function = item.get("function")
                            if isinstance(function, dict):
                                merged = call.setdefault("function", {})
                                for field in ("name", "arguments"):
                                    if isinstance(function.get(field), str):
                                        merged[field] = merged.get(field, "") + function[field]
        except urllib.error.HTTPError as exc:
            try: detail = exc.read().decode("utf-8", "replace")
            except Exception: detail = ""
            raise self.decode_error(detail, status=exc.code) from exc
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise AgentError(f"provider stream error: {type(exc).__name__}: {exc}") from exc

        if not interrupted and not completed:
            raise AgentError("provider stream ended before [DONE]; incomplete response discarded")

        completion_details = usage.get("completion_tokens_details")
        completion_details = completion_details if isinstance(completion_details, dict) else {}
        def usage_int(value: Any) -> int | None:
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        turn = ProviderTurn(
            content="".join(content_parts), reasoning_content="".join(reasoning_parts),
            finish_reason=finish_reason, prompt_tokens=usage_int(usage.get("prompt_tokens")),
            completion_tokens=usage_int(usage.get("completion_tokens")),
            reasoning_tokens=usage_int(completion_details.get("reasoning_tokens")),
            tool_calls=tool_calls,
        )
        if interrupted:
            raise WorkerStreamInterrupted(turn)
        return turn
