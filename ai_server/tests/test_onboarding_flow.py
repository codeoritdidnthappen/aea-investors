"""Integration and static tests for `OnboardingFlow` (TICK-017, AC1-AC5).

Exercises the whole guided flow against synthetic OpenEMR endpoints
(`httpx.MockTransport`, matching every other OpenEMR-adapter test in this suite),
including a simulated AI-server restart (AC2) and both completion failure modes
(AC3). Static checks close AC5 by construction: the onboarding package never imports
the external-model client at all.
"""

from __future__ import annotations

import ast
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from ai_server.onboarding.draft_client import (
    AssessmentDraftAdapter,
    OpenEmrPortalSettings,
)
from ai_server.onboarding.flow import (
    FieldCheckpointRejected,
    OnboardingCursor,
    OnboardingFlow,
    OnboardingIncompleteError,
)
from ai_server.openemr.adapter import OpenEmrRequestError
from ai_server.openemr.demographics import OpenEmrDemographicsAdapter, OpenEmrDemographicsSettings

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ONBOARDING_DIR = _REPO_ROOT / "ai_server" / "onboarding"

PORTAL_BASE_URL = "https://openemr.test/apis/default"
DEMOGRAPHICS_BASE_URL = "https://openemr.test/apis/default/api"
NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)
PATIENT_UUID = "synthetic-patient-uuid"

_REQUIRED_COMPLETE_FIELDS = {
    "preferred_contact": {"method": "portal_message"},
    "help_type": "both",
    "visit_preference": {"format": "video", "time_window": "weekday_morning"},
}
_IDENTITY = {
    "given_name": "Avery",
    "family_name": "Alden",
    "date_of_birth": "1990-01-01",
    "address": {
        "street1": "100 Maple Avenue",
        "city": "Springfield",
        "state": "IL",
        "zip_code": "62704",
    },
}


def run(coroutine):
    return asyncio.run(coroutine)


class _SyntheticOpenEmr:
    """A minimal in-memory stand-in for the draft + demographics endpoints.

    Shared across independently constructed `OnboardingFlow`/adapter instances so a
    test can simulate an AI-server restart: nothing here is held by `OnboardingFlow`
    itself, only by this synthetic server, exactly like real OpenEMR would be.
    """

    def __init__(self) -> None:
        self.drafts: dict[str, dict[str, object]] = {}
        self.demographics_writes: list[httpx.Request] = []
        self._next_id = 1

    def draft_handler(self, request: httpx.Request):
        if request.method == "POST" and request.url.path.endswith("/assessment"):
            uuid = f"draft-{self._next_id}"
            self._next_id += 1
            body = _json_body(request)
            self.drafts[uuid] = {"status": "draft", "fields": body}
            return httpx.Response(201, json={"uuid": uuid, "status": "draft", "fields": body})
        uuid = request.url.path.rsplit("/", 1)[-1]
        record = self.drafts.get(uuid)
        if record is None:
            return httpx.Response(404, json={"error": "no assessment draft with that id"})
        if request.method == "GET":
            return httpx.Response(
                200, json={"uuid": uuid, "status": record["status"], "fields": record["fields"]}
            )
        if request.method == "PUT":
            if record["status"] == "completed":
                return httpx.Response(409, json={"error": "this assessment is already completed"})
            body = _json_body(request)
            requested_complete = body.pop("status", None) == "completed"
            record["fields"] = {**record["fields"], **body}
            if requested_complete:
                missing = [
                    key
                    for key in (
                        "preferred_contact_method",
                        "help_type",
                        "visit_format",
                        "visit_time_window",
                    )
                    if not record["fields"].get(key)
                ]
                if missing:
                    return httpx.Response(
                        400,
                        json={
                            "error": "validation failed",
                            "details": [f"{key} is required" for key in missing],
                        },
                    )
                record["status"] = "completed"
            return httpx.Response(
                200, json={"uuid": uuid, "status": record["status"], "fields": record["fields"]}
            )
        raise AssertionError(f"unexpected method {request.method}")

    def demographics_handler(self, request: httpx.Request):
        self.demographics_writes.append(request)
        return httpx.Response(200, json={"data": {}})

    def failing_demographics_handler(self, request: httpx.Request):
        self.demographics_writes.append(request)
        return httpx.Response(403, json={"error": "forbidden"})


def _json_body(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content) if request.content else {}


def _flow(server: _SyntheticOpenEmr, *, demographics_ok: bool = True) -> OnboardingFlow:
    draft_client = httpx.AsyncClient(transport=httpx.MockTransport(server.draft_handler))
    draft_adapter = AssessmentDraftAdapter(
        OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL), draft_client
    )
    demographics_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            server.demographics_handler if demographics_ok else server.failing_demographics_handler
        )
    )
    demographics_adapter = OpenEmrDemographicsAdapter(
        OpenEmrDemographicsSettings(api_base_url=DEMOGRAPHICS_BASE_URL), demographics_client
    )
    return OnboardingFlow(draft_adapter, demographics_adapter)


