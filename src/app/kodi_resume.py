"""Deferred Floppy -> Kodi resume synchronisation."""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils import timezone

from app import live_playback
from app.kodi_client import KodiClient, KodiError
from app.kodi_state import KODI_CONTROL_BACKEND
from app.models import Item, MediaTypes, PlaybackProgress, Sources

logger = logging.getLogger(__name__)

PENDING_RESUME_PREFIX = "kodi_pending_resume_v1"
PENDING_RESUME_TTL_SECONDS = 5 * 60
MIN_RESUME_SECONDS = 30


def _cache_key(user_id: int) -> str:
    return f"{PENDING_RESUME_PREFIX}:{user_id}"


def clear_pending_resume(user_id: int) -> None:
    """Discard any queued Kodi resume for the user."""
    cache.delete(_cache_key(user_id))


def get_pending_resume(user_id: int) -> dict | None:
    """Return the queued resume request, if any."""
    pending = cache.get(_cache_key(user_id))
    return dict(pending) if pending else None


def _find_item(
    media_type: str,
    media_id: str,
    *,
    season_number: int | None = None,
    episode_number: int | None = None,
):
    query = Item.objects.filter(
        media_id=str(media_id),
        source=Sources.TMDB.value,
        media_type=media_type,
    )
    if media_type == MediaTypes.EPISODE.value:
        if season_number is None or episode_number is None:
            return None
        query = query.filter(
            season_number=season_number,
            episode_number=episode_number,
        )
    return query.first()


def queue_pending_resume(
    user,
    media_type: str,
    media_id: str,
    *,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> int | None:
    """Queue the user's saved Floppy position for the next Kodi playback."""
    clear_pending_resume(user.id)
    if media_type not in (MediaTypes.MOVIE.value, MediaTypes.EPISODE.value):
        return None

    try:
        item = _find_item(
            media_type,
            media_id,
            season_number=season_number,
            episode_number=episode_number,
        )
        if item is None:
            return None

        progress = PlaybackProgress.objects.filter(user=user, item=item).first()
        if progress is None or progress.completed:
            return None

        position = int(progress.position_seconds or 0)
        if position < MIN_RESUME_SECONDS:
            return None

        pending = {
            "media_type": media_type,
            "media_id": str(media_id),
            "source": Sources.TMDB.value,
            "season_number": season_number,
            "episode_number": episode_number,
            "position_seconds": position,
            "queued_at_ts": int(timezone.now().timestamp()),
        }
        cache.set(
            _cache_key(user.id),
            pending,
            timeout=PENDING_RESUME_TTL_SECONDS,
        )
        logger.info(
            "Queued Kodi resume: user=%s type=%s media_id=%s "
            "season=%s episode=%s position=%s",
            user.id,
            media_type,
            media_id,
            season_number,
            episode_number,
            position,
        )
    except Exception:
        logger.warning("Could not queue Kodi resume", exc_info=True)
        return None
    else:
        return position


def _matches_live_state(pending: dict, state: dict | None) -> bool:
    if not state:
        return False
    if state.get("control_backend") != KODI_CONTROL_BACKEND:
        return False
    if state.get("media_type") != pending.get("media_type"):
        return False
    if str(state.get("media_id") or "") != str(pending.get("media_id") or ""):
        return False
    if pending.get("media_type") == MediaTypes.EPISODE.value:
        if state.get("season_number") != pending.get("season_number"):
            return False
        if state.get("episode_number") != pending.get("episode_number"):
            return False
    return True


def apply_pending_resume(user) -> bool:
    """Seek Kodi when the live webhook state matches a queued resume."""
    pending = get_pending_resume(user.id)
    if not pending:
        return False

    state = live_playback.get_user_playback_state(user.id)
    if not _matches_live_state(pending, state):
        return False

    try:
        kodi = KodiClient.from_env()
        players = [
            player
            for player in kodi.get_active_players()
            if str(player.get("type") or "").lower() == "video"
        ]
        if not players:
            return False

        player_id = int(players[0]["playerid"])
        position = int(pending["position_seconds"])
        kodi.seek_seconds(player_id, position)
        clear_pending_resume(user.id)
        logger.info(
            "Applied Kodi resume: user=%s player=%s position=%s",
            user.id,
            player_id,
            position,
        )
    except KodiError as exc:
        logger.debug("Kodi resume not ready for user=%s: %s", user.id, exc)
        return False
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Invalid pending Kodi resume state: %s", exc)
        clear_pending_resume(user.id)
        return False
    else:
        return True
