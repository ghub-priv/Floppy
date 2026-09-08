"""Template helpers for Kodi-specific controls on shared playback UI."""

from django import template

from app import live_playback
from app.kodi_state import KODI_CONTROL_BACKEND

register = template.Library()


@register.simple_tag
def kodi_controls_available(user) -> bool:
    """Show controls only when the currently-rendered session belongs to Kodi."""
    if not getattr(user, "is_authenticated", False):
        return False

    state = live_playback.get_user_playback_state(user.id)
    return bool(
        state
        and state.get("control_backend") == KODI_CONTROL_BACKEND
        and state.get("status") != live_playback.PLAYBACK_STATUS_STOPPED
    )
