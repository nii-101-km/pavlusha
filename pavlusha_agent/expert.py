"""One stateless, text-only consultant; no task state or execution capabilities."""
from __future__ import annotations

import os
import math
import http.client
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .core import AgentError
from .provider import ChatProvider, ProviderContextOverflow


EXPERT_SYSTEM = (
    "You are a technical consultant assisting an autonomous Worker. Analyze only "
    "the supplied question and context. Return useful technical analysis or "
    "recommendations as ordinary answer text. You do not control the task and "
    "cannot perform actions. Do not return hidden chain-of-thought."
)
EXPERT_PROMPT = '''
OPTIONAL EXPERT CONSULTATION
You may explicitly ask a separate technical consultant for advice with:
{"action":"ask_expert","question":"concrete question","context":"selected plain-text context"}
Both fields are required; context may be empty. Only these fields are sent.
You own the task. Decide when consultation is useful, evaluate its answer, and
verify recommendations with your normal tools where practical. The Expert has
no tools or project access. Failures and the call ceiling return ordinary tool
errors; continue deciding what to do. Existing checkpoint/review gates apply.
'''.strip()


class Expert:
    def __init__(self, args):
        self.base_url = getattr(args, "expert_base_url", None)
        self.model = getattr(args, "expert_model", None)
        self.key_env = getattr(args, "expert_api_key_env", "EXPERT_API_KEY")
        self.max_tokens = getattr(args, "expert_max_tokens", 4096)
        self.reasoning_effort = getattr(args, "expert_reasoning_effort", None)
        self.max_calls = getattr(args, "expert_max_calls", 5)
        self.timeout = getattr(args, "expert_timeout", 60.0)
        self.transport = getattr(args, "expert_transport", "chat-completions")
        if self.transport not in {"chat-completions", "perplexity-agent"}:
            raise AgentError("unknown expert transport")
        self.network = bool(args.network)
        self.calls = 0
        try:
            url = urlsplit(self.base_url or "")
        except ValueError:
            raise AgentError("expert-base-url is not a valid HTTP(S) URL") from None
        if (url.scheme not in {"http", "https"} or not url.netloc or url.username
                or url.password or url.query or url.fragment):
            raise AgentError("expert-base-url must be an HTTP(S) URL without credentials, query or fragment")
        if not isinstance(self.model, str) or not self.model.strip():
            raise AgentError("--expert on requires --expert-model")
        if not self.key_env:
            raise AgentError("expert-api-key-env must name an environment variable")
        if self.max_tokens <= 0 or self.max_calls < 0 or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise AgentError("expert-max-tokens/timeout must be positive; expert-max-calls must be >= 0")

    def ask(self, question, context, *, telemetry=None):
        if not isinstance(question, str) or not question.strip() or not isinstance(context, str):
            return {"error": "invalid_expert_input", "hint": "question must be non-empty text; context must be text"}
        if not self.network:
            return {"error": "network_not_granted", "hint": "Expert API access requires --network"}
        if self.calls >= self.max_calls:
            return {"error": "expert_call_limit", "calls": self.calls, "max_calls": self.max_calls}
        key = os.environ.get(self.key_env, "")

        def safe(text):
            return text.replace(key, "[REDACTED]") if key else text

        self.calls += 1  # Every attempted request counts, including transport failures.
        started = time.monotonic()
        metadata = {"model": safe(self.model), "calls": self.calls, "max_calls": self.max_calls}
        if telemetry:
            telemetry({**metadata, "event": "started"})
        try:
            if self.transport == "perplexity-agent":
                result = self._perplexity_completion(safe(question), safe(context), key)
            else:
                provider = ChatProvider(self.base_url, self.model, key, self.timeout, 0.1, self.max_tokens)
                turn = provider.stateless_completion([
                    {"role": "system", "content": EXPERT_SYSTEM},
                    {"role": "user", "content": safe(question)},
                    {"role": "user", "content": safe(context)},
                ], reasoning_effort=self.reasoning_effort)
                if not turn.content.strip():
                    result = {"error": "expert_malformed_response"}
                else:
                    result = {"answer": turn.content, "prompt_tokens": turn.prompt_tokens,
                              "completion_tokens": turn.completion_tokens}
            if "answer" in result:
                result["answer"] = safe(result["answer"])
        except (AgentError, OSError, http.client.HTTPException, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            # Provider bodies/exception strings may echo credentials or prompts. Never publish them.
            cause = exc.__cause__ or exc
            if isinstance(cause, urllib.error.HTTPError):
                result = {"error": "expert_http_error", "http_status": cause.code}
                cause.close()
            elif isinstance(exc, ProviderContextOverflow):
                result = {"error": "expert_context_overflow"}
            elif isinstance(cause, TimeoutError) or (
                isinstance(cause, urllib.error.URLError) and isinstance(cause.reason, TimeoutError)
            ):
                result = {"error": "expert_timeout"}
            elif isinstance(cause, (OSError, urllib.error.URLError, http.client.HTTPException)):
                result = {"error": "expert_connection_error"}
            else:
                result = {"error": "expert_malformed_response"}
        result.update(metadata)
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        if telemetry:
            telemetry({k: v for k, v in result.items() if k != "answer"} |
                      {"event": "failed" if "error" in result else "completed"})
        return result

    def _perplexity_completion(self, question: str, context: str, key: str) -> dict:
        """Translate one explicit request using the documented POST /v1/agent contract.

        https://docs.perplexity.ai/api-reference/agent-post
        No preset, tools, previous response, background run or SDK retries.
        """
        payload = {
            "model": self.model,
            "instructions": EXPERT_SYSTEM,
            "input": [
                {"type": "message", "role": "user", "content": question},
                {"type": "message", "role": "user", "content": context},
            ],
            "max_output_tokens": self.max_tokens,
            "stream": False,
        }
        if self.reasoning_effort is not None:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/agent",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.load(response)
        usage = body.get("usage")
        usage = usage if isinstance(usage, dict) else {}

        def tokens(name):
            value = usage.get(name)
            return value if type(value) is int and value >= 0 else None

        counts = {"prompt_tokens": tokens("input_tokens"), "completion_tokens": tokens("output_tokens")}
        status = body.get("status")
        if status not in {"completed", "failed", "incomplete", "in_progress", "queued", "cancelled"}:
            raise ValueError("missing or invalid response status")
        if status != "completed" or body.get("error") is not None:
            return {"error": "expert_provider_failed", "status": status, **counts}
        texts = []
        for item in body["output"]:
            if item.get("type") != "message" or item.get("role") != "assistant":
                continue
            if item.get("status") != "completed":
                continue
            for part in item["content"]:
                if part.get("type") == "output_text":
                    if not isinstance(part.get("text"), str):
                        raise ValueError("invalid output text")
                    texts.append(part["text"])
        answer = "\n".join(texts)
        if not answer.strip():
            return {"error": "expert_malformed_response", **counts}
        return {"answer": answer, **counts}
