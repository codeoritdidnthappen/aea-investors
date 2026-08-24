"""Tests for the local model as the front door for every turn (TICK-063).

Every test here drives a real turn: a real `HttpLocalModelClient` posting to a mocked
Ollama at the wire (the pattern `test_local_llm.py` established), a real
`parse_tool_call`/`validate_write`/`confirmation_prompt`/`write_authority` chain, and
real `BookingService`/`CancellationService`/`OpenEmrDemographicsAdapter`/`OnboardingFlow`
writing to a mocked OpenEMR at the wire. Nothing between the model's JSON and OpenEMR's
request body is stubbed, so what these assert is the actual write.

Live verification against the local Docker topology with a real seeded patient and a
real Ollama is recorded under `evidence/TICK-063/`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import httpx
import pytest

from ai_server.app.auth import AuthSettings, OAuthTokens, SessionStore
from ai_server.app.chat import ASSISTANT_UNAVAILABLE_RESPONSE
from ai_server.app.conversation import MAX_TRANSCRIPT_MESSAGES, ConversationStore
from ai_server.app.main import create_app
from ai_server.app.model_turn import (
    GENERAL_KNOWLEDGE_ANSWER_TEMPLATE,
    GENERAL_KNOWLEDGE_UNAVAILABLE_RESPONSE,
    GENERAL_KNOWLEDGE_WITHHELD_RESPONSE,
    ModelTurnService,
    TurnMetrics,
    TurnServices,
    log_turn_metrics,
    unavailable_model_turn_service,
)
from ai_server.llm.general_knowledge import GeneralKnowledgeService
from ai_server.llm.groq import GroqSettings, HttpGroqClient
from ai_server.llm.local import HttpLocalModelClient, LocalModelSettings
from ai_server.llm.prompt import PROMPT_VERSION, SYSTEM_PROMPT
from ai_server.llm.tools import (
    TOOL_CALL_REFUSED_RESPONSE,
    UNKNOWN_TOOL_RESPONSE,
    envelope_json_schema,
)
from ai_server.llm.validation import (
    APPOINTMENT_REFUSAL,
    SLOT_REFUSAL,
    STATE_REFUSAL,
    VALIDATED_WRITING_TOOLS,
)
from ai_server.ocr.service import OcrService
from ai_server.onboarding.draft_client import AssessmentDraftAdapter, OpenEmrPortalSettings
from ai_server.onboarding.flow import OnboardingFlow
from ai_server.openemr.adapter import Appointment
from ai_server.openemr.demographics import OpenEmrDemographicsAdapter
from ai_server.privacy.gate import GENERAL_KNOWLEDGE_SYSTEM_PROMPT, PrivacyGate
from ai_server.scheduling.appointments import AnonymousAppointmentStore, AppointmentDiscoveryService
from ai_server.scheduling.booking import (
    AppointmentRequest,
    BookingService,
    OpenEmrBookingAdapter,
)
from ai_server.scheduling.cancel import AppointmentCancelAdapter, CancellationService
from ai_server.scheduling.slots import (
    AnonymousSlotStore,
    CandidateSlot,
    SlotDiscoveryService,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
MODEL_BASE_URL = "http://ollama.test:11434"
MODEL_ENDPOINT = f"{MODEL_BASE_URL}/v1/chat/completions"
PORTAL_BASE_URL = "https://openemr.test/apis/default"
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

ACCESS_TOKEN = "synthetic-access"
PATIENT_ID = "patient-uuid"

CORPUS = json.loads(Path("eval/acceptance-corpus.json").read_text())


def run(coroutine):
    return asyncio.run(coroutine)


# --- The mocked runtimes -----------------------------------------------------------


@dataclass
class Runtime:
    """One mocked Ollama and one mocked OpenEMR, both recording every request."""

    replies: list[str]
    model_requests: list[httpx.Request] = field(default_factory=list)
    portal_requests: list[httpx.Request] = field(default_factory=list)
    groq_requests: list[httpx.Request] = field(default_factory=list)
    booked_id: str = "appointment-77"
    groq_answer: str = "CBT is a talking therapy."
    groq_status: int = 200

    def groq_handler(self, request: httpx.Request) -> httpx.Response:
        """The external model, recorded separately so egress is observable on its own."""
        self.groq_requests.append(request)
        return httpx.Response(
            self.groq_status,
            json={"choices": [{"message": {"content": self.groq_answer}}]},
        )

    def model_handler(self, request: httpx.Request) -> httpx.Response:
        self.model_requests.append(request)
        if not self.replies:
            raise AssertionError("the model was asked for more turns than were scripted")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": self.replies.pop(0)}}]}
        )

    def portal_handler(self, request: httpx.Request) -> httpx.Response:
        self.portal_requests.append(request)
        path = request.url.path
        if path.endswith("/portal/patient/demographics"):
            return httpx.Response(200, json={})
        if path.endswith("/portal/patient/appointment") and request.method == "POST":
            return httpx.Response(201, json={"id": self.booked_id})
        if "/portal/patient/appointment/" in path and request.method == "PUT":
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "status": "cancelled"})
        if path.endswith("/portal/patient/assessment") and request.method == "POST":
            return httpx.Response(201, json={"uuid": "draft-1", "status": "draft", "fields": {}})
        if "/portal/patient/assessment/" in path:
            return httpx.Response(
                200,
                json={
                    "uuid": "draft-1",
                    "status": "draft",
                    "fields": json.loads(request.content or b"{}"),
                },
            )
        raise AssertionError(f"unexpected OpenEMR request: {request.method} {request.url}")

    def writes(self, method: str, suffix: str) -> list[dict[str, Any]]:
        """Every request body sent to a path ending in `suffix` with `method`."""
        return [
            json.loads(request.content or b"{}")
            for request in self.portal_requests
            if request.method == method and request.url.path.endswith(suffix)
        ]


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
class _Candidates:
    async def candidate_slots(self) -> list[CandidateSlot]:
        return list(SLOTS)


@dataclass
class _Appointments:
    async def active_appointments(self, access_token: str) -> list[Appointment]:
        del access_token
        return list(APPOINTMENTS)


@dataclass
class _Cursors:
    """The two `SessionStore` cursor methods, in memory."""

    cursor: str | None = None

    def load_cursor(self, handle: str, now: datetime) -> str | None:
        del handle, now
        return self.cursor

    def save_cursor(self, handle: str, cursor: str, now: datetime) -> None:
        del handle, now
        self.cursor = cursor


def turn_service(
    *replies: str,
    metrics: list[TurnMetrics] | None = None,
    ocr: OcrService | None = None,
    groq: bool = True,
    runtime: Runtime | None = None,
) -> tuple[ModelTurnService, Runtime, _Cursors]:
    """Build a real turn service over mocked Ollama, OpenEMR and Groq transports.

    `groq=False` builds the service with no general-knowledge backing at all, which is
    what an unconfigured `GROQ_API_KEY` produces in `_build_model_turn_service` (AC6).
    The real `PrivacyGate` is used throughout -- Presidio is never mocked in this suite,
    because a mocked detector would make every privacy assertion vacuous.
    """
    runtime = runtime if runtime is not None else Runtime(replies=list(replies))
    model_client = HttpLocalModelClient(
        LocalModelSettings(model="llama3.1:8b-instruct-q4_K_M", base_url=MODEL_BASE_URL),
        httpx.AsyncClient(transport=httpx.MockTransport(runtime.model_handler)),
    )
    portal_client = httpx.AsyncClient(transport=httpx.MockTransport(runtime.portal_handler))
    portal_settings = OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL)
    slot_store = AnonymousSlotStore()
    appointment_store = AnonymousAppointmentStore()
    demographics = OpenEmrDemographicsAdapter(portal_settings, portal_client)
    cursors = _Cursors()
    sink = metrics if metrics is not None else []
    return (
        ModelTurnService(
            client=model_client,
            services=TurnServices(
                general_knowledge=(
                    GeneralKnowledgeService(
                        PrivacyGate.create(),
                        HttpGroqClient(
                            GroqSettings(api_key="k", zdr_verified_on=date(2026, 1, 1)),
                            httpx.AsyncClient(transport=httpx.MockTransport(runtime.groq_handler)),
                        ),
                    )
                    if groq
                    else None
                ),
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
                ocr=ocr,
            ),
            cursors=cursors,
            clock=lambda: NOW,
            conversations=ConversationStore(),
            metrics=sink.append,
        ),
        runtime,
        cursors,
    )


def call(tool: str, **arguments: Any) -> str:
    """One model response, in the shape the envelope grammar constrains it to."""
    return json.dumps({"tool": tool, "arguments": arguments})


async def _turn(
    service: ModelTurnService, message: str, handle: str = "handle-1", **kwargs: Any
) -> str:
    chunks = [
        chunk
        async for chunk in service.stream_reply(
            handle, message, access_token=ACCESS_TOKEN, patient_id=PATIENT_ID, **kwargs
        )
    ]
    return "".join(chunks)


def turns(service: ModelTurnService, *messages: str, handle: str = "handle-1") -> list[str]:
    async def scenario() -> list[str]:
        return [await _turn(service, message, handle) for message in messages]

    return run(scenario())


def system_message(runtime: Runtime, index: int = -1) -> str:
    return json.loads(runtime.model_requests[index].content)["messages"][0]["content"]


def sent_messages(runtime: Runtime, index: int = -1) -> list[dict[str, str]]:
    return json.loads(runtime.model_requests[index].content)["messages"]


# --- AC1: every turn goes to the model, no phrasing is matched first ----------------


def test_ac1_a_phrase_that_used_to_start_onboarding_now_reaches_the_model() -> None:
    """`onboarding_mode()` matched "start onboarding" and intercepted the turn before
    the model saw it. That interception is gone: the message reaches the model and the
    model's own call decides the turn."""
    service, runtime, _ = turn_service(call("reply", message="Sure -- what is your given name?"))

    reply = turns(service, "start onboarding")[0]

    assert runtime.model_requests, "the turn never reached the model"
    assert sent_messages(runtime)[-1] == {"role": "user", "content": "start onboarding"}
    assert reply == "Sure -- what is your given name?"


