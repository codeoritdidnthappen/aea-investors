"""Golden-set accuracy metric and release-threshold tests (TICK-015, NFR-29)."""

from __future__ import annotations

import asyncio
import shutil
import struct
import zlib
from pathlib import Path

import pytest

from ai_server.fixtures.generator import generate
from ai_server.ocr.service import (
    ExtractedIdentity,
    SubprocessTesseractEngine,
    TesseractUnavailableError,
)
from scripts.evaluate_ocr_accuracy import (
    RELEASE_THRESHOLD_PERCENT,
    FieldAccuracy,
    evaluate,
    expected_identity_from_label,
    load_golden_set,
    render_report,
    run_golden_set,
)


def _identity(index: int) -> ExtractedIdentity:
    return ExtractedIdentity(
        name=f"Person {index}",
        date_of_birth=f"1990-01-{index + 1:02d}",
        address=f"{index} Main St",
    )


def _golden_set(size: int = 10) -> dict[str, ExtractedIdentity]:
    return {f"SYN-{index:02d}": _identity(index) for index in range(size)}


class FakeEngine:
    def __init__(
        self, texts: dict[str, str] | None = None, fail_for: set[str] | None = None
    ) -> None:
        self._texts = texts or {}
        self._fail_for = fail_for or set()

    async def recognize_text(self, image: bytes) -> str:
        key = image.decode("utf-8")
        if key in self._fail_for:
            raise TesseractUnavailableError("engine unavailable")
        return self._texts.get(key, "")


def test_field_accuracy_percentage_and_threshold() -> None:
    accuracy = FieldAccuracy(field="name", matched=9, total=10)

    assert accuracy.percentage == 90.0
    assert accuracy.meets_threshold()


def test_field_accuracy_percentage_requires_at_least_one_sample() -> None:
    with pytest.raises(ValueError, match="at least one"):
        FieldAccuracy(field="name", matched=0, total=0).percentage


def test_evaluate_rejects_an_empty_golden_set() -> None:
    with pytest.raises(ValueError, match="golden set is empty"):
        evaluate({}, {})


def test_evaluate_scores_a_passing_sample_set_at_the_90_percent_boundary() -> None:
    expected = _golden_set(10)
    actual = dict(expected)
    # Exactly one mismatch per field (9/10 = 90.0%), each on a different identifier.
    actual["SYN-00"] = ExtractedIdentity(
        name="wrong",
        date_of_birth=expected["SYN-00"].date_of_birth,
        address=expected["SYN-00"].address,
    )
    actual["SYN-01"] = ExtractedIdentity(
        name=expected["SYN-01"].name, date_of_birth="wrong", address=expected["SYN-01"].address
    )
    actual["SYN-02"] = ExtractedIdentity(
        name=expected["SYN-02"].name,
        date_of_birth=expected["SYN-02"].date_of_birth,
        address="wrong",
    )

    report = evaluate(expected, actual)

    for field in ("name", "date_of_birth", "address"):
        assert report.field_accuracy(field).percentage == RELEASE_THRESHOLD_PERCENT
    assert report.is_release_ready


def test_evaluate_blocks_a_failing_sample_set_below_the_90_percent_threshold() -> None:
    expected = _golden_set(10)
    actual = dict(expected)
    # Two mismatches on address (8/10 = 80.0%), below the target.
    actual["SYN-00"] = ExtractedIdentity(
        name=expected["SYN-00"].name,
        date_of_birth=expected["SYN-00"].date_of_birth,
        address="wrong",
    )
    actual["SYN-01"] = ExtractedIdentity(
        name=expected["SYN-01"].name,
        date_of_birth=expected["SYN-01"].date_of_birth,
        address="wrong",
    )

    report = evaluate(expected, actual)

    assert report.field_accuracy("address").percentage == 80.0
    assert not report.field_accuracy("address").meets_threshold()
    assert not report.is_release_ready


def test_evaluate_counts_a_missing_extraction_as_a_non_match_without_fabrication() -> None:
    expected = _golden_set(2)

    report = evaluate(expected, actual={})

    for field in ("name", "date_of_birth", "address"):
        assert report.field_accuracy(field).matched == 0
        assert report.field_accuracy(field).total == 2
    assert not report.is_release_ready


