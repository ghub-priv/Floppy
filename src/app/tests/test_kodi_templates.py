from django.template.loader import get_template
from django.test import SimpleTestCase


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
