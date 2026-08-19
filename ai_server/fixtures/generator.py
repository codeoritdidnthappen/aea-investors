"""Generate paired, evaluation-only synthetic OpenEMR and identity-card fixtures."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True)
class SyntheticIdentity:
    """A deterministic synthetic identity used only to create evaluation artifacts."""

    identifier: str
    given_name: str
    family_name: str
    birth_date: str
    street: str
    city: str
    state: str
    postal_code: str

    def openemr_seed(self) -> dict[str, str]:
        return {
            "external_id": self.identifier,
            "fname": self.given_name,
            "lname": self.family_name,
            "DOB": self.birth_date,
            "street": self.street,
            "city": self.city,
            "state": self.state,
            "postal_code": self.postal_code,
        }


_GIVEN_NAMES = ("Avery", "Blake", "Casey", "Devon", "Emery", "Finley")
_FAMILY_NAMES = ("Alden", "Briar", "Cedar", "Dover", "Ellis", "Frost")
_STREETS = ("Maple Avenue", "Cedar Street", "River Road", "Juniper Lane")
_CITIES = ("Austin", "Chicago", "Denver", "Madison")
_STATES = ("TX", "IL", "CO", "WI")


def _number(seed: str, index: int) -> int:
    digest = hashlib.sha256(f"{seed}:{index}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def make_identity(seed: str, index: int) -> SyntheticIdentity:
    """Create one stable, explicitly fictional identity from *seed* and *index*."""
    value = _number(seed, index)
    birth = date(1970, 1, 1) + timedelta(days=value % 13000)
    return SyntheticIdentity(
        identifier=f"SYN-{value:08X}",
        given_name=_GIVEN_NAMES[value % len(_GIVEN_NAMES)],
        family_name=_FAMILY_NAMES[(value // 7) % len(_FAMILY_NAMES)],
        birth_date=birth.isoformat(),
        street=f"{100 + value % 8900} {_STREETS[(value // 13) % len(_STREETS)]}",
        city=_CITIES[(value // 17) % len(_CITIES)],
        state=_STATES[(value // 19) % len(_STATES)],
        postal_code=f"{10000 + value % 89999:05d}",
    )


def _card(identity: SyntheticIdentity) -> str:
    rows = (
        ("NAME", f"{identity.given_name} {identity.family_name}"),
        ("DOB", identity.birth_date),
        ("ADDRESS", f"{identity.street}, {identity.city}, {identity.state} {identity.postal_code}"),
        ("ID", identity.identifier),
    )
    rendered = "".join(
        f'<text x="40" y="{110 + step * 55}" font-family="sans-serif" font-size="24">'
        f"{html.escape(label)}: {html.escape(value)}</text>"
        for step, (label, value) in enumerate(rows)
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="400" role="img" '
        'aria-label="Synthetic identity card"><rect width="800" height="400" fill="#f3f6fa"/>'
        '<text x="40" y="55" font-family="sans-serif" font-size="30">SYNTHETIC DEMO ID</text>'
        f"{rendered}</svg>"
    )


def generate(seed: str, count: int, output: Path) -> None:
    """Write paired seeds, SVG cards, expected OCR labels, and privacy corpus offline."""
    if count < 1:
        raise ValueError("count must be at least one")
    seeds = output / "openemr-seeds"
    images = output / "identity-images"
    evaluation = output / "evaluation"
    for directory in (seeds, images, evaluation):
        directory.mkdir(parents=True, exist_ok=True)
    identities = [make_identity(seed, index) for index in range(count)]
    for identity in identities:
        (seeds / f"{identity.identifier}.json").write_text(
            json.dumps(identity.openemr_seed(), indent=2) + "\n", encoding="utf-8"
        )
        (images / f"{identity.identifier}.svg").write_text(_card(identity), encoding="utf-8")
    labels = {identity.identifier: asdict(identity) for identity in identities}
    (evaluation / "ocr-labels.json").write_text(
        json.dumps(labels, indent=2) + "\n", encoding="utf-8"
    )
    corpus = sorted({value for identity in identities for value in asdict(identity).values()})
    (evaluation / "privacy-corpus.json").write_text(
        json.dumps(corpus, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate offline synthetic evaluation fixtures")
    parser.add_argument("--seed", required=True)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    generate(arguments.seed, arguments.count, arguments.output)


if __name__ == "__main__":
    main()
