from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import musicbrainzngs


@dataclass
class MetadataCandidate:
    title: str
    artist: str
    album: str | None
    year: str | None
    recording_id: str | None
    release_id: str | None
    score: int


def configure_musicbrainz() -> None:
    # MusicBrainz asks API clients to identify themselves with a meaningful user agent.
    musicbrainzngs.set_useragent(
        "MusicVault",
        "0.1",
        "local-personal-app"
    )
    musicbrainzngs.set_rate_limit(1.0, 1)


def search_recording(title: str, artist: str | None = None) -> list[MetadataCandidate]:
    configure_musicbrainz()

    query = f'recording:"{title}"'
    if artist:
        query += f' AND artist:"{artist}"'

    result = musicbrainzngs.search_recordings(
        query=query,
        limit=5
    )

    candidates: list[MetadataCandidate] = []
    for rec in result.get("recording-list", []):
        rec_title = rec.get("title") or title
        rec_id = rec.get("id")
        score = int(rec.get("ext:score", 0))

        artist_name = artist or ""
        credits = rec.get("artist-credit", [])
        if credits and isinstance(credits[0], dict):
            artist_name = credits[0].get("artist", {}).get("name", artist_name)

        album = None
        year = None
        release_id = None
        releases = rec.get("release-list", [])
        if releases:
            release = releases[0]
            album = release.get("title")
            year = release.get("date", "")[:4] or None
            release_id = release.get("id")

        candidates.append(MetadataCandidate(
            title=rec_title,
            artist=artist_name,
            album=album,
            year=year,
            recording_id=rec_id,
            release_id=release_id,
            score=score
        ))

    return candidates
