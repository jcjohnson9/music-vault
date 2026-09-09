"""Allowlisted acquisition diagnostics; never serialize provider error text.

A 403 is an observation, not a permanent classification of a track. Circuit
state belongs to one user-requested sync batch and is discarded afterwards.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class AcquisitionStage(str, Enum):
    READINESS = "readiness"
    ENUMERATION = "api_enumeration"
    METADATA = "metadata_extraction"
    TRANSFER = "media_transfer"
    TRANSFORM = "audio_transformation"
    VERIFICATION = "final_verification"
    IMPORT = "library_import"


class AcquisitionReason(str, Enum):
    NOT_READY = "stack_not_ready"
    API_ACCESS = "api_access_denied"
    QUOTA = "api_quota_exhausted"
    UNAVAILABLE = "item_unavailable"
    CHALLENGE = "client_challenge"
    HTTP_FORBIDDEN = "http_forbidden"
    RATE_LIMITED = "rate_limited"
    NETWORK = "network_unavailable"
    SERVER = "server_error"
    TRANSFORMATION = "transformation_failed"
    VERIFICATION = "verification_failed"
    NO_AUDIO = "no_usable_audio_format"
    IMPORT = "import_failed"
    LIMIT = "diagnostic_limit_reached"
    UNKNOWN = "unclassified_failure"


_MESSAGES = {
    AcquisitionReason.NOT_READY: "The acquisition components are not ready. Check acquisition health in Settings.",
    AcquisitionReason.API_ACCESS: "The playlist API denied access. Check authorized public/unlisted access and API setup.",
    AcquisitionReason.QUOTA: "The playlist API quota is exhausted. Retry after the quota resets.",
    AcquisitionReason.UNAVAILABLE: "This item is unavailable through anonymous authorized acquisition.",
    AcquisitionReason.CHALLENGE: "The provider requested a client challenge. Check the supported acquisition stack; no account access was attempted.",
    AcquisitionReason.HTTP_FORBIDDEN: "The provider denied this request (HTTP 403). This may be a client or access failure, not a permanently unavailable track.",
    AcquisitionReason.RATE_LIMITED: "The provider is rate limiting requests. Wait before retrying.",
    AcquisitionReason.NETWORK: "The network request failed. Check connectivity before retrying.",
    AcquisitionReason.SERVER: "The provider returned a temporary server error. Retry later.",
    AcquisitionReason.TRANSFORMATION: "Audio preparation failed. Check FFmpeg readiness; the output was not accepted.",
    AcquisitionReason.VERIFICATION: "The final audio did not pass verification and was not accepted.",
    AcquisitionReason.NO_AUDIO: "The source did not provide a supported, verifiable audio format.",
    AcquisitionReason.IMPORT: "Verified audio could not be committed to the library. It was not archived as imported.",
    AcquisitionReason.LIMIT: "The disposable acquisition diagnostic reached its byte or time limit.",
    AcquisitionReason.UNKNOWN: "Acquisition failed at this stage. No unverified output was accepted.",
}
_SYSTEMIC = {
    AcquisitionReason.NOT_READY,
    AcquisitionReason.CHALLENGE,
    AcquisitionReason.HTTP_FORBIDDEN,
    AcquisitionReason.RATE_LIMITED,
    AcquisitionReason.NETWORK,
    AcquisitionReason.SERVER,
}


@dataclass(frozen=True)
class AcquisitionDiagnostic:
    stage: AcquisitionStage
    reason: AcquisitionReason
    http_status: int | None = None

    @property
    def systemic_candidate(self) -> bool:
        return self.reason in _SYSTEMIC

    @property
    def retry_recommendation(self) -> str:
        if self.reason in {AcquisitionReason.NETWORK, AcquisitionReason.SERVER, AcquisitionReason.RATE_LIMITED, AcquisitionReason.QUOTA}:
            return "retry_later"
        if self.reason in {AcquisitionReason.NOT_READY, AcquisitionReason.CHALLENGE, AcquisitionReason.HTTP_FORBIDDEN}:
            return "check_acquisition_health"
        if self.reason == AcquisitionReason.UNAVAILABLE:
            return "check_item_availability"
        return "review_stage_before_retry"

    @property
    def message(self) -> str:
        return f"{self.stage.value}: {_MESSAGES[self.reason]}"

    def to_dict(self) -> dict:
        return {
            "stage": self.stage.value,
            "reason": self.reason.value,
            "http_status": self.http_status,
            "retry_recommendation": self.retry_recommendation,
        }


class AcquisitionError(RuntimeError):
    def __init__(self, diagnostic: AcquisitionDiagnostic):
        self.diagnostic = diagnostic
        super().__init__(diagnostic.message)


def classify_acquisition_error(error: object, stage: AcquisitionStage) -> AcquisitionDiagnostic:
    """Inspect ephemeral text for known signals, retaining none of that text."""
    existing = getattr(error, "diagnostic", None)
    if isinstance(existing, AcquisitionDiagnostic):
        return existing
    # Backend errors can contain terminal styling even with quiet logging.
    # Strip only formatting for classification; never retain the raw text.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(error)).casefold()
    match = re.search(r"(?:http(?:\s+error)?|status(?:\s+code)?)\s*[:=]?\s*([45]\d{2})\b", text)
    status = int(match.group(1)) if match else None
    if stage == AcquisitionStage.READINESS:
        reason = AcquisitionReason.NOT_READY
    elif stage == AcquisitionStage.ENUMERATION and any(word in text for word in ("quotaexceeded", "dailylimitexceeded", "quota exhausted")):
        reason = AcquisitionReason.QUOTA
    elif stage == AcquisitionStage.ENUMERATION and status in {401, 403}:
        reason = AcquisitionReason.API_ACCESS
    elif any(word in text for word in ("sign in to confirm", "confirm you’re not a bot", "confirm you're not a bot", "challenge solving", "javascript runtime", "signature solving", "n challenge", "po token")):
        reason = AcquisitionReason.CHALLENGE
    elif any(word in text for word in ("rate-limited", "rate limited", "this content isn't available, try again later")):
        reason = AcquisitionReason.RATE_LIMITED
    elif any(word in text for word in ("private video", "video unavailable", "has been removed", "deleted video", "not available in your country", "members-only")):
        reason = AcquisitionReason.UNAVAILABLE
    elif status == 403:
        # yt-dlp can reject the actual HTTP media request before invoking its
        # first progress hook. This explicit backend signal disambiguates it.
        if "download video data" in text:
            stage = AcquisitionStage.TRANSFER
        reason = AcquisitionReason.HTTP_FORBIDDEN
    elif status == 429:
        reason = AcquisitionReason.RATE_LIMITED
    elif status is not None and status >= 500:
        reason = AcquisitionReason.SERVER
    elif any(word in text for word in ("timed out", "timeout", "connection reset", "connection refused", "unable to connect", "name resolution", "getaddrinfo", "network is unreachable", "proxyerror")):
        reason = AcquisitionReason.NETWORK
    elif stage == AcquisitionStage.TRANSFORM or any(word in text for word in ("postprocessing:", "ffmpeg exited", "ffmpeg not found", "ffprobe not found")):
        stage, reason = AcquisitionStage.TRANSFORM, AcquisitionReason.TRANSFORMATION
    elif stage == AcquisitionStage.VERIFICATION:
        reason = AcquisitionReason.VERIFICATION
    elif stage == AcquisitionStage.IMPORT:
        reason = AcquisitionReason.IMPORT
    else:
        reason = AcquisitionReason.UNKNOWN
    return AcquisitionDiagnostic(stage, reason, status)


class AcquisitionCircuitBreaker:
    """Bound futile work without persisting failures as permanent item bans."""
    def __init__(self, threshold: int = 3):
        if threshold < 2:
            raise ValueError("A circuit requires evidence from at least two items.")
        self.threshold = threshold
        self.consecutive_failures = 0
        self.open = False
        self.last_diagnostic: AcquisitionDiagnostic | None = None
        self._failed_items: set[str] = set()

    def failure(self, item_identity: str, diagnostic: AcquisitionDiagnostic) -> bool:
        if self.open:
            return True
        if not diagnostic.systemic_candidate:
            self.success()
            return False
        if item_identity in self._failed_items:
            return False
        self._failed_items.add(item_identity)
        self.consecutive_failures += 1
        self.last_diagnostic = diagnostic
        self.open = self.consecutive_failures >= self.threshold
        return self.open

    def success(self) -> None:
        if not self.open:
            self.consecutive_failures = 0
            self._failed_items.clear()
            self.last_diagnostic = None
