"""Drive real chat turns through the real local model, and report what actually happened.

TICK-063. The unit and integration suites mock the model at the wire, so what they prove
is that *this codebase* executes a tool call correctly. What they cannot prove is the
half that is genuinely uncertain: whether `llama3.1:8b-instruct-q4_K_M` (D17), reading
the shipped prompt, actually emits the right call for a patient talking normally --
including the multi-turn cases the corpus does not cover, because a corpus case is one
turn.

So this runs the production turn loop (`ai_server.app.model_turn.ModelTurnService`)
against a live Ollama, with real conversations, and reports every routing decision, every
confirmation, every write body, and the pre-stream pause D16 accepts.

OpenEMR is mocked at the wire here rather than written to, deliberately: what a confirmed
write puts on that wire is what is worth recording, and the wire format itself is already
proven live against a real OpenEMR in `evidence/TICK-049` (demographics/address) and
`evidence/TICK-031` (appointments). Nothing about the model changes those bodies.

Usage: `python -m evidence.TICK_063.live_turns` is *not* how this runs -- see
`run_live_verification.sh`, which passes the model server's base URL.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from ai_server.app.conversation import ConversationStore
from ai_server.app.model_turn import ModelTurnService, TurnMetrics, TurnServices
from ai_server.llm.local import HttpLocalModelClient, LocalModelSettings
from ai_server.onboarding.draft_client import AssessmentDraftAdapter, OpenEmrPortalSettings
from ai_server.onboarding.flow import OnboardingFlow
from ai_server.openemr.adapter import Appointment
from ai_server.openemr.demographics import OpenEmrDemographicsAdapter
from ai_server.scheduling.appointments import AnonymousAppointmentStore, AppointmentDiscoveryService
from ai_server.scheduling.booking import AppointmentRequest, BookingService, OpenEmrBookingAdapter
from ai_server.scheduling.cancel import AppointmentCancelAdapter, CancellationService
from ai_server.scheduling.slots import AnonymousSlotStore, CandidateSlot, SlotDiscoveryService

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
PORTAL_BASE_URL = "https://openemr.local/apis/default"
ACCESS_TOKEN = "live-verification-access-token"
PATIENT_ID = "live-verification-patient"

SLOTS = (
    CandidateSlot(
        starts_at=datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 15, 10, 50, tzinfo=timezone.utc),
    ),
    CandidateSlot(
        starts_at=datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 16, 14, 50, tzinfo=timezone.utc),
    ),
)

APPOINTMENTS = (
    Appointment(
        id="appointment-31",
        status="booked",
        start=datetime(2026, 9, 17, 11, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 17, 11, 50, tzinfo=timezone.utc),
    ),
)


@dataclass
class Recorder:
    """Every request this process makes to anything, so egress can be stated not assumed."""

    portal: list[httpx.Request] = field(default_factory=list)
    model: list[httpx.Request] = field(default_factory=list)

    def portal_handler(self, request: httpx.Request) -> httpx.Response:
        self.portal.append(request)
        path, method = request.url.path, request.method
        if path.endswith("/portal/patient/demographics"):
            return httpx.Response(200, json={})
        if path.endswith("/portal/patient/appointment") and method == "POST":
            return httpx.Response(201, json={"id": "appointment-77"})
        if "/portal/patient/appointment/" in path and method == "PUT":
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "status": "cancelled"})
        if path.endswith("/portal/patient/assessment") and method == "POST":
            return httpx.Response(201, json={"uuid": "draft-live", "status": "draft", "fields": {}})
        if "/portal/patient/assessment/" in path:
            return httpx.Response(
                200,
                json={
                    "uuid": "draft-live",
                    "status": "draft",
                    "fields": json.loads(request.content or b"{}"),
                },
            )
        return httpx.Response(404, json={})


class _Candidates:
    async def candidate_slots(self) -> list[CandidateSlot]:
        return list(SLOTS)


class _Appointments:
    async def active_appointments(self, access_token: str) -> list[Appointment]:
        del access_token
        return list(APPOINTMENTS)


class _Cursors:
    def __init__(self) -> None:
        self.cursor: str | None = None

    def load_cursor(self, handle: str, now: datetime) -> str | None:
        del handle, now
        return self.cursor

    def save_cursor(self, handle: str, cursor: str, now: datetime) -> None:
        del handle, now
        self.cursor = cursor


def build(base_url: str, model: str) -> tuple[ModelTurnService, Recorder, list[TurnMetrics]]:
    recorder = Recorder()

    def watch(request: httpx.Request) -> None:
        recorder.model.append(request)

    metrics: list[TurnMetrics] = []
    model_client = HttpLocalModelClient(
        LocalModelSettings(model=model, base_url=base_url),
        httpx.AsyncClient(timeout=600.0, event_hooks={"request": [_sync_hook(watch)]}),
    )
    portal_client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.portal_handler))
    portal_settings = OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL)
    slot_store = AnonymousSlotStore()
    appointment_store = AnonymousAppointmentStore()
    demographics = OpenEmrDemographicsAdapter(portal_settings, portal_client)
    return (
        ModelTurnService(
            client=model_client,
            services=TurnServices(
                slot_discovery=SlotDiscoveryService(_Candidates(), _Appointments(), slot_store),
                appointment_discovery=AppointmentDiscoveryService(
                    _Appointments(), appointment_store
                ),
                booking=BookingService(
                    slot_store, OpenEmrBookingAdapter(portal_settings, portal_client)
                ),
                cancellation=CancellationService(
                    appointment_store, AppointmentCancelAdapter(portal_settings, portal_client)
                ),
                appointment_request=AppointmentRequest(
                    category_id="5",
                    title="Office Visit",
                    facility_id="9",
                    billing_location_id="10",
                ),
                demographics=demographics,
                onboarding=OnboardingFlow(
                    AssessmentDraftAdapter(portal_settings, portal_client), demographics
                ),
            ),
            cursors=_Cursors(),
            clock=lambda: NOW,
            conversations=ConversationStore(),
            metrics=metrics.append,
        ),
        recorder,
        metrics,
    )


def _sync_hook(record):
    async def hook(request: httpx.Request) -> None:
        record(request)

    return hook


CONVERSATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "address update, plainly stated, then confirmed",
        (
            "I've moved. My new address is 88 Larch Street, Toms River, NJ 08753.",
            "Yes, that's right.",
        ),
    ),
    (
        "AC5: the patient changes their mind mid-confirmation, with no cancel keyword",
        (
            "My date of birth is wrong on your records. It should be 1 April 2003.",
            "actually, make it 2004",
            "yes please save that",
        ),
    ),
    (
        "AC5: the patient abandons a pending change by asking for something else",
        (
            "Please change my last name to Okonkwo.",
            "actually never mind, what appointments do I have?",
        ),
    ),
    (
        "scheduling: offer, choose, confirm, book",
        (
            "What appointment times do you have?",
            "The Tuesday one at ten works for me.",
            "Yes, book it.",
        ),
    ),
    (
        "onboarding: an intake answer, recorded against a draft this turn creates",
        (
            "Both, please -- counselling and medication support.",
            "Yes that's right.",
            "Yes, record that.",
        ),
    ),
    (
        "D15: a value the validator will not vouch for is refused, never written",
        ("I'd like counselling, and psychiatric help too.",),
    ),
    (
        "a general question, which must not reach Groq (FR-34, TICK-064 owns the path)",
        ("What is CBT anyway?",),
    ),
    (
        "an address given only in part, which must be asked about rather than written",
        ("I live on Larch Street now.",),
    ),
)


async def drive(service: ModelTurnService, handle: str, message: str) -> str:
    return "".join(
        [
            chunk
            async for chunk in service.stream_reply(
                handle, message, access_token=ACCESS_TOKEN, patient_id=PATIENT_ID
            )
        ]
    )


async def main(base_url: str, model: str) -> int:
    service, recorder, metrics = build(base_url, model)
    print(f"model server : {base_url}")
    print(f"model        : {model}")
    print(f"prompt       : {__import__('ai_server.llm.prompt', fromlist=['x']).PROMPT_VERSION}")

    for index, (title, messages) in enumerate(CONVERSATIONS):
        handle = f"live-{index}"
        print(f"\n=== {title} ===")
        for message in messages:
            before = len(recorder.portal)
            reply = await drive(service, handle, message)
            metric = metrics[-1]
            print(f"\npatient  : {message}")
            print(
                f"routing  : {metric.routing_seconds:.2f}s  tool={metric.tool or '-'}  "
                f"outcome={metric.outcome}"
            )
            print("assistant: " + reply.replace("\n", "\n           "))
            for request in recorder.portal[before:]:
                body = (request.content or b"").decode()
                print(f"openemr  : {request.method} {request.url.path} {body}")

    print("\n=== egress ===")
    hosts = sorted({request.url.host for request in recorder.model + recorder.portal})
    print(f"every host this process contacted: {hosts}")
    groq = [str(r.url) for r in recorder.model + recorder.portal if "groq.com" in str(r.url)]
    print(f"requests to Groq: {groq}")

    print("\n=== the pre-stream pause (D16) ===")
    pauses = sorted(metric.routing_seconds for metric in metrics)
    print(
        f"turns={len(pauses)} min={pauses[0]:.2f}s median={pauses[len(pauses) // 2]:.2f}s "
        f"max={pauses[-1]:.2f}s"
    )
    return 0 if not groq else 1


if __name__ == "__main__":
    sys.exit(
        asyncio.run(
            main(
                os.environ.get("MODEL_BASE_URL", "http://localhost:11499"),
                os.environ.get("MODEL", "llama3.1:8b-instruct-q4_K_M"),
            )
        )
    )
