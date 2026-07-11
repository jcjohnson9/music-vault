from __future__ import annotations

from pathlib import Path

from .artwork import CoverArtArchiveProvider


def download_front_cover(
    release_id: str,
    output_dir: str | Path | None = None,
) -> str | None:
    """Compatibility wrapper for explicit, validated Cover Art Archive apply.

    ``output_dir`` is intentionally rejected: permanent provider artwork is
    always stored in Music Vault's ignored content-addressed runtime cache.
    """

    if output_dir is not None:
        raise ValueError("Cover artwork storage is managed by Music Vault.")
    result = CoverArtArchiveProvider().fetch_and_store(release_id)
    return str(result) if result is not None else None
