from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase

from app.models import MediaTypes
from app.templatetags import derived_tv_ratings


def _user(*, scale=10, authenticated=True):
    return SimpleNamespace(
        is_authenticated=authenticated,
        rating_scale_max=scale,
    )


class DerivedTVRatingsTests(SimpleTestCase):
    def test_tv_rating_deduplicates_rewatches_and_uses_newest_row(self):
        with patch.object(
            derived_tv_ratings,
            "_episode_rows",
            return_value=[
                {"item_id": 1, "score": Decimal(8)},
                {"item_id": 1, "score": Decimal(2)},
                {"item_id": 2, "score": None},
            ],
        ):
            result = derived_tv_ratings.derived_tv_rating(
                _user(),
                MediaTypes.TV.value,
                {"media_id": "123", "source": "tmdb"},
            )

        self.assertEqual(result["score"], "8.00")
        self.assertEqual(result["raw_score"], 8.0)
        self.assertEqual(result["rated"], 1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["coverage_percent"], 50.0)
        self.assertTrue(result["specials_excluded"])

    def test_rating_uses_five_point_display_scale(self):
        with patch.object(
            derived_tv_ratings,
            "_episode_rows",
            return_value=[{"item_id": 1, "score": Decimal(8)}],
        ):
            result = derived_tv_ratings.derived_tv_rating(
                _user(scale=5),
                MediaTypes.TV.value,
                {"media_id": "123", "source": "tmdb"},
            )

        self.assertEqual(result["score"], "4.00")

    def test_unrated_completed_episodes_return_coverage_without_average(self):
        with patch.object(
            derived_tv_ratings,
            "_episode_rows",
            return_value=[
                {"item_id": 1, "score": None},
                {"item_id": 2, "score": None},
            ],
        ):
            result = derived_tv_ratings.derived_tv_rating(
                _user(),
                MediaTypes.SEASON.value,
                {"media_id": "123", "source": "tmdb", "season_number": 2},
            )

        self.assertIsNone(result["score"])
        self.assertEqual(result["rated"], 0)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["coverage_percent"], 0.0)
        self.assertEqual(str(result["label"]), "Season 2")

    def test_specials_mapping_uses_specials_label(self):
        with patch.object(
            derived_tv_ratings,
            "_episode_rows",
            return_value=[{"item_id": 1, "score": Decimal(7)}],
        ):
            result = derived_tv_ratings.derived_tv_rating(
                _user(),
                MediaTypes.SEASON.value,
                {"media_id": "123", "source": "tmdb", "season_number": "0"},
            )

        self.assertEqual(str(result["label"]), "Specials")

    def test_anonymous_and_non_tv_media_are_not_supported(self):
        with patch.object(derived_tv_ratings, "_episode_rows", return_value=[]):
            self.assertIsNone(
                derived_tv_ratings.derived_tv_rating(
                    _user(authenticated=False),
                    MediaTypes.TV.value,
                    {"media_id": "123", "source": "tmdb"},
                )
            )
            self.assertIsNone(
                derived_tv_ratings.derived_tv_rating(
                    _user(),
                    MediaTypes.MOVIE.value,
                    {"media_id": "123", "source": "tmdb"},
                )
            )

    def test_episode_rows_excludes_specials_for_tv(self):
        qs = MagicMock()
        qs.filter.return_value = qs
        qs.order_by.return_value = qs
        expected = object()
        qs.values.return_value = expected

        manager = MagicMock()
        manager.filter.return_value = qs
        fake_episode = SimpleNamespace(objects=manager)

        with patch.object(derived_tv_ratings, "Episode", fake_episode):
            result = derived_tv_ratings._episode_rows(
                _user(),
                MediaTypes.TV.value,
                {"media_id": "123", "source": "tmdb"},
            )

        self.assertIs(result, expected)
        qs.filter.assert_called_once_with(item__season_number__gt=0)
        qs.order_by.assert_called_once_with(
            "item_id",
            "-end_date",
            "-created_at",
            "-id",
        )

    def test_episode_rows_filters_exact_season_from_mapping(self):
        qs = MagicMock()
        qs.filter.return_value = qs
        qs.order_by.return_value = qs
        expected = object()
        qs.values.return_value = expected

        manager = MagicMock()
        manager.filter.return_value = qs
        fake_episode = SimpleNamespace(objects=manager)

        with patch.object(derived_tv_ratings, "Episode", fake_episode):
            result = derived_tv_ratings._episode_rows(
                _user(),
                MediaTypes.SEASON.value,
                {"media_id": "123", "source": "tmdb", "season_number": "3"},
            )

        self.assertIs(result, expected)
        qs.filter.assert_called_once_with(item__season_number=3)

    def test_detail_score_slot_renders_derived_rating_for_tv_and_season(self):
        template = (
            settings.BASE_DIR
            / "templates"
            / "app"
            / "components"
            / "detail_score_chip_slot.html"
        ).read_text(encoding="utf-8")

        self.assertIn("{% load derived_tv_ratings %}", template)
        self.assertIn(
            "{% derived_tv_rating user media_type media as derived_tv_score %}",
            template,
        )
        self.assertIn(
            'include "app/components/derived_tv_rating_detail.html"',
            template,
        )
