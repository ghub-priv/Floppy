import os
from unittest.mock import patch

from django.template.loader import get_template
from django.test import SimpleTestCase

from app.templatetags.kodi_tags import kodi_playback_available


class KodiTemplateLoadTests(SimpleTestCase):
    def test_kodi_templates_compile(self):
        for template_name in (
            "app/components/kodi_play_action.html",
            "app/components/detail_action_buttons.html",
            "app/components/detail_episode_hero_track_button.html",
            "app/components/active_playback_card.html",
        ):
            with self.subTest(template=template_name):
                self.assertIsNotNone(get_template(template_name))

    def test_kodi_playback_action_hidden_without_configuration(self):
        with patch.dict(os.environ, {"KODI_HOST": ""}, clear=False):
            self.assertFalse(kodi_playback_available())

    def test_kodi_playback_action_available_with_valid_configuration(self):
        with patch.dict(
            os.environ,
            {
                "KODI_HOST": "192.0.2.10",
                "KODI_PORT": "8080",
                "KODI_SCHEME": "http",
                "KODI_TIMEOUT": "6",
            },
            clear=False,
        ):
            self.assertTrue(kodi_playback_available())
