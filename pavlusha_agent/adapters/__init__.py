"""Model adapter selection; backend identity is explicit, never guessed from a URL."""
from ..core import AgentError
from ._chat import ChatProvider
from .lm_studio import LMStudioAdapter
from .llama_cpp import LlamaCppAdapter


def selected_backend(backend="auto", *, owned=False):
    if backend == "auto":
        return "llama-cpp" if owned else "lm-studio"
    if backend not in {"lm-studio", "llama-cpp", "chat-completions"}:
        raise AgentError(f"Unknown model backend: {backend!r}")
    if owned and backend != "llama-cpp":
        raise AgentError("--llama-server-config requires --backend llama-cpp (or auto)")
    return backend


def adapter_class(backend="auto", *, owned=False):
    return {"lm-studio": LMStudioAdapter, "llama-cpp": LlamaCppAdapter,
            "chat-completions": ChatProvider}[selected_backend(backend, owned=owned)]


__all__ = ["ChatProvider", "LMStudioAdapter", "LlamaCppAdapter", "selected_backend"]
