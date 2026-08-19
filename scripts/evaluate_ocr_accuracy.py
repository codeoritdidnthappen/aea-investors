"""Gate local-demo readiness on synthetic-ID golden-set OCR accuracy (TICK-015, NFR-29).

Tesseract is the sole v1 OCR engine. This script runs the pinned local Tesseract binary
and English trained data (`ai_server.ocr.service`) against the isolated synthetic-ID
golden set produced offline by `ai_server.fixtures.generator`, calculates field-level
accuracy for name, date of birth, and address, and blocks local-demo readiness below the
90% NFR-29 target. A failing report reopens the pinned-engine decision; it never adds
another OCR engine or lowers the target (Out of Scope).

This script is intentionally not copied into the deployment image (see
`deploy/local/ai-server.Dockerfile`): the golden set it reads is evaluation-only and must
never ship as a runtime artifact (TICK-006, `scripts/verify_release_governance.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ai_server.ocr.service import (
    TESSERACT_BINARY,
    TESSERACT_LANGUAGE,
    ExtractedIdentity,
    SubprocessTesseractEngine,
    TesseractEngine,
    TesseractUnavailableError,
    parse_extracted_fields,
)

RELEASE_THRESHOLD_PERCENT = 90.0
FIELDS: tuple[str, ...] = ("name", "date_of_birth", "address")


@dataclass(frozen=True)
class FieldAccuracy:
    """How often one extracted field matched the golden-set label."""

    field: str
    matched: int
    total: int

    @property
    def percentage(self) -> float:
        if self.total == 0:
            raise ValueError("field accuracy requires at least one golden-set sample")
        return (self.matched / self.total) * 100.0

    def meets_threshold(self, threshold: float = RELEASE_THRESHOLD_PERCENT) -> bool:
        return self.percentage >= threshold


@dataclass(frozen=True)
class AccuracyReport:
    """Field-level accuracy for name, date of birth, and address (NFR-29)."""

    fields: tuple[FieldAccuracy, ...]

    def field_accuracy(self, field: str) -> FieldAccuracy:
        for accuracy in self.fields:
            if accuracy.field == field:
                return accuracy
        raise KeyError(field)

    @property
    def is_release_ready(self) -> bool:
        """Local-demo readiness requires every field to meet the 90% target."""
        return all(accuracy.meets_threshold() for accuracy in self.fields)


def evaluate(
    expected: Mapping[str, ExtractedIdentity], actual: Mapping[str, ExtractedIdentity]
) -> AccuracyReport:
    """Score *actual* extractions against the golden-set *expected* labels per field.

    A golden-set identifier missing from *actual* counts toward the field's total but
    never as a match, so a failed or missing extraction lowers accuracy rather than
    being silently excluded.
    """
    if not expected:
        raise ValueError("golden set is empty")
    accuracies = tuple(
        FieldAccuracy(
            field=field,
            matched=sum(
                1
                for identifier, expected_identity in expected.items()
                if getattr(expected_identity, field) is not None
                and getattr(actual.get(identifier), field, None)
                == getattr(expected_identity, field)
            ),
            total=len(expected),
        )
        for field in FIELDS
    )
    return AccuracyReport(fields=accuracies)


def render_report(report: AccuracyReport) -> str:
    """Render a human-readable report; a failing report reopens the engine decision."""
    lines = [
        f"{accuracy.field}: {accuracy.matched}/{accuracy.total} ({accuracy.percentage:.1f}%)"
        for accuracy in report.fields
    ]
    if report.is_release_ready:
        header = (
            "OCR_GOLDEN_SET_ACCURACY_VALID: local-demo readiness meets the NFR-29 90% "
            "field-level accuracy target."
        )
    else:
        header = (
            "OCR_GOLDEN_SET_BELOW_THRESHOLD: local-demo readiness is blocked because "
            "field-level accuracy on the synthetic-ID golden set is below the NFR-29 90% "
            "target. This reopens the pinned local Tesseract engine decision; it does not "
            "authorize adding another OCR engine."
        )
    return "\n".join([header, *lines])


def expected_identity_from_label(label: Mapping[str, str]) -> ExtractedIdentity:
    """Build the expected extraction from one `ai_server.fixtures.generator` label.

    Mirrors the identity-card layout the generator renders (`NAME`, `DOB`, `ADDRESS`),
    so this must stay in lockstep with `ai_server.fixtures.generator._card`.
    """
    return ExtractedIdentity(
        name=f"{label['given_name']} {label['family_name']}",
        date_of_birth=label["birth_date"],
        address=f"{label['street']}, {label['city']}, {label['state']} {label['postal_code']}",
    )


async def _extract(engine: TesseractEngine, image: bytes) -> ExtractedIdentity:
    """Run one image through *engine*; an unavailable engine leaves fields blank (FR-7)."""
    try:
        raw_text = await engine.recognize_text(image)
    except TesseractUnavailableError:
        return ExtractedIdentity()
    return parse_extracted_fields(raw_text)


async def run_golden_set(
    engine: TesseractEngine,
    images: Mapping[str, bytes],
    labels: Mapping[str, Mapping[str, str]],
) -> AccuracyReport:
    """Run pinned Tesseract over every golden-set image and score it against its label."""
    expected = {
        identifier: expected_identity_from_label(label) for identifier, label in labels.items()
    }
    actual = {
        identifier: await _extract(engine, images[identifier])
        for identifier in labels
        if identifier in images
    }
    return evaluate(expected, actual)


def load_golden_set(golden_set_root: Path) -> tuple[dict[str, bytes], dict[str, dict]]:
    """Load the isolated golden set produced by `ai_server.fixtures.generator.generate`."""
    labels_path = golden_set_root / "evaluation" / "ocr-labels.json"
    images_dir = golden_set_root / "identity-images"
    if not labels_path.is_file():
        raise FileNotFoundError(f"golden-set labels are missing: {labels_path}")
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    images = {path.stem: path.read_bytes() for path in images_dir.glob("*") if path.is_file()}
    return images, labels


def main() -> int:
    """Run the golden-set gate and return a process exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--golden-set",
        type=Path,
        required=True,
        help="root of an isolated synthetic-ID golden set from ai_server.fixtures.generator",
    )
    parser.add_argument("--tesseract-binary", default=TESSERACT_BINARY)
    parser.add_argument("--language", default=TESSERACT_LANGUAGE)
    arguments = parser.parse_args()

    images, labels = load_golden_set(arguments.golden_set)
    engine = SubprocessTesseractEngine(
        binary=arguments.tesseract_binary, language=arguments.language
    )
    report = asyncio.run(run_golden_set(engine, images, labels))
    print(render_report(report))
    return 0 if report.is_release_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
