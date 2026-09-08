from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from app.kodi_resume import _matches_live_state, apply_pending_resume


class KodiResumeTests(SimpleTestCase):
    def test_match_requires_kodi_provenance(self):
        pending = {
            "media_type": "movie",
            "media_id": "603",
            "position_seconds": 90,
        }
        self.assertFalse(
            _matches_live_state(
                pending,
                {"media_type": "movie", "media_id": "603"},
            )
        )
        self.assertTrue(
            _matches_live_state(
                pending,
                {
                    "media_type": "movie",
                    "media_id": "603",
                    "control_backend": "kodi",
                },
            )
        )

    @patch("app.kodi_resume.clear_pending_resume")
    @patch("app.kodi_resume.KodiClient.from_env")
    @patch("app.kodi_resume.live_playback.get_user_playback_state")
    @patch("app.kodi_resume.get_pending_resume")
    def test_applies_exact_pending_resume(
        self,
        get_pending_resume,
        get_state,
        from_env,
        clear_pending_resume,
    ):
        user = SimpleNamespace(id=7)
        get_pending_resume.return_value = {
            "media_type": "episode",
            "media_id": "1396",
            "season_number": 2,
            "episode_number": 3,
            "position_seconds": 125,
        }
        get_state.return_value = {
            "media_type": "episode",
            "media_id": "1396",
            "season_number": 2,
            "episode_number": 3,
            "control_backend": "kodi",
        }
        kodi = Mock()
        kodi.get_active_players.return_value = [{"type": "video", "playerid": 1}]
        from_env.return_value = kodi

        self.assertTrue(apply_pending_resume(user))
        kodi.seek_seconds.assert_called_once_with(1, 125)
        clear_pending_resume.assert_called_once_with(7)

    @patch("app.kodi_resume.clear_pending_resume")
    @patch("app.kodi_resume.KodiClient.from_env")
    @patch("app.kodi_resume.live_playback.get_user_playback_state")
    @patch("app.kodi_resume.get_pending_resume")
    def test_no_active_player_keeps_pending_resume(
        self,
        get_pending_resume,
        get_state,
        from_env,
        clear_pending_resume,
    ):
        user = SimpleNamespace(id=7)
        get_pending_resume.return_value = {
            "media_type": "movie",
            "media_id": "603",
            "position_seconds": 90,
        }
        get_state.return_value = {
            "media_type": "movie",
            "media_id": "603",
            "control_backend": "kodi",
        }
        kodi = Mock()
        kodi.get_active_players.return_value = []
        from_env.return_value = kodi

        self.assertFalse(apply_pending_resume(user))
        clear_pending_resume.assert_not_called()