def test_ac1_a_phrase_that_used_to_start_the_address_flow_now_reaches_the_model() -> None:
    """The same for `address_update_mode()`'s "update my address"."""
    service, runtime, _ = turn_service(call("reply", message="Of course. What is the new address?"))

    reply = turns(service, "update my address")[0]

    assert sent_messages(runtime)[-1] == {"role": "user", "content": "update my address"}
    assert reply == "Of course. What is the new address?"


def test_ac1_the_route_consults_no_deterministic_handler_on_any_turn(tmp_path: Path) -> None:
    """At the route itself: every turn is answered by the model, including the two
    messages and the active cursor that used to guarantee a deterministic handler would
    take it (TICK-050 AC8).

    This test used to inject `_ScriptedHandler` doubles as `onboarding_service=` and
    `address_service=` and assert neither was called. TICK-065 deleted both services and
    both parameters, so the double has nothing to stand in for; what is left to assert is
    that the model answered all three turns. The stronger claim -- that no such handler
    can be reintroduced without changing `create_app`'s signature -- is asserted directly
    in `test_deterministic_handlers_deleted.py`.
    """
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = store.create_session(OAuthTokens("a", "r", "n"), NOW, configured.session_ttl)
    # An active cursor was the strongest of the old triggers: with one set, *every*
    # message went to onboarding regardless of its content.
    store.save_cursor(handle, "draft-1", NOW)

    service, runtime, _ = turn_service(
        call("reply", message="one"),
        call("reply", message="two"),
        call("reply", message="three"),
    )
    app = create_app(configured, clock=lambda: NOW, model_turn_service=service)

    messages = ["start onboarding", "update my address", "what's the weather"]
    replies = run(_post_all(app, handle, messages))

    assert replies == ["one", "two", "three"]
    # Each turn reached the model, and reached it verbatim: three inferences, and the
    # last user message of each carries exactly what the patient typed.
    assert len(runtime.model_requests) == 3
    assert [sent_messages(runtime, index)[-1]["content"] for index in (0, 1, 2)] == messages


