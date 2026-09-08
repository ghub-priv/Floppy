"""Kodi JSON-RPC reconciliation for Floppy Now Playing state."""

from __future__ import annotations

import logging
import os

from django.core.cache import cache
from django.utils import timezone

from app import live_playback
from app.kodi_client import KodiClient, KodiError
from app.kodi_state import KODI_CONTROL_BACKEND, mark_kodi_state
from app.models import MediaTypes, Sources

logger = logging.getLogger(__name__)

RECONCILE_STALE_SECONDS = 25
RECONCILE_GUARD_SECONDS = 15
RECONCILE_GUARD_PREFIX = "kodi_reconcile_v1"
COLD_RECOVERY_GUARD_SECONDS = 20
COLD_RECOVERY_GUARD_PREFIX = "kodi_cold_recover_v1"
MILLISECONDS_ROUND_THRESHOLD = 500


def _coerce_int(value, default=None):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _time_to_seconds(value) -> int | None:
    if not isinstance(value, dict):
        return None
    hours = _coerce_int(value.get("hours"), 0)
    minutes = _coerce_int(value.get("minutes"), 0)
    seconds = _coerce_int(value.get("seconds"), 0)
    milliseconds = _coerce_int(value.get("milliseconds"), 0)
    if any(part is None for part in (hours, minutes, seconds, milliseconds)):
        return None
    total = max(0, hours) * 3600 + max(0, minutes) * 60 + max(0, seconds)
    if milliseconds >= MILLISECONDS_ROUND_THRESHOLD:
        total += 1
    return total


def _normalise_unique_ids(item: dict) -> dict[str, str]:
    unique_ids = item.get("uniqueid") or {}
    if not isinstance(unique_ids, dict):
        return {}
    return {
        str(key).strip().lower(): str(value).strip()
        for key, value in unique_ids.items()
        if value not in (None, "")
    }


def _same_text(left, right) -> bool:
    left = str(left or "").strip().casefold()
    right = str(right or "").strip().casefold()
    return bool(left and right and left == right)


def _item_matches_state(state: dict, item: dict) -> bool:
    """Conservatively confirm Kodi is playing the cached Floppy item."""
    state_type = state.get("media_type")
    item_type = str(item.get("type") or "").strip().lower()

    if state_type == MediaTypes.MOVIE.value:
        if item_type and item_type != "movie":
            return False
        state_tmdb = str(state.get("media_id") or "").strip()
        item_tmdb = _normalise_unique_ids(item).get("tmdb")
        if state_tmdb and item_tmdb:
            return state_tmdb == item_tmdb
        return _same_text(state.get("title"), item.get("title"))

    if state_type == MediaTypes.EPISODE.value:
        if item_type and item_type != "episode":
            return False
        state_season = _coerce_int(state.get("season_number"))
        state_episode = _coerce_int(state.get("episode_number"))
        item_season = _coerce_int(item.get("season"))
        item_episode = _coerce_int(item.get("episode"))
        if any(
            value is None
            for value in (state_season, state_episode, item_season, item_episode)
        ):
            return False
        if state_season != item_season or state_episode != item_episode:
            return False
        # Never match SxxExx alone across unrelated programmes.
        return _same_text(state.get("series_title"), item.get("showtitle"))

    return False


def _apply_cached_stop(user_id: int, state: dict) -> None:
    live_playback.apply_playback_event(
        user_id=user_id,
        event_type="media.stop",
        playback_media_type=state.get("media_type"),
        media_id=state.get("media_id"),
        source=state.get("source") or Sources.TMDB.value,
        rating_key=state.get("rating_key"),
        title=state.get("title"),
        series_title=state.get("series_title"),
        episode_title=state.get("episode_title"),
        season_number=_coerce_int(state.get("season_number")),
        episode_number=_coerce_int(state.get("episode_number")),
        view_offset_seconds=_coerce_int(
            state.get("estimated_progress_seconds"),
            _coerce_int(state.get("view_offset_seconds"), 0),
        ),
        duration_seconds=_coerce_int(state.get("duration_seconds"), 0),
        store_progress=False,
    )


def _configured_floppy_user_id() -> int | None:
    raw_value = str(os.getenv("KODI_FLOPPY_USER_ID") or "").strip()
    if not raw_value:
        return None
    try:
        user_id = int(raw_value)
    except (TypeError, ValueError):
        return None
    return user_id if user_id > 0 else None


