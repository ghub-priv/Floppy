from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from app.kodi_reconcile import _item_matches_state, reconcile_for_user


class KodiReconcileTests(SimpleTestCase):
    def test_episode_match_requires_show_identity_and_episode_numbers(self):
        state = {
            "media_type": "episode",
            "series_title": "The Office",
            "season_number": 2,
            "episode_number": 3,
        }
        self.assertTrue(
            _item_matches_state(
                state,
                {
                    "type": "episode",
                    "showtitle": "The Office",
                    "season": 2,
                    "episode": 3,
                },
            )
        )
        self.assertFalse(
            _item_matches_state(
                state,
                {
                    "type": "episode",
                    "showtitle": "Another Show",
                    "season": 2,
                    "episode": 3,
                },
            )
        )

    @patch("app.kodi_reconcile.KodiClient.from_env")
    @patch("app.kodi_reconcile.live_playback.get_user_playback_state")
    def test_never_reconciles_another_integration_state(self, get_state, from_env):
        get_state.return_value = {
            "media_type": "movie",
            "media_id": "603",
            "source": "tmdb",
            "control_backend": "plex",
        }

        self.assertFalse(reconcile_for_user(SimpleNamespace(id=3)))
        from_env.assert_not_called()