# --- AC2: the call executes through the TICK-060 surface and TICK-061's validation ---


def test_ac2_a_proposed_write_is_read_back_and_nothing_is_saved_yet() -> None:
    service, runtime, _ = turn_service(
        call(
            "update_address",
            street1="88 Larch Street",
            city="Toms River",
            state="NJ",
            zip_code="08753",
        )
    )

    reply = turns(service, "I've moved to 88 Larch Street, Toms River, NJ 08753.")[0]

    assert "Nothing has been saved yet" in reply
    assert "88 Larch Street" in reply
    assert runtime.writes("PUT", "/demographics") == []


def test_ac2_a_confirmed_write_reaches_openemr_with_the_validators_values() -> None:
    """The values on the wire are the validator's, not the model's: the state code is
    upper-cased and the spacing collapsed by `validate_address`, and
    `executable_arguments()` hands the service the authority's values rather than the
    call's."""
    proposal = call(
        "update_address",
        street1="88   Larch  Street",
        city="Toms River",
        state="nj",
        zip_code="08753",
    )
    service, runtime, _ = turn_service(proposal, proposal)

    turns(service, "I've moved to 88 Larch Street, Toms River, nj 08753.", "yes that's right")

    assert runtime.writes("PUT", "/demographics") == [
        {
            "street": "88 Larch Street",
            "street_line_2": "",
            "city": "Toms River",
            "state": "NJ",
            "postal_code": "08753",
        }
    ]


def test_ac2_a_field_the_validator_refuses_writes_nothing_and_says_what_to_send() -> None:
    service, runtime, _ = turn_service(
        call(
            "update_address",
            street1="88 Larch Street",
            city="Toms River",
            state="Nowhere",
            zip_code="08753",
        )
    )

    reply = turns(service, "I've moved to 88 Larch Street, Toms River, Nowhere 08753.")[0]

    assert reply == STATE_REFUSAL
    assert runtime.writes("PUT", "/demographics") == []


def test_ac2_an_invented_tool_name_is_refused_by_the_surface() -> None:
    service, runtime, _ = turn_service(json.dumps({"tool": "delete_record", "arguments": {}}))

    reply = turns(service, "delete everything")[0]

    assert reply == UNKNOWN_TOOL_RESPONSE
    assert runtime.portal_requests == []


def test_ac2_a_response_that_is_not_a_tool_call_is_refused() -> None:
    service, runtime, _ = turn_service("I'm happy to help with that!")

    reply = turns(service, "hello")[0]

    assert reply == TOOL_CALL_REFUSED_RESPONSE
    assert runtime.portal_requests == []


def test_ac2_a_slot_token_the_model_invented_never_reaches_booking() -> None:
    service, runtime, _ = turn_service(call("book_appointment", slot_token="slot_madeup00000"))

    reply = turns(service, "book me the ten o'clock")[0]

    assert reply == SLOT_REFUSAL
    assert runtime.portal_requests == []


def test_ac2_an_appointment_token_the_model_invented_never_reaches_cancellation() -> None:
    service, runtime, _ = turn_service(
        call("cancel_appointment", appointment_token="appt_madeup00000")
    )

    reply = turns(service, "cancel my appointment")[0]

    assert reply == APPOINTMENT_REFUSAL
    assert runtime.portal_requests == []


def test_ac2_every_validated_writing_tool_has_an_executor() -> None:
    """A writing tool added to TICK-061's validators without an execution branch here
    would validate, confirm, and then silently do nothing."""
    from ai_server.app.model_turn import _WRITING_TOOLS_HANDLED_HERE

    assert _WRITING_TOOLS_HANDLED_HERE == VALIDATED_WRITING_TOOLS


def test_ac2_the_runtime_is_constrained_by_the_published_envelope_grammar() -> None:
    service, runtime, _ = turn_service(call("reply", message="hi"))

    turns(service, "hello")

    body = json.loads(runtime.model_requests[0].content)
    assert body["response_format"]["json_schema"]["schema"] == envelope_json_schema()
    assert body["stream"] is False


# --- AC3: the reply streams, and the pre-stream pause is measured -------------------


def test_ac3_the_reply_reaches_the_browser_in_more_than_one_chunk() -> None:
    long_reply = (
        "Here is everything I can help with: your appointments, your mailing address, "
        "your name and date of birth, and the questions on your intake assessment."
    )
    service, _, _ = turn_service(call("reply", message=long_reply))

    async def scenario() -> list[str]:
        return [
            chunk
            async for chunk in service.stream_reply(
                "handle-1", "what can you do?", access_token=ACCESS_TOKEN, patient_id=PATIENT_ID
            )
        ]

    chunks = run(scenario())

    assert len(chunks) > 1
    assert "".join(chunks) == long_reply


