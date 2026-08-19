from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_server.fixtures.generator import generate


def test_generator_is_deterministic_and_pairs_seed_with_svg(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    generate("demo-seed", 3, first)
    generate("demo-seed", 3, second)

    first_labels = (first / "evaluation" / "ocr-labels.json").read_text(encoding="utf-8")
    assert first_labels == (second / "evaluation" / "ocr-labels.json").read_text(encoding="utf-8")
    labels = json.loads(first_labels)
    assert len(labels) == 3
    for identifier, identity in labels.items():
        seed = json.loads((first / "openemr-seeds" / f"{identifier}.json").read_text())
        image = (first / "identity-images" / f"{identifier}.svg").read_text()
        assert seed["fname"] == identity["given_name"]
        assert seed["lname"] == identity["family_name"]
        assert identity["street"] in image


def test_generator_rejects_an_empty_fixture_set(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one"):
        generate("demo-seed", 0, tmp_path)


def test_privacy_corpus_contains_every_seeded_value(tmp_path: Path) -> None:
    generate("demo-seed", 2, tmp_path)
    corpus = set(json.loads((tmp_path / "evaluation" / "privacy-corpus.json").read_text()))
    labels = json.loads((tmp_path / "evaluation" / "ocr-labels.json").read_text())
    assert all(value in corpus for identity in labels.values() for value in identity.values())
