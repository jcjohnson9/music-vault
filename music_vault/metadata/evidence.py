"""Bounded catalogue evidence, without transport, SQL, or acceptance authority.

Provider candidates keep their compatibility fields. ``normalize_candidate``
reduces those fields to immutable, provider-qualified facts. ``to_dict`` and
``from_dict`` round-trip only this versioned allowlist; arbitrary responses,
URLs, filesystem paths, and acquisition timestamps are not persisted here.
``evidence_key`` hashes the canonical facts, so retrieval retries are identical.
Artist joins are converted from provider suffixes to prefix joins exactly once.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
from typing import Any, Mapping

from .schema import normalize_release_date


_PROVIDERS = frozenset({"discogs", "musicbrainz", "unknown"})
_KINDS = frozenset({"recording", "release_family", "edition", "artist"})
_FIELDS = frozenset({"title", "artist", "album", "album_artist", "version_type", "version_label", "duration_seconds"})
_ROLES = frozenset({"primary", "featured", "collaborator", "remixer", "performer"})
_ENTITY_TYPES = frozenset({"person", "group", "band", "duo", "orchestra", "fictional", "collective", "unknown"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PATH = re.compile(r"(?:^[A-Za-z]:[\\/]|^[/\\]|file:|https?://)", re.IGNORECASE)


def _text(value: object, maximum: int = 512, *, empty: bool = False, join: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("Invalid bounded evidence text.")
    if (not empty and not value.strip()) or (_PATH.search(value) and not (join and value.strip() == "/")) or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid evidence text.")
    return value


def _tuple(value: object, maximum: int, kind: type) -> None:
    if not isinstance(value, tuple) or len(value) > maximum or any(not isinstance(item, kind) for item in value):
        raise ValueError("Invalid evidence collection.")


@dataclass(frozen=True)
class ProviderIdentity:
    provider: str
    entity_kind: str
    entity_id: str

    def __post_init__(self) -> None:
        if self.provider not in _PROVIDERS - {"unknown"} or self.entity_kind not in _KINDS:
            raise ValueError("Unknown catalogue identity namespace.")
        if not isinstance(self.entity_id, str) or not _ID.fullmatch(self.entity_id):
            raise ValueError("Invalid catalogue identifier.")


@dataclass(frozen=True)
class CreditEvidence:
    canonical_name: str | None
    credited_as: str
    identities: tuple[ProviderIdentity, ...] = ()
    role: str = "primary"
    role_basis: str = "provider_credit"
    order: int = 0
    prefix_join: str = ""
    entity_type: str = "unknown"

    def __post_init__(self) -> None:
        if self.canonical_name is not None:
            _text(self.canonical_name)
        _text(self.credited_as)
        _text(self.prefix_join, 80, empty=True, join=True)
        _tuple(self.identities, 2, ProviderIdentity)
        if any(identity.entity_kind != "artist" for identity in self.identities):
            raise ValueError("Credit identity must identify an artist.")
        if len({identity.provider for identity in self.identities}) != len(self.identities):
            raise ValueError("Conflicting credit provider identities.")
        if self.role not in _ROLES or self.role_basis not in {"provider_credit", "provider_join", "unspecified"}:
            raise ValueError("Invalid credit role.")
        if type(self.order) is not int or not 0 <= self.order < 64 or self.entity_type not in _ENTITY_TYPES:
            raise ValueError("Invalid credit order or entity type.")


@dataclass(frozen=True)
class FieldAssertion:
    field_name: str
    value: str | float
    subject: ProviderIdentity | None = None

    def __post_init__(self) -> None:
        if self.field_name not in _FIELDS or (self.subject is not None and not isinstance(self.subject, ProviderIdentity)):
            raise ValueError("Invalid evidence field.")
        if self.field_name == "duration_seconds":
            if type(self.value) not in (int, float) or not math.isfinite(self.value) or not 0 < self.value <= 86400:
                raise ValueError("Invalid duration evidence.")
        else:
            _text(self.value)


@dataclass(frozen=True)
class DateAssertion:
    value: str
    precision: str
    meaning: str
    subject: ProviderIdentity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or normalize_release_date(self.value) != self.value:
            raise ValueError("Invalid date evidence.")
        if self.precision != {4: "year", 7: "month", 10: "day"}.get(len(self.value)):
            raise ValueError("Date precision does not match value.")
        if self.meaning not in {"edition_release", "recording_first_release", "family_first_release"}:
            raise ValueError("Invalid date meaning.")
        if self.subject is not None and not isinstance(self.subject, ProviderIdentity):
            raise ValueError("Invalid date subject.")
        expected_kind = {"edition_release": "edition", "recording_first_release": "recording", "family_first_release": "release_family"}[self.meaning]
        if self.subject is not None and self.subject.entity_kind != expected_kind:
            raise ValueError("Date meaning does not match subject.")


def _reference(identity: ProviderIdentity | None) -> str | None:
    if identity is None:
        return None
    if identity.provider == "musicbrainz":
        category = {"edition": "release", "release_family": "release-group", "recording": "recording", "artist": "artist"}[identity.entity_kind]
        return f"https://musicbrainz.org/{category}/{identity.entity_id}"
    category = {"edition": "release", "release_family": "master", "artist": "artist"}.get(identity.entity_kind)
    return f"https://www.discogs.com/{category}/{identity.entity_id}" if category else None


@dataclass(frozen=True)
class MetadataEvidence:
    provider: str
    adapter_version: str
    recording: ProviderIdentity | None = None
    release_family: ProviderIdentity | None = None
    edition: ProviderIdentity | None = None
    fields: tuple[FieldAssertion, ...] = ()
    dates: tuple[DateAssertion, ...] = ()
    recording_credits: tuple[CreditEvidence, ...] = ()
    release_credits: tuple[CreditEvidence, ...] = ()
    reference: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.provider not in _PROVIDERS or type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported evidence schema or provider.")
        _text(self.adapter_version, 64)
        for name, kind in (("recording", "recording"), ("release_family", "release_family"), ("edition", "edition")):
            identity = getattr(self, name)
            if identity is not None and (not isinstance(identity, ProviderIdentity) or identity.entity_kind != kind or identity.provider != self.provider):
                raise ValueError("Evidence identity namespace mismatch.")
        _tuple(self.fields, 16, FieldAssertion)
        _tuple(self.dates, 8, DateAssertion)
        subjects = {value for value in (self.recording, self.release_family, self.edition) if value is not None}
        if any(item.subject is not None and item.subject not in subjects for item in (*self.fields, *self.dates)):
            raise ValueError("Evidence assertion has an unrelated subject.")
        for credits in (self.recording_credits, self.release_credits):
            _tuple(credits, 64, CreditEvidence)
            if tuple(credit.order for credit in credits) != tuple(range(len(credits))):
                raise ValueError("Credit order must be contiguous.")
        if self.reference is not None and self.reference not in {_reference(value) for value in (self.recording, self.release_family, self.edition)}:
            raise ValueError("Untrusted evidence reference.")

    @property
    def evidence_key(self) -> str:
        payload = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return "metadata-evidence-v1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-native normalized facts and their content identity."""
        result = json.loads(json.dumps(asdict(self), ensure_ascii=False, allow_nan=False))
        result["evidence_key"] = self.evidence_key
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MetadataEvidence:
        """Validate a serialized allowlist; reject unknown keys and tampering."""
        if not isinstance(value, Mapping):
            raise ValueError("Evidence must be an object.")
        try:
            if len(json.dumps(value, allow_nan=False)) > 131072:
                raise ValueError("Evidence exceeds storage bound.")
            data = dict(value)
            key = data.pop("evidence_key", None)
            def identity(raw):
                return ProviderIdentity(**raw) if raw is not None else None
            for name in ("recording", "release_family", "edition"):
                data[name] = identity(data.get(name))
            for name, constructor in (("fields", FieldAssertion), ("dates", DateAssertion)):
                data[name] = tuple(constructor(**{**raw, "subject": identity(raw.get("subject"))}) for raw in data.get(name, ()))
            for name in ("recording_credits", "release_credits"):
                data[name] = tuple(CreditEvidence(**{**raw, "identities": tuple(identity(item) for item in raw.get("identities", ()))}) for raw in data.get(name, ()))
            result = cls(**data)
            if key is not None and key != result.evidence_key:
                raise ValueError("Evidence content identity mismatch.")
            return result
        except (TypeError, KeyError, AttributeError, OverflowError) as exc:
            raise ValueError("Malformed normalized evidence.") from exc


