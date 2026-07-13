"""Authoritative Music Vault product-version metadata."""

from __future__ import annotations


APP_NAME = "Music Vault"
APP_VERSION = "1.0.0"
RELEASE_CHANNEL = "stable"
DISPLAY_VERSION = f"v{APP_VERSION}"
WINDOWS_VERSION = (1, 0, 0, 0)
PUBLISHER = "Jeremy Johnson"
ORIGINAL_FILENAME = "MusicVault.exe"
PROJECT_URL = "https://github.com/jcjohnson9/music-vault"


def user_agent(product: str = "MusicVault") -> str:
    """Return the public, non-identifying user agent used by metadata providers."""
    return f"{product}/{APP_VERSION} ({PROJECT_URL})"
