"""The acceptance corpus and the harness that scores it (TICK-062, NFR-36, D8/D15).

The harness decides whether a model may be on a path that reaches a medical record, so
it is tested the way that claim deserves: a known-good run passes, a seeded wrong write
is caught and reported as a blocker, and a seeded misunderstanding is reported under the
softer bar without ever being mistaken for the first.

The corpus itself is tested too. An expectation the real validator would refuse is a
broken expectation, so every `expected_write` in the shipped corpus is round-tripped
through `ai_server.llm.validation.validate_write` here -- otherwise the corpus could
quietly hold a target no model could ever hit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_server.llm.validation import validate_write
from scripts.evaluate_acceptance_corpus import (
    ACCEPTANCE_PROMPT_VERSION,
    DEFAULT_CORPUS,
    UNDERSTANDING_THRESHOLD_PERCENT,
    WRITE_CORRECT,
    WRITE_NONE,
    WRITE_REFUSED,
    WRITE_WRONG,
    BackendSettings,
    Case,
    CorpusError,
    Report,
    compare_backends,
    compare_write,
    envelope_json_schema,
    load_corpus,
    load_replay,
    render_comparison,
    render_messages,
    render_report,
    replay_corpus,
    score_case,
)

CORPUS = load_corpus(DEFAULT_CORPUS)
CONTEXT = CORPUS.context


def case(identifier: str) -> Case:
    for entry in CORPUS.cases:
        if entry.identifier == identifier:
            return entry
    raise AssertionError(f"no such corpus case: {identifier}")


def call(tool: str, **arguments: object) -> str:
    return json.dumps({"tool": tool, "arguments": arguments})


ADDRESS_CASE = "address-lead-in-colon"
GOOD_ADDRESS = call(
    "update_address",
    street1="2002 Bridge Avenue",
    city="Brick",
    state="NJ",
    zip_code="08723",
)


# --- The corpus is a usable target -------------------------------------------------


def test_every_expected_write_survives_the_real_validator() -> None:
    """An expectation TICK-061's validator would refuse is a target no model can hit."""
    for entry in CORPUS.cases:
        if not entry.expects_a_write:
            continue
        proposed = {
            name: value[0] if isinstance(value, list) else value
            for name, value in entry.expected_write.items()
        }
        validated = validate_write(entry.expected_tool, proposed, context=CONTEXT)

        assert compare_write(entry.expected_write, validated.values) == (), (
            f"{entry.identifier}: expected_write does not match what validate_write "
            f"returns for it ({dict(validated.values)})"
        )


def test_the_corpus_holds_the_phrasings_that_broke_the_parsers() -> None:
    """AC1 names six shapes by hand; none of them may quietly leave the corpus."""
    identifiers = {entry.identifier for entry in CORPUS.cases}

    assert "address-lead-in-colon" in identifiers  # a lead-in phrase (TICK-050)
    assert "address-answer-to-a-different-question" in identifiers
    assert "address-correction-mid-sentence" in identifiers
    assert "address-partial-no-city-state-zip" in identifiers
    assert "address-refusal" in identifiers
    assert "address-question-instead-of-answer" in identifiers


def test_the_tick_050_phrasing_is_present_verbatim() -> None:
    """The literal string that reached a chart, so a reworded corpus cannot lose it."""
    assert case(ADDRESS_CASE).utterance == "Update it to: 2002 Bridge Avenue, Brick, NJ 08723"
    assert case(ADDRESS_CASE).expected_write["street1"] == "2002 Bridge Avenue"


def test_every_capability_on_the_tool_surface_is_exercised() -> None:
    from ai_server.llm.tools import TOOL_NAMES

    expected_tools = {entry.expected_tool for entry in CORPUS.cases}

    assert set(TOOL_NAMES) - expected_tools == set()


def test_the_ci_subset_is_a_strict_subset_and_is_not_empty() -> None:
    subset = CORPUS.subset(ci_only=True)

    assert subset
    assert len(subset) < len(CORPUS.cases)


def test_the_corpus_carries_no_real_patient_information() -> None:
    """NFR-1. Contact details are the ones that would matter; they are reserved forms."""
    document = DEFAULT_CORPUS.read_text(encoding="utf-8")

    assert "synthetic" in json.loads(document)
    for line in document.splitlines():
        if "@" in line and "example.com" not in line:
            raise AssertionError(f"non-reserved email domain in the corpus: {line.strip()}")


