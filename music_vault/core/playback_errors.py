from __future__ import annotations


def playback_error_message(title: str | None) -> str:
    clean_title = "".join(
        character for character in str(title or "").strip()
        if character not in "\r\n\t" and ord(character) >= 32
    )
    if clean_title:
        return f'Playback failed for "{clean_title[:160]}". Music Vault will try to continue.'
    return "Playback failed for the current track. Music Vault will try to continue."
