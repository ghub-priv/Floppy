from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import RequestFactory, SimpleTestCase

from app.kodi_runtime_views import kodi_active_playback_fragment


class KodiRuntimeViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("app.views.active_playback_fragment")
    @patch("app.kodi_runtime_views.reconcile_for_user", side_effect=RuntimeError("boom"))
    def test_reconcile_failure_does_not_break_playback_fragment(
        self,
        reconcile_for_user,
        active_playback_fragment,
    ):
        request = self.factory.get("/api/active-playback/")
        request.user = SimpleNamespace(id=3, is_authenticated=True)
        expected = Mock()
        active_playback_fragment.return_value = expected

        response = kodi_active_playback_fragment(request)

        self.assertIs(response, expected)
        reconcile_for_user.assert_called_once_with(request.user)
        active_playback_fragment.assert_called_once_with(request)
