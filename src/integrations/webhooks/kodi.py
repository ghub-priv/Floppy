# Original implementation by sboddy — FuzzyGrim/Yamtrack PR #1506
import logging

from app.models import MediaTypes

from .base import BaseWebhookProcessor
from .kodi_runtime import (
    KODI_LIVE_EVENT_MAP,
    PERCENT_COMPLETE_THRESHOLD,
    KodiEvent,
    KodiRuntimeMixin,
)

logger = logging.getLogger(__name__)


class KodiWebhookProcessor(KodiRuntimeMixin, BaseWebhookProcessor):
    """Processor for Kodi webhook events via the HTTP Scrobbler add-on."""

    def _is_supported_event(self, event_type):
        return event_type in KODI_LIVE_EVENT_MAP

    def _is_played(self, payload):
        if payload.get("event") == KodiEvent.PLAYBACK_END:
            return True
        if payload.get("event") == KodiEvent.PLAYBACK_STOP:
            percent = payload.get("progress", {}).get("percent", 0)
            if percent and percent >= PERCENT_COMPLETE_THRESHOLD:
                return True
        return False

    def _get_media_type(self, payload):
        return self.MEDIA_TYPE_MAPPING.get(payload.get("mediaType", "").capitalize())

    def _get_media_title(self, payload):
        if self._get_media_type(payload) == MediaTypes.TV.value:
            series_name = payload.get("tvShowTitle")
            season_number = payload.get("season")
            episode_number = payload.get("episode")
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
        episode_ids = payload.get("uniqueIds", {})
        series_ids = payload.get("tvShowUniqueIds", {})
        return {
            "tmdb_id": episode_ids.get("tmdb") or series_ids.get("tmdb"),
            "imdb_id": episode_ids.get("imdb") or series_ids.get("imdb"),
            "tvdb_id": episode_ids.get("tvdb") or series_ids.get("tvdb"),
            "tvmaze_id": episode_ids.get("tvmaze") or series_ids.get("tvmaze"),
        }