def _recover_cold_state(user) -> bool:
    """Create Now Playing state when Kodi is active but Floppy has none."""
    configured_user_id = _configured_floppy_user_id()
    if configured_user_id is None or int(user.id) != configured_user_id:
        return False

    guard_key = f"{COLD_RECOVERY_GUARD_PREFIX}:{user.id}"
    if not cache.add(guard_key, True, timeout=COLD_RECOVERY_GUARD_SECONDS):
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
        properties = kodi.get_player_properties(player_id)
        item = kodi.get_player_item(player_id)
        item_type = str(item.get("type") or "").strip().lower()

        media_type = media_id = title = series_title = episode_title = None
        season_number = episode_number = None

        if item_type == "movie":
            media_id = _normalise_unique_ids(item).get("tmdb")
            if not media_id:
                logger.info("Kodi cold recovery skipped movie without TMDB ID")
                return False
            media_type = MediaTypes.MOVIE.value
            title = str(item.get("title") or "").strip() or None
        elif item_type == "episode":
            season_number = _coerce_int(item.get("season"))
            episode_number = _coerce_int(item.get("episode"))
            series_title = str(item.get("showtitle") or "").strip() or None
            episode_title = str(item.get("title") or "").strip() or None
            tvshow_id = _coerce_int(item.get("tvshowid"))
            if (
                season_number is None
                or episode_number is None
                or not series_title
                or tvshow_id is None
                or tvshow_id < 0
            ):
                return False
            media_id = _normalise_unique_ids(kodi.get_tvshow_details(tvshow_id)).get(
                "tmdb"
            )
            if not media_id:
                logger.info("Kodi cold recovery skipped TV show without TMDB ID")
                return False
            media_type = MediaTypes.EPISODE.value
            title = episode_title
        else:
            return False

        offset_seconds = _time_to_seconds(properties.get("time"))
        duration_seconds = _time_to_seconds(properties.get("totaltime"))
        speed = _coerce_int(properties.get("speed"), 0)
        status = (
            live_playback.PLAYBACK_STATUS_PAUSED
            if speed == 0
            else live_playback.PLAYBACK_STATUS_PLAYING
        )
        event_type = (
            "media.pause"
            if status == live_playback.PLAYBACK_STATUS_PAUSED
            else "media.play"
        )
        now_ts = int(timezone.now().timestamp())
        state = {
            "event_type": event_type,
            "media_type": media_type,
            "media_id": str(media_id),
            "source": Sources.TMDB.value,
            "rating_key": None,
            "title": title,
            "series_title": series_title,
            "episode_title": episode_title,
            "season_number": season_number,
            "episode_number": episode_number,
            "view_offset_seconds": max(0, offset_seconds or 0),
            "duration_seconds": max(0, duration_seconds or 0),
            "started_at_ts": now_ts,
            "status": status,
            "updated_at_ts": now_ts,
            "expires_at_ts": now_ts + live_playback.PLAYBACK_HARD_STALE_SECONDS,
            "pause_expires_at_ts": None,
            "scrobble_expires_at_ts": None,
            "control_backend": KODI_CONTROL_BACKEND,
        }
        if status == live_playback.PLAYBACK_STATUS_PAUSED:
            state["pause_expires_at_ts"] = (
                now_ts + live_playback.PLAYBACK_PAUSE_STALE_SECONDS
            )

        # Request-time cold recovery writes only known Kodi facts to cache.
        # Provider/image resolution remains in Floppy's background path.
        live_playback.set_user_playback_state(user.id, state)
        logger.info(
            "Kodi cold recovery created Now Playing state "
            "user=%s player=%s type=%s media_id=%s",
            user.id,
            player_id,
            media_type,
            media_id,
        )
        return True
    except KodiError as exc:
        logger.debug("Kodi cold recovery unavailable for user=%s: %s", user.id, exc)
        return False
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Kodi cold recovery received unexpected data: %s", exc)
        return False


def reconcile_for_user(user) -> bool:
    """Repair stale Kodi-backed Now Playing state."""
    state = live_playback.get_user_playback_state(user.id)
    if not state:
        return _recover_cold_state(user)

    # live_playback is shared by Plex/Jellyfin. Never interrogate Kodi for a
    # session owned by another integration.
    if state.get("control_backend") != KODI_CONTROL_BACKEND:
        return False
    if state.get("source") != Sources.TMDB.value:
        return False
    if state.get("media_type") not in (
        MediaTypes.MOVIE.value,
        MediaTypes.EPISODE.value,
    ):
        return False
    if state.get("status") == live_playback.PLAYBACK_STATUS_STOPPED:
        return False

    now_ts = int(timezone.now().timestamp())
    updated_at_ts = _coerce_int(state.get("updated_at_ts"), now_ts)
    if now_ts - updated_at_ts < RECONCILE_STALE_SECONDS:
        return False

    guard_key = f"{RECONCILE_GUARD_PREFIX}:{user.id}"
    if not cache.add(guard_key, True, timeout=RECONCILE_GUARD_SECONDS):
        return False

    try:
        kodi = KodiClient.from_env()
        players = [
            player
            for player in kodi.get_active_players()
            if str(player.get("type") or "").lower() == "video"
        ]
        if not players:
            _apply_cached_stop(user.id, state)
            return True

        player_id = int(players[0]["playerid"])
        properties = kodi.get_player_properties(player_id)
        item = kodi.get_player_item(player_id)
        if not _item_matches_state(state, item):
            _apply_cached_stop(user.id, state)
            return True

        speed = _coerce_int(properties.get("speed"), 0)
        event_type = "media.pause" if speed == 0 else "media.play"
        offset_seconds = _time_to_seconds(properties.get("time"))
        duration_seconds = _time_to_seconds(properties.get("totaltime"))

        live_playback.apply_playback_event(
            user_id=user.id,
            event_type=event_type,
            playback_media_type=state.get("media_type"),
            media_id=state.get("media_id"),
            source=state.get("source") or Sources.TMDB.value,
            rating_key=state.get("rating_key"),
            title=state.get("title"),
            series_title=state.get("series_title"),
            episode_title=state.get("episode_title"),
            season_number=_coerce_int(state.get("season_number")),
            episode_number=_coerce_int(state.get("episode_number")),
            view_offset_seconds=offset_seconds,
            duration_seconds=duration_seconds,
            store_progress=False,
        )
        mark_kodi_state(user.id)
        logger.info(
            "Kodi reconciliation repaired playback state "
            "user=%s player=%s status=%s offset=%s",
            user.id,
            player_id,
            event_type,
            offset_seconds,
        )
        return True
    except KodiError as exc:
        logger.debug("Kodi reconciliation unavailable for user=%s: %s", user.id, exc)
        return False
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Kodi reconciliation received unexpected player data: %s", exc)
        return False
