"""Consent-gated, transient local OCR for synthetic identity documents.

Runtime extraction derives identity fields only from an uploaded image and explicit
patient corrections (FR-25): this module never imports the fixture generator, never
reads OpenEMR demographics, and never reads an evaluation OCR label. TICK-015 owns
accuracy measurement against the fixture golden set; this module only owns consent,
upload validation, extraction, and verifiable purge/expiry (FR-6, FR-7, FR-21-FR-23,
NFR-23, NFR-29).
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import tempfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

TESSERACT_BINARY = "tesseract"
TESSERACT_LANGUAGE = "eng"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
DEFAULT_UPLOAD_TTL = timedelta(minutes=15)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_PREFIX = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"


class InvalidUploadError(Exception):
    """Base class for an upload rejected before OCR runs (FR-22)."""


class UnsupportedImageFormatError(InvalidUploadError):
    """Raised for an empty upload or one that is not a supported image format."""


class CorruptImageError(InvalidUploadError):
    """Raised for a malformed or corrupt image that cannot be safely processed."""


class OversizedUploadError(InvalidUploadError):
    """Raised when an upload exceeds the configured size limit."""


def validate_image_upload(data: bytes, *, max_bytes: int = MAX_UPLOAD_BYTES) -> None:
    """Reject a malformed, corrupt, oversized, or non-image upload before OCR (FR-22).

    Only PNG and JPEG are accepted. Validation reads structural metadata (chunk
    checksums and end-of-image markers); it never invokes Tesseract.
    """
    if not data:
        raise UnsupportedImageFormatError("upload is empty")
    if len(data) > max_bytes:
        raise OversizedUploadError(f"upload exceeds the {max_bytes}-byte limit")
    if data.startswith(_PNG_SIGNATURE):
        _validate_png(data)
    elif data.startswith(_JPEG_PREFIX):
        _validate_jpeg(data)
    else:
        raise UnsupportedImageFormatError("upload is not a supported PNG or JPEG image")


def _validate_png(data: bytes) -> None:
    offset = len(_PNG_SIGNATURE)
    seen_header = False
    seen_end = False
    while offset < len(data):
        if offset + 8 > len(data):
            raise CorruptImageError("PNG chunk header is truncated")
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 8 + length + 4
        if length < 0 or chunk_end > len(data):
            raise CorruptImageError("PNG chunk data is truncated")
        chunk_data = data[offset + 8 : offset + 8 + length]
        crc_expected = int.from_bytes(data[offset + 8 + length : chunk_end], "big")
        if zlib.crc32(chunk_type + chunk_data) != crc_expected:
            raise CorruptImageError("PNG chunk checksum does not match")
        if not seen_header:
            if chunk_type != b"IHDR" or length != 13:
                raise CorruptImageError("PNG is missing a valid header chunk")
            seen_header = True
        if chunk_type == b"IEND":
            seen_end = True
            break
        offset = chunk_end
    if not seen_header or not seen_end:
        raise CorruptImageError("PNG is missing a required chunk")


def _validate_jpeg(data: bytes) -> None:
    if not data.endswith(_JPEG_EOI):
        raise CorruptImageError("JPEG is missing its end-of-image marker")


@dataclass(frozen=True)
class ExtractedIdentity:
    """Only the three approved fields; a missing field is `None`, never fabricated."""

    name: str | None = None
    date_of_birth: str | None = None
    address: str | None = None

    def is_complete(self) -> bool:
        return self.name is not None and self.date_of_birth is not None and self.address is not None


_EMPTY_IDENTITY = ExtractedIdentity()

_FIELD_PATTERNS = {
    "name": re.compile(r"(?im)^\s*name\s*:\s*(.*?)\s*$"),
    "date_of_birth": re.compile(r"(?im)^\s*dob\s*:\s*(.*?)\s*$"),
    "address": re.compile(r"(?im)^\s*address\s*:\s*(.*?)\s*$"),
}


def parse_extracted_fields(raw_text: str) -> ExtractedIdentity:
    """Parse only name, date of birth, and address from raw OCR text (FR-6).

    A missing or blank label leaves the field `None` for manual completion (FR-7)
    rather than guessing a value.
    """
    values: dict[str, str | None] = {}
    for field, pattern in _FIELD_PATTERNS.items():
        match = pattern.search(raw_text)
        value = match.group(1).strip() if match else ""
        values[field] = value or None
    return ExtractedIdentity(**values)


class TesseractUnavailableError(Exception):
    """Raised when the pinned local Tesseract binary cannot process an image."""


class TesseractEngine(Protocol):
    """The narrow boundary through which image bytes reach local OCR."""

    async def recognize_text(self, image: bytes) -> str:
        """Return raw OCR text for *image*, or raise `TesseractUnavailableError`."""
        ...


class SubprocessTesseractEngine:
    """Shell out to the pinned local `tesseract` binary; never a network call."""

    def __init__(
        self,
        binary: str = TESSERACT_BINARY,
        language: str = TESSERACT_LANGUAGE,
        timeout: float = 10.0,
    ) -> None:
        self._binary = binary
        self._language = language
        self._timeout = timeout

    async def recognize_text(self, image: bytes) -> str:
        """Write *image* to a request-duration temporary file and run Tesseract on it."""
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "upload"
            image_path.write_bytes(image)
            process = await asyncio.create_subprocess_exec(
                self._binary,
                str(image_path),
                "stdout",
                "-l",
                self._language,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
            except asyncio.TimeoutError as exc:
                process.kill()
                await process.wait()
                raise TesseractUnavailableError("local Tesseract timed out") from exc
            if process.returncode != 0:
                raise TesseractUnavailableError("local Tesseract failed to process the upload")
            return stdout.decode("utf-8", errors="replace")


class ConsentRequiredError(Exception):
    """Raised when upload or processing is attempted without prior explicit consent."""


class UnknownUploadError(Exception):
    """Raised for an upload id that is unrecognized, already purged, or expired."""


class PurgeReason(Enum):
    """The only non-sensitive purge outcomes recorded in the event log."""

    CONSENT_REVOKED = "consent_revoked"
    DELETION_REQUESTED = "deletion_requested"
    EXPIRED = "expired"


@dataclass
class _StoredUpload:
    image: bytes | None
    identity: ExtractedIdentity | None
    expires_at: datetime


class OcrUploadStore:
    """A request-duration, in-memory store for image bytes and extracted fields.

    Nothing here is written to durable storage: an upload exists only in process
    memory until it is purged or the process ends (NFR-23).
    """

    def __init__(self, ttl: timedelta = DEFAULT_UPLOAD_TTL) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        self._ttl = ttl
        self._uploads: dict[str, _StoredUpload] = {}

    def open(self, now: datetime) -> str:
        """Create one new transient upload session and return its id."""
        upload_id = secrets.token_urlsafe(18)
        self._uploads[upload_id] = _StoredUpload(
            image=None, identity=None, expires_at=now + self._ttl
        )
        return upload_id

    def store_image(self, upload_id: str, image: bytes, now: datetime) -> None:
        self._active(upload_id, now).image = image

    def store_identity(self, upload_id: str, identity: ExtractedIdentity, now: datetime) -> None:
        self._active(upload_id, now).identity = identity

    def image(self, upload_id: str, now: datetime) -> bytes | None:
        """Return the stored image, or `None` once purged, expired, or unknown."""
        record = self._live(upload_id, now)
        return record.image if record else None

    def identity(self, upload_id: str, now: datetime) -> ExtractedIdentity | None:
        """Return the stored extraction result, or `None` once purged or expired."""
        record = self._live(upload_id, now)
        return record.identity if record else None

    def _live(self, upload_id: str, now: datetime) -> _StoredUpload | None:
        """Return the record if it is still fresh, evicting it lazily otherwise."""
        record = self._uploads.get(upload_id)
        if record is None:
            return None
        if now >= record.expires_at:
            del self._uploads[upload_id]
            return None
        return record

    def purge(self, upload_id: str, now: datetime) -> None:
        """Remove one upload's image and extracted values immediately and verifiably."""
        self._active(upload_id, now)
        del self._uploads[upload_id]

    def purge_expired(self, now: datetime) -> int:
        """Remove every abandoned upload independent of any explicit purge request."""
        expired = [
            upload_id for upload_id, record in self._uploads.items() if now >= record.expires_at
        ]
        for upload_id in expired:
            del self._uploads[upload_id]
        return len(expired)

    def _active(self, upload_id: str, now: datetime) -> _StoredUpload:
        record = self._live(upload_id, now)
        if record is None:
            raise UnknownUploadError("upload is unrecognized, purged, or expired")
        return record


