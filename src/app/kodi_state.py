"""Kodi-specific provenance for Floppy's shared live-playback cache."""

from __future__ import annotations

from app import live_playback

KODI_CONTROL_BACKEND = "kodi"


def mark_kodi_state(user_id: int) -> bool:
    """Mark the current live-playback state as Kodi-controlled."""
    state = live_playback.get_user_playback_state(user_id)
    if not state:
        return False

    # get_user_playback_state() adds a derived value that must never become
    # part of the persisted cache state.
    state.pop("estimated_progress_seconds", None)
    state["control_backend"] = KODI_CONTROL_BACKEND
    live_playback.set_user_playback_state(user_id, state)
    return True


def is_kodi_state(user_id: int) -> bool:
    """Return whether the user's current active live session is Kodi-owned."""
    state = live_playback.get_user_playback_state(user_id)
    return bool(
        state
        and state.get("control_backend") == KODI_CONTROL_BACKEND
        and state.get("status") != live_playback.PLAYBACK_STATUS_STOPPED
    )
