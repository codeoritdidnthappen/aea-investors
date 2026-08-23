"""Score a local model against the acceptance corpus on two separate bars (TICK-062).

`docs/LOCAL_LLM_SPEC.md` D8 makes the eval set a deliverable rather than a test
afterthought, and D15 fixes what it measures:

* **Writes.** Any field reaching the patient's record is right or refused. Zero wrong,
  across the whole corpus. This is PRD NFR-36 and it is not a percentage.
* **Understanding.** Failing to grasp what the patient asked costs a retry, not a
  record. It is held to a stated threshold, and imperfection is acceptable.

The two are reported separately and never combined. A single blended score would price
a corrupted chart like a misunderstood question, which is the specific mistake this
project has already made once (TICK-050 wrote `"Update it to: 2002 Bridge Avenue"` into
a chart).

**What "reaching the record" means here.** The harness does not diff the model's JSON
against the corpus. It runs the model's proposed arguments through
`ai_server.llm.validation.validate_write` -- the same door TICK-061 put in front of the
real write path -- and scores what comes out the other side. So a model that proposes
nonsense the validator refuses has not written a wrong field; it has been refused, which
under NFR-36 is an acceptable outcome and is reported as such. Only a value the
validator *accepts* and that differs from the corpus is a wrong write.

**Backends.** `--backend` selects an OpenAI-compatible server; both Ollama and vLLM
serve that shape, which is why D7's "same model name, two runtimes, different numerics"
is measurable at all. `--compare` runs the same corpus against two backends and reports
where they disagree, because D7 makes that divergence structural rather than
hypothetical. With no `--backend`, the configured `LLM_PROVIDER` selects it.

**Not copied into the deployment image.** `deploy/local/ai-server.Dockerfile` copies
`ai_server/` and nothing else; this script and `eval/` stay out of it deliberately, the
same rule `scripts/evaluate_ocr_accuracy.py` states for the OCR golden set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import httpx

from ai_server.llm.prompt import PROMPT_VERSION, render_turn_messages
from ai_server.llm.provider import LlmProviderError, selected_llm_provider
from ai_server.llm.tools import (
    TOOL_CALL_SCHEMA_NAME,
    TOOL_SURFACE,
    ToolSurfaceError,
    envelope_json_schema,
    parse_tool_call,
    tool_call_json_schema,
)
from ai_server.llm.validation import ValidationRefused, WriteContext, validate_write
from ai_server.scheduling.appointments import AnonymousAppointmentToken
from ai_server.scheduling.slots import AnonymousSlotToken

DEFAULT_CORPUS = Path("eval/acceptance-corpus.json")

# The understanding bar (D15). Deliberately a number that can be argued with, unlike the
# write bar, which is zero and is not a threshold at all. It is set where a ~7-8B
# instruct model (D11) is useful rather than where one is flawless: below this the chat
# asks the patient to reword often enough to be the wrong tool for the job.
UNDERSTANDING_THRESHOLD_PERCENT = 80.0

# The prompt now lives in `ai_server/llm/prompt.py`, because TICK-063 made it the
# runtime's prompt too and `deploy/local/ai-server.Dockerfile` copies `ai_server/` and
# nothing else. Re-exported under its harness names so a replay recorded before that
# move still names the same version, and so the prompt this harness scores is the same
# object production sends rather than a second string that agrees today.
ACCEPTANCE_PROMPT_VERSION = PROMPT_VERSION


class CorpusError(Exception):
    """Raised when the corpus file cannot be read or does not describe an evaluation."""


# --- The corpus --------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One patient phrasing and the structured output a correct reading produces."""

    identifier: str
    capability: str
    utterance: str
    expected_tool: str
    expected_write: Mapping[str, Any] | None
    in_ci_subset: bool
    why: str
    asked: str | None = None

    @property
    def expects_a_write(self) -> bool:
        return self.expected_write is not None


