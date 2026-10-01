"""Legacy standalone RAW-buffer helpers, retained for historical compatibility.

The active runtime does not import this module. Reasoning now belongs to complete
chronological steps in WorkingContext, with the same retention as actions/results.
"""
from __future__ import annotations

from typing import Any


def raw_reasoning_message(items: list[dict[str, Any]]) -> dict[str, str] | None:
    if not items:
        return None
    return {
        "role": "user",
        "content": (
            "RECENT RAW REASONING. This is bounded short-term working memory, not project truth. "
            "It may be intentionally discarded after a Project State checkpoint.\n" +
            "\n\n".join(str(item["text"]) for item in items)
        ),
    }


def reasoning_token_cost(text: str, reported_tokens: int | None) -> int:
    if reported_tokens is not None and reported_tokens >= 0:
        return reported_tokens
    return max(1, (len(text) + 3) // 4)


def append_raw_reasoning(items: list[dict[str, Any]], text: str, reported_tokens: int | None) -> None:
    if text.strip():
        items.append({"text": text, "tokens": reasoning_token_cost(text, reported_tokens)})


def reasoning_buffer_tokens(items: list[dict[str, Any]]) -> int:
    return sum(int(item.get("tokens", 0) or 0) for item in items)


def reasoning_limit_reached(items: list[dict[str, Any]], limit: int) -> bool:
    return limit > 0 and reasoning_buffer_tokens(items) >= limit
