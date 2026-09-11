from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from app import post_watch
from app.models import MediaTypes, Sources


class PostWatchSmartDatesContractTests(SimpleTestCase):
    """Keep Post-Watch aligned with the integrated Smart Watched Dates contract."""

    @patch(
        "app.post_watch.metadata_resolution.metadata_language_default",
        return_value="en-GB",
    )
    @patch("app.post_watch.suggestions_for_media", return_value={})
    def test_uses_watch_provider_region_and_metadata_language(
        self,
        resolver,
        language_default,
    ):
        user = SimpleNamespace(watch_provider_region="GB")
        movie = SimpleNamespace(
            item=SimpleNamespace(
                release_datetime=None,
                source=Sources.TMDB.value,
                media_id="603",
            )
        )

        post_watch._movie_date_suggestions(movie, user)

        language_default.assert_called_once_with(user)
        resolver.assert_called_once_with(
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            media_id="603",
            preferred_region="GB",
            language="en-GB",
        )