def test_render_report_confirms_readiness_at_or_above_threshold() -> None:
    expected = _golden_set(10)
    report = evaluate(expected, dict(expected))

    text = render_report(report)

    assert "OCR_GOLDEN_SET_ACCURACY_VALID" in text
    assert "name: 10/10 (100.0%)" in text


def test_render_report_reopens_the_engine_decision_without_naming_another_engine() -> None:
    expected = _golden_set(10)

    report = evaluate(expected, actual={})
    text = render_report(report)

    assert "OCR_GOLDEN_SET_BELOW_THRESHOLD" in text
    assert "reopens the pinned local Tesseract engine decision" in text
    assert "does not authorize adding another OCR engine" in text
    assert "PaddleOCR" not in text
    assert "cloud" not in text.lower()


def test_expected_identity_from_label_matches_the_generator_card_layout() -> None:
    label = {
        "given_name": "Avery",
        "family_name": "Alden",
        "birth_date": "1990-01-01",
        "street": "100 Maple Avenue",
        "city": "Austin",
        "state": "TX",
        "postal_code": "78701",
    }

    identity = expected_identity_from_label(label)

    assert identity == ExtractedIdentity(
        name="Avery Alden", date_of_birth="1990-01-01", address="100 Maple Avenue, Austin, TX 78701"
    )


def test_run_golden_set_extracts_via_the_engine_and_scores_against_labels() -> None:
    labels = {
        "SYN-01": {
            "given_name": "Avery",
            "family_name": "Alden",
            "birth_date": "1990-01-01",
            "street": "100 Maple Avenue",
            "city": "Austin",
            "state": "TX",
            "postal_code": "78701",
        }
    }
    images = {"SYN-01": b"image-01"}
    card_text = "NAME: Avery Alden\nDOB: 1990-01-01\nADDRESS: 100 Maple Avenue, Austin, TX 78701\n"
    engine = FakeEngine(texts={"image-01": card_text})

    report = asyncio.run(run_golden_set(engine, images, labels))

    assert report.is_release_ready
    for field in ("name", "date_of_birth", "address"):
        assert report.field_accuracy(field).matched == 1


def test_run_golden_set_leaves_engine_failures_as_a_non_match_not_a_fabrication() -> None:
    labels = {
        "SYN-01": {
            "given_name": "Avery",
            "family_name": "Alden",
            "birth_date": "1990-01-01",
            "street": "100 Maple Avenue",
            "city": "Austin",
            "state": "TX",
            "postal_code": "78701",
        }
    }
    images = {"SYN-01": b"image-01"}
    engine = FakeEngine(fail_for={"image-01"})

    report = asyncio.run(run_golden_set(engine, images, labels))

    for field in ("name", "date_of_birth", "address"):
        assert report.field_accuracy(field).matched == 0


def test_load_golden_set_reads_the_isolated_fixture_layout(tmp_path: Path) -> None:
    generate("evaluator-seed", 3, tmp_path)

    images, labels = load_golden_set(tmp_path)

    assert set(images) == set(labels)
    assert len(labels) == 3
    identifier = next(iter(labels))
    assert images[identifier] == (tmp_path / "identity-images" / f"{identifier}.svg").read_bytes()


def _png_bytes() -> bytes:
    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", zlib.crc32(chunk_type + data))
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 64, 32, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([255]) * 64 for _ in range(32))
    idat = zlib.compress(raw)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed locally")
def test_run_golden_set_runs_against_the_pinned_local_tesseract_engine() -> None:
    labels = {
        "SYN-01": {
            "given_name": "Avery",
            "family_name": "Alden",
            "birth_date": "1990-01-01",
            "street": "100 Maple Avenue",
            "city": "Austin",
            "state": "TX",
            "postal_code": "78701",
        }
    }
    images = {"SYN-01": _png_bytes()}
    engine = SubprocessTesseractEngine()

    report = asyncio.run(run_golden_set(engine, images, labels))

    assert report.field_accuracy("name").total == 1
