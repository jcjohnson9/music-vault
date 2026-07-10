from __future__ import annotations

from pathlib import Path
import requests

from music_vault.core.paths import covers_dir


def download_front_cover(release_id: str, output_dir: str | Path | None = None) -> str | None:
    """
    Downloads front cover art from Cover Art Archive for a MusicBrainz release ID.
    Returns the local file path, or None if unavailable.
    """
    output_dir = Path(output_dir) if output_dir is not None else covers_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    url = f"https://coverartarchive.org/release/{release_id}/front-500"
    response = requests.get(url, timeout=20)
    if response.status_code != 200:
        return None

    out_path = output_dir / f"{release_id}.jpg"
    out_path.write_bytes(response.content)
    return str(out_path.resolve())