def test_ac3_a_line_structured_reply_survives_being_streamed_intact() -> None:
    """The confirmation prompts are line-structured and the page renders them with
    `white-space: pre-wrap`, so chunking must not drop or normalise a character."""
    service, _, _ = turn_service(
        call(
            "update_address",
            street1="88 Larch Street",
            street2="Apt 4B",
            city="Toms River",
            state="NJ",
            zip_code="08753",
        )
    )

    reply = turns(service, "new address")[0]

    assert "Street: 88 Larch Street\nApartment or unit: Apt 4B\nCity: Toms River\n" in reply


def test_ac3_the_pre_stream_pause_is_measured_and_recorded_for_every_turn() -> None:
    """D16 accepts the pause and asks for it to be honest rather than hidden: every
    turn reports the routing inference's own wall-clock time and what it decided."""
    recorded: list[TurnMetrics] = []
    service, _, _ = turn_service(
        call("reply", message="hello"),
        json.dumps({"tool": "delete_record", "arguments": {}}),
        metrics=recorded,
    )

    turns(service, "hi", "delete everything")

    assert [(metric.tool, metric.outcome) for metric in recorded] == [
        ("reply", "replied"),
        (None, "refused"),
    ]
    assert all(metric.routing_seconds > 0 for metric in recorded)


def test_ac3_the_recorded_pause_carries_no_patient_or_model_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="ai_server.app.model_turn"):
        log_turn_metrics(
            TurnMetrics(routing_seconds=1.25, tool="update_address", outcome="written")
        )

    assert caplog.records[0].getMessage() == (
        "model turn: routing_seconds=1.250 tool=update_address outcome=written"
    )


# --- AC4: multi-turn state survives, and is not read back out of the transcript ------


def test_ac4_offered_slot_tokens_survive_to_the_turn_that_books_one() -> None:
    """The tokens the model was shown are recorded when they are issued, so the turn
    that books resolves the token this session actually offered -- not one re-derived
    from the words of an earlier reply."""
    service, runtime, _ = turn_service(call("find_slots"))
    turns(service, "when are you free?")
    state = service.conversations.state("handle-1", NOW)
    offered = state.offered_slots
    assert len(offered) == len(SLOTS)

    booking_call = call("book_appointment", slot_token=offered[0].slot_token)
    runtime.replies.extend([booking_call, booking_call])
    replies = turns(service, "the Tuesday one", "yes please")

    assert "Nothing has been booked yet" in replies[0]
    assert "Booked and confirmed" in replies[1]
    assert runtime.writes("POST", "/appointment")[0]["pc_eventDate"] == "2026-09-15"


def test_ac4_a_pending_confirmation_survives_a_turn_and_is_not_reread_from_the_transcript() -> None:
    """The pending change is a `ValidatedWrite` recorded when it was validated. Wiping
    the transcript -- which is bounded and will be trimmed in a long conversation --
    leaves the confirmation working, which is what "not reconstructed by re-reading the
    transcript" means in practice."""
    proposal = call("update_demographics", date_of_birth="2003-04-01")
    service, runtime, _ = turn_service(proposal, proposal)

    turns(service, "my date of birth is wrong, it's 1 April 2003")
    state = service.conversations.state("handle-1", NOW)
    assert state.pending is not None
    state.turns.clear()  # the transcript is gone; the recorded state is not

    reply = turns(service, "yes")[0]

    assert "Saved" in reply
    assert runtime.writes("PUT", "/demographics") == [{"DOB": "2003-04-01"}]


def test_ac4_an_onboarding_position_survives_as_the_draft_id_and_is_resumed() -> None:
    """The onboarding position stays where ARCHITECTURE.md Sec. 5 puts it: the opaque
    draft id in `sessions.cursor`. The first answer starts the draft, the second resumes
    the same one rather than starting a second."""
    first = call("record_assessment_answer", field="help_type", answer="both")
    second = call("record_assessment_answer", field="visit_preference", answer="video,weekend")
    service, runtime, cursors = turn_service(first, first, second, second)

    turns(service, "both please", "yes", "video, at the weekend", "yes")

    assert cursors.cursor == "draft-1"
    assert len(runtime.writes("POST", "/assessment")) == 1  # one draft created, not two
    assert runtime.writes("PUT", "/assessment/draft-1") == [
        {"help_type": "both"},
        {"visit_format": "video", "visit_time_window": "weekend"},
    ]


def test_ac4_the_transcript_the_model_sees_is_bounded_and_carries_earlier_turns() -> None:
    replies = [call("reply", message=f"answer {index}") for index in range(12)]
    service, runtime, _ = turn_service(*replies)

    turns(service, *[f"question {index}" for index in range(12)])

    messages = sent_messages(runtime)
    # system + the bounded transcript + this turn's own user message.
    assert len(messages) <= MAX_TRANSCRIPT_MESSAGES + 2
    assert {"role": "user", "content": "question 10"} in messages
    # The most recent assistant message is carried as the question just asked, so it
    # reaches the model exactly once rather than twice.
    assert 'You have just asked the patient: "answer 10"' in messages[0]["content"]
    assert {"role": "assistant", "content": "answer 10"} not in messages


def test_ac4_logout_forgets_the_conversation_including_a_pending_change() -> None:
    service, _, _ = turn_service(
        call("update_demographics", given_name="Jordan"),
        call("reply", message="ok"),
    )
    turns(service, "my first name is Jordan")
    assert service.conversations.state("handle-1", NOW).pending is not None

    service.discard("handle-1")

    fresh = service.conversations.state("handle-1", NOW)
    assert fresh.pending is None
    assert fresh.turns == []


