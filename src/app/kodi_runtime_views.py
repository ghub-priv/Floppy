"""Request-time Kodi reconciliation hooks."""

from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET

from app.kodi_reconcile import reconcile_for_user


@login_required
@require_GET
def kodi_active_playback_fragment(request):
    """Reconcile Kodi before delegating to Floppy's normal playback fragment."""
    reconcile_for_user(request.user)

    # Import lazily to avoid a config-time app.views -> URL-module cycle.
    from app.views import active_playback_fragment

    return active_playback_fragment(request)
