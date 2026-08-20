"""Dependency reachability checks with a deliberately non-sensitive boundary."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping

import httpx

logger = logging.getLogger(__name__)

DependencyCheck = Callable[[], Awaitable[bool]]

DEPENDENCY_NAMES = ("ai_server", "openemr_api", "ocr", "external_llm")


@dataclass(frozen=True)
class HealthSettings:
    """Non-response configuration used by dependency probes."""

    openemr_url: str
    groq_api_key: str | None

    @classmethod
    def from_environment(cls, openemr_url: str) -> HealthSettings:
        """Read optional operational settings once during application startup."""
        provider = os.environ.get("LLM_PROVIDER", "").lower()
        api_key = os.environ.get("GROQ_API_KEY") if provider == "groq" else None
        return cls(openemr_url=openemr_url, groq_api_key=api_key)


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
    settings: HealthSettings, openemr_client: httpx.AsyncClient, groq_client: httpx.AsyncClient
) -> HealthService:
    """Build probes for the approved local OCR, OpenEMR, and Groq dependencies.

    Takes separate clients because the OpenEMR probe hits a self-signed local cert
    (verification deliberately disabled by the caller) while the Groq probe sends a
    live API key to the public internet and must keep full TLS verification.
    """
    return HealthService(
        {
            "openemr_api": _http_probe(settings.openemr_url, openemr_client, {}),
            "ocr": _tesseract_probe,
            "external_llm": _groq_probe(settings.groq_api_key, groq_client),
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
