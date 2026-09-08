from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


TEMPLATES_ROOT = Path(settings.BASE_DIR) / "templates" / "app" / "components"


class KodiDetailActionContractTests(SimpleTestCase):
    def test_generic_detail_actions_include_kodi_before_tracker(self):
        template = (TEMPLATES_ROOT / "detail_action_buttons.html").read_text()
        kodi_pos = template.index('app/components/kodi_play_action.html')
        tracker_pos = template.index("Keep the hero tracker action full-width")
        self.assertLess(kodi_pos, tracker_pos)
        self.assertIn("MediaTypes.MOVIE.value", template)
        self.assertIn("MediaTypes.TV.value", template)
        self.assertIn("MediaTypes.SEASON.value", template)

    def test_episode_action_keeps_oob_tracker_isolated(self):
        template = (TEMPLATES_ROOT / "detail_episode_hero_track_button.html").read_text()
        self.assertIn("if not track_button_oob and source == Sources.TMDB.value", template)
        self.assertIn('kodi_media_type="episode"', template)
        self.assertIn("kodi_include_season=1", template)
        self.assertIn("kodi_include_episode=1", template)