def test_ac4_an_idle_conversation_is_dropped_rather_than_resumed() -> None:
    service, _, _ = turn_service(call("update_demographics", given_name="Jordan"))
    turns(service, "my first name is Jordan")

    later = service.conversations.state("handle-1", NOW + timedelta(minutes=30))

    assert later.pending is None


# --- AC5: a patient changing their mind mid-flow, with no cancel keyword -------------


def test_ac5_a_correction_during_a_confirmation_replaces_the_pending_change() -> None:
    """ "actually, make it 2004" during a confirmation. The model emits a corrected call;
    that is a different validated write, so it is read back rather than saved, and the
    superseded one never reaches OpenEMR."""
    service, runtime, _ = turn_service(
        call("update_demographics", date_of_birth="2003-04-01"),
        call("update_demographics", date_of_birth="2004-04-01"),
    )

    replies = turns(service, "my date of birth is 1 April 2003", "actually, make it 2004")

    assert "2003-04-01" in replies[0]
    assert "2004-04-01" in replies[1] and "Nothing has been saved yet" in replies[1]
    assert runtime.writes("PUT", "/demographics") == []
    assert dict(service.conversations.state("handle-1", NOW).pending.values) == {
        "date_of_birth": "2004-04-01"
    }


def test_ac5_the_corrected_change_is_the_one_that_is_finally_saved() -> None:
    corrected = call("update_demographics", date_of_birth="2004-04-01")
    service, runtime, _ = turn_service(
        call("update_demographics", date_of_birth="2003-04-01"), corrected, corrected
    )

    turns(service, "my date of birth is 1 April 2003", "actually, make it 2004", "that's right")

    assert runtime.writes("PUT", "/demographics") == [{"DOB": "2004-04-01"}]


def test_ac5_changing_the_subject_abandons_the_pending_change_without_saving_it() -> None:
    service, runtime, _ = turn_service(
        call("update_demographics", date_of_birth="2003-04-01"),
        call("list_appointments"),
    )

    replies = turns(
        service, "my date of birth is 1 April 2003", "never mind, what do I have booked?"
    )

    assert "Here are your upcoming appointments" in replies[1]
    assert runtime.writes("PUT", "/demographics") == []
    assert service.conversations.state("handle-1", NOW).pending is None


def test_ac5_the_model_is_told_what_is_pending_rather_than_asked_to_match_a_keyword() -> None:
    service, runtime, _ = turn_service(
        call("update_demographics", date_of_birth="2003-04-01"),
        call("reply", message="ok"),
    )

    turns(service, "my date of birth is 1 April 2003", "hmm")

    pending_context = system_message(runtime)
    assert "A change is waiting for this patient's approval" in pending_context
    assert "2003-04-01" in pending_context
    assert "If it corrects any part of it" in pending_context


def test_ac5_a_refused_correction_abandons_the_pending_change(tmp_path: Path) -> None:
    """Review finding. A patient who corrects a pending change with a value the validator
    refuses is told to re-send it. If the superseded change stayed pending, their next
    "yes" would save the value they had just corrected -- refusing the correction must not
    make the original *more* likely to be written."""
    del tmp_path
    good = call(
        "update_address",
        street1="88 Larch Street",
        city="Toms River",
        state="NJ",
        zip_code="08753",
    )
    bad = call(
        "update_address",
        street1="88 Larch Street",
        city="Toms River",
        state="Nowhere",
        zip_code="08753",
    )
    service, runtime, _ = turn_service(good, bad, good)

    replies = turns(
        service,
        "I've moved to 88 Larch Street, Toms River, NJ 08753.",
        "no wait, the state is Nowhere",
        "yes",
    )

    assert replies[1] == STATE_REFUSAL
    # The refusal cleared the pending change, so the third turn reads the address back
    # again rather than saving the superseded one.
    assert "Nothing has been saved yet" in replies[2]
    assert runtime.writes("PUT", "/demographics") == []


def test_ac5_a_pending_confirmation_reaches_the_model_exactly_once() -> None:
    """The pending block quotes the confirmation, so it must not also arrive as the
    question just asked and as a transcript entry."""
    service, runtime, _ = turn_service(
        call("update_demographics", given_name="Jordan"),
        call("reply", message="ok"),
    )

    turns(service, "my first name is Jordan", "hmm")

    messages = sent_messages(runtime)
    rendered = json.dumps(messages)
    assert rendered.count("Here is the detail I am about to change") == 1
    assert "You have just asked the patient" not in messages[0]["content"]


def test_ac5_no_confirmation_or_cancel_phrase_list_is_consulted() -> None:
    """The same word means different things depending on the model's reading of it, and
    nothing here inspects the patient's words at all: "confirm" saves only because the
    model re-emitted the same call, and does nothing when it did not."""
    service, runtime, _ = turn_service(
        call("update_demographics", given_name="Jordan"),
        call("reply", message="Sorry, which part would you like to change?"),
    )

    replies = turns(service, "my first name is Jordan", "confirm")

    assert replies[1] == "Sorry, which part would you like to change?"
    assert runtime.writes("PUT", "/demographics") == []


# --- AC6/FR-34: nothing the patient typed reaches Groq, on any path -----------------


