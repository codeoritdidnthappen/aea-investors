"""TICK-059: prove the AI server reaches the pinned model server by service name.

Run *inside* the ai-server container, on the same Docker network as the model
server, by `run_live_verification.sh`. Nothing here is imported by the application
or by the test suite -- it is the live half of this ticket's evidence, kept beside
the write-up so the timings can be reproduced rather than taken on trust.

It deliberately goes through `ai_server.llm.local` rather than issuing its own HTTP
request. A hand-rolled `curl` to `http://ollama:11434` would prove the container can
resolve the name; it would not prove that the *configured* client resolves it, and
`LocalModelSettings.from_environment()` defaulting to `http://localhost:11434` is
exactly the failure this ticket has to rule out.

Emits `key=value` lines so the shell wrapper can record timings without parsing prose.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx

from ai_server.llm.local import HttpLocalModelClient, LocalModelSettings
from ai_server.privacy.gate import (
    AnonymousAppointment,
    OutboundMessage,
    OutboundPayload,
    ResponseFormat,
    SchedulingContext,
    SchedulingRules,
)

NOW = datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc)


def payload() -> OutboundPayload:
    """The same de-identified payload shape the privacy gate already validates.

    Copied from `ai_server/tests/test_local_llm.py` rather than imported: the test
    module is not in the runtime image, and this must run against the shipped one.
    """
    return OutboundPayload(
        model="openai/gpt-oss-120b",
        messages=[
            OutboundMessage(
                role="system",
                content=(
                    "You are a scheduling assistant. Respond with JSON matching the "
                    "provided schema."
                ),
            ),
            OutboundMessage(role="user", content="Do I have an appointment coming up?"),
        ],
        scheduling_context=SchedulingContext(
            current_datetime=NOW,
            timezone="America/Chicago",
            office_hours=[],
            closures=[],
            open_slots=[],
            current_appointments=[
                AnonymousAppointment(
                    appointment_token="appt_tick059probe",
                    starts_at=NOW + timedelta(hours=19),
                    ends_at=NOW + timedelta(hours=19, minutes=30),
                )
            ],
        ),
        scheduling_rules=SchedulingRules(
            minimum_booking_notice_minutes=1440,
            booking_enabled=False,
            rescheduling_enabled=False,
            cancellation_enabled=True,
        ),
        response_format=ResponseFormat(type="json_schema", schema_version="1"),
    )


async def main() -> int:
    settings = LocalModelSettings.from_environment()
    print(f"model={settings.model}")
    print(f"endpoint={settings.endpoint}")
    if "localhost" in settings.base_url or "127.0.0.1" in settings.base_url:
        # Inside this container that address is the AI server itself. It passes on a
        # laptop running Ollama natively and fails on every deployment.
        print("FAIL reason=base_url_is_a_host_address_not_a_service_name")
        return 1

    # The 30s timeout the application uses is too tight for a 7B model on CPU; this
    # is a measurement harness, and the timeout under test is TICK-061's concern.
    async with httpx.AsyncClient(timeout=600.0) as http:
        client = HttpLocalModelClient(settings, http)

        started = time.monotonic()
        completion = await client.complete(payload())
        complete_seconds = time.monotonic() - started
        print(f"complete_seconds={complete_seconds:.2f}")
        print(f"complete_chars={len(completion)}")
        print(f"complete_preview={completion[:160]!r}")

        started = time.monotonic()
        first_token_seconds = None
        chunks = 0
        streamed = 0
        async for chunk in client.stream(payload()):
            if first_token_seconds is None:
                first_token_seconds = time.monotonic() - started
            chunks += 1
            streamed += len(chunk)
        stream_seconds = time.monotonic() - started
        if first_token_seconds is None:
            print("FAIL reason=stream_yielded_nothing")
            return 1
        print(f"stream_first_token_seconds={first_token_seconds:.2f}")
        print(f"stream_total_seconds={stream_seconds:.2f}")
        print(f"stream_chunks={chunks}")
        print(f"stream_chars={streamed}")

    if not completion.strip():
        print("FAIL reason=empty_completion")
        return 1
    print("PROBE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
