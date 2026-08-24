"""Dependency reachability checks with a deliberately non-sensitive boundary."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping

import httpx

from ai_server.llm.local import LocalModelConfigurationError, LocalModelSettings
from ai_server.llm.provider import GROQ, selected_llm_provider

logger = logging.getLogger(__name__)

DependencyCheck = Callable[[], Awaitable[bool]]

# `model_server` sits second, directly after the process itself, because since TICK-065
# it is the dependency chat availability *is*: D12 removed the deterministic fallback, so
# an unreachable model server is a chat outage rather than a lost capability. Reporting
# it here is what lets monitoring see that outage before a patient does -- the spec names
# this endpoint as the mitigation for D12's accepted risk.
DEPENDENCY_NAMES = ("ai_server", "model_server", "openemr_api", "ocr", "external_llm")


@dataclass(frozen=True)
class HealthSettings:
    """Non-response configuration used by dependency probes."""

    openemr_url: str
    groq_api_key: str | None
    model_server: LocalModelSettings | None

    @classmethod
    def from_environment(cls, openemr_url: str) -> HealthSettings:
        """Read optional operational settings once during application startup.

        The Groq probe used to run only when `LLM_PROVIDER == "groq"`. That gate is wrong
        since TICK-064: `LLM_PROVIDER` selects the *front door*, which must be local
        (D3), while Groq now backs `ask_general_knowledge` on every provider (D13). Under
        the only provider the chat can actually run on, the old gate reported
        `external_llm` unavailable while Groq was a live dependency. The configured key
        is what decides now, so a deployment with no Groq at all still reports honestly
        -- `default_health_service` treats an absent key as unavailable.

        `model_server` is read the same way `_build_model_turn_service` reads it, and is
        `None` in exactly the cases that leave the chat unavailable there: `LLM_PROVIDER`
        pointing at Groq, which may never be the front door, or an absent `LLM_MODEL`.
        Both report `unavailable` rather than `ok`, because from the patient's side an
        unconfigured model server and an unreachable one are the same outage -- and a
        deployment that reported `ok` while every turn answered "the assistant is
        temporarily unavailable" would be the monitoring failure this criterion exists to
        prevent.
        """
        model_server: LocalModelSettings | None = None
        if selected_llm_provider() != GROQ:
            try:
                model_server = LocalModelSettings.from_environment()
            except LocalModelConfigurationError:
                model_server = None
        return cls(
            openemr_url=openemr_url,
            groq_api_key=os.environ.get("GROQ_API_KEY"),
            model_server=model_server,
        )


class HealthService:
    """Report fixed dependency names without exposing probe configuration."""

    def __init__(self, checks: Mapping[str, DependencyCheck]) -> None:
        unknown_names = set(checks).difference(DEPENDENCY_NAMES)
        if unknown_names:
            raise ValueError("health checks must use known dependency names")
        self._checks = dict(checks)

    async def report(self) -> dict[str, object]:
        """Return an aggregate status and fixed, non-sensitive dependency states."""
        dependencies: dict[str, str] = {"ai_server": "ok"}
        for name in DEPENDENCY_NAMES[1:]:
            dependencies[name] = "ok" if await self._reachable(name) else "unavailable"
        status = "ok" if all(value == "ok" for value in dependencies.values()) else "degraded"
        return {"status": status, "dependencies": dependencies}

    async def _reachable(self, name: str) -> bool:
        check = self._checks.get(name)
        if check is None:
            return False
        try:
            reachable = await check()
        except (httpx.HTTPError, OSError, asyncio.TimeoutError):
            logger.warning("dependency reachability check failed: %s", name)
            return False
        if not reachable:
            logger.warning("dependency reachability check failed: %s", name)
        return reachable


def default_health_service(
    settings: HealthSettings, openemr_client: httpx.AsyncClient, verified_client: httpx.AsyncClient
) -> HealthService:
    """Build probes for the model server and the approved OCR, OpenEMR, and Groq deps.

    Takes separate clients because the OpenEMR probe hits a self-signed local cert
    (verification deliberately disabled by the caller) while the Groq probe sends a
    live API key to the public internet and must keep full TLS verification. The model
    server shares the verified client: it is an ordinary HTTP service with no self-signed
    cert to accommodate, and handing it the OpenEMR client would silently disable
    verification for a call that does not need it disabled.
    """
    return HealthService(
        {
            "model_server": _model_server_probe(settings.model_server, verified_client),
            "openemr_api": _http_probe(settings.openemr_url, openemr_client, {}),
            "ocr": _tesseract_probe,
            "external_llm": _groq_probe(settings.groq_api_key, verified_client),
        }
    )


def unavailable_health_service() -> HealthService:
    """Provide a safe report before startup config is available."""
    return HealthService({})


def _http_probe(url: str, client: httpx.AsyncClient, headers: Mapping[str, str]) -> DependencyCheck:
    async def check() -> bool:
        response = await client.get(url, headers=headers)
        return response.status_code < 500

    return check


def _model_server_probe(
    settings: LocalModelSettings | None, client: httpx.AsyncClient
) -> DependencyCheck:
    """Probe the configured model server's OpenAI-compatible model listing.

    `/v1/models` rather than the chat-completions endpoint the turn actually uses: a
    reachability probe must not run an inference. It is a GET on every OpenAI-compatible
    server this deployment targets -- Ollama today, vLLM when D7's second runtime exists
    -- so the probe does not have to know which one is answering, which is the same
    property `HttpLocalModelClient` is built on.

    An unconfigured model server reports unavailable, exactly as an absent Groq key does,
    because `_build_model_turn_service` degrades the chat in precisely those cases.
    """
    if settings is None:

        async def unavailable() -> bool:
            return False

        return unavailable
    headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
    return _http_probe(f"{settings.base_url.rstrip('/')}/v1/models", client, headers)


def _groq_probe(api_key: str | None, client: httpx.AsyncClient) -> DependencyCheck:
    if not api_key:

        async def unavailable() -> bool:
            return False

        return unavailable
    return _http_probe(
        "https://api.groq.com/openai/v1/models",
        client,
        {"Authorization": f"Bearer {api_key}"},
    )


async def _tesseract_probe() -> bool:
    process = await asyncio.create_subprocess_exec(
        "tesseract",
        "--version",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(process.wait(), timeout=2.0) == 0
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