def test_ac6_no_patient_text_reaches_groq_on_any_path(tmp_path: Path) -> None:
    """FR-34, asserted rather than inspected, and stronger than before TICK-064.

    Groq is now genuinely wired and genuinely called on the last turn, so this is no
    longer "nothing goes out at all" -- something does. Every outbound request the whole
    app makes during a full conversation is recorded, and the patient's own words appear
    only in requests to the *local* model. What reaches Groq is the restatement.
    """
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = store.create_session(OAuthTokens("a", "r", "n"), NOW, configured.session_ttl)

    address_proposal = call(
        "update_address",
        street1="88 Larch Street",
        city="Toms River",
        state="NJ",
        zip_code="08753",
    )
    service, runtime, _ = turn_service(
        call("reply", message="Hello -- how can I help?"),
        address_proposal,
        address_proposal,
        call("list_appointments"),
        call("ask_general_knowledge", restatement="What is cognitive behavioural therapy?"),
    )
    app = create_app(configured, clock=lambda: NOW, model_turn_service=service)

    secrets = [
        "I have a headache and I live at 88 Larch Street",
        "88 Larch Street, Toms River, NJ 08753",
        "yes save it",
        "what have I got booked",
        "what is CBT anyway",
    ]
    replies = run(_post_all(app, handle, secrets))

    assert "Saved" in replies[2]
    assert "CBT is a talking therapy." in replies[4]

    # The turn really did egress, so the assertions below are about a live boundary.
    assert len(runtime.groq_requests) == 1

    outbound = runtime.model_requests + runtime.portal_requests + runtime.groq_requests
    for request in outbound:
        if str(request.url) == MODEL_ENDPOINT:
            continue
        body = (request.content or b"").decode()
        for secret in secrets:
            assert secret not in body, f"{secret!r} left in a request to {request.url}"

    # Control: the recorder does see the patient's words where they are allowed to be,
    # so the assertion above is a real observation rather than a broken harness.
    assert any(
        secrets[0] in (request.content or b"").decode() for request in runtime.model_requests
    )


def test_ac2_only_the_restatement_reaches_the_wire() -> None:
    """AC2: what leaves is the model's restatement plus content this codebase authored.

    Asserted as an equality over the whole Groq body rather than a substring check, so
    an extra field, an appended transcript, or a folded-in context blob fails here.
    """
    service, runtime, _ = turn_service(
        call("ask_general_knowledge", restatement="What is cognitive behavioural therapy?")
    )

    turns(service, "so what is CBT anyway, my therapist mentioned it")

    body = json.loads(runtime.groq_requests[0].content)
    assert body == {
        "model": "openai/gpt-oss-120b",
        "messages": [
            {"role": "system", "content": GENERAL_KNOWLEDGE_SYSTEM_PROMPT},
            {"role": "user", "content": "What is cognitive behavioural therapy?"},
        ],
        "stream": False,
    }


def test_ac4_the_patient_is_shown_the_question_asked_on_their_behalf() -> None:
    """AC4. The restatement is composed by the model and would otherwise be invisible.

    LOCAL_LLM_SPEC records the consequence as D14's standing risk: a restatement that
    drops or distorts the question yields a correct-looking answer to something the
    patient did not ask. Showing it is what makes a distortion noticeable.
    """
    service, _, _ = turn_service(
        call("ask_general_knowledge", restatement="How long does a flu shot take?")
    )

    reply = turns(service, "quick one -- how long will the jab take?")[0]

    assert "How long does a flu shot take?" in reply
    assert reply == GENERAL_KNOWLEDGE_ANSWER_TEMPLATE.format(
        asked="How long does a flu shot take?", answer="CBT is a talking therapy."
    )


def test_ac3_presidio_rejects_a_phi_bearing_restatement_and_nothing_is_sent() -> None:
    """AC3/ADR-5, with a seeded sensitive value: reject, never scrub.

    The local model is the thing that went wrong here -- it restated the question with
    the patient's phone number still in it, which is exactly the case D14 says a
    restatement "could in principle" produce and D4 positions Presidio to catch. The
    payload was constructed correctly and screened anyway, which is the whole point of
    having two independent controls.
    """
    service, runtime, _ = turn_service(
        call(
            "ask_general_knowledge",
            restatement="Is it normal to be called back on 555-555-5555 after a test?",
        )
    )

    reply = turns(service, "is it normal for them to call me back?")[0]

    # Nothing was sent -- not a redacted version, not a truncated one, nothing.
    assert runtime.groq_requests == []
    # And the patient is told, rather than being handed an answer to a question that
    # was never asked.
    assert reply == GENERAL_KNOWLEDGE_WITHHELD_RESPONSE.format(
        asked="Is it normal to be called back on 555-555-5555 after a test?"
    )
    assert "555-555-5555" not in "".join(
        (request.content or b"").decode() for request in runtime.portal_requests
    )


def test_ac3_a_withheld_question_is_never_answered_from_nothing() -> None:
    """A rejection must not be dressed up as an answer.

    The failure worth guarding against is a reply that reads like a lookup happened, so
    this asserts the reply says nothing was sent and carries none of the stub answer the
    external model would have given.
    """
    service, runtime, _ = turn_service(
        call("ask_general_knowledge", restatement="My MRN is MRN-889900, what does it mean?")
    )

    reply = turns(service, "what does my record number mean?")[0]

    assert runtime.groq_requests == []
    assert "I did not send it" in reply
    assert "CBT is a talking therapy." not in reply


def test_ac6_an_unconfigured_groq_costs_general_knowledge_and_nothing_else() -> None:
    """AC6: Groq being absent degrades one tool. Everything patient-specific is local.

    Driven as one conversation rather than two cases, because the claim is about the
    turns sitting either side of the unavailable one: the address write and the
    appointment list run through the same service instance and must be untouched.
    """
    service, runtime, _ = turn_service(
        call("ask_general_knowledge", restatement="What is a routine physical?"),
        call(
            "update_address",
            street1="88 Larch Street",
            city="Toms River",
            state="NJ",
            zip_code="08753",
        ),
        call(
            "update_address",
            street1="88 Larch Street",
            city="Toms River",
            state="NJ",
            zip_code="08753",
        ),
        call("list_appointments"),
        groq=False,
    )

    replies = turns(
        service,
        "what is a physical?",
        "my address is 88 Larch Street, Toms River, NJ 08753",
        "yes",
        "what have I got booked",
    )

    assert replies[0] == GENERAL_KNOWLEDGE_UNAVAILABLE_RESPONSE
    assert "Saved" in replies[2]
    assert "Here are your upcoming appointments" in replies[3]
    assert runtime.writes("PUT", "/demographics")


