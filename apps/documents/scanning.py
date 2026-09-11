"""
Virus scanning, fail-closed.

ClamAV runs as its own compose service and is spoken to over a socket, so
nothing about virus definitions lives in the application image.

The policy that matters is what happens when the scanner is unreachable. With
``DOCUMENT_SCAN_REQUIRED`` true — the production default — an upload is refused.
That is deliberately the inconvenient choice: the alternative is accepting a file
nobody checked and storing it where a counselor will later click it. A ministry
office noticing that uploads are failing is a better outcome than one quietly
passing malware between a counselee and their counselor.

Scanning happens **before** the file is encrypted and stored, so an infected file
never reaches disk in the first place. That means the scan runs on the plaintext
in the request, which is the only point at which the bytes are readable anyway.
"""

import logging
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanResult:
    status: str
    detail: str = ""

    @property
    def is_clean(self) -> bool:
        from apps.documents.models import ScanStatus

        return self.status in (ScanStatus.CLEAN, ScanStatus.SKIPPED)


class ScannerUnavailable(Exception):
    """The scanner could not be reached, and scanning is required."""


class InfectedFile(Exception):
    """ClamAV named a signature in this upload."""

    def __init__(self, signature: str):
        self.signature = signature
        super().__init__(f"Rejected by the virus scanner: {signature}")


def _client():
    import clamd

    socket = getattr(settings, "CLAMAV_UNIX_SOCKET", "") or ""
    if socket:
        return clamd.ClamdUnixSocket(path=socket)
    return clamd.ClamdNetworkSocket(
        host=settings.CLAMAV_HOST,
        port=settings.CLAMAV_PORT,
        timeout=settings.CLAMAV_TIMEOUT_SECONDS,
    )


def scan(upload) -> ScanResult:
    """Scan an uploaded file. Raises rather than returning a soft failure.

    Returning "probably fine" from a security check is how the check stops
    meaning anything, so the only non-exception outcomes are clean and — when
    scanning is switched off for local development — skipped.
    """
    from apps.documents.models import ScanStatus

    if not settings.DOCUMENT_SCAN_ENABLED:
        # Only reachable in dev and test: prod.py refuses to start with scanning
        # off while DOCUMENT_SCAN_REQUIRED is true.
        return ScanResult(ScanStatus.SKIPPED, "Scanning is disabled in this environment.")

    upload.seek(0)
    try:
        response = _client().instream(upload)
    except Exception as exc:
        logger.exception("ClamAV was unreachable while scanning an upload")
        if settings.DOCUMENT_SCAN_REQUIRED:
            raise ScannerUnavailable(str(exc)) from exc
        return ScanResult(ScanStatus.SKIPPED, f"Scanner unavailable: {exc}")
    finally:
        upload.seek(0)

    # clamd returns {"stream": ("OK"|"FOUND"|"ERROR", signature_or_none)}.
    status, detail = response.get("stream", ("ERROR", "no response from scanner"))
    if status == "OK":
        return ScanResult(ScanStatus.CLEAN)
    if status == "FOUND":
        raise InfectedFile(detail or "unnamed signature")

    logger.error("ClamAV returned an error for an upload: %s", detail)
    if settings.DOCUMENT_SCAN_REQUIRED:
        raise ScannerUnavailable(detail or "scanner error")
    return ScanResult(ScanStatus.SKIPPED, str(detail))
