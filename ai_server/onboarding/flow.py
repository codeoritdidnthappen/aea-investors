"""Guided-onboarding orchestration: validate, checkpoint, and complete the approved
v1 assessment (`ONBOARDING_CONTRACT.md`).

Field capture, validation, draft writes, and completion checks are deterministic
local operations (the contract's "Conversational boundary") -- this module never
calls an external model and holds no identity value beyond the current call
(ARCHITECTURE.md Sec. 5: "LangGraph keeps only request-duration patient values in
memory and stores a non-patient workflow cursor in SQLite"). The per-field
checkpoint sequence (validate -> checkpoint -> respond) is modeled as a small
LangGraph `StateGraph` so validation failures never reach the OpenEMR write step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict

from langgraph.graph import END, StateGraph

from ai_server.onboarding.draft_client import (
    AssessmentDraft,
    AssessmentDraftAdapter,
    AssessmentDraftConflictError,
    AssessmentDraftValidationError,
)
from ai_server.onboarding.fields import (
    DRAFT_FIELDS,
    Address,
    FieldValidationError,
    validate_field,
)
from ai_server.openemr.demographics import (
    ConfirmedIdentity,
    OpenEmrDemographicsAdapter,
    confirm_identity,
)

_REQUIRED_DRAFT_KEYS: tuple[str, ...] = (
    "preferred_contact_method",
    "help_type",
    "visit_format",
    "visit_time_window",
)


@dataclass(frozen=True)
class OnboardingCursor:
    """The only state persisted outside OpenEMR: a non-patient workflow position.

    This is exactly ARCHITECTURE.md Sec. 5's "non-patient workflow cursor" --
    `serialize()`/`deserialize()` round-trip through the plain-text `sessions.cursor`
    column (`SessionStore.save_cursor`/`load_cursor`), and it never carries a field
    value, only the draft's own id.
    """

    draft_uuid: str

    def serialize(self) -> str:
        return self.draft_uuid

    @classmethod
    def deserialize(cls, raw: str) -> OnboardingCursor:
        if not raw:
            raise ValueError("an onboarding cursor cannot be empty")
        return cls(draft_uuid=raw)


class FieldCheckpointRejected(Exception):
    """Raised when a submitted field is invalid, locally or per OpenEMR's own check.

    `details` holds every human-readable validation message; nothing is
    checkpointed to OpenEMR when this is raised (AC1: an invalid field never
    advances the draft).
    """

    def __init__(self, details: list[str]) -> None:
        super().__init__(details[0] if details else "field checkpoint rejected")
        self.details = details


class OnboardingIncompleteError(Exception):
    """Raised when completion is attempted before every required field is valid."""

    def __init__(self, details: list[str]) -> None:
        super().__init__(details[0] if details else "onboarding is incomplete")
        self.details = details


@dataclass(frozen=True)
class AssessmentRecord:
    """The approved structured record (AC1), produced only once completion succeeds.

    `draft_fields` is exactly what OpenEMR's own completed-draft response returned
    -- never a locally reconstructed guess -- so this record reflects the native
    persisted state, not AI-server state.
    """

    given_name: str
    family_name: str
    date_of_birth: str
    address: Address
    draft_fields: dict[str, object]


class _CheckpointState(TypedDict, total=False):
    access_token: str
    draft_uuid: str
    field: str
    value: object
    now: datetime
    validated: object
    error: list[str] | None
    draft: AssessmentDraft | None


class OnboardingFlow:
    """Guides one patient through the approved assessment and checkpoints it live.

    Neither adapter is stored beyond this instance's lifetime, and neither is ever
    given a token or patient id it did not receive as a call argument -- this class
    adds no new authority beyond what its two adapters already enforce.
    """

    def __init__(
        self,
        draft_adapter: AssessmentDraftAdapter,
        demographics_adapter: OpenEmrDemographicsAdapter,
    ) -> None:
        self._draft_adapter = draft_adapter
        self._demographics_adapter = demographics_adapter
        self._checkpoint_graph = self._build_checkpoint_graph()

    async def start(self, access_token: str) -> OnboardingCursor:
        """Begin a flow: create an empty native draft and return its cursor (AC2)."""
        draft = await self._draft_adapter.create(access_token, {})
        return OnboardingCursor(draft_uuid=draft.uuid)

    async def resume(self, access_token: str, cursor: OnboardingCursor) -> AssessmentDraft:
        """Reload a draft's already-checkpointed fields, including after a restart (AC2)."""
        return await self._draft_adapter.read(access_token, cursor.draft_uuid)

    async def checkpoint_field(
        self,
        access_token: str,
        cursor: OnboardingCursor,
        field: str,
        value: object,
        now: datetime,
    ) -> AssessmentDraft:
        """Validate one field and, only if valid, checkpoint it into the native draft.

        Only accepts a draft-backed field (`preferred_contact`, `help_type`,
        `visit_preference`, `accommodations`). Identity fields (given/family name,
        date of birth, address) are not part of the OpenEMR draft resource
        (`openemr_modules/aeai-portal-chat`'s own docstring: identity is TICK-016's
        concern) -- validate them directly with `fields.validate_field` and hold
        them until `complete()`, which writes them together.
        """
        if field not in DRAFT_FIELDS:
            raise ValueError(
                f"{field!r} is not a draft-backed field; use fields.validate_field "
                "for an identity field and hold it until complete()"
            )
        result: _CheckpointState = await self._checkpoint_graph.ainvoke(
            {
                "access_token": access_token,
                "draft_uuid": cursor.draft_uuid,
                "field": field,
                "value": value,
                "now": now,
            }
        )
        if result.get("error"):
            raise FieldCheckpointRejected(result["error"])
        return result["draft"]

    async def complete(
        self,
        access_token: str,
        patient_uuid: str,
        cursor: OnboardingCursor,
        identity: dict[str, object],
        now: datetime,
    ) -> AssessmentRecord:
        """Write confirmed demographics, then finalize the native assessment (AC3).

        Both OpenEMR operations must succeed before completion is reported; if
        either fails, no local state changes and the draft is left exactly where it
        was so the caller can show a retry message and try again
        (`ONBOARDING_CONTRACT.md` "Draft and completion semantics" #5).
        """
        errors: list[str] = []
        given_name = family_name = date_of_birth = None
        address: Address | None = None
        for field, raw in (
            ("given_name", identity.get("given_name")),
            ("family_name", identity.get("family_name")),
            ("date_of_birth", identity.get("date_of_birth")),
            ("address", identity.get("address")),
        ):
            try:
                validated = validate_field(field, raw, now=now)
            except FieldValidationError as exc:
                errors.extend(exc.details)
                continue
            if field == "given_name":
                given_name = validated
            elif field == "family_name":
                family_name = validated
            elif field == "date_of_birth":
                date_of_birth = validated
            elif field == "address":
                address = validated

        draft = await self._draft_adapter.read(access_token, cursor.draft_uuid)
        missing = [key for key in _REQUIRED_DRAFT_KEYS if not draft.fields.get(key)]
        if missing:
            errors.append(f"the assessment draft is missing required fields: {', '.join(missing)}")

        if errors:
            raise OnboardingIncompleteError(errors)
        assert (
            given_name and family_name and date_of_birth and address
        )  # narrows for the type checker

        confirmed: ConfirmedIdentity = confirm_identity(
            given_name, family_name, date_of_birth, _format_address(address)
        )
        # A failure here (network, non-200) propagates as `OpenEmrRequestError` and
        # nothing further runs: the draft is never marked completed, matching
        # "retain the draft and show a retry message" -- there is no path from a
        # failed demographics write to a completed assessment.
        await self._demographics_adapter.write_confirmed_demographics(
            access_token, patient_uuid, confirmed
        )
        completed_draft = await self._draft_adapter.update(
            access_token, cursor.draft_uuid, {}, complete=True
        )
        return AssessmentRecord(
            given_name=given_name,
            family_name=family_name,
            date_of_birth=date_of_birth,
            address=address,
            draft_fields=completed_draft.fields,
        )

    def _build_checkpoint_graph(self):
        graph = StateGraph(_CheckpointState)
        graph.add_node("validate", self._validate_node)
        graph.add_node("checkpoint", self._checkpoint_node)
        graph.set_entry_point("validate")
        graph.add_conditional_edges(
            "validate", self._route_after_validate, {"checkpoint": "checkpoint", END: END}
        )
        graph.add_edge("checkpoint", END)
        return graph.compile()

    @staticmethod
    def _validate_node(state: _CheckpointState) -> dict[str, object]:
        try:
            validated = validate_field(state["field"], state["value"], now=state["now"])
        except FieldValidationError as exc:
            return {"error": list(exc.details)}
        return {"validated": validated, "error": None}

    @staticmethod
    def _route_after_validate(state: _CheckpointState) -> str:
        return END if state.get("error") else "checkpoint"

    async def _checkpoint_node(self, state: _CheckpointState) -> dict[str, object]:
        try:
            draft = await self._draft_adapter.update(
                state["access_token"], state["draft_uuid"], state["validated"]
            )
        except AssessmentDraftValidationError as exc:
            return {"error": [str(exc), *exc.details] if exc.details else [str(exc)]}
        except AssessmentDraftConflictError as exc:
            return {"error": [str(exc)]}
        return {"draft": draft, "error": None}


def _format_address(address: Address) -> str:
    """Reshape a validated `Address` into the single-line string the demographics
    adapter's Standard API `street` field expects; nothing here is fabricated, only
    reformatted from already-confirmed components."""
    line = address.street1
    if address.street2:
        line = f"{line}, {address.street2}"
    return f"{line}, {address.city}, {address.state} {address.zip_code}"