def _get(value: object, name: str, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _identity(provider: str, kind: str, value: object) -> ProviderIdentity | None:
    if value in (None, "") or provider == "unknown":
        return None
    return ProviderIdentity(provider, kind, str(value))


def _credits(values: object, provider: str) -> tuple[CreditEvidence, ...]:
    if not isinstance(values, (tuple, list)) or len(values) > 64:
        raise ValueError("Invalid provider credits.")
    result: list[CreditEvidence] = []
    previous_join = ""
    for credit in values:
        name = _get(credit, "credited_as") or _get(credit, "name") or _get(credit, "display_name")
        if not name:
            continue
        namespace = str(_get(credit, "provider") or provider).casefold()
        if namespace not in _PROVIDERS or (namespace != provider and provider != "unknown"):
            raise ValueError("Credit provider namespace mismatch.")
        identity = _identity(namespace, "artist", _get(credit, "artist_id"))
        role = _get(credit, "role", "primary")
        result.append(CreditEvidence(
            canonical_name=_get(credit, "canonical_name"), credited_as=name,
            identities=(identity,) if identity else (), role=role,
            role_basis="provider_join" if provider == "discogs" and role in {"featured", "collaborator"} else "provider_credit",
            order=len(result), prefix_join=previous_join,
            entity_type=_get(credit, "entity_type", "unknown"),
        ))
        previous_join = _get(credit, "join_phrase", "")
        _text(previous_join, 80, empty=True, join=True)
    return tuple(result)


def normalize_candidate(candidate: object) -> MetadataEvidence:
    """Adapt known candidate facts without lookup, inference, or writes.

    Discogs master-year is family evidence, not recording-original evidence.
    A MusicBrainz recording first-release-date and release-group first date
    remain separate. Compatibility objects with no provider stay unknown.
    """
    provider = str(_get(candidate, "provider", "unknown")).casefold()
    if provider not in _PROVIDERS:
        provider = "unknown"
    recording = _identity(provider, "recording", _get(candidate, "recording_id"))
    edition = _identity(provider, "edition", _get(candidate, "release_id"))
    family = _identity(provider, "release_family", _get(candidate, "release_group_id") if provider == "musicbrainz" else _get(candidate, "master_id") if provider == "discogs" else None)
    fields = []
    for name in sorted(_FIELDS):
        value = _get(candidate, name)
        if value not in (None, ""):
            subject = edition if name in {"album", "album_artist"} else recording
            fields.append(FieldAssertion(name, value, subject))
    dates = []
    assertions = [(_get(candidate, "release_date"), "edition_release", edition)]
    if provider == "musicbrainz":
        assertions.extend(((_get(candidate, "original_release_date"), "recording_first_release", recording), (_get(candidate, "release_group_first_release_date"), "family_first_release", family)))
    elif provider == "discogs":
        assertions.append((_get(candidate, "original_release_date"), "family_first_release", family))
    for value, meaning, subject in assertions:
        if value not in (None, ""):
            dates.append(DateAssertion(value, {4: "year", 7: "month", 10: "day"}.get(len(value), "unknown"), meaning, subject))
    return MetadataEvidence(
        provider=provider, adapter_version=f"{provider}-candidate-v1",
        recording=recording, release_family=family, edition=edition,
        fields=tuple(fields), dates=tuple(dates),
        recording_credits=_credits(_get(candidate, "artist_credits", ()) or (), provider),
        release_credits=_credits(_get(candidate, "album_artist_credits", ()) or (), provider),
        reference=_reference(edition or recording or family),
    )
