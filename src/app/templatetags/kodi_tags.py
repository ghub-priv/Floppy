"""Template helpers for Kodi-specific controls on shared playback UI."""

from django import template

from app.kodi_state import is_kodi_state

register = template.Library()


@register.simple_tag
def kodi_controls_available(user) -> bool:
    """Show controls only when the currently-rendered session belongs to Kodi."""
    if not getattr(user, "is_authenticated", False):
        return False
    return is_kodi_state(user.id)
