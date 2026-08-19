from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.verify_release_governance import (
    GovernanceError,
    validate_ai_usage,
    validate_artifact_root,
)


def test_governance_gate_accepts_release_metadata_and_clean_artifact_root(tmp_path: Path) -> None:
    repository_root = Path(__file__).parents[2]
    artifact_root = tmp_path / "artifact"
    artifact_root.mkdir()
    shutil.copy(repository_root / "AI_USAGE.md", tmp_path / "AI_USAGE.md")

    validate_ai_usage(tmp_path / "AI_USAGE.md")
    validate_artifact_root(artifact_root)


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        (Path("evaluation/ocr-labels.json"), "{}"),
        (Path("evaluation/privacy-corpus.json"), "[]"),
        (Path("openemr-seeds/SYN-001.json"), "{}"),
    ],
)
def test_governance_gate_rejects_evaluation_data_in_artifacts(
    tmp_path: Path, relative_path: Path, content: str
) -> None:
    artifact = tmp_path / "artifact"
    prohibited_file = artifact / relative_path
    prohibited_file.parent.mkdir(parents=True)
    prohibited_file.write_text(content, encoding="utf-8")

    with pytest.raises(GovernanceError, match="evaluation-only data"):
        validate_artifact_root(artifact)


def test_governance_gate_rejects_ai_usage_without_prompt_version(tmp_path: Path) -> None:
    usage = tmp_path / "AI_USAGE.md"
    usage.write_text(
        "| Component | Pinned selection | Purpose |\n"
        "| External language model | Groq `openai/gpt-oss-120b` | planning |\n"
        "| OCR | Local Tesseract | extraction |\n"
        "| Prompt contract | `ONBOARDING_CONTRACT.md` | fields |\n",
        encoding="utf-8",
    )

    with pytest.raises(GovernanceError, match="versioned prompt contract"):
        validate_ai_usage(usage)


def test_governance_gate_rejects_ai_usage_without_pinned_ocr_entry(tmp_path: Path) -> None:
    usage = tmp_path / "AI_USAGE.md"
    usage.write_text(
        "| Component | Pinned selection | Purpose |\n"
        "| External language model | Groq `openai/gpt-oss-120b` | planning |\n"
        "| Prompt contract | `ONBOARDING_CONTRACT.md` v1 | fields |\n",
        encoding="utf-8",
    )

    with pytest.raises(GovernanceError, match="pinned runtime entry for OCR"):
        validate_ai_usage(usage)
