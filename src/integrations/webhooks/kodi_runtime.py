"""Kodi Control + Sync v2.7 webhook behaviour.

This module deliberately layers Kodi-specific runtime behaviour over the current
BaseWebhookProcessor instead of replacing its provider/anime resolution logic.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from app import history_cache, live_playback
from app.kodi_resume import apply_pending_resume, clear_pending_resume
from app.kodi_state import KODI_CONTROL_BACKEND, mark_kodi_state
from app.models import Episode, Item, MediaTypes, Movie, Season, Sources
from app.providers import tmdb
from app.services.completion import select_preferred_activity_entry

logger = logging.getLogger(__name__)

PERCENT_COMPLETE_THRESHOLD = 80


class KodiEvent(StrEnum):
    """Kodi HTTP Scrobbler event names used by the v2.7 runtime."""

    PLAYBACK_START = "start"
    PLAYBACK_PAUSE = "pause"
    PLAYBACK_RESUME = "resume"
    PLAYBACK_SEEK = "seek"
    PLAYBACK_INTERVAL = "interval"
    PLAYBACK_STOP = "stop"
    PLAYBACK_END = "end"


KODI_LIVE_EVENT_MAP = {
    KodiEvent.PLAYBACK_START: "media.play",
    KodiEvent.PLAYBACK_PAUSE: "media.pause",
    KodiEvent.PLAYBACK_RESUME: "media.resume",
    KodiEvent.PLAYBACK_SEEK: "media.play",
    KodiEvent.PLAYBACK_INTERVAL: "media.play",
    KodiEvent.PLAYBACK_STOP: "media.stop",
    KodiEvent.PLAYBACK_END: "media.scrobble",
}

KODI_LIVE_ONLY_EVENTS = {
    KodiEvent.PLAYBACK_PAUSE,
    KodiEvent.PLAYBACK_RESUME,
    KodiEvent.PLAYBACK_SEEK,
    KodiEvent.PLAYBACK_INTERVAL,
}

# Interval is intentionally absent. HTTP Scrobbler heartbeats are cache-only;
# otherwise the normal 10-second interval becomes a 10-second database write.
KODI_DURABLE_PROGRESS_EVENT_MAP = {
    KodiEvent.PLAYBACK_PAUSE: "media.pause",
    # A seek is a checkpoint, not a completion signal. Reuse pause semantics
    # only for the durable writer while live state remains playing/paused.
    KodiEvent.PLAYBACK_SEEK: "media.pause",
    KodiEvent.PLAYBACK_STOP: "media.stop",
    KodiEvent.PLAYBACK_END: "media.scrobble",
}

KODI_RESUME_TRIGGER_EVENTS = {
    KodiEvent.PLAYBACK_START,
    KodiEvent.PLAYBACK_RESUME,
    KodiEvent.PLAYBACK_INTERVAL,
}


def _coerce_seconds(value):
    if value is None:
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def _invalidate_activity_days(user_id: int, end_dates) -> None:
    day_keys = {
        history_cache.history_day_key(end_date)
        for end_date in end_dates
        if end_date is not None
    }
    day_keys.discard(None)
    if day_keys:
        history_cache.invalidate_history_days(
            user_id,
            day_keys=sorted(day_keys),
            logging_styles=("sessions", "repeats"),
            reason="kodi_rating_change",
        )


class KodiRuntimeMixin:
    """Kodi-specific runtime layer mixed into KodiWebhookProcessor."""

    def process_payload(self, payload, user):
        """Process one Kodi playback or rating webhook payload."""
        # MDBList Scrobbler posts rating-only payloads through this webhook.
        # Handle them before playback so a rating can never alter Now Playing,
        # progress, watched state, or episode/season reconciliation.
        if "rating" in payload:
            self._process_rating(payload, user)
            return

        event_type = payload.get("event")
        if not self._is_supported_event(event_type):
            logger.debug("Ignoring Kodi webhook event type: %s", event_type)
            return

        ids = self._extract_external_ids(payload)
        logger.info("Extracted IDs from Kodi payload: %s", ids)

        # Live state comes first. Pause/resume/seek/interval payloads can omit
        # useful external IDs but still belong to the existing Kodi session.
        self._update_live_playback_state(payload, user, ids)

        if event_type in KODI_RESUME_TRIGGER_EVENTS:
            # The first start can arrive while TMDb Helper is still resolving
            # the actual stream. Failed seeks remain queued for a later interval.
            apply_pending_resume(user)
        elif event_type in {
            KodiEvent.PLAYBACK_SEEK,
            KodiEvent.PLAYBACK_STOP,
            KodiEvent.PLAYBACK_END,
        }:
            # Manual seek or an ended session supersedes a queued auto-resume.
            clear_pending_resume(user.id)

        progress_stored = False
        if event_type in KODI_DURABLE_PROGRESS_EVENT_MAP:
            progress_stored = self._store_durable_playback_progress(payload, user)

        if event_type in KODI_LIVE_ONLY_EVENTS:
            return

        if not any(ids.values()):
            logger.warning("Ignoring Kodi webhook: no external ID found in payload.")
            return

        if self._skip_completed_episode_replay_activity(payload, user, ids):
            return

        self._process_media(payload, user, ids)

        # On a first-ever stop/end, normal processing may have just created the
        # exact Item. Retry once so resume progress is not lost.
        if (
            not progress_stored
            and event_type in {KodiEvent.PLAYBACK_STOP, KodiEvent.PLAYBACK_END}
        ):
            self._store_durable_playback_progress(payload, user)

    def _skip_completed_episode_replay_activity(self, payload, user, ids):
        """Do not reopen a completed season for an ordinary partial replay."""
        from app.models.choices import Status

        if self._get_media_type(payload) != MediaTypes.TV.value:
            return False
        if self._is_played(payload):
            return False

        season_number, _episode_number = self._extract_season_episode_from_payload(
            payload
        )
        if season_number is None:
            return False

        media_id, _found_season, _found_episode = self._find_tv_media_id(
            ids,
            series_title=self._extract_series_title(payload),
            allow_title_fallback=True,
        )
        if not media_id:
            return False

        tv_item = self._find_existing_tracked_tv_item(user, ids, media_id)
        if tv_item is None:
            return False

        season = (
            Season.objects.filter(
                related_tv__user=user,
                related_tv__item=tv_item,
                item__season_number=season_number,
            )
            .order_by("id")
            .first()
        )
        if season is None:
            return False

        if season.status == Status.COMPLETED.value and season.rewatch_started_at is None:
            logger.info(
                "Kodi partial replay left completed season unchanged: "
                "user=%s show=%s season=%s",
                user.id,
                media_id,
                season_number,
            )
            return True
        return False

    def _store_durable_playback_progress(self, payload, user):
        """Persist selected Kodi positions without creating watch history."""
        kodi_event = payload.get("event")
        progress_event = KODI_DURABLE_PROGRESS_EVENT_MAP.get(kodi_event)
        if not progress_event:
            return False

        state = live_playback.get_user_playback_state(user.id)
        if not state or state.get("control_backend") != KODI_CONTROL_BACKEND:
            return False

        playback_media_type = self._get_live_playback_media_type(payload)
        if playback_media_type not in {
            MediaTypes.MOVIE.value,
            MediaTypes.EPISODE.value,
        }:
            return False
        if state.get("media_type") != playback_media_type:
            return False

        media_id = str(state.get("media_id") or "").strip()
        if not media_id:
            return False

        source = state.get("source") or Sources.TMDB.value
        item_query = Item.objects.filter(
            media_id=media_id,
            source=source,
            media_type=playback_media_type,
        )
        if playback_media_type == MediaTypes.EPISODE.value:
            season_number = state.get("season_number")
            episode_number = state.get("episode_number")
            if season_number is None or episode_number is None:
                return False
            item_query = item_query.filter(
                season_number=season_number,
                episode_number=episode_number,
            )

        item = item_query.first()
        if item is None:
            # Do not provider-search or title-guess when persisting a position.
            return False

        progress = payload.get("progress") or {}
        offset_seconds = _coerce_seconds(progress.get("time"))
        duration_seconds = _coerce_seconds(payload.get("duration"))

        provider_completed = None
        if kodi_event == KodiEvent.PLAYBACK_END:
            provider_completed = True
        elif kodi_event == KodiEvent.PLAYBACK_STOP:
            provider_completed = self._is_played(payload)

        live_playback.store_playback_progress(
            user.id,
            event_type=progress_event,
            playback_media_type=playback_media_type,
            view_offset_seconds=offset_seconds,
            duration_seconds=duration_seconds,
            item=item,
            provider_completed=provider_completed,
        )
        return True

    def _process_rating(self, payload, user):
        raw_rating = payload.get("rating")
        try:
            rating = Decimal(str(raw_rating))
        except (InvalidOperation, TypeError, ValueError):
            logger.warning("Ignoring invalid Kodi rating: %r", raw_rating)
            return
        if not rating.is_finite() or rating < 0 or rating > 10:
            logger.warning("Ignoring out-of-range Kodi rating: %r", raw_rating)
            return
        rating = rating.quantize(Decimal("0.1"))

        ids = self._extract_external_ids(payload)
        if not any(ids.values()):
            logger.warning("Ignoring Kodi rating: no external ID found in payload.")
            return

        media_type = (payload.get("mediaType") or "").strip().lower()
        if media_type == "movie":
            self._apply_movie_rating(user, ids, rating)
        elif media_type == "episode":
            self._apply_episode_rating(payload, user, ids, rating)
        else:
            logger.warning(
                "Ignoring Kodi rating for unsupported mediaType: %r",
                payload.get("mediaType"),
            )

    def _resolve_movie_tmdb_id(self, ids):
        tmdb_id = ids.get("tmdb_id")
        if tmdb_id not in (None, ""):
            return str(tmdb_id)

        imdb_id = ids.get("imdb_id")
        if not imdb_id:
            return None
        try:
            response = tmdb.find(imdb_id, "imdb_id")
        except Exception as exc:  # pragma: no cover - provider guard
            logger.warning("Kodi rating IMDB->TMDB lookup failed: %s", exc)
            return None
        results = (response or {}).get("movie_results") or []
        media_id = results[0].get("id") if results else None
        return str(media_id) if media_id not in (None, "") else None

    def _apply_movie_rating(self, user, ids, rating):
        """Apply an overall movie rating without changing watched state."""
        media_id = self._resolve_movie_tmdb_id(ids)
        if not media_id:
            return

        item = Item.objects.filter(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        ).first()

        if item is None:
            # Floppy explicitly supports statusless rating-only media. Creating
            # one may store the rating, but must not mark the movie watched.
            try:
                metadata = tmdb.movie(media_id)
            except Exception as exc:  # pragma: no cover - provider guard
                logger.warning("Kodi rating TMDB movie lookup failed: %s", exc)
                return
            item, _created = Item.objects.get_or_create(
                media_id=media_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                defaults={
                    "title": metadata["title"],
                    "image": metadata["image"],
                },
            )

        movie = select_preferred_activity_entry(Movie.objects.filter(item=item, user=user))
        if movie is None:
            Movie.objects.create(item=item, user=user, status=None, score=rating)
            logger.info("Kodi rating saved as statusless movie: TMDB %s", media_id)
            return

        end_date = movie.end_date
        Movie.objects.filter(pk=movie.pk).update(score=rating)
        _invalidate_activity_days(user.id, [end_date])
        logger.info("Kodi rating saved: movie TMDB %s = %s/10", media_id, rating)

    def _episode_rating_ids(self, payload, ids):
        """Prefer show-level IDs for episode rating resolution."""
        show_ids = payload.get("tvShowUniqueIds") or {}
        if not isinstance(show_ids, dict) or not show_ids:
            return ids
        return {
            "tmdb_id": show_ids.get("tmdb") or ids.get("tmdb_id"),
            "imdb_id": show_ids.get("imdb") or ids.get("imdb_id"),
            "tvdb_id": show_ids.get("tvdb") or ids.get("tvdb_id"),
            "tvmaze_id": show_ids.get("tvmaze") or ids.get("tvmaze_id"),
        }

    def _apply_episode_rating(self, payload, user, ids, rating):
        season_number, episode_number = self._extract_season_episode_from_payload(payload)
        try:
            season_number = int(season_number)
            episode_number = int(episode_number)
        except (TypeError, ValueError):
            return

        resolution_ids = self._episode_rating_ids(payload, ids)
        media_id, _season, _episode = self._find_tv_media_id(
            resolution_ids,
            series_title=self._extract_series_title(payload),
            allow_title_fallback=True,
            year=payload.get("year"),
        )
        if not media_id:
            return

        episode_rows = Episode.objects.filter(
            item__media_id=str(media_id),
            item__source=Sources.TMDB.value,
            item__media_type=MediaTypes.EPISODE.value,
            item__season_number=season_number,
            item__episode_number=episode_number,
            related_season__user=user,
        )
        if not episode_rows.exists():
            tracked_tv_item = self._find_existing_tracked_tv_item(
                user, resolution_ids, media_id
            )
            if tracked_tv_item is not None:
                episode_rows = Episode.objects.filter(
                    related_season__related_tv__item=tracked_tv_item,
                    related_season__user=user,
                    item__season_number=season_number,
                    item__episode_number=episode_number,
                )

        if not episode_rows.exists():
            # Never manufacture an episode watch merely to hold a rating.
            return

        end_dates = list(episode_rows.values_list("end_date", flat=True))
        updated = episode_rows.update(score=rating)
        _invalidate_activity_days(user.id, end_dates)
        logger.info(
            "Kodi rating saved: TMDB %s S%02dE%02d = %s/10 (%s rows)",
            media_id,
            season_number,
            episode_number,
            rating,
            updated,
        )

    def _get_live_playback_media_type(self, payload):
        media_type = (payload.get("mediaType") or "").strip().lower()
        if media_type == "episode":
            return MediaTypes.EPISODE.value
        if media_type == "movie":
            return MediaTypes.MOVIE.value
        return None

    def _show_level_ids(self, payload, fallback_ids):
        show_ids = payload.get("tvShowUniqueIds") or {}
        if not isinstance(show_ids, dict):
            show_ids = {}
        return {
            "tmdb_id": show_ids.get("tmdb") or fallback_ids.get("tmdb_id"),
            "imdb_id": show_ids.get("imdb") or fallback_ids.get("imdb_id"),
            "tvdb_id": show_ids.get("tvdb") or fallback_ids.get("tvdb_id"),
            "tvmaze_id": show_ids.get("tvmaze") or fallback_ids.get("tvmaze_id"),
        }

    def _resolve_live_playback_media_id(
        self,
        payload,
        ids,
        playback_media_type,
        existing_state,
        rating_key,
    ):
        if (
            existing_state
            and existing_state.get("control_backend") == KODI_CONTROL_BACKEND
            and rating_key
            and str(existing_state.get("rating_key") or "") == rating_key
            and existing_state.get("media_id")
        ):
            return str(existing_state["media_id"])

        if playback_media_type == MediaTypes.MOVIE.value:
            return self._resolve_movie_tmdb_id(ids)
        if playback_media_type != MediaTypes.EPISODE.value:
            return None

        show_ids = self._show_level_ids(payload, ids)
        tmdb_id = show_ids.get("tmdb_id")
        if tmdb_id not in (None, ""):
            return str(tmdb_id)

        resolved_id, _season, _episode = self._find_tv_media_id(
            show_ids,
            series_title=self._extract_series_title(payload),
            allow_title_fallback=True,
            year=payload.get("year"),
        )
        return str(resolved_id) if resolved_id else None

    def _update_live_playback_state(self, payload, user, ids):
        event_type = payload.get("event")
        live_event_type = KODI_LIVE_EVENT_MAP.get(event_type)
        if not live_event_type:
            return

        playback_media_type = self._get_live_playback_media_type(payload)
        if playback_media_type not in {
            MediaTypes.MOVIE.value,
            MediaTypes.EPISODE.value,
        }:
            return

        rating_key = str(payload.get("sessionId") or "").strip() or None
        existing_state = live_playback.get_user_playback_state(user.id)

        # Kodi emits seek while paused. Preserve that pause rather than
        # spuriously flipping the shared Now Playing card to playing.
        if (
            event_type == KodiEvent.PLAYBACK_SEEK
            and existing_state
            and existing_state.get("control_backend") == KODI_CONTROL_BACKEND
            and existing_state.get("status") == live_playback.PLAYBACK_STATUS_PAUSED
        ):
            live_event_type = "media.pause"

        media_id = self._resolve_live_playback_media_id(
            payload,
            ids,
            playback_media_type,
            existing_state,
            rating_key,
        )
        progress = payload.get("progress") or {}
        offset_seconds = _coerce_seconds(progress.get("time"))
        duration_seconds = _coerce_seconds(payload.get("duration"))

        season_number = episode_number = None
        if playback_media_type == MediaTypes.EPISODE.value:
            season_number, episode_number = self._extract_season_episode_from_payload(
                payload
            )

        live_playback.apply_playback_event(
            user_id=user.id,
            event_type=live_event_type,
            playback_media_type=playback_media_type,
            media_id=media_id,
            source=Sources.TMDB.value,
            rating_key=rating_key,
            title=payload.get("title"),
            series_title=payload.get("tvShowTitle"),
            episode_title=(
                payload.get("title")
                if playback_media_type == MediaTypes.EPISODE.value
                else None
            ),
            season_number=season_number,
            episode_number=episode_number,
            view_offset_seconds=offset_seconds,
            duration_seconds=duration_seconds,
            store_progress=False,
        )
        mark_kodi_state(user.id)
