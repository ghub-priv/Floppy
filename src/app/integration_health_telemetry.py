"""Best-effort telemetry used by the Integration Health Centre.

This module is deliberately tiny and side-effect free apart from cache writes.
Integration processing must never depend on health telemetry succeeding.
"""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

TELEMETRY_TIMEOUT = 30 * 24 * 60 * 60
KODI_LAST_RECEIVED_KEY = "integration-health:kodi:last-received:{user_id}"
KODI_LAST_SUCCESS_KEY = "integration-health:kodi:last-success:{user_id}"
MDBLIST_LAST_RATING_KEY = "integration-health:mdblist:last-rating:{user_id}"


def _key(template: str, user_id: int) -> str:
    return template.format(user_id=user_id)


def record_integration_health_event(user, payload, outcome: str) -> None:
    """Record non-sensitive Kodi/MDBList activity without affecting processing."""
    try:
        is_rating = "rating" in payload
        data = {
            "recorded_at": timezone.now().isoformat(),
            "outcome": outcome,
            "kind": "rating" if is_rating else "playback",
            "event": payload.get("event"),
            "media_type": payload.get("mediaType"),
            "title": payload.get("title"),
        }
        if is_rating:
            data["rating"] = payload.get("rating")

        cache.set(
            _key(KODI_LAST_RECEIVED_KEY, user.id),
            data,
            timeout=TELEMETRY_TIMEOUT,
        )
        if outcome == "success":
            cache.set(
                _key(KODI_LAST_SUCCESS_KEY, user.id),
                data,
                timeout=TELEMETRY_TIMEOUT,
            )
            if is_rating:
                cache.set(
                    _key(MDBLIST_LAST_RATING_KEY, user.id),
                    data,
                    timeout=TELEMETRY_TIMEOUT,
                )
    except Exception:
        # Diagnostics are observational and must never interfere with a webhook.
        logger.debug("Could not record Integration Health telemetry", exc_info=True)


def get_integration_health_telemetry(user_id: int) -> dict[str, object | None]:
    """Return the current user's cached Integration Health event snapshots."""
    keys = {
        "kodi_last_received": _key(KODI_LAST_RECEIVED_KEY, user_id),
        "kodi_last_success": _key(KODI_LAST_SUCCESS_KEY, user_id),
        "mdblist_last_rating": _key(MDBLIST_LAST_RATING_KEY, user_id),
    }
    try:
        values = cache.get_many(keys.values())
    except Exception:
        logger.debug("Could not read Integration Health telemetry", exc_info=True)
        return {name: None for name in keys}
    return {name: values.get(cache_key) for name, cache_key in keys.items()}
