from unittest.mock import patch

from django.test import SimpleTestCase

from app.kodi_state import is_kodi_state


class KodiStateTests(SimpleTestCase):
    @patch("app.kodi_state.live_playback.get_user_playback_state")
    def test_active_kodi_state_is_controllable(self, get_state):
        get_state.return_value = {
            "control_backend": "kodi",
            "status": "playing",
        }

        self.assertTrue(is_kodi_state(4))

    @patch("app.kodi_state.live_playback.get_user_playback_state")
    def test_stopped_kodi_state_is_not_controllable(self, get_state):
        get_state.return_value = {
            "control_backend": "kodi",
            "status": "stopped",
        }

        self.assertFalse(is_kodi_state(4))

    @patch("app.kodi_state.live_playback.get_user_playback_state")
    def test_other_integration_state_is_not_controllable(self, get_state):
        get_state.return_value = {
            "control_backend": "plex",
            "status": "playing",
        }

        self.assertFalse(is_kodi_state(4))
