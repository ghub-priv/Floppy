"""Request-time Kodi reconciliation hooks."""

import logging

from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET

from app.kodi_reconcile import reconcile_for_user

logger = logging.getLogger(__name__)


@login_required
@require_GET
def kodi_active_playback_fragment(request):
    """Reconcile Kodi before delegating to Floppy's normal playback fragment."""
    try:
        reconcile_for_user(request.user)
    except Exception:
        # Reconciliation is diagnostic recovery. A Kodi/network/cache failure
        # must never turn the normal home-page playback poll into a 500.
        logger.debug("Kodi playback reconciliation failed", exc_info=True)

    # Import lazily to avoid a config-time app.views -> URL-module cycle.
    from app.views import active_playback_fragment

    return active_playback_fragment(request)