@dataclass(frozen=True)
class Corpus:
    """Every case, plus the conversation state the scheduling cases refer to."""

    now: datetime
    cases: tuple[Case, ...]
    offered_slots: tuple[AnonymousSlotToken, ...]
    offered_appointments: tuple[AnonymousAppointmentToken, ...]

    @property
    def context(self) -> WriteContext:
        """The `WriteContext` TICK-061's validators are given for every case.

        One context for the whole corpus rather than one per case: the tokens are what
        this patient's session was offered, and a case that must refuse an un-offered
        token can only be scored against a context that genuinely holds the others.
        """
        return WriteContext(
            now=self.now,
            offered_appointments=self.offered_appointments,
            offered_slots=self.offered_slots,
        )

    def subset(self, *, ci_only: bool = False, limit: int | None = None) -> tuple[Case, ...]:
        """Return the cases to run, narrowed by the CI subset flag and then by count."""
        cases = tuple(case for case in self.cases if case.in_ci_subset) if ci_only else self.cases
        return cases[:limit] if limit is not None else cases


def load_corpus(path: Path) -> Corpus:
    """Read and structurally check the corpus, refusing an unusable one loudly."""
    if not path.is_file():
        raise CorpusError(f"corpus file is missing: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CorpusError(f"corpus file is not valid JSON: {path}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("cases"), list):
        raise CorpusError(f"corpus file has no list of cases: {path}")
    if not document["cases"]:
        raise CorpusError(f"corpus is empty: {path}")

    cases = tuple(_case(entry, index) for index, entry in enumerate(document["cases"]))
    identifiers = [case.identifier for case in cases]
    duplicates = sorted({name for name in identifiers if identifiers.count(name) > 1})
    if duplicates:
        raise CorpusError(f"corpus has duplicate case ids: {', '.join(duplicates)}")

    return Corpus(
        now=_moment(document.get("now"), "now"),
        cases=cases,
        offered_slots=tuple(
            AnonymousSlotToken(
                slot_token=entry["slot_token"],
                starts_at=_moment(entry.get("starts_at"), "starts_at"),
                ends_at=_moment(entry.get("ends_at"), "ends_at"),
            )
            for entry in document.get("offered_slots", ())
        ),
        offered_appointments=tuple(
            AnonymousAppointmentToken(
                appointment_token=entry["appointment_token"],
                starts_at=_moment(entry.get("starts_at"), "starts_at"),
                ends_at=_moment(entry.get("ends_at"), "ends_at"),
            )
            for entry in document.get("offered_appointments", ())
        ),
    )


