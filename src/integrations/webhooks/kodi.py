# Original implementation by sboddy — FuzzyGrim/Yamtrack PR #1506
import logging

from app.integration_health_telemetry import record_integration_health_event
from app.models import MediaTypes

from .base import BaseWebhookProcessor
from .kodi_runtime import (
    KODI_LIVE_EVENT_MAP,
    KODI_LIVE_ONLY_EVENTS,
    PERCENT_COMPLETE_THRESHOLD,
    KodiEvent,
    KodiRuntimeMixin,
)

logger = logging.getLogger(__name__)


def _coerce_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class KodiWebhookProcessor(KodiRuntimeMixin, BaseWebhookProcessor):
    """Processor for Kodi webhook events via the HTTP Scrobbler add-on."""

    def process_payload(self, payload, user):
        """Process a Kodi payload while recording best-effort health telemetry."""
        record_integration_health_event(user, payload, "received")

        event_type = payload.get("event")
        if "rating" not in payload and event_type in KODI_LIVE_ONLY_EVENTS:
            logger.info("Handling Kodi live-only event type: %s", event_type)

        result = super().process_payload(payload, user)

        # Rating success is recorded by _process_rating below and ordinary
        # playback success by _process_media. Live-only events deliberately do
        # not enter _process_media, so record their successful handling here.
        if "rating" not in payload and event_type in KODI_LIVE_ONLY_EVENTS:
            record_integration_health_event(user, payload, "success")
        return result

    def _process_rating(self, payload, user):
        result = super()._process_rating(payload, user)
        # Preserve the accepted v1 telemetry contract: a handled rating payload
        # is a successful scrobbler event even when it does not create media.
        record_integration_health_event(user, payload, "success")
        return result

    def _process_media(self, payload, user, ids):
        result = super()._process_media(payload, user, ids)
        record_integration_health_event(user, payload, "success")
        return result

    def _is_supported_event(self, event_type):
        supported = event_type in KODI_LIVE_EVENT_MAP
        if not supported:
            logger.info("Ignoring Kodi webhook event type: %s", event_type)
        return supported

    def _is_played(self, payload):
        if payload.get("event") == KodiEvent.PLAYBACK_END:
            return True
        if payload.get("event") == KodiEvent.PLAYBACK_STOP:
            progress = payload.get("progress") or {}
            if not isinstance(progress, dict):
                return False
            percent = _coerce_float(progress.get("percent"))
            return percent is not None and percent >= PERCENT_COMPLETE_THRESHOLD
        return False

    def _get_media_type(self, payload):
        media_type = (payload.get("mediaType") or "").capitalize()
        return self.MEDIA_TYPE_MAPPING.get(media_type)

    def _get_media_title(self, payload):
        if self._get_media_type(payload) == MediaTypes.TV.value:
            series_name = payload.get("tvShowTitle")
            season_number = _coerce_int(payload.get("season"))
            episode_number = _coerce_int(payload.get("episode"))
            if season_number is None or episode_number is None:
                return series_name
            return f"{series_name} S{season_number:02d}E{episode_number:02d}"

        if self._get_media_type(payload) == MediaTypes.MOVIE.value:
            movie_name = payload.get("title")
            year = payload.get("year")
            return f"{movie_name} ({year})" if movie_name and year else movie_name

        return None

    def _extract_season_episode_from_payload(self, payload):
        return payload.get("season"), payload.get("episode")

    def _extract_series_title(self, payload):
        return payload.get("tvShowTitle")

    def _skip_completed_episode_replay_activity(self, payload, user, ids):
        """Resolve the completed-season safeguard against the show identity."""
        show_ids = self._show_level_ids(payload, ids)
        return super()._skip_completed_episode_replay_activity(payload, user, show_ids)

    def _extract_external_ids(self, payload):
        # Preserve upstream Floppy's episode-first ID behaviour. The Kodi
        # runtime layer explicitly chooses tvShowUniqueIds only where a show
        # identity is required for live state, rating, or completed-season
        # replay resolution.
        episode_ids = payload.get("uniqueIds") or {}
        series_ids = payload.get("tvShowUniqueIds") or {}
        if not isinstance(episode_ids, dict):
            episode_ids = {}
        if not isinstance(series_ids, dict):
            series_ids = {}
        return {
            "tmdb_id": episode_ids.get("tmdb") or series_ids.get("tmdb"),
            "imdb_id": episode_ids.get("imdb") or series_ids.get("imdb"),
            "tvdb_id": episode_ids.get("tvdb") or series_ids.get("tvdb"),
            "tvmaze_id": episode_ids.get("tvmaze") or series_ids.get("tvmaze"),
        }