def test_ac6_an_unreachable_groq_degrades_only_that_answer() -> None:
    """Same criterion, but with Groq configured and failing rather than absent.

    A 500 from a third party must not escape as a 500 from this server, and must not
    become the chat-wide unavailable message either -- the local front door is fine.
    """
    runtime = Runtime(
        replies=[call("ask_general_knowledge", restatement="What is a routine physical?")],
        groq_status=500,
    )
    service, _, _ = turn_service(runtime=runtime)

    reply = turns(service, "what is a physical?")[0]

    assert reply == GENERAL_KNOWLEDGE_UNAVAILABLE_RESPONSE
    assert reply != ASSISTANT_UNAVAILABLE_RESPONSE


def test_the_general_knowledge_turn_reaches_no_patient_service() -> None:
    """The tool writes nothing and reads no record: `TOOL_SURFACE` says `writes=False`
    and `backed_by="Groq"`, and this is that claim observed."""
    service, runtime, _ = turn_service(
        call("ask_general_knowledge", restatement="What is cognitive behavioural therapy?")
    )

    turns(service, "what is CBT?")

    assert runtime.portal_requests == []


def test_ac6_the_turn_client_protocol_excludes_the_groq_client() -> None:
    """Structural, not conventional: what the turn asks a runtime for is a `tool_call`,
    and `HttpGroqClient` does not have one, so the front door cannot be pointed at Groq
    by a wiring mistake."""
    from ai_server.llm.groq import HttpGroqClient

    assert hasattr(HttpLocalModelClient, "tool_call")
    assert not hasattr(HttpGroqClient, "tool_call")


def test_ac6_a_groq_provider_degrades_the_chat_rather_than_becoming_the_front_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_server.app.main import _build_model_turn_service

    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-key")
    monkeypatch.setenv("LLM_MODEL", "llama3.1:8b-instruct-q4_K_M")
    store = SessionStore(Path("/tmp/unused-tick063.sqlite3"), b"k" * 32)

    service = _build_model_turn_service(
        httpx.AsyncClient(), httpx.AsyncClient(), store, lambda: NOW
    )

    assert service.client is None
    assert (
        run(_collect(service.stream_reply("handle-1", "hello"))) == ASSISTANT_UNAVAILABLE_RESPONSE
    )


def test_ac6_an_unavailable_model_server_reports_an_honest_outage() -> None:
    """D12: with no deterministic fallback, model-server availability is chat
    availability, and the outage must not look like a broken feature."""
    assert (
        run(_collect(unavailable_model_turn_service().stream_reply("handle-1", "hello")))
        == ASSISTANT_UNAVAILABLE_RESPONSE
    )

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    service, _, _ = turn_service()
    service.client = HttpLocalModelClient(
        LocalModelSettings(model="llama3.1:8b-instruct-q4_K_M", base_url=MODEL_BASE_URL),
        httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )

    assert turns(service, "hello")[0] == ASSISTANT_UNAVAILABLE_RESPONSE


# --- AC7: every capability the acceptance corpus measures still works ----------------


def _offer_first(service: ModelTurnService, runtime: Runtime, tool: str) -> dict[str, str]:
    """Run the turn that offers this session its scheduling tokens, and return the first.

    The corpus states its own token strings, which no store ever issued. A real session
    only ever holds a token `SlotDiscoveryService`/`AppointmentDiscoveryService` minted,
    and only such a token resolves -- so the case is driven the way the case describes:
    the assistant offers, then the patient chooses one of what was offered.
    """
    if tool == "book_appointment":
        runtime.replies.insert(0, call("find_slots"))
        turns(service, "what times do you have?")
        return {
            "slot_token": service.conversations.state("handle-1", NOW).offered_slots[0].slot_token
        }
    runtime.replies.insert(0, call("list_appointments"))
    turns(service, "what do I have booked?")
    return {
        "appointment_token": service.conversations.state("handle-1", NOW)
        .offered_appointments[0]
        .appointment_token
    }


def _write_cases() -> list[dict[str, Any]]:
    return [case for case in CORPUS["cases"] if case["expected_write"] is not None]


@pytest.mark.parametrize("case", _write_cases(), ids=lambda case: case["id"])
def test_ac7_every_corpus_write_case_is_read_back_then_written(case: dict[str, Any]) -> None:
    """TICK-062's corpus, executed rather than only scored.

    Each case's expected structured output is fed to the turn loop as the model's call.
    The first turn must read it back and write nothing; the second, agreeing, must reach
    OpenEMR. That covers booking, cancelling, onboarding answers, address and
    demographics updates across every phrasing the corpus holds.
    """
    tool = case["expected_tool"]
    service, runtime, _ = turn_service()
    arguments = dict(case["expected_write"])
    if tool in ("book_appointment", "cancel_appointment"):
        arguments = _offer_first(service, runtime, tool)
    proposal = call(tool, **arguments)
    runtime.replies.extend([proposal, proposal])
    before = len(runtime.portal_requests)

    read_back = turns(service, case["utterance"])[0]
    assert "Nothing has been" in read_back, read_back
    # `record_assessment_answer` creates the OpenEMR draft it will checkpoint into, but
    # no field value may reach it before the patient agrees.
    assert runtime.writes("PUT", "/demographics") == []
    assert runtime.writes("POST", "/appointment") == []
    assert [request for request in runtime.portal_requests if request.method == "PUT"] == []

    confirmed = turns(service, "yes, that's right")[0]
    assert any(
        marker in confirmed for marker in ("Saved", "Booked and confirmed", "Cancelled and")
    ), confirmed
    assert len(runtime.portal_requests) > before, "the confirmed turn wrote nothing"


