"""Consent, invalid upload, partial OCR, purge, expiry, and isolation tests (TICK-014)."""

from __future__ import annotations

import ast
import asyncio
import shutil
import struct
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_server.ocr.service import (
    ConsentRequiredError,
    CorruptImageError,
    ExtractedIdentity,
    OcrService,
    OcrUploadStore,
    OversizedUploadError,
    PurgeReason,
    SubprocessTesseractEngine,
    TesseractUnavailableError,
    UnknownUploadError,
    UnsupportedImageFormatError,
    parse_extracted_fields,
    validate_image_upload,
)

TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 8, 18, 14, 30, tzinfo=TZ)


def png_bytes(width: int = 4, height: int = 2) -> bytes:
    """Build a minimal, structurally valid grayscale PNG without any dependency."""

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", zlib.crc32(chunk_type + data))
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([255]) * width for _ in range(height))
    idat = zlib.compress(raw)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def jpeg_bytes() -> bytes:
    return b"\xff\xd8\xff\xe0\x00\x10JFIFsome-data\xff\xd9"


class FakeEngine:
    def __init__(self, text: str = "", fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.calls: list[bytes] = []

    async def recognize_text(self, image: bytes) -> str:
        self.calls.append(image)
        if self.fail:
            raise TesseractUnavailableError("engine unavailable")
        return self.text


CARD_TEXT = (
    "SYNTHETIC DEMO ID\n"
    "NAME: Avery Alden\n"
    "DOB: 1990-01-01\n"
    "ADDRESS: 100 Maple Avenue, Austin, TX 78701\n"
    "ID: SYN-00000001\n"
)


def test_ticket_014_validation_accepts_well_formed_png_and_jpeg() -> None:
    validate_image_upload(png_bytes())
    validate_image_upload(jpeg_bytes())


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (b"", UnsupportedImageFormatError),
        (b"not an image at all", UnsupportedImageFormatError),
        (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", UnsupportedImageFormatError),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 4, CorruptImageError),
        (b"\xff\xd8\xff\xe0\x00\x10JFIFtruncated", CorruptImageError),
    ],
)
def test_ticket_014_rejects_non_image_and_corrupt_uploads_with_clear_errors(
    data: bytes, error: type[Exception]
) -> None:
    with pytest.raises(error):
        validate_image_upload(data)


def test_ticket_014_rejects_a_png_with_a_tampered_chunk_checksum() -> None:
    valid = bytearray(png_bytes())
    valid[-5] ^= 0xFF  # flip a byte inside the IEND chunk's CRC
    with pytest.raises(CorruptImageError):
        validate_image_upload(bytes(valid))


def test_ticket_014_rejects_an_oversized_upload() -> None:
    with pytest.raises(OversizedUploadError):
        validate_image_upload(png_bytes(), max_bytes=8)


def test_ticket_014_invalid_upload_never_reaches_the_ocr_engine() -> None:
    engine = FakeEngine()
    service = OcrService(engine)
    upload_id = service.begin(consent=True, now=NOW)

    with pytest.raises(UnsupportedImageFormatError):
        asyncio.run(service.submit(upload_id, b"not an image", NOW))

    assert engine.calls == []


def test_ticket_014_explicit_consent_precedes_upload_or_processing() -> None:
    engine = FakeEngine()
    service = OcrService(engine)

    with pytest.raises(ConsentRequiredError):
        service.begin(consent=False, now=NOW)

    with pytest.raises(UnknownUploadError):
        asyncio.run(service.submit("never-began", png_bytes(), NOW))
    assert engine.calls == []


def test_ticket_014_extracts_only_name_dob_and_address_from_a_complete_card() -> None:
    identity = parse_extracted_fields(CARD_TEXT)

    assert identity == ExtractedIdentity(
        name="Avery Alden",
        date_of_birth="1990-01-01",
        address="100 Maple Avenue, Austin, TX 78701",
    )
    assert identity.is_complete()


def test_ticket_014_partial_ocr_leaves_missing_fields_blank_for_manual_completion() -> None:
    identity = parse_extracted_fields("NAME: Avery Alden\nDOB:\n")

    assert identity == ExtractedIdentity(name="Avery Alden", date_of_birth=None, address=None)
    assert not identity.is_complete()


def test_ticket_014_engine_failure_leaves_every_field_blank_without_fabrication() -> None:
    engine = FakeEngine(fail=True)
    service = OcrService(engine)
    upload_id = service.begin(consent=True, now=NOW)

    identity = asyncio.run(service.submit(upload_id, png_bytes(), NOW))

    assert identity == ExtractedIdentity()
    assert not identity.is_complete()