def test_load_corpus_refuses_a_case_expecting_a_write_from_a_read_only_tool(
    tmp_path: Path,
) -> None:
    path = tmp_path / "corpus.json"
    path.write_text(
        json.dumps(
            {
                "now": "2026-09-14T09:00:00",
                "cases": [
                    {
                        "id": "bad",
                        "capability": "reply",
                        "utterance": "hi",
                        "expected_tool": "reply",
                        "expected_write": {"street1": "1 Main St"},
                        "why": "invalid",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CorpusError, match="does not write"):
        load_corpus(path)


def test_load_corpus_refuses_duplicate_case_ids(tmp_path: Path) -> None:
    path = tmp_path / "corpus.json"
    entry = {
        "id": "same",
        "capability": "reply",
        "utterance": "hi",
        "expected_tool": "reply",
        "why": "x",
    }
    path.write_text(
        json.dumps({"now": "2026-09-14T09:00:00", "cases": [entry, dict(entry)]}), encoding="utf-8"
    )

    with pytest.raises(CorpusError, match="duplicate case ids"):
        load_corpus(path)


# --- A known-good fixture passes ---------------------------------------------------


def test_a_known_good_response_is_a_correct_write_on_both_bars() -> None:
    result = score_case(case(ADDRESS_CASE), GOOD_ADDRESS, context=CONTEXT)

    assert result.understood
    assert result.write == WRITE_CORRECT
    assert result.wrong_fields == ()
    assert not result.is_wrong_write


def test_a_known_good_run_meets_both_bars_and_exits_clean() -> None:
    good = {
        "address-lead-in-colon": GOOD_ADDRESS,
        "appointments-next": call("list_appointments"),
        "reply-greeting": call("reply", message="Hello, how can I help?"),
    }
    cases = [case(identifier) for identifier in good]

    report = Report(
        backend="fixture",
        model="fixture",
        results=tuple(
            score_case(entry, good[entry.identifier], context=CONTEXT) for entry in cases
        ),
    )

    assert report.meets_write_bar
    assert report.understanding_percentage == 100.0
    assert report.is_release_ready()
    assert "WRITE_BAR_MET" in render_report(report)


def test_an_expected_refusal_case_passes_when_nothing_is_written() -> None:
    """A partial address answered with `reply` is the correct outcome, not a failure."""
    result = score_case(
        case("address-partial-no-city-state-zip"),
        call("reply", message="What city, state and ZIP should I use?"),
        context=CONTEXT,
    )

    assert result.understood
    assert result.write == WRITE_NONE
    assert not result.is_wrong_write


# --- A seeded wrong write is caught and reported as a blocker ----------------------


def test_a_seeded_wrong_write_is_a_blocker_and_names_the_field() -> None:
    """The TICK-050 failure itself: the lead-in phrase reaching street1.

    Seeded as a value the validator *accepts* -- `validate_street` would refuse the
    literal `"Update it to: 2002 Bridge Avenue"`, and a refusal is not a wrong write.
    A plausible-but-wrong street is the case that must be caught, because it is the one
    the door cannot stop.
    """
    seeded = call(
        "update_address",
        street1="2002 Bridge Road",
        city="Brick",
        state="NJ",
        zip_code="08723",
    )

    result = score_case(case(ADDRESS_CASE), seeded, context=CONTEXT)

    assert result.is_wrong_write
    assert result.write == WRITE_WRONG
    assert [(w.name, w.expected, w.produced) for w in result.wrong_fields] == [
        ("street1", "2002 Bridge Avenue", "2002 Bridge Road")
    ]


def test_a_seeded_wrong_write_fails_the_write_bar_regardless_of_understanding() -> None:
    seeded = call(
        "update_address", street1="2002 Bridge Road", city="Brick", state="NJ", zip_code="08723"
    )
    report = Report(
        backend="fixture",
        model="fixture",
        results=(score_case(case(ADDRESS_CASE), seeded, context=CONTEXT),),
    )

    # Understanding is perfect -- the tool was right. The write bar still fails, and
    # nothing about the first is allowed to soften the second (D15).
    assert report.understanding_percentage == 100.0
    assert report.meets_understanding_bar()
    assert not report.meets_write_bar
    assert not report.is_release_ready()


def test_the_wrong_write_report_carries_input_expected_and_produced() -> None:
    seeded = call(
        "update_address", street1="2002 Bridge Road", city="Brick", state="NJ", zip_code="08723"
    )
    report = Report(
        backend="fixture",
        model="fixture",
        results=(score_case(case(ADDRESS_CASE), seeded, context=CONTEXT),),
    )

    text = render_report(report)

    assert "WRITE_BAR_FAILED" in text
    assert "blocks release" in text
    assert "Update it to: 2002 Bridge Avenue, Brick, NJ 08723" in text  # the input
    assert "'2002 Bridge Avenue'" in text  # expected
    assert "'2002 Bridge Road'" in text  # produced
    assert "street1" in text


def test_writing_anything_when_nothing_should_be_written_is_a_wrong_write() -> None:
    """A city the patient never gave is exactly as wrong as a mistyped one."""
    invented = call(
        "update_address", street1="12 Oak Street", city="Brick", state="NJ", zip_code="08723"
    )

    result = score_case(case("address-partial-no-city-state-zip"), invented, context=CONTEXT)

    assert result.is_wrong_write
    assert {wrong.name for wrong in result.wrong_fields} == {
        "street1",
        "city",
        "state",
        "zip_code",
    }
    assert all("nothing should be written" in wrong.expected for wrong in result.wrong_fields)


def test_a_refused_proposal_is_not_counted_as_a_wrong_write() -> None:
    """NFR-36 asks for right-or-refused. The refusal half must not read as a failure."""
    refused = call(
        "update_address",
        street1="Update it to: 2002 Bridge Avenue",
        city="Brick",
        state="NJ",
        zip_code="08723",
    )

    result = score_case(case(ADDRESS_CASE), refused, context=CONTEXT)

    assert result.write == WRITE_REFUSED
    assert not result.is_wrong_write
    assert result.refusal


def test_an_invented_slot_token_is_refused_rather_than_booked() -> None:
    """TICK-059 recorded the model fabricating a token; the validator must hold."""
    invented = call("book_appointment", slot_token="slot_thisWasNeverOffered")

    result = score_case(case("book-offered-slot"), invented, context=CONTEXT)

    assert result.write == WRITE_REFUSED
    assert not result.is_wrong_write


def test_booking_a_slot_that_was_offered_but_is_the_wrong_one_is_a_wrong_write() -> None:
    wrong_slot = call("book_appointment", slot_token="slot_Nb9Rt3ZeWc")

    result = score_case(case("book-offered-slot"), wrong_slot, context=CONTEXT)

    assert result.is_wrong_write
    assert result.wrong_fields[0].name == "slot_token"


# --- A seeded misunderstanding sits under the softer bar --------------------------


def test_a_seeded_misunderstanding_is_a_miss_that_writes_nothing() -> None:
    misread = call("list_appointments")

    result = score_case(case(ADDRESS_CASE), misread, context=CONTEXT)

    assert not result.understood
    assert result.write == WRITE_NONE
    assert not result.is_wrong_write


def test_a_misunderstanding_lowers_understanding_and_leaves_the_write_bar_met() -> None:
    results = (
        score_case(case(ADDRESS_CASE), call("list_appointments"), context=CONTEXT),
        score_case(case("appointments-next"), call("list_appointments"), context=CONTEXT),
    )
    report = Report(backend="fixture", model="fixture", results=results)

    assert report.understanding_percentage == 50.0
    assert not report.meets_understanding_bar()
    assert report.meets_write_bar  # nothing reached the record, so nothing is wrong

    text = render_report(report)
    assert "UNDERSTANDING_BAR_BELOW_THRESHOLD" in text
    assert "a retry, not a record" in text
    assert "WRITE_BAR_MET" in text


def test_an_unparseable_response_is_a_miss_and_never_a_wrong_write() -> None:
    result = score_case(case(ADDRESS_CASE), "I'm not sure what you mean!", context=CONTEXT)

    assert not result.understood
    assert result.produced_tool is None
    assert result.write == WRITE_NONE
    assert result.refusal


def test_the_two_bars_are_reported_separately_and_never_blended() -> None:
    """AC3: a single score would price a corrupted record like a misread question."""
    results = (
        score_case(
            case(ADDRESS_CASE),
            call(
                "update_address",
                street1="2002 Bridge Road",
                city="Brick",
                state="NJ",
                zip_code="08723",
            ),
            context=CONTEXT,
        ),
        score_case(case("appointments-next"), call("list_appointments"), context=CONTEXT),
    )
    text = render_report(Report(backend="fixture", model="fixture", results=results))

    assert "-- write bar (NFR-36: zero wrong, across the whole corpus) --" in text
    assert "-- understanding bar" in text
    # Both cases were routed to the expected tool, so understanding is perfect and one
    # of them still corrupted a record. A blended score would hide exactly this.
    assert "wrong writes:     1" in text
    assert "understood:       2/2 (100.0%)" in text
    assert "WRITE_BAR_FAILED" in text
    assert "UNDERSTANDING_BAR_MET" in text


def test_the_write_bar_is_zero_not_a_threshold() -> None:
    """One wrong write in a hundred cases still fails. No amount of it passes.

    Every case here is routed correctly, so understanding is 100% -- and the single
    corrupted record still blocks release. That is the write bar being a count rather
    than a percentage, which is the whole of NFR-36.
    """
    good = tuple(
        score_case(case("appointments-next"), call("list_appointments"), context=CONTEXT)
        for _ in range(99)
    )
    seeded = score_case(
        case(ADDRESS_CASE),
        call("update_address", street1="9 Wrong Way", city="Brick", state="NJ", zip_code="08723"),
        context=CONTEXT,
    )
    report = Report(backend="fixture", model="fixture", results=good + (seeded,))

    assert report.understanding_percentage == 100.0
    assert report.meets_understanding_bar(UNDERSTANDING_THRESHOLD_PERCENT)
    assert len(report.wrong_writes) == 1
    assert not report.meets_write_bar
    assert not report.is_release_ready()


# --- Any-of expectations -----------------------------------------------------------


def test_a_list_expectation_admits_any_of_its_values_and_nothing_else() -> None:
    expected = {"street1": ["100 Maple Ave", "100 Maple Avenue"], "city": "Brick"}

    assert compare_write(expected, {"street1": "100 Maple Avenue", "city": "Brick"}) == ()
    assert compare_write(expected, {"street1": "100 Maple Ave", "city": "Brick"}) == ()
    wrong = compare_write(expected, {"street1": "100 Maple Road", "city": "Brick"})
    assert len(wrong) == 1
    assert wrong[0].expected == "100 Maple Ave | 100 Maple Avenue"


def test_a_missing_field_is_reported_as_absent() -> None:
    wrong = compare_write({"city": "Brick"}, {})

    assert wrong[0].produced == "(absent)"


# --- Replay, and the prompt-version lock ------------------------------------------


def _replay_file(tmp_path: Path, *, prompt_version: str, responses: dict) -> Path:
    path = tmp_path / "replay.json"
    path.write_text(
        json.dumps(
            {
                "prompt_version": prompt_version,
                "backend": "ollama",
                "model": "fixture",
                "responses": responses,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_a_replay_scores_without_contacting_a_model_server(tmp_path: Path) -> None:
    path = _replay_file(
        tmp_path,
        prompt_version=ACCEPTANCE_PROMPT_VERSION,
        responses={ADDRESS_CASE: GOOD_ADDRESS},
    )

    report = replay_corpus(CORPUS, [case(ADDRESS_CASE)], load_replay(path))

    assert report.meets_write_bar
    assert report.understanding_percentage == 100.0


def test_a_replay_recorded_under_an_older_prompt_is_refused(tmp_path: Path) -> None:
    """AC7's teeth. A prompt edit cannot reach a green build on a stale measurement."""
    path = _replay_file(
        tmp_path, prompt_version="acceptance-tool-call-v0", responses={ADDRESS_CASE: GOOD_ADDRESS}
    )

    with pytest.raises(CorpusError, match="may not ride on a stale run"):
        load_replay(path)


def test_a_replay_missing_a_case_is_refused_rather_than_scored_as_a_pass(
    tmp_path: Path,
) -> None:
    path = _replay_file(tmp_path, prompt_version=ACCEPTANCE_PROMPT_VERSION, responses={})

    with pytest.raises(CorpusError, match="no recorded response"):
        replay_corpus(CORPUS, [case(ADDRESS_CASE)], load_replay(path))


# --- Backend divergence (D7) -------------------------------------------------------


def test_two_backends_that_agree_report_no_divergence() -> None:
    results = (score_case(case(ADDRESS_CASE), GOOD_ADDRESS, context=CONTEXT),)
    left = Report(backend="ollama", model="m", results=results)
    right = Report(backend="vllm", model="m", results=results)

    comparison = compare_backends(left, right)

    assert comparison.divergences == ()
    assert comparison.agreement_percentage == 100.0
    assert "BACKENDS_AGREE" in render_comparison(comparison)


def test_a_backend_divergence_is_reported_per_case_not_averaged_away() -> None:
    left = Report(
        backend="ollama",
        model="m",
        results=(score_case(case(ADDRESS_CASE), GOOD_ADDRESS, context=CONTEXT),),
    )
    right = Report(
        backend="vllm",
        model="m",
        results=(
            score_case(
                case(ADDRESS_CASE),
                call(
                    "update_address",
                    street1="2002 Bridge Road",
                    city="Brick",
                    state="NJ",
                    zip_code="08723",
                ),
                context=CONTEXT,
            ),
        ),
    )

    comparison = compare_backends(left, right)
    text = render_comparison(comparison)

    assert len(comparison.divergences) == 1
    assert comparison.divergences[0].case_id == ADDRESS_CASE
    assert comparison.divergences[0].left_write == WRITE_CORRECT
    assert comparison.divergences[0].right_write == WRITE_WRONG
    assert "BACKENDS_DIVERGE" in text
    assert "reported rather than averaged away" in text


# --- The prompt ---------------------------------------------------------------------


def test_the_prompt_offers_the_tokens_a_booking_case_needs() -> None:
    messages = render_messages(CORPUS, case("book-offered-slot"))
    system = messages[0]["content"]

    assert "slot_7Kq2mVx4Ld" in system
    assert "appt_Qm4Xy7BdRn" in system
    assert messages[1]["content"] == case("book-offered-slot").utterance


def test_the_prompt_states_the_asked_question_when_a_case_has_one() -> None:
    system = render_messages(CORPUS, case("address-refusal"))[0]["content"]

    assert "What is your new mailing address?" in system


def test_the_prompt_tells_the_model_to_refuse_rather_than_guess() -> None:
    """The prompt half of NFR-36. Without it the corpus measures a different system."""
    system = render_messages(CORPUS, case(ADDRESS_CASE))[0]["content"]

    assert "Never invent a value" in system
    assert "choose the refusal" in system


# --- Constraining generation -------------------------------------------------------


def test_the_envelope_grammar_admits_only_published_tool_names() -> None:
    from ai_server.llm.tools import TOOL_NAMES

    schema = envelope_json_schema()

    assert schema["properties"]["tool"]["enum"] == list(TOOL_NAMES)
    assert schema["required"] == ["tool", "arguments"]


@pytest.mark.parametrize(
    ("response_format", "expected_type"),
    [
        ("envelope", "json_schema"),
        ("tool_call", "json_schema"),
        ("json_object", "json_object"),
    ],
)
def test_each_response_format_asks_the_runtime_for_the_right_thing(
    response_format: str, expected_type: str
) -> None:
    settings = BackendSettings(
        name="ollama", base_url="http://localhost:11434", model="m", response_format=response_format
    )

    assert settings.response_format_body()["type"] == expected_type


def test_text_response_format_constrains_nothing() -> None:
    """A runtime with no structured-output support still has to be measurable."""
    settings = BackendSettings(
        name="vllm", base_url="http://localhost:8000", model="m", response_format="text"
    )

    assert settings.response_format_body() is None


def test_an_unconstrained_runtime_is_still_protected_by_parse_tool_call() -> None:
    """The envelope grammar is a convenience; TICK-060's parser is the guarantee.

    Ollama cannot compile the full tool-call schema, so generation is only partly
    constrained there. Anything the grammar lets through still has to survive
    `parse_tool_call`, which is what actually stands between the model and the record.
    """
    result = score_case(case(ADDRESS_CASE), '{"tool": "update_address"}', context=CONTEXT)

    assert result.produced_tool is None
    assert result.write == WRITE_NONE
    assert not result.is_wrong_write
