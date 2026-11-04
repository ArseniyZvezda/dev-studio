from .base import AgentBackend, AgentEvent, AgentRunRequest, get_backend, register_backend

# Importing the module is what registers the backend, so do it here rather than relying
# on some other call site having imported it first.
from . import claude_code  # noqa: F401  (side effect: register_backend("claude_code"))
from . import qwen  # noqa: F401  (side effect: register_backend("qwen"), §10.6)
from . import codex  # noqa: F401  (side effect: register_backend("codex"), §10.6)

__all__ = [
    "AgentBackend",
    "AgentEvent",
    "AgentRunRequest",
    "claude_code",
    "codex",
    "qwen",
    "get_backend",
    "register_backend",
]
