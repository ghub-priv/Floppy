from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import RequestFactory, SimpleTestCase

from app.kodi_client import KodiConnectionError
from app.kodi_playback import kodi_play


class KodiPlaybackTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = SimpleNamespace(id=9, is_authenticated=True)

    def _request(self, data):
        request = self.factory.post("/kodi/play/", data)
        request.user = self.user
        return request

    def test_rejects_non_tmdb_source(self):
        response = kodi_play(
            self._request({"source": "tvdb", "media_type": "movie", "media_id": "1"})
        )
        self.assertContains(response, "requires a TMDb item")

    @patch("app.kodi_playback.queue_pending_resume", return_value=95)
    @patch("app.kodi_playback.KodiClient.from_env")
    def test_movie_play_queues_resume_before_player_open(self, from_env, queue_resume):
        kodi = Mock()
        from_env.return_value = kodi

        response = kodi_play(
            self._request(
                {"source": "tmdb", "media_type": "movie", "media_id": "603"}
            )
        )

        self.assertEqual(response.status_code, 200)
        queue_resume.assert_called_once_with(
            self.user,
            "movie",
            "603",
            season_number=None,
            episode_number=None,
        )
        opened_url = kodi.open_file.call_args.args[0]
        self.assertIn("plugin.video.themoviedb.helper", opened_url)
        self.assertIn("tmdb_id=603", opened_url)
        self.assertContains(response, "resume from 1:35")

    @patch("app.kodi_playback.clear_pending_resume")
    @patch("app.kodi_playback.queue_pending_resume", return_value=95)
    @patch("app.kodi_playback.KodiClient.from_env")
    def test_player_open_failure_clears_pending_resume(
        self,
        from_env,
        queue_resume,
        clear_pending_resume,
    ):
        kodi = Mock()
        kodi.open_file.side_effect = KodiConnectionError("offline")
        from_env.return_value = kodi

        response = kodi_play(
            self._request(
                {"source": "tmdb", "media_type": "movie", "media_id": "603"}
            )
        )

        self.assertEqual(response.status_code, 200)
        queue_resume.assert_called_once()
        clear_pending_resume.assert_called_once_with(self.user.id)
        self.assertContains(response, "Kodi unavailable")