def test_ticket_014_successful_submit_stores_image_and_identity_for_confirmation() -> None:
    engine = FakeEngine(text=CARD_TEXT)
    service = OcrService(engine)
    upload_id = service.begin(consent=True, now=NOW)
    image = png_bytes()

    identity = asyncio.run(service.submit(upload_id, image, NOW))

    assert identity.name == "Avery Alden"
    assert service.image(upload_id, NOW) == image
    assert service.identity(upload_id, NOW) == identity


def test_ticket_014_revocation_stops_processing_and_purges_verifiably(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = FakeEngine(text=CARD_TEXT)
    service = OcrService(engine)
    upload_id = service.begin(consent=True, now=NOW)
    asyncio.run(service.submit(upload_id, png_bytes(), NOW))

    with caplog.at_level("INFO"):
        service.revoke(upload_id, NOW)

    assert service.image(upload_id, NOW) is None
    assert service.identity(upload_id, NOW) is None
    with pytest.raises(UnknownUploadError):
        asyncio.run(service.submit(upload_id, png_bytes(), NOW))
    assert PurgeReason.CONSENT_REVOKED.value in caplog.text
    assert "Avery Alden" not in caplog.text


def test_ticket_014_deletion_request_purges_image_and_extracted_values() -> None:
    engine = FakeEngine(text=CARD_TEXT)
    service = OcrService(engine)
    upload_id = service.begin(consent=True, now=NOW)
    asyncio.run(service.submit(upload_id, png_bytes(), NOW))

    service.delete(upload_id, NOW)

    assert service.image(upload_id, NOW) is None
    assert service.identity(upload_id, NOW) is None


def test_ticket_014_abandoned_upload_expires_independently_of_any_purge_request() -> None:
    engine = FakeEngine(text=CARD_TEXT)
    store = OcrUploadStore(ttl=timedelta(minutes=5))
    service = OcrService(engine, store)
    upload_id = service.begin(consent=True, now=NOW)
    asyncio.run(service.submit(upload_id, png_bytes(), NOW))

    later = NOW + timedelta(minutes=10)

    assert service.image(upload_id, later) is None
    assert service.identity(upload_id, later) is None
    assert store.purge_expired(later) == 0  # the lazy read already dropped it


def test_ticket_014_purge_expired_sweeps_abandoned_uploads_without_a_read() -> None:
    engine = FakeEngine(text=CARD_TEXT)
    store = OcrUploadStore(ttl=timedelta(minutes=5))
    service = OcrService(engine, store)
    upload_id = service.begin(consent=True, now=NOW)
    asyncio.run(service.submit(upload_id, png_bytes(), NOW))

    purged = service.purge_expired(NOW + timedelta(minutes=10))

    assert purged == 1


def test_ticket_014_purge_leaves_no_retrievable_value_and_only_a_safe_reason_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = FakeEngine(text=CARD_TEXT)
    service = OcrService(engine)
    upload_id = service.begin(consent=True, now=NOW)
    asyncio.run(service.submit(upload_id, png_bytes(), NOW))

    with caplog.at_level("INFO"):
        service.delete(upload_id, NOW)

    assert service.image(upload_id, NOW) is None
    assert service.identity(upload_id, NOW) is None
    assert PurgeReason.DELETION_REQUESTED.value in caplog.text
    for secret in ("Avery Alden", "1990-01-01", "100 Maple Avenue"):
        assert secret not in caplog.text


def test_ticket_014_runtime_module_never_imports_fixtures_or_openemr() -> None:
    source_path = Path("ai_server/ocr/service.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert not any(module.startswith("ai_server.fixtures") for module in imported_modules)
    assert not any(module.startswith("ai_server.openemr") for module in imported_modules)


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed locally")
def test_ticket_014_real_tesseract_processes_a_valid_local_image() -> None:
    engine = SubprocessTesseractEngine()

    text = asyncio.run(engine.recognize_text(png_bytes(width=64, height=32)))

    assert isinstance(text, str)


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed locally")
def test_ticket_014_real_tesseract_rejects_an_unreadable_image() -> None:
    engine = SubprocessTesseractEngine(timeout=5.0)

    with pytest.raises(TesseractUnavailableError):
        asyncio.run(engine.recognize_text(b"this is not image data at all" * 5))