class OcrService:
    """Enforce consent and validation, then run local extraction and purge.

    FR-6, FR-7, FR-21, FR-22, FR-23.
    """

    def __init__(self, engine: TesseractEngine, store: OcrUploadStore | None = None) -> None:
        self._engine = engine
        self._store = store or OcrUploadStore()

    def begin(self, *, consent: bool, now: datetime) -> str:
        """Open a new upload session; refuses to proceed without explicit consent (FR-21)."""
        if not consent:
            raise ConsentRequiredError("explicit consent is required before upload or processing")
        return self._store.open(now)

    async def submit(self, upload_id: str, image: bytes, now: datetime) -> ExtractedIdentity:
        """Validate *image*, then extract only the approved fields.

        Validation runs before any OCR call (FR-22). An unavailable or failing local
        Tesseract leaves every field `None` rather than fabricating a value (FR-7).
        """
        self._store.store_image(upload_id, self._validated(image), now)
        identity = await self._extract(image)
        self._store.store_identity(upload_id, identity, now)
        return identity

    @staticmethod
    def _validated(image: bytes) -> bytes:
        validate_image_upload(image)
        return image

    async def _extract(self, image: bytes) -> ExtractedIdentity:
        try:
            raw_text = await self._engine.recognize_text(image)
        except TesseractUnavailableError:
            logger.warning("local OCR extraction failed")
            return _EMPTY_IDENTITY
        return parse_extracted_fields(raw_text)

    def image(self, upload_id: str, now: datetime) -> bytes | None:
        """Return the transient uploaded image, or `None` once unavailable (FR-23)."""
        return self._store.image(upload_id, now)

    def identity(self, upload_id: str, now: datetime) -> ExtractedIdentity | None:
        """Return the transient extraction result, or `None` once unavailable (FR-23)."""
        return self._store.identity(upload_id, now)

    def revoke(self, upload_id: str, now: datetime) -> None:
        """Stop processing and verifiably purge on consent revocation (FR-23)."""
        self._purge(upload_id, PurgeReason.CONSENT_REVOKED, now)

    def delete(self, upload_id: str, now: datetime) -> None:
        """Stop processing and verifiably purge on an explicit deletion request (FR-23)."""
        self._purge(upload_id, PurgeReason.DELETION_REQUESTED, now)

    def purge_expired(self, now: datetime) -> int:
        """Purge every abandoned upload independent of user action (FR-23, NFR-23)."""
        count = self._store.purge_expired(now)
        if count:
            logger.info("ocr uploads purged: reason=%s count=%d", PurgeReason.EXPIRED.value, count)
        return count

    def _purge(self, upload_id: str, reason: PurgeReason, now: datetime) -> None:
        self._store.purge(upload_id, now)
        logger.info("ocr upload purged: reason=%s", reason.value)
