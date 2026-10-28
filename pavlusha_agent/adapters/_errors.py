"""Wire error decoding shared by backend adapters."""
import json
from ..core import AgentError, _trim
from ..model_backend import ProviderContextOverflow


def chat_error(detail: str, *, status: int | None = None, unwrap_lm_studio: bool = False) -> AgentError:
    """Recognize only measured structured overflow; keep all other errors opaque."""
    try:
        body = json.loads(detail)
        error = body.get("error") if isinstance(body, dict) else None
        wrapped = error.get("message") if isinstance(error, dict) else error
        prefix = "Engine protocol predict request returned 400: "
        if unwrap_lm_studio and isinstance(wrapped, str) and wrapped.startswith(prefix):
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