def test_ac2_start_creates_an_empty_draft_and_returns_a_usable_cursor() -> None:
    server = _SyntheticOpenEmr()
    cursor = run(_flow(server).start("token"))

    assert isinstance(cursor, OnboardingCursor)
    assert server.drafts[cursor.draft_uuid] == {"status": "draft", "fields": {}}


def test_ac1_a_valid_draft_field_is_checkpointed_through_openemr() -> None:
    server = _SyntheticOpenEmr()
    flow = _flow(server)
    cursor = run(flow.start("token"))

    draft = run(flow.checkpoint_field("token", cursor, "help_type", "counseling_or_therapy", NOW))

    assert draft.fields["help_type"] == "counseling_or_therapy"
    assert server.drafts[cursor.draft_uuid]["fields"]["help_type"] == "counseling_or_therapy"


def test_ac1_an_invalid_draft_field_is_rejected_and_never_checkpointed() -> None:
    server = _SyntheticOpenEmr()
    flow = _flow(server)
    cursor = run(flow.start("token"))

    with pytest.raises(FieldCheckpointRejected):
        run(flow.checkpoint_field("token", cursor, "help_type", "not_a_real_option", NOW))

    assert server.drafts[cursor.draft_uuid]["fields"] == {}


def test_ac1_a_field_that_openemr_itself_rejects_surfaces_its_message() -> None:
    """A value the AI server's own validator accepts but OpenEMR's rejects (e.g. a
    stale contract) still fails safely with no partial checkpoint."""
    server = _SyntheticOpenEmr()
    flow = _flow(server)
    cursor = run(flow.start("token"))
    server.drafts[cursor.draft_uuid]["status"] = "completed"

    with pytest.raises(FieldCheckpointRejected, match="already completed"):
        run(flow.checkpoint_field("token", cursor, "help_type", "both", NOW))


def test_checkpoint_field_refuses_an_identity_field() -> None:
    server = _SyntheticOpenEmr()
    flow = _flow(server)
    cursor = run(flow.start("token"))

    with pytest.raises(ValueError, match="not a draft-backed field"):
        run(flow.checkpoint_field("token", cursor, "given_name", "Avery", NOW))


def test_ac2_a_reload_after_a_simulated_restart_sees_every_checkpointed_field() -> None:
    """AC2: draft changes checkpoint through OpenEMR and reload after a restart.

    `first_process`/`second_process` are two independent `OnboardingFlow` instances
    that share nothing but the synthetic OpenEMR backing store -- exactly the
    invariant a real AI-server restart requires (ARCHITECTURE.md Sec. 5).
    """
    server = _SyntheticOpenEmr()
    first_process = _flow(server)
    cursor = run(first_process.start("token"))
    run(first_process.checkpoint_field("token", cursor, "help_type", "both", NOW))
    run(
        first_process.checkpoint_field(
            "token",
            cursor,
            "visit_preference",
            {"format": "video", "time_window": "weekend"},
            NOW,
        )
    )

    reloaded_cursor = OnboardingCursor.deserialize(cursor.serialize())
    second_process = _flow(server)
    reloaded = run(second_process.resume("token", reloaded_cursor))

    assert reloaded.fields == {
        "help_type": "both",
        "visit_format": "video",
        "visit_time_window": "weekend",
    }


def test_ac3_completion_writes_demographics_and_finalizes_the_native_assessment() -> None:
    server = _SyntheticOpenEmr()
    flow = _flow(server)
    cursor = run(flow.start("token"))
    for field, value in _REQUIRED_COMPLETE_FIELDS.items():
        run(flow.checkpoint_field("token", cursor, field, value, NOW))

    record = run(flow.complete("token", PATIENT_UUID, cursor, _IDENTITY, NOW))

    assert server.drafts[cursor.draft_uuid]["status"] == "completed"
    assert len(server.demographics_writes) == 1
    demographics_body = json.loads(server.demographics_writes[0].content)
    assert demographics_body == {
        "fname": "Avery",
        "lname": "Alden",
        "DOB": "1990-01-01",
        "street": "100 Maple Avenue, Springfield, IL 62704",
    }
    assert record.given_name == "Avery"
    assert record.draft_fields["help_type"] == "both"