def test_ac7_listing_appointments_still_works() -> None:
    service, runtime, _ = turn_service(call("list_appointments"))

    reply = turns(service, "what have I got booked?")[0]

    assert "Here are your upcoming appointments" in reply
    assert "Thursday 17 September 2026" in reply
    assert len(service.conversations.state("handle-1", NOW).offered_appointments) == 1
    assert runtime.portal_requests == []


def test_ac7_finding_slots_still_works() -> None:
    service, _, _ = turn_service(call("find_slots"))

    reply = turns(service, "when can I come in?")[0]

    assert "Here are the appointment times I can offer" in reply
    assert "Tuesday 15 September 2026" in reply


def test_ac7_ocr_confirmation_still_works() -> None:
    """The upload arrives in its own request field -- a structural fact, not a phrasing
    -- and reading it stays the model's decision through `extract_document_fields`."""
    ocr = OcrService(_ScriptedTesseract())
    service, runtime, _ = turn_service(
        call("reply", message="Thanks -- let me read that."), ocr=ocr
    )
    consent_action = json.dumps({"action": "upload_identity_document", "consent": True})

    async def scenario() -> list[str]:
        first = await _turn(service, consent_action, image_base64=_PNG_BASE64)
        state = service.conversations.state("handle-1", NOW)
        runtime.replies.append(call("extract_document_fields", upload_id=state.upload_id))
        return [first, await _turn(service, "yes please read it")]

    replies = run(scenario())

    upload_id = service.conversations.state("handle-1", NOW).upload_id
    assert upload_id is not None
    assert f'Its upload_id is "{upload_id}"' in system_message(runtime)
    assert "Name: Jordan Rivera" in replies[1]
    assert "Nothing has been saved" in replies[1]


def test_ac7_an_upload_without_consent_is_never_read() -> None:
    ocr = OcrService(_ScriptedTesseract())
    service, _, _ = turn_service(ocr=ocr)

    async def scenario() -> str:
        return await _turn(
            service,
            json.dumps({"action": "upload_identity_document", "consent": False}),
            image_base64=_PNG_BASE64,
        )

    reply = run(scenario())

    assert "did not have your consent" in reply
    assert service.conversations.state("handle-1", NOW).upload_id is None


def test_ac7_an_upload_id_the_model_invented_reads_nothing() -> None:
    ocr = OcrService(_ScriptedTesseract())
    service, _, _ = turn_service(call("extract_document_fields", upload_id="Xy" * 12), ocr=ocr)

    reply = turns(service, "read my ID")[0]

    assert "don't have a document to read" in reply


# --- The prompt the corpus measured is the prompt production sends -------------------


def test_the_runtime_and_the_harness_share_one_measured_prompt() -> None:
    """`AI_USAGE.md` records the model's corpus scores against a prompt version. Those
    numbers only describe production if production sends that prompt."""
    import scripts.evaluate_acceptance_corpus as harness

    assert harness.ACCEPTANCE_PROMPT_VERSION == PROMPT_VERSION
    service, runtime, _ = turn_service(call("reply", message="hi"))
    turns(service, "hello")
    assert SYSTEM_PROMPT in system_message(runtime)


def test_a_corpus_case_renders_exactly_the_two_messages_it_was_measured_with() -> None:
    import scripts.evaluate_acceptance_corpus as harness

    corpus = harness.load_corpus(harness.DEFAULT_CORPUS)
    for case in corpus.cases:
        messages = harness.render_messages(corpus, case)
        assert len(messages) == 2
        assert messages[1] == {"role": "user", "content": case.utterance}


# --- Shared fixtures ----------------------------------------------------------------


class _ScriptedTesseract:
    async def recognize_text(self, image: bytes) -> str:
        del image
        return "Name: Jordan Rivera\nDOB: 1985-04-01\nAddress: 88 Larch Street, Toms River, NJ"


# The smallest valid PNG `ai_server.ocr.service.validate_image_upload` accepts.
_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg=="
)


def settings(tmp_path: Path) -> AuthSettings:
    return AuthSettings(
        database_path=tmp_path / "sessions.sqlite3",
        encryption_key=b"k" * 32,
        authorize_url="https://openemr.test/oauth2/default/authorize",
        token_url="https://openemr.test/oauth2/default/token",
        jwks_url="https://openemr.test/oauth2/default/jwks",
        issuer="https://openemr.test",
        client_id="synthetic-client",
        client_secret="synthetic-secret",
        redirect_uri="https://chat.test/oauth/callback",
        dashboard_redirect_uri="https://emr.test/portal/home.php",
        chat_origin="https://chat.test",
        session_ttl=timedelta(minutes=30),
        state_ttl=timedelta(minutes=5),
        expiry_warning_window=timedelta(0),
    )


async def _collect(stream: AsyncIterator[str]) -> str:
    return "".join([chunk async for chunk in stream])


async def _post_all(app, cookie: str, messages: Iterable[str]) -> list[str]:
    """Drive a whole conversation through one app instance, keeping the in-process
    conversation state alive across turns (a fresh lifespan per turn would not)."""
    replies: list[str] = []
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://chat.test",
            cookies={"ai_session": cookie},
        ) as client:
            for message in messages:
                response = await client.post(
                    "/api/chat",
                    json={"message": message},
                    headers={"origin": "https://chat.test"},
                )
                assert response.status_code == 200, response.text
                replies.append(response.text)
    return replies


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
