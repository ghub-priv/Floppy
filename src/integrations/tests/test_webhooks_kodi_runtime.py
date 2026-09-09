from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from integrations.webhooks.kodi import KodiWebhookProcessor


class KodiWebhookRuntimeTests(SimpleTestCase):
    def setUp(self):
        self.processor = KodiWebhookProcessor()
        self.user = SimpleNamespace(id=11)

    @patch("integrations.webhooks.kodi_runtime.apply_pending_resume")
    def test_interval_is_live_only_and_never_persists_progress(self, apply_resume):
        self.processor._update_live_playback_state = Mock()
        self.processor._store_durable_playback_progress = Mock()
        self.processor._process_media = Mock()

        self.processor.process_payload(
            {
                "event": "interval",
                "mediaType": "movie",
                "uniqueIds": {"tmdb": "603"},
                "progress": {"time": 60, "percent": 10},
                "duration": 600,
            },
            self.user,
        )

        self.processor._update_live_playback_state.assert_called_once()
        self.processor._store_durable_playback_progress.assert_not_called()
        self.processor._process_media.assert_not_called()
        apply_resume.assert_called_once_with(self.user)

    def test_pause_is_live_only_but_is_durable_checkpoint(self):
        self.processor._update_live_playback_state = Mock()
        self.processor._store_durable_playback_progress = Mock(return_value=True)
        self.processor._process_media = Mock()

        self.processor.process_payload(
            {"event": "pause", "mediaType": "movie", "uniqueIds": {"tmdb": "603"}},
            self.user,
        )

        self.processor._store_durable_playback_progress.assert_called_once()
        self.processor._process_media.assert_not_called()

    def test_rating_short_circuits_all_playback_processing(self):
        self.processor._process_rating = Mock()
        self.processor._update_live_playback_state = Mock()
        self.processor._process_media = Mock()

        payload = {"rating": 8, "mediaType": "movie", "uniqueIds": {"tmdb": "603"}}
        self.processor.process_payload(payload, self.user)

        self.processor._process_rating.assert_called_once_with(payload, self.user)
        self.processor._update_live_playback_state.assert_not_called()
        self.processor._process_media.assert_not_called()

    @patch("integrations.webhooks.kodi_runtime.select_preferred_activity_entry")
    @patch("integrations.webhooks.kodi_runtime.Movie.objects.filter")
    @patch("integrations.webhooks.kodi_runtime.Item.objects.filter")
    def test_existing_movie_rating_uses_score_only_save(
        self,
        item_filter,
        movie_filter,
        select_preferred,
    ):
        item = Mock()
        item_filter.return_value.first.return_value = item
        movie_query = Mock()
        movie_filter.return_value = movie_query
        movie = Mock()
        select_preferred.return_value = movie
        self.processor._resolve_movie_tmdb_id = Mock(return_value="603")

        self.processor._apply_movie_rating(
            self.user,
            {"tmdb_id": "603"},
            Decimal("8.0"),
        )

        movie_filter.assert_called_once_with(item=item, user=self.user)
        select_preferred.assert_called_once_with(movie_query)
        self.assertEqual(movie.score, Decimal("8.0"))
        movie.save.assert_called_once_with(update_fields=["score"])

    @patch("integrations.webhooks.kodi_runtime._invalidate_activity_days")
    @patch("integrations.webhooks.kodi_runtime.Episode.objects.filter")
    def test_episode_rating_updates_all_replays_without_episode_save(
        self,
        episode_filter,
        invalidate_days,
    ):
        episode_rows = Mock()
        episode_rows.exists.return_value = True
        episode_rows.values_list.return_value = ["first-watch", "repeat-watch"]
        episode_filter.return_value = episode_rows
        self.processor._find_tv_media_id = Mock(return_value=("1396", None, None))

        self.processor._apply_episode_rating(
            {
                "season": 2,
                "episode": 3,
                "tvShowTitle": "Breaking Bad",
            },
            self.user,
            {"tmdb_id": "1396"},
            Decimal("8.0"),
        )

        episode_filter.assert_called_once_with(
            item__media_id="1396",
            item__source="tmdb",
            item__media_type="episode",
            item__season_number=2,
            item__episode_number=3,
            related_season__user=self.user,
        )
        episode_rows.update.assert_called_once_with(score=Decimal("8.0"))
        invalidate_days.assert_called_once_with(
            self.user.id,
            ["first-watch", "repeat-watch"],
        )

    def test_completed_replay_guard_uses_show_level_identity(self):
        self.processor._get_media_type = Mock(return_value="tv")
        self.processor._is_played = Mock(return_value=False)
        self.processor._extract_season_episode_from_payload = Mock(return_value=(2, 3))
        self.processor._find_tv_media_id = Mock(return_value=(None, None, None))
        payload = {
            "tvShowTitle": "Breaking Bad",
            "tvShowUniqueIds": {"tmdb": "1396"},
        }
        episode_ids = {
            "tmdb_id": "999999",
            "imdb_id": None,
            "tvdb_id": "123",
            "tvmaze_id": None,
        }

        self.assertFalse(
            self.processor._skip_completed_episode_replay_activity(
                payload,
                self.user,
                episode_ids,
            )
        )
        self.processor._find_tv_media_id.assert_called_once_with(
            {
                "tmdb_id": "1396",
                "imdb_id": None,
                "tvdb_id": "123",
                "tvmaze_id": None,
            },
            series_title="Breaking Bad",
            allow_title_fallback=True,
        )

    def test_show_level_ids_override_episode_ids_for_live_identity(self):
        ids = {"tmdb_id": "999999", "tvdb_id": "123"}
        result = self.processor._show_level_ids(
            {"tvShowUniqueIds": {"tmdb": "1396", "tvmaze": "526"}},
            ids,
        )
        self.assertEqual(result["tmdb_id"], "1396")
        self.assertEqual(result["tvmaze_id"], "526")
        self.assertEqual(result["tvdb_id"], "123")

    def test_external_ids_tolerate_null_and_non_mapping_payload_values(self):
        self.assertEqual(
            self.processor._extract_external_ids(
                {"uniqueIds": None, "tvShowUniqueIds": "not-a-mapping"}
            ),
            {
                "tmdb_id": None,
                "imdb_id": None,
                "tvdb_id": None,
                "tvmaze_id": None,
            },
        )

    def test_tv_title_formats_numeric_string_season_and_episode(self):
        title = self.processor._get_media_title(
            {
                "mediaType": "episode",
                "tvShowTitle": "Breaking Bad",
                "season": "2",
                "episode": "3",
            }
        )
        self.assertEqual(title, "Breaking Bad S02E03")

    def test_stop_completion_accepts_numeric_string_percent(self):
        self.assertTrue(
            self.processor._is_played(
                {"event": "stop", "progress": {"percent": "80.0"}}
            )
        )
        self.assertFalse(
            self.processor._is_played(
                {"event": "stop", "progress": {"percent": "79.9"}}
            )
        )

    def test_stop_completion_ignores_malformed_progress(self):
        self.assertFalse(
            self.processor._is_played({"event": "stop", "progress": "invalid"})
        )
        self.assertFalse(
            self.processor._is_played(
                {"event": "stop", "progress": {"percent": "invalid"}}
            )
        )

    @patch("integrations.webhooks.kodi_runtime.mark_kodi_state")
    @patch("integrations.webhooks.kodi_runtime.live_playback.apply_playback_event")
    @patch("integrations.webhooks.kodi_runtime.live_playback.get_user_playback_state")
    def test_seek_while_paused_preserves_pause(
        self,
        get_state,
        apply_event,
        mark_state,
    ):
        get_state.return_value = {
            "control_backend": "kodi",
            "status": "paused",
            "rating_key": "abc",
            "media_id": "603",
        }
        payload = {
            "event": "seek",
            "mediaType": "movie",
            "sessionId": "abc",
            "uniqueIds": {"tmdb": "603"},
            "progress": {"time": 120},
            "duration": 600,
        }

        self.processor._update_live_playback_state(
            payload,
            self.user,
            self.processor._extract_external_ids(payload),
        )

        self.assertEqual(apply_event.call_args.kwargs["event_type"], "media.pause")
        self.assertFalse(apply_event.call_args.kwargs["store_progress"])
        mark_state.assert_called_once_with(self.user.id)
