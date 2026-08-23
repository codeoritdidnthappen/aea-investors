"""Which language-model provider this server's chat runs on (LOCAL_LLM_SPEC D7).

`LLM_PROVIDER` has been declared in `.env.example` since the first deployment, but the
only code that ever read it was `ai_server/app/health.py`, which merely reports it: the
config surface existed and the dispatch did not. This module is the dispatch's one
source of truth for what the accepted values are, and it deliberately holds no import
of any client -- the adapters (`ai_server/llm/groq.py`, `ai_server/llm/local.py`) import
`LlmUnavailableError` from here, so selection stays free of the vendors it selects.
"""

from __future__ import annotations

import os

GROQ = "groq"
OLLAMA = "ollama"
LLM_PROVIDERS = (GROQ, OLLAMA)
# Every environment ran Groq before `LLM_PROVIDER` selected anything, so an absent
# value must keep selecting Groq. Unset is not a request for a different model, and
# silently starting a different one is the failure mode this module exists to prevent.
DEFAULT_LLM_PROVIDER = GROQ


class LlmProviderError(Exception):
    """Raised when `LLM_PROVIDER` names a provider this server cannot build."""


class LlmUnavailableError(Exception):
    """Raised when the configured provider cannot provide a safe response.

    Provider-agnostic on purpose. `GroqWorkflow.respond()` degrades a turn on this
    exception, so a client written for a second provider degrades into the same honest
    "could not handle that request" reply rather than escaping as a 500.
    """


def selected_llm_provider() -> str:
    """Return the configured provider name, defaulting when `LLM_PROVIDER` is unset.

    Raises `LlmProviderError` for any other value. This is called from application
    startup (`_build_chat_service`, via `lifespan`), so an unrecognised provider stops
    the boot with a message naming what is accepted, instead of quietly serving a
    provider the operator did not ask for. It is intentionally *not* a subclass of any
    provider's configuration error: the startup path degrades on those, and a
    misspelled provider must not be swallowed by that same tolerance.
    """
    configured = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if not configured:
        return DEFAULT_LLM_PROVIDER
    if configured not in LLM_PROVIDERS:
        raise LlmProviderError(
            f"LLM_PROVIDER={configured!r} is not a supported provider; "
            f"accepted values are: {', '.join(LLM_PROVIDERS)}"
        )
    return configured
