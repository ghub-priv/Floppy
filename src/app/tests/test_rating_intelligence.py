from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import TestCase
from django.urls import reverse

from app import rating_intelligence, rating_intelligence_advanced
from app.models import RatingIntelligencePreference
from app.rating_intelligence_preferences import (
    PRI_COLOUR_DEFAULTS,
    rating_intelligence_colours_for_user,
)


class RatingIntelligenceCoreTests(TestCase):
    def test_media_kind_contract_defaults_to_movies(self):
        self.assertEqual(rating_intelligence._normalise_media_kind("movies"), "movies")
        self.assertEqual(rating_intelligence._normalise_media_kind("tv"), "tv")
        self.assertEqual(rating_intelligence._normalise_media_kind("combined"), "combined")
        self.assertEqual(rating_intelligence._normalise_media_kind("unknown"), "movies")
        self.assertEqual(rating_intelligence._normalise_media_kind(None), "movies")

    def test_rating_distribution_preserves_counts_and_percentages(self):
        rows = rating_intelligence._rating_distribution([5.0, 7.0, 7.0, 9.0])

        self.assertEqual(
            rows,
            [
                {"score": 5.0, "count": 1, "pct": 25.0, "bar_pct": 50.0},
                {"score": 7.0, "count": 2, "pct": 50.0, "bar_pct": 100.0},
                {"score": 9.0, "count": 1, "pct": 25.0, "bar_pct": 50.0},
            ],
        )

    def test_combined_profile_gives_movies_and_tv_equal_medium_weight(self):
        movie_profile = {
            "source_rows": 100,
            "unique_rated_titles": 100,
            "deduplicated_rows": 0,
            "baseline": {"mean": 8.0, "high_rating_7_plus_pct": 80.0},
            "world_alignment": {"pearson": 0.3},
            "families": {
                "genres": {
                    "label": "Genres",
                    "eligible_rows": [
                        {"label": "Drama", "shrunk_delta": 1.0, "samples": 40},
                        {"label": "Comedy", "shrunk_delta": 0.8, "samples": 30},
                    ],
                },
            },
        }
        tv_profile = {
            "source_rows": 1000,
            "unique_rated_titles": 10,
            "deduplicated_rows": 50,
            "baseline": {"mean": 6.0, "high_rating_7_plus_pct": 40.0},
            "world_alignment": {"pearson": 0.2},
            "families": {
                "genres": {
                    "label": "Genres",
                    "eligible_rows": [
                        {"label": "Drama", "shrunk_delta": 0.5, "samples": 8},
                        {"label": "Comedy", "shrunk_delta": -1.2, "samples": 7},
                    ],
                },
            },
            "tv_summary": {"rated_episodes": 950},
        }

        def fake_profile(_user, media_kind="movies"):
            return tv_profile if media_kind == "tv" else movie_profile

        with patch.object(
            rating_intelligence,
            "compute_rating_intelligence_profile",
            side_effect=fake_profile,
        ):
            profile = rating_intelligence._compute_combined_profile(object())

        self.assertEqual(profile["combined_summary"]["media_balanced_baseline"], 7.0)
        self.assertEqual(profile["combined_summary"]["rated_episodes"], 950)
        self.assertEqual(profile["combined_summary"]["shared_signals"], 2)
        genres = profile["cross_media_cards"][0]
        self.assertEqual(genres["positive"][0]["label"], "Drama")
        self.assertEqual(genres["positive"][0]["combined_effect"], 0.75)
        self.assertEqual(genres["divergent"][0]["label"], "Comedy")

    def test_advanced_public_calibration_falls_back_without_public_scores(self):
        samples = [
            {"world_score": None, "rating": 6.0},
            {"world_score": None, "rating": 8.0},
        ]

        calibration = rating_intelligence_advanced._public_calibration(samples)

        self.assertEqual(calibration["sample_size"], 0)
        self.assertEqual(calibration["fallback"], 7.0)
        self.assertEqual(calibration["intercept"], 7.0)
        self.assertEqual(calibration["slope"], 0.0)

    def test_advanced_engine_version_matches_ported_contract(self):
        self.assertEqual(rating_intelligence_advanced.ADVANCED_ENGINE_VERSION, "2.3.0")
        self.assertEqual(rating_intelligence.TV_EXTENSION_VERSION, "2.1.0")
        self.assertEqual(rating_intelligence.COMBINED_EXTENSION_VERSION, "2.3.0")


class RatingIntelligencePreferenceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="rating-intelligence-user",
            password="password",
        )
        self.client.force_login(self.user)

    def test_colour_defaults_do_not_require_a_preference_row(self):
        self.assertEqual(
            rating_intelligence_colours_for_user(self.user),
            PRI_COLOUR_DEFAULTS,
        )
        self.assertFalse(
            RatingIntelligencePreference.objects.filter(user=self.user).exists()
        )

    def test_colour_settings_persist_and_normalise_hex_values(self):
        response = self.client.post(
            reverse("rating_intelligence_colours"),
            {
                "pri_colour_info": "#AABBCC",
                "pri_colour_positive": "#11AA22",
                "pri_colour_negative": "#CC3344",
                "pri_colour_caution": "#DDAA00",
            },
        )

        self.assertRedirects(response, reverse("rating_intelligence_colours"))
        preference = RatingIntelligencePreference.objects.get(user=self.user)
        self.assertEqual(preference.pri_colour_info, "#aabbcc")
        self.assertEqual(preference.pri_colour_positive, "#11aa22")
        self.assertEqual(preference.pri_colour_negative, "#cc3344")
        self.assertEqual(preference.pri_colour_caution, "#ddaa00")

    def test_invalid_colour_payload_is_rejected_without_creating_preferences(self):
        response = self.client.post(
            reverse("rating_intelligence_colours"),
            {
                "pri_colour_info": "red",
                "pri_colour_positive": "#11AA22",
                "pri_colour_negative": "#CC3344",
                "pri_colour_caution": "#DDAA00",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            RatingIntelligencePreference.objects.filter(user=self.user).exists()
        )
        self.assertContains(response, "Colours must use #RRGGBB hexadecimal format.")

    def test_demo_user_cannot_change_rating_intelligence_colours(self):
        self.user.is_demo = True
        self.user.save(update_fields=["is_demo"])

        response = self.client.post(
            reverse("rating_intelligence_colours"),
            PRI_COLOUR_DEFAULTS,
        )

        self.assertRedirects(response, reverse("rating_intelligence_colours"))
        self.assertFalse(
            RatingIntelligencePreference.objects.filter(user=self.user).exists()
        )

    def test_rating_intelligence_route_attaches_persisted_colours(self):
        RatingIntelligencePreference.objects.create(
            user=self.user,
            pri_colour_info="#123456",
        )

        def fake_rating_view(request):
            self.assertEqual(request.user.pri_colour_info, "#123456")
            self.assertEqual(request.user.pri_colour_positive, "#34d399")
            return HttpResponse("ok")

        with patch(
            "app.rating_intelligence_views._rating_intelligence",
            side_effect=fake_rating_view,
        ):
            response = self.client.get(reverse("rating_intelligence"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    def test_advanced_route_attaches_persisted_colours(self):
        RatingIntelligencePreference.objects.create(
            user=self.user,
            pri_colour_negative="#654321",
        )

        def fake_advanced_view(request):
            self.assertEqual(request.user.pri_colour_negative, "#654321")
            return HttpResponse("ok")

        with patch(
            "app.rating_intelligence_views._advanced_rating_intelligence",
            side_effect=fake_advanced_view,
        ):
            response = self.client.get(reverse("rating_intelligence_advanced"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
