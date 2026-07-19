from __future__ import annotations

from pathlib import Path
from typing import Mapping

from music_vault.core.paths import discogs_token_path
from music_vault.core.runtime_policy import runtime_policy_for


METADATA_INTELLIGENCE_CONSENT_VERSION = 1
DISCOGS_CONSENT_VERSION = 1

METADATA_INTELLIGENCE_DEFAULTS = {
    "metadata_intelligence_enabled": False,
    "metadata_discogs_enabled": False,
    "metadata_musicbrainz_secondary_enabled": True,
    "metadata_writeback_enabled": False,
    "metadata_fill_missing_artwork_enabled": False,
    "metadata_scan_existing_after_setup": False,
    "metadata_intelligence_consent_version": 0,
    "metadata_discogs_consent_version": 0,
}


def normalize_metadata_intelligence_settings(config: Mapping[str, object]) -> dict:
    """Normalize every external-provider opt-in conservatively.

    Only literal JSON booleans enable provider work or file mutation. This
    prevents old, malformed, or hand-edited configuration from silently opting
    a user into a network lookup or media-tag write.
    """

    normalized = dict(METADATA_INTELLIGENCE_DEFAULTS)
    for name in (
        "metadata_intelligence_enabled",
        "metadata_discogs_enabled",
        "metadata_musicbrainz_secondary_enabled",
        "metadata_writeback_enabled",
        "metadata_fill_missing_artwork_enabled",
        "metadata_scan_existing_after_setup",
    ):
        default = bool(METADATA_INTELLIGENCE_DEFAULTS[name])
        value = config.get(name, default)
        normalized[name] = value is True
    for name in (
        "metadata_intelligence_consent_version",
        "metadata_discogs_consent_version",
    ):
        value = config.get(name, 0)
        if isinstance(value, bool):
            value = 0
        try:
            normalized[name] = max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            normalized[name] = 0

    consented = (
        normalized["metadata_intelligence_consent_version"]
        >= METADATA_INTELLIGENCE_CONSENT_VERSION
    )
    discogs_consented = (
        normalized["metadata_discogs_consent_version"] >= DISCOGS_CONSENT_VERSION
    )
    if not consented:
        normalized["metadata_intelligence_enabled"] = False
        normalized["metadata_writeback_enabled"] = False
        normalized["metadata_fill_missing_artwork_enabled"] = False
        normalized["metadata_scan_existing_after_setup"] = False
    if not discogs_consented:
        normalized["metadata_discogs_enabled"] = False
        normalized["metadata_fill_missing_artwork_enabled"] = False
    return normalized


class DiscogsTokenStore:
    """Local personal-token storage that never serializes into app config."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else discogs_token_path()

    @staticmethod
    def _secrets_disabled() -> bool:
        return not runtime_policy_for().secrets_allowed

    def read(self) -> str:
        if self._secrets_disabled():
            return ""
        try:
            return self.path.read_text(encoding="utf-8", errors="strict").strip()
        except (OSError, UnicodeError):
            return ""

    def configured(self) -> bool:
        return bool(self.read())

    def stored(self) -> bool:
        """Return whether a token file is stored without reading its contents.

        Startup/status surfaces only need a non-secret readiness hint.  Actual
        provider work continues to call :meth:`read`, where the token value is
        needed and the runtime policy is enforced again.
        """

        if self._secrets_disabled():
            return False
        try:
            return self.path.is_file() and self.path.stat().st_size > 0
        except OSError:
            return False

    def save(self, token: object) -> None:
        value = str(token or "").strip()
        if not value:
            raise ValueError("Enter a personal Discogs token before saving.")
        if "\n" in value or "\r" in value or len(value) > 512:
            raise ValueError("The Discogs token format is invalid.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        temporary.write_text(value + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def remove(self) -> bool:
        if not self.path.exists():
            return False
        self.path.unlink()
        return True
