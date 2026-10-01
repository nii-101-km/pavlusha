"""OpenAI-compatible chat provider integration."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import AgentError, ProviderTurn, _trim


class ProviderContextOverflow(AgentError):
    """Observed provider context-capacity rejection; safe to cold-recover only this error."""


def provider_error(detail: str, *, status: int | None = None) -> AgentError:
    """Decode LM Studio's structured error, including its engine-protocol wrapper."""
    try:
        body = json.loads(detail)
        error = body.get("error") if isinstance(body, dict) else None
        wrapped = error.get("message") if isinstance(error, dict) else error
        prefix = "Engine protocol predict request returned 400: "
        if isinstance(wrapped, str) and wrapped.startswith(prefix):
            error = json.loads(wrapped[len(prefix):]).get("error")
        if (status in (None, 400) and isinstance(error, dict)
                and error.get("type") == "exceed_context_size_error"
                and error.get("code") == 400
                and type(error.get("n_prompt_tokens")) is int
                and type(error.get("n_ctx")) is int
                and error["n_prompt_tokens"] > error["n_ctx"] > 0):
            return ProviderContextOverflow(f"provider context overflow: {_trim(detail, 4000)}")
    except (ValueError, TypeError, AttributeError):
        pass
    return AgentError(f"provider {'HTTP ' + str(status) if status is not None else 'stream error'}: {_trim(detail, 4000)}")


class WorkerStreamInterrupted(AgentError):
    """The client closed a Worker response at an explicit observer cancellation request."""
    def __init__(self, turn: ProviderTurn):
        super().__init__("Worker stream interrupted by observer")
        self.turn = turn  # Diagnostic only: never treat this as an executable response.

class ChatProvider:
    """OpenAI-compatible chat client; KV lifetime belongs to the backend.

    No conversation, prefix snapshot, cache handle or generation is retained.
    The role-specific entry points keep Manager calls off the Worker path, but
    do not promise physical KV isolation on a shared LM Studio model instance.
    """
    def __init__(
        self,
        base_url: str,
        model: str | None,
        api_key: str,
        timeout: float,
        temperature: float,
        max_tokens: int,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens

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

        This does NOT disable LM Studio's backend prefix cache. The API does not
        expose a verified per-request disable control; do not add guessed flags.
        """
        return self.complete_turn(copy.deepcopy(messages), **options)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def resolve_model(self) -> str:
        if self.model:
            return self.model
        request = urllib.request.Request(self.base_url + "/models", headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
            models = body.get("data", [])
            ids = [item.get("id") for item in models if isinstance(item, dict) and item.get("id")]
        except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError) as exc:
            raise AgentError(f"cannot discover model from {self.base_url}/models: {exc}") from exc
        if not ids:
            raise AgentError("provider returned no models; pass --model explicitly")
        self.model = ids[0]
        return self.model

    def discover_context_length(self) -> int:
        """Return the active LM Studio instance context length.

        This is intentionally LM Studio-specific: the OpenAI-compatible /v1 API
        does not expose the loaded instance context size.
        """
        model = self.resolve_model()
        root = self.base_url
        if root.endswith("/v1"):
            root = root[:-3]
        endpoint = root.rstrip("/") + "/api/v1/models"
        request = urllib.request.Request(endpoint, headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
        except (OSError, urllib.error.URLError, ValueError, TypeError) as exc:
            raise AgentError(f"cannot discover LM Studio context length from {endpoint}: {exc}") from exc

        matches: list[int] = []
        all_values: list[int] = []

        def walk(value: Any, inherited_match: bool = False) -> None:
            if isinstance(value, dict):
                text_fields = [
                    str(v) for k, v in value.items()
                    if k in {"id", "key", "model", "model_key", "path", "name"}
                    and isinstance(v, str)
                ]
                here_match = inherited_match or any(model == text or model in text for text in text_fields)
                context = value.get("context_length")
                if isinstance(context, int) and not isinstance(context, bool) and context > 0:
                    all_values.append(context)
                    if here_match:
                        matches.append(context)
                for child in value.values():
                    walk(child, here_match)
            elif isinstance(value, list):
                for child in value:
                    walk(child, inherited_match)

        walk(body)
        candidates = sorted(set(matches))
        if not candidates:
            candidates = sorted(set(all_values))
        if len(candidates) != 1:
            detail = ", ".join(str(v) for v in candidates) if candidates else "none"
            raise AgentError(
                f"cannot unambiguously determine active LM Studio context length for {model!r} "
                f"(candidates: {detail}); pass --worker-context-budget explicitly"
            )
        return candidates[0]

    def complete_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        thinking_budget_tokens: int | None = None,
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
        if thinking_budget_tokens is not None:
            payload["thinking_budget_tokens"] = thinking_budget_tokens
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
            raise provider_error(detail, status=exc.code) from exc
        except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError, IndexError) as exc:
            raise AgentError(f"provider error: {type(exc).__name__}: {exc}") from exc

    def complete_turn_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        on_delta,
        response_format: dict[str, Any] | None = None,
        thinking_budget_tokens: int | None = None,
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
        if thinking_budget_tokens is not None: payload["thinking_budget_tokens"] = thinking_budget_tokens
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
        finish_reason = None
        usage: dict[str, Any] = {}
        interrupted = False
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if isinstance(chunk, dict) and "error" in chunk:
                        raise provider_error(data)
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
                        tool_calls.extend(item for item in calls if isinstance(item, dict))
        except urllib.error.HTTPError as exc:
            try: detail = exc.read().decode("utf-8", "replace")
            except Exception: detail = ""
            raise provider_error(detail, status=exc.code) from exc
        except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError, IndexError) as exc:
            raise AgentError(f"provider stream error: {type(exc).__name__}: {exc}") from exc

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

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        thinking_budget_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> str:
        turn = self.complete_turn(
            messages,
            response_format=response_format,
            thinking_budget_tokens=thinking_budget_tokens,
            tools=tools,
            tool_choice=tool_choice,
        )
        if not turn.content.strip():
            raise AgentError(
                "provider returned empty assistant content"
                + (f" (finish_reason={turn.finish_reason})" if turn.finish_reason else "")
            )
        return turn.content
