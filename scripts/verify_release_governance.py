"""Verify release metadata and keep evaluation-only data out of artifacts."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


class GovernanceError(Exception):
    """Raised when a release-governance requirement is not met."""


_REQUIRED_RUNTIME_COMPONENTS = ("External language model", "OCR")
_PROMPT_COMPONENT = "Prompt contract"
_PROHIBITED_FILENAMES = frozenset({"ocr-labels.json", "privacy-corpus.json"})
_PROHIBITED_DIRECTORIES = frozenset({"generated-fixtures", "identity-images", "openemr-seeds"})


def validate_ai_usage(path: Path) -> None:
    """Require pinned runtime selections and a versioned prompt contract."""
    if not path.is_file():
        raise GovernanceError(f"AI usage record is missing: {path}")

    document = path.read_text(encoding="utf-8")
    if "| Component | Pinned selection | Purpose |" not in document:
        raise GovernanceError("AI usage record lacks the pinned-selection inventory")

    for component in _REQUIRED_RUNTIME_COMPONENTS:
        match = re.search(rf"^\| {re.escape(component)} \| (.+?) \|", document, re.MULTILINE)
        if match is None or not match.group(1).strip():
            raise GovernanceError(f"AI usage record lacks a pinned runtime entry for {component}")

    prompt_match = re.search(rf"^\| {_PROMPT_COMPONENT} \| (.+?) \|", document, re.MULTILINE)
    if prompt_match is None or re.search(r"\bv\d+\b", prompt_match.group(1)) is None:
        raise GovernanceError("AI usage record lacks a versioned prompt contract")


def find_prohibited_artifacts(artifact_root: Path) -> list[Path]:
    """Return evaluation-only files and directories found below an artifact root."""
    if not artifact_root.is_dir():
        raise GovernanceError(f"artifact root does not exist: {artifact_root}")

    prohibited: list[Path] = []
    for path in artifact_root.rglob("*"):
        relative_parts = path.relative_to(artifact_root).parts
        if path.name in _PROHIBITED_FILENAMES or any(
            part in _PROHIBITED_DIRECTORIES for part in relative_parts
        ):
            prohibited.append(path)
    return prohibited


def validate_artifact_root(artifact_root: Path) -> None:
    """Reject OCR labels, fixture source records, and privacy golden-corpus data."""
    prohibited = find_prohibited_artifacts(artifact_root)
    if prohibited:
        paths = ", ".join(str(path.relative_to(artifact_root)) for path in prohibited)
        raise GovernanceError(f"artifact contains evaluation-only data: {paths}")


def main() -> int:
    """Run the governance gate and return a process exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ai-usage", type=Path, default=Path("AI_USAGE.md"))
    parser.add_argument("--artifact-root", type=Path, required=True)
    arguments = parser.parse_args()

    try:
        validate_ai_usage(arguments.ai_usage)
        validate_artifact_root(arguments.artifact_root)
    except GovernanceError as error:
        print(f"GOVERNANCE_GATE_FAILED: {error}", file=sys.stderr)
        return 1

    print("AI_USAGE_VALID")
    print("ARTIFACT_GOVERNANCE_VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
