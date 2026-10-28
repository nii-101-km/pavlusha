"""Backend-independent model contract and errors used by the controller."""
from __future__ import annotations
from typing import Any, Callable, ContextManager, Protocol
from .core import AgentError, ProviderTurn


class ProviderContextOverflow(AgentError):
    """Confirmed context-capacity rejection; the controller may cold-recover."""


class ProviderUnsupported(AgentError):
    """The selected backend cannot safely perform this operation."""


class WorkerStreamInterrupted(AgentError):
    """Observer cancelled a response; its partial turn must never be executed."""
    def __init__(self, turn: ProviderTurn):
        super().__init__("Worker stream interrupted by observer")
        self.turn = turn


class ModelBackend(Protocol):
    backend_name: str
    model: str | None

    def resolve_model(self) -> str: ...
    def discover_context_length(self) -> int: ...
    def worker_completion(self, messages: list[dict[str, Any]], *,
                          on_delta: Callable | None = None, **options: Any) -> ProviderTurn: ...
    def stateless_completion(self, messages: list[dict[str, Any]], **options: Any) -> ProviderTurn: ...
    def released_worker(self, *, restore_guard=None) -> ContextManager[None]: ...