def _case(entry: object, index: int) -> Case:
    if not isinstance(entry, dict):
        raise CorpusError(f"case {index} is not an object")
    for key in ("id", "capability", "utterance", "expected_tool", "why"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise CorpusError(f"case {index} is missing {key!r}")
    if entry["expected_tool"] not in TOOL_SURFACE:
        raise CorpusError(f"case {entry['id']!r} expects an unpublished tool")
    expected_write = entry.get("expected_write")
    if expected_write is not None and not isinstance(expected_write, dict):
        raise CorpusError(f"case {entry['id']!r} has a non-object expected_write")
    if expected_write is not None and not TOOL_SURFACE[entry["expected_tool"]].writes:
        raise CorpusError(
            f"case {entry['id']!r} expects a write from {entry['expected_tool']!r}, "
            "which does not write"
        )
    return Case(
        identifier=entry["id"],
        capability=entry["capability"],
        utterance=entry["utterance"],
        expected_tool=entry["expected_tool"],
        expected_write=expected_write,
        in_ci_subset=bool(entry.get("ci", False)),
        why=entry["why"],
        asked=entry.get("asked"),
    )


def _moment(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise CorpusError(f"{label} must be an ISO timestamp")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise CorpusError(f"{label} is not an ISO timestamp: {value!r}") from exc


# --- Scoring -----------------------------------------------------------------------

# What reached the record, which is not the same question as what the model said.
WRITE_NONE = "none"  # no writing tool was proposed; nothing could reach the record
WRITE_REFUSED = "refused"  # a write was proposed and the validator refused it (NFR-36 ok)
WRITE_CORRECT = "correct"  # a write reached the record and every field is right
WRITE_WRONG = "wrong"  # a write reached the record and a field is wrong -- a blocker

_ABSENT = "(absent)"
_NOTHING = "(nothing should be written)"


@dataclass(frozen=True)
class WrongField:
    """One field that reached the record with the wrong value."""

    name: str
    expected: str
    produced: str


@dataclass(frozen=True)
class CaseResult:
    """How one case came out, on each bar separately."""

    case: Case
    raw_response: str
    produced_tool: str | None
    understood: bool
    write: str
    wrong_fields: tuple[WrongField, ...] = ()
    refusal: str | None = None

    @property
    def is_wrong_write(self) -> bool:
        return self.write == WRITE_WRONG


def score_case(case: Case, raw_response: str, *, context: WriteContext) -> CaseResult:
    """Score one model response on both bars.

    The response is put through exactly the path a real turn takes: `parse_tool_call`
    decides whether it is an admissible call at all, and `validate_write` decides what
    -- if anything -- would reach the record. Nothing here re-implements either.
    """
    try:
        call = parse_tool_call(raw_response)
    except ToolSurfaceError as exc:
        # No admissible call, so nothing can reach the record. The turn is a miss on
        # understanding and safe on writes; that asymmetry is D15 working as intended.
        return CaseResult(
            case=case,
            raw_response=raw_response,
            produced_tool=None,
            understood=False,
            write=WRITE_NONE,
            refusal=str(exc),
        )

    produced_tool = call.tool
    understood = produced_tool == case.expected_tool

    if not TOOL_SURFACE[produced_tool].writes:
        return CaseResult(
            case=case,
            raw_response=raw_response,
            produced_tool=produced_tool,
            understood=understood,
            write=WRITE_NONE,
        )

    try:
        validated = validate_write(produced_tool, call.arguments.model_dump(), context=context)
    except ValidationRefused as exc:
        # Refused, not wrong. The model proposed something the record would not take and
        # the door held -- which is the outcome NFR-36 asks for when a value cannot be
        # produced correctly.
        return CaseResult(
            case=case,
            raw_response=raw_response,
            produced_tool=produced_tool,
            understood=understood,
            write=WRITE_REFUSED,
            refusal="; ".join(exc.details) or str(exc),
        )

    expected = case.expected_write if produced_tool == case.expected_tool else None
    wrong_fields = compare_write(expected, validated.values)
    return CaseResult(
        case=case,
        raw_response=raw_response,
        produced_tool=produced_tool,
        understood=understood,
        write=WRITE_WRONG if wrong_fields else WRITE_CORRECT,
        wrong_fields=wrong_fields,
    )


def compare_write(
    expected: Mapping[str, Any] | None, produced: Mapping[str, Any]
) -> tuple[WrongField, ...]:
    """Return every field that reached the record wrongly.

    `expected` of `None` means nothing should have been written at all, so every
    produced field is wrong -- a value written into a record that should have stayed
    untouched is exactly as wrong as a mistyped one.

    An expected value may be a list, which admits any one of those values. That is how
    genuine surface ambiguity is allowed: stated per field, in the corpus, where a
    reviewer can see it, rather than by a tolerance in this function that would loosen
    the gate for every field at once.
    """
    if expected is None:
        return tuple(
            WrongField(name=name, expected=_NOTHING, produced=_render(value))
            for name, value in sorted(produced.items())
        )
    wrong: list[WrongField] = []
    for name in sorted(set(expected) | set(produced)):
        if name not in produced:
            wrong.append(WrongField(name=name, expected=_render(expected[name]), produced=_ABSENT))
        elif name not in expected:
            wrong.append(WrongField(name=name, expected=_NOTHING, produced=_render(produced[name])))
        elif not _matches(expected[name], produced[name]):
            wrong.append(
                WrongField(
                    name=name,
                    expected=_render(expected[name]),
                    produced=_render(produced[name]),
                )
            )
    return tuple(wrong)


def _matches(expected: Any, produced: Any) -> bool:
    """A list of expected values admits any one of them; anything else is exact."""
    if isinstance(expected, list):
        return any(candidate == produced for candidate in expected)
    return expected == produced


def _render(value: Any) -> str:
    if isinstance(value, list):
        return " | ".join(str(item) for item in value)
    return str(value)


# --- The report --------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    """The two bars, reported separately and never combined into one number."""

    backend: str
    model: str
    results: tuple[CaseResult, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def wrong_writes(self) -> tuple[CaseResult, ...]:
        return tuple(result for result in self.results if result.is_wrong_write)

    @property
    def understood(self) -> int:
        return sum(1 for result in self.results if result.understood)

    @property
    def understanding_percentage(self) -> float:
        if not self.results:
            raise ValueError("an understanding score requires at least one case")
        return (self.understood / self.total) * 100.0

    @property
    def writes_attempted(self) -> int:
        return sum(1 for result in self.results if result.write != WRITE_NONE)

    @property
    def writes_correct(self) -> int:
        return sum(1 for result in self.results if result.write == WRITE_CORRECT)

    @property
    def writes_refused(self) -> int:
        return sum(1 for result in self.results if result.write == WRITE_REFUSED)

    @property
    def meets_write_bar(self) -> bool:
        """NFR-36: zero wrong writes across the whole corpus. Not a percentage."""
        return not self.wrong_writes

    def meets_understanding_bar(self, threshold: float = UNDERSTANDING_THRESHOLD_PERCENT) -> bool:
        return self.understanding_percentage >= threshold

    def is_release_ready(self, threshold: float = UNDERSTANDING_THRESHOLD_PERCENT) -> bool:
        return self.meets_write_bar and self.meets_understanding_bar(threshold)


def render_report(report: Report, *, threshold: float = UNDERSTANDING_THRESHOLD_PERCENT) -> str:
    """Render both bars separately, and every wrong write in full.

    A wrong write is a release blocker under NFR-36, so it is printed individually with
    the phrasing that produced it, the value expected, and the value that reached the
    record -- enough to act on without re-running anything.
    """
    lines = [
        f"backend: {report.backend}  model: {report.model}  cases: {report.total}",
        f"prompt: {ACCEPTANCE_PROMPT_VERSION}",
        "",
        "-- write bar (NFR-36: zero wrong, across the whole corpus) --",
        f"wrong writes:     {len(report.wrong_writes)}",
        f"correct writes:   {report.writes_correct}",
        f"refused writes:   {report.writes_refused}  (proposed, and the validator held)",
    ]
    if report.meets_write_bar:
        lines.append("WRITE_BAR_MET: no model-produced value reached the record wrongly.")
    else:
        lines.append(
            "WRITE_BAR_FAILED: a model-produced value reached the record wrongly. Under "
            "NFR-36 this blocks release; it is not traded off against the understanding "
            "score below."
        )
        lines.append("")
        for result in report.wrong_writes:
            lines.extend(_render_wrong_write(result))

    lines += [
        "",
        f"-- understanding bar (D15: imperfect is acceptable, target {threshold:.0f}%) --",
        f"understood:       {report.understood}/{report.total} "
        f"({report.understanding_percentage:.1f}%)",
    ]
    lines.append(
        "UNDERSTANDING_BAR_MET: the model reads the corpus well enough to be useful."
        if report.meets_understanding_bar(threshold)
        else "UNDERSTANDING_BAR_BELOW_THRESHOLD: misread turns cost the patient a retry. "
        "This is a quality problem, not a record-integrity one."
    )
    lines += ["", *_render_misses(report)]
    return "\n".join(lines)


def _render_wrong_write(result: CaseResult) -> list[str]:
    lines = [
        f"  WRONG WRITE [{result.case.identifier}] via {result.produced_tool}",
        f"    input:    {result.case.utterance!r}",
    ]
    if result.case.asked:
        lines.append(f"    asked:    {result.case.asked!r}")
    for wrong in result.wrong_fields:
        lines.append(f"    field:    {wrong.name}")
        lines.append(f"      expected: {wrong.expected!r}")
        lines.append(f"      produced: {wrong.produced!r}")
    lines.append(f"    why it is in the corpus: {result.case.why}")
    lines.append("")
    return lines


def _render_misses(report: Report) -> list[str]:
    misses = [result for result in report.results if not result.understood]
    if not misses:
        return ["every case was routed to the expected tool."]
    lines = ["misread turns (softer bar -- a retry, not a record):"]
    for result in misses:
        produced = result.produced_tool or f"no admissible call ({result.refusal})"
        lines.append(
            f"  [{result.case.identifier}] expected {result.case.expected_tool}, got {produced}"
        )
    return lines


# --- Backend divergence (D7) -------------------------------------------------------


@dataclass(frozen=True)
class Divergence:
    """One case the two backends did not agree on."""

    case_id: str
    utterance: str
    left_tool: str | None
    right_tool: str | None
    left_write: str
    right_write: str


@dataclass(frozen=True)
class ComparisonReport:
    """Two backends over the same corpus, and every case where they differ."""

    left: Report
    right: Report
    divergences: tuple[Divergence, ...]

    @property
    def agreement_percentage(self) -> float:
        if not self.left.results:
            raise ValueError("a comparison requires at least one case")
        agreed = self.left.total - len(self.divergences)
        return (agreed / self.left.total) * 100.0


def compare_backends(left: Report, right: Report) -> ComparisonReport:
    """Report where two runtimes disagree on the same corpus.

    D7 puts Ollama locally and vLLM in the deployment, serving the same model under
    different numerics. That divergence is assumed to exist and measured rather than
    argued about; a case is a divergence when the two runtimes routed it differently or
    when what reached the record differed.
    """
    right_by_id = {result.case.identifier: result for result in right.results}
    divergences = tuple(
        Divergence(
            case_id=result.case.identifier,
            utterance=result.case.utterance,
            left_tool=result.produced_tool,
            right_tool=right_by_id[result.case.identifier].produced_tool,
            left_write=result.write,
            right_write=right_by_id[result.case.identifier].write,
        )
        for result in left.results
        if result.case.identifier in right_by_id
        and (
            result.produced_tool != right_by_id[result.case.identifier].produced_tool
            or result.write != right_by_id[result.case.identifier].write
        )
    )
    return ComparisonReport(left=left, right=right, divergences=divergences)


def render_comparison(comparison: ComparisonReport) -> str:
    """Render the two runs and every case where the runtimes parted company."""
    lines = [
        render_report(comparison.left),
        "",
        "=" * 78,
        "",
        render_report(comparison.right),
        "",
        "=" * 78,
        "",
        f"-- backend divergence (D7): {comparison.left.backend} vs {comparison.right.backend} --",
        f"agreement: {comparison.left.total - len(comparison.divergences)}"
        f"/{comparison.left.total} ({comparison.agreement_percentage:.1f}%)",
    ]
    if not comparison.divergences:
        lines.append("BACKENDS_AGREE: the two runtimes produced the same routing and the")
        lines.append("same record outcome on every case.")
        return "\n".join(lines)
    lines.append(
        "BACKENDS_DIVERGE: the same model under two runtimes behaved differently. This "
        "is expected (GGUF Q4 versus fp16/AWQ) and is reported rather than averaged away."
    )
    for divergence in comparison.divergences:
        lines += [
            f"  [{divergence.case_id}] {divergence.utterance!r}",
            f"    {comparison.left.backend:<10} tool={divergence.left_tool} "
            f"write={divergence.left_write}",
            f"    {comparison.right.backend:<10} tool={divergence.right_tool} "
            f"write={divergence.right_write}",
        ]
    return "\n".join(lines)


# --- Backends ----------------------------------------------------------------------


def render_messages(corpus: Corpus, case: Case) -> list[dict[str, str]]:
    """Build the system and user messages for one case.

    Delegates to the runtime's own renderer (`ai_server.llm.prompt`), which TICK-063
    made the single owner of both the prompt and its per-turn context. A corpus case has
    no transcript, no pending confirmation and no upload, so what comes back is exactly
    the system+user pair this harness has always sent -- and now it is provably the same
    request production makes, which is the whole point of measuring it (D8).
    """
    return render_turn_messages(
        message=case.utterance,
        now=corpus.now,
        offered_slots=corpus.offered_slots,
        offered_appointments=corpus.offered_appointments,
        asked=case.asked,
    )


# How much of the turn contract to hand the runtime as a grammar. `envelope` is the
# default because it is what Ollama compiles; `tool_call` is the full TICK-060 schema,
# for a runtime that supports it (vLLM's outlines backend does); `json_object` asks only
# for valid JSON, and `text` constrains nothing, which is what a runtime with no
# structured-output support leaves you.
RESPONSE_FORMATS = ("envelope", "tool_call", "json_object", "text")


@dataclass(frozen=True)
class BackendSettings:
    """Where an OpenAI-compatible server is, and which model to ask it for."""

    name: str
    base_url: str
    model: str
    api_key: str | None = None
    timeout_seconds: float = 600.0
    response_format: str = "envelope"

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1/chat/completions"

    def response_format_body(self) -> dict[str, Any] | None:
        """Return the `response_format` this backend should be sent, if any."""
        if self.response_format == "text":
            return None
        if self.response_format == "json_object":
            return {"type": "json_object"}
        schema = (
            tool_call_json_schema()
            if self.response_format == "tool_call"
            else envelope_json_schema()
        )
        return {
            "type": "json_schema",
            "json_schema": {"name": TOOL_CALL_SCHEMA_NAME, "schema": schema},
        }


def run_case(client: httpx.Client, settings: BackendSettings, messages: Sequence[Mapping]) -> str:
    """Ask the backend for one turn and return its raw text, unparsed.

    `temperature` is zero and `seed` is fixed so a re-run of the same corpus against the
    same weights is comparable; neither makes a quantised model deterministic across
    runtimes, which is the whole reason `--compare` exists.
    """
    headers = {"Content-Type": "application/json"}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    body: dict[str, Any] = {
        "model": settings.model,
        "messages": list(messages),
        "stream": False,
        "temperature": 0,
        "seed": 0,
    }
    response_format = settings.response_format_body()
    if response_format is not None:
        body["response_format"] = response_format
    response = client.post(
        settings.endpoint, headers=headers, json=body, timeout=settings.timeout_seconds
    )
    response.raise_for_status()
    payload = response.json()
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise CorpusError(f"{settings.name} returned an unreadable response") from exc


def run_corpus(
    corpus: Corpus, cases: Iterable[Case], settings: BackendSettings
) -> tuple[Report, dict[str, str]]:
    """Run `cases` against a live backend, returning the report and the raw responses.

    The raw responses come back so a run can be recorded and replayed (`--record`); a
    replay is what makes the CI subset deterministic without a model server.
    """
    context = corpus.context
    results: list[CaseResult] = []
    recorded: dict[str, str] = {}
    with httpx.Client() as client:
        for case in cases:
            raw = run_case(client, settings, render_messages(corpus, case))
            recorded[case.identifier] = raw
            results.append(score_case(case, raw, context=context))
    return (
        Report(backend=settings.name, model=settings.model, results=tuple(results)),
        recorded,
    )


def replay_corpus(corpus: Corpus, cases: Iterable[Case], replay: Mapping[str, Any]) -> Report:
    """Score a recorded run without contacting any model server."""
    responses = replay["responses"]
    context = corpus.context
    results = []
    for case in cases:
        if case.identifier not in responses:
            raise CorpusError(
                f"replay has no recorded response for {case.identifier!r}; re-record it"
            )
        results.append(score_case(case, responses[case.identifier], context=context))
    return Report(
        backend=f"{replay.get('backend', 'replay')} (replay)",
        model=replay.get("model", "unknown"),
        results=tuple(results),
    )


def load_replay(path: Path) -> dict[str, Any]:
    """Load a recorded run, refusing one that predates the current prompt.

    This is what makes AC7 bite. A replay cannot detect that a *prompt change* altered
    the model's behaviour -- the responses in it are frozen. What it can do is refuse to
    be used at all once the prompt has moved, which forces a fresh run against a real
    backend before CI is green again. So a prompt edit cannot regress the write bar
    unnoticed: it cannot reach a green build without someone re-measuring it.
    """
    if not path.is_file():
        raise CorpusError(f"replay file is missing: {path}")
    try:
        replay = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CorpusError(f"replay file is not valid JSON: {path}") from exc
    if not isinstance(replay, dict) or not isinstance(replay.get("responses"), dict):
        raise CorpusError(f"replay file has no recorded responses: {path}")
    recorded_prompt = replay.get("prompt_version")
    if recorded_prompt != ACCEPTANCE_PROMPT_VERSION:
        raise CorpusError(
            f"replay was recorded under prompt {recorded_prompt!r} but the harness now "
            f"uses {ACCEPTANCE_PROMPT_VERSION!r}. Re-run the corpus against a real "
            "backend and re-record it; a prompt change may not ride on a stale run."
        )
    return replay


def _settings(
    name: str | None,
    base_url: str | None,
    model: str | None,
    timeout: float,
    response_format: str,
) -> BackendSettings:
    """Resolve a backend from the flags, falling back to `LLM_PROVIDER` and its env."""
    resolved = name or selected_llm_provider()
    resolved_model = model or os.environ.get("LLM_MODEL")
    if not resolved_model:
        raise CorpusError(f"no model for backend {resolved!r}: pass --model or set LLM_MODEL")
    resolved_url = base_url or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
    return BackendSettings(
        name=resolved,
        base_url=resolved_url,
        model=resolved_model,
        api_key=os.environ.get("LLM_API_KEY") or None,
        timeout_seconds=timeout,
        response_format=response_format,
    )


def main() -> int:
    """Run the corpus and return a process exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--ci-subset",
        action="store_true",
        help="run only the small deterministic subset marked `ci` in the corpus",
    )
    parser.add_argument("--limit", type=int, default=None, help="run at most this many cases")
    parser.add_argument(
        "--threshold",
        type=float,
        default=UNDERSTANDING_THRESHOLD_PERCENT,
        help="the understanding bar; the write bar is zero and cannot be set",
    )
    parser.add_argument("--backend", default=None, help="ollama | vllm (default: LLM_PROVIDER)")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--response-format",
        choices=RESPONSE_FORMATS,
        default="envelope",
        help="how much of the turn contract to constrain generation with",
    )
    parser.add_argument("--record", type=Path, default=None, help="write the raw responses here")
    parser.add_argument(
        "--replay", type=Path, default=None, help="score a recorded run; contacts no server"
    )
    parser.add_argument("--compare", action="store_true", help="run a second backend too (D7)")
    parser.add_argument("--other-backend", default=None)
    parser.add_argument("--other-base-url", default=None)
    parser.add_argument("--other-model", default=None)
    parser.add_argument(
        "--other-replay", type=Path, default=None, help="compare against a recorded run"
    )
    arguments = parser.parse_args()

    try:
        corpus = load_corpus(arguments.corpus)
        cases = corpus.subset(ci_only=arguments.ci_subset, limit=arguments.limit)
        if not cases:
            raise CorpusError("no cases selected")

        if arguments.replay:
            report = replay_corpus(corpus, cases, load_replay(arguments.replay))
        else:
            settings = _settings(
                arguments.backend,
                arguments.base_url,
                arguments.model,
                arguments.timeout,
                arguments.response_format,
            )
            report, recorded = run_corpus(corpus, cases, settings)
            if arguments.record:
                arguments.record.parent.mkdir(parents=True, exist_ok=True)
                arguments.record.write_text(
                    json.dumps(
                        {
                            "prompt_version": ACCEPTANCE_PROMPT_VERSION,
                            "backend": settings.name,
                            "model": settings.model,
                            "responses": recorded,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )

        if not arguments.compare:
            print(render_report(report, threshold=arguments.threshold))
            return 0 if report.is_release_ready(arguments.threshold) else 1

        if arguments.other_replay:
            other = replay_corpus(corpus, cases, load_replay(arguments.other_replay))
        else:
            other_settings = _settings(
                arguments.other_backend,
                arguments.other_base_url,
                arguments.other_model,
                arguments.timeout,
                arguments.response_format,
            )
            other, _ = run_corpus(corpus, cases, other_settings)
        comparison = compare_backends(report, other)
        print(render_comparison(comparison))
        return (
            0
            if report.is_release_ready(arguments.threshold)
            and other.is_release_ready(arguments.threshold)
            else 1
        )
    except (CorpusError, LlmProviderError) as error:
        print(f"ACCEPTANCE_CORPUS_FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
