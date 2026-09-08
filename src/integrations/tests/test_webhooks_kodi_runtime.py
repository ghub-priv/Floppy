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

    def test_show_level_ids_override_episode_ids_for_live_identity(self):
        ids = {"tmdb_id": "999999", "tvdb_id": "123"}
        result = self.processor._show_level_ids(
            {"tvShowUniqueIds": {"tmdb": "1396", "tvmaze": "526"}},
            ids,
        )
        self.assertEqual(result["tmdb_id"], "1396")
        self.assertEqual(result["tvmaze_id"], "526")
        self.assertEqual(result["tvdb_id"], "123")

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
