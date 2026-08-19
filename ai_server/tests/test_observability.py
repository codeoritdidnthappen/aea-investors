from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from ai_server.app.health import HealthService, HealthSettings, default_health_service
from ai_server.app.main import create_app


def test_ticket_011_health_reports_each_fixed_dependency_without_configuration() -> None:
    async def unavailable() -> bool:
        return False

    app = create_app(
        health_service=HealthService(
            {"openemr_api": unavailable, "ocr": unavailable, "external_llm": unavailable}
        )
    )

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://chat.test"
        ) as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {
            "status": "degraded",
            "dependencies": {
                "ai_server": "ok",
                "openemr_api": "unavailable",
                "ocr": "unavailable",
                "external_llm": "unavailable",
            },
        }

    asyncio.run(scenario())


def test_ticket_011_health_never_logs_or_returns_sensitive_failure_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "patient-name token prompt document-content"

    async def unavailable() -> bool:
        raise httpx.ConnectError(secret)

    service = HealthService(
        {"openemr_api": unavailable, "ocr": unavailable, "external_llm": unavailable}
    )
    with caplog.at_level(logging.WARNING):
        report = asyncio.run(service.report())

    assert secret not in caplog.text
    assert secret not in str(report)
    assert "openemr_api" in caplog.text
    assert report["dependencies"] == {
        "ai_server": "ok",
        "openemr_api": "unavailable",
        "ocr": "unavailable",
        "external_llm": "unavailable",
    }


def test_ticket_011_groq_probe_keeps_api_key_out_of_health_output_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api_key = "gsk_sensitive_delegated_token"
    settings = HealthSettings(openemr_url="https://openemr.test/private", groq_api_key=api_key)

    async def responder(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("patient record value")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
            service = default_health_service(settings, client)
            return await service.report()

    with caplog.at_level(logging.WARNING):
        report = asyncio.run(scenario())

    assert api_key not in caplog.text
    assert api_key not in str(report)
    assert "patient record value" not in caplog.text
