from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from integrations.imports import imdb


class IMDBLookupCacheTests(SimpleTestCase):
    @patch("integrations.imports.imdb.helpers.get_existing_media", return_value={})
    @patch("integrations.imports.imdb.app.providers.tmdb.get_image_url")
    @patch("integrations.imports.imdb.app.providers.tmdb.find")
    def test_successful_lookup_is_reused_across_import_passes(
        self,
        tmdb_find,
        get_image_url,
        _get_existing_media,
    ):
        tmdb_find.return_value = {
            "movie_results": [
                {
                    "id": 278,
                    "title": "The Shawshank Redemption",
                    "poster_path": "/poster.jpg",
                }
            ]
        }
        get_image_url.return_value = "https://example.com/poster.jpg"
        importer = imdb.IMDBImporter(
            None,
            SimpleNamespace(username="test"),
            "new",
        )

        first = importer._lookup_in_tmdb("tt0111161", "Movie")
        second = importer._lookup_in_tmdb("tt0111161", "Movie")

        self.assertEqual(first, second)
        tmdb_find.assert_called_once_with("tt0111161", "imdb_id")
        get_image_url.assert_called_once_with("/poster.jpg")

    @patch("integrations.imports.imdb.helpers.get_existing_media", return_value={})
    @patch("integrations.imports.imdb.app.providers.tmdb.find", return_value={})
    def test_missing_lookup_is_cached(self, tmdb_find, _get_existing_media):
        importer = imdb.IMDBImporter(
            None,
            SimpleNamespace(username="test"),
            "new",
        )

        self.assertIsNone(importer._lookup_in_tmdb("tt9999999", "Movie"))
        self.assertIsNone(importer._lookup_in_tmdb("tt9999999", "Movie"))

        tmdb_find.assert_called_once_with("tt9999999", "imdb_id")

    @patch("integrations.imports.imdb.helpers.get_existing_media", return_value={})
    @patch("integrations.imports.imdb.app.providers.tmdb.find")
    def test_cache_key_keeps_title_types_separate(
        self,
        tmdb_find,
        _get_existing_media,
    ):
        tmdb_find.return_value = {}
        importer = imdb.IMDBImporter(
            None,
            SimpleNamespace(username="test"),
            "new",
        )

        importer._lookup_in_tmdb("tt1234567", "Movie")
        importer._lookup_in_tmdb("tt1234567", "TV Series")

        self.assertEqual(tmdb_find.call_count, 2)