def test_ac3_completion_never_joins_then_re_splits_a_multi_word_family_name() -> None:
    """given_name/family_name reach the demographics write exactly as confirmed, never
    joined into one string and re-split -- a re-split cannot recover a multi-word
    family name ("Van Der Berg") without guessing which word is the family name."""
    server = _SyntheticOpenEmr()
    flow = _flow(server)
    cursor = run(flow.start("token"))
    for field, value in _REQUIRED_COMPLETE_FIELDS.items():
        run(flow.checkpoint_field("token", cursor, field, value, NOW))

    identity = {**_IDENTITY, "family_name": "Van Der Berg"}
    record = run(flow.complete("token", PATIENT_UUID, cursor, identity, NOW))

    demographics_body = json.loads(server.demographics_writes[0].content)
    assert demographics_body["fname"] == "Avery"
    assert demographics_body["lname"] == "Van Der Berg"
    assert record.family_name == "Van Der Berg"


def test_ac3_completion_with_a_stale_cursor_raises_the_documented_error_type() -> None:
    """A cursor whose OpenEMR draft no longer exists (deleted out from under it, or
    just invalid) must still fail as OnboardingIncompleteError -- the type complete()
    documents and demographics_writes == [] verifies -- not the draft-client's own
    AssessmentDraftNotFoundError leaking through undocumented."""
    server = _SyntheticOpenEmr()
    flow = _flow(server)
    cursor = OnboardingCursor(draft_uuid="never-created")

    with pytest.raises(OnboardingIncompleteError):
        run(flow.complete("token", PATIENT_UUID, cursor, _IDENTITY, NOW))

    assert server.demographics_writes == []


def test_ac3_completion_with_a_missing_required_field_writes_nothing() -> None:
    """AC3: no separate durable patient record -- an incomplete attempt persists
    nothing anywhere, OpenEMR included."""
    server = _SyntheticOpenEmr()
    flow = _flow(server)
    cursor = run(flow.start("token"))
    # Only two of the three required draft fields are checkpointed.
    run(flow.checkpoint_field("token", cursor, "help_type", "both", NOW))

    with pytest.raises(OnboardingIncompleteError):
        run(flow.complete("token", PATIENT_UUID, cursor, _IDENTITY, NOW))

    assert server.drafts[cursor.draft_uuid]["status"] == "draft"
    assert server.demographics_writes == []


def test_ac3_completion_with_invalid_identity_writes_nothing() -> None:
    server = _SyntheticOpenEmr()
    flow = _flow(server)
    cursor = run(flow.start("token"))
    for field, value in _REQUIRED_COMPLETE_FIELDS.items():
        run(flow.checkpoint_field("token", cursor, field, value, NOW))
    invalid_identity = {**_IDENTITY, "date_of_birth": "2020-01-01"}  # under 18

    with pytest.raises(OnboardingIncompleteError, match="under 18"):
        run(flow.complete("token", PATIENT_UUID, cursor, invalid_identity, NOW))

    assert server.drafts[cursor.draft_uuid]["status"] == "draft"
    assert server.demographics_writes == []


def test_ac3_a_failed_demographics_write_retains_the_draft_for_retry() -> None:
    """`ONBOARDING_CONTRACT.md` "Draft and completion semantics" #5: on failure,
    retain the draft and show a retry message -- completion is never reported."""
    server = _SyntheticOpenEmr()
    flow = _flow(server, demographics_ok=False)
    cursor = run(flow.start("token"))
    for field, value in _REQUIRED_COMPLETE_FIELDS.items():
        run(flow.checkpoint_field("token", cursor, field, value, NOW))

    with pytest.raises(OpenEmrRequestError):
        run(flow.complete("token", PATIENT_UUID, cursor, _IDENTITY, NOW))

    assert server.drafts[cursor.draft_uuid]["status"] == "draft"


def test_onboarding_cursor_round_trips_through_serialize_and_deserialize() -> None:
    cursor = OnboardingCursor(draft_uuid="draft-1")
    assert OnboardingCursor.deserialize(cursor.serialize()) == cursor


def test_onboarding_cursor_refuses_to_deserialize_an_empty_value() -> None:
    with pytest.raises(ValueError):
        OnboardingCursor.deserialize("")


def test_ac5_the_onboarding_package_never_imports_the_external_model_client() -> None:
    """AC5: sensitive patient content is not sent to the external model.

    Enforced structurally: nothing under `ai_server/onboarding/` may import
    `ai_server.llm` (the only path to Groq) at all, so there is no code path from a
    field value to an outbound model request.
    """
    for path in _ONBOARDING_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                assert not module.startswith("ai_server.llm"), (
                    f"{path.name} imports {module}, which can reach the external model"
                )


def test_ac5_the_onboarding_package_has_no_direct_local_storage() -> None:
    """AC3/NFR: the flow keeps no separate durable patient record of its own.

    Enforced structurally: nothing under `ai_server/onboarding/` opens a database
    connection or writes a local file -- the only durable writes are the two
    adapters' outbound HTTP calls to OpenEMR itself.
    """
    for path in _ONBOARDING_DIR.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "sqlite3" not in source, f"{path.name} must not open a local database"
        assert "open(" not in source, f"{path.name} must not write a local file"
