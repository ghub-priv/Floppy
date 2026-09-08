from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import RequestFactory, SimpleTestCase

from app.kodi_control import _kodi_time_to_seconds, kodi_control


class KodiControlTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = SimpleNamespace(id=4, is_authenticated=True)

    def _request(self, action):
        request = self.factory.post("/kodi/control/", {"action": action})
        request.user = self.user
        return request

    def test_time_conversion(self):
        self.assertEqual(
            _kodi_time_to_seconds({"hours": 1, "minutes": 2, "seconds": 3}),
            3723,
        )
        self.assertIsNone(_kodi_time_to_seconds("bad"))

    @patch("app.kodi_control.KodiClient.from_env")
    @patch("app.kodi_control.is_kodi_state", return_value=True)
    def test_seek_back_is_absolute_and_clamped(self, is_kodi_state, from_env):
        kodi = Mock()
        kodi.get_active_players.return_value = [{"type": "video", "playerid": 1}]
        kodi.get_player_properties.return_value = {
            "time": {"hours": 0, "minutes": 0, "seconds": 10},
            "totaltime": {"hours": 0, "minutes": 10, "seconds": 0},
        }
        from_env.return_value = kodi

        response = kodi_control(self._request("seek_back"))

        self.assertEqual(response.status_code, 204)
        is_kodi_state.assert_called_once_with(self.user.id)
        kodi.seek_seconds.assert_called_once_with(1, 0)

    @patch("app.kodi_control.KodiClient.from_env")
    @patch("app.kodi_control.is_kodi_state", return_value=True)
    def test_no_active_player_is_conflict(self, is_kodi_state, from_env):
        kodi = Mock()
        kodi.get_active_players.return_value = []
        from_env.return_value = kodi

        response = kodi_control(self._request("pause"))

        self.assertEqual(response.status_code, 409)
        is_kodi_state.assert_called_once_with(self.user.id)

    @patch("app.kodi_control.KodiClient.from_env")
    @patch("app.kodi_control.is_kodi_state", return_value=False)
    def test_non_kodi_live_state_cannot_control_kodi(self, is_kodi_state, from_env):
        response = kodi_control(self._request("pause"))

        self.assertEqual(response.status_code, 409)
        is_kodi_state.assert_called_once_with(self.user.id)
        from_env.assert_not_called()
