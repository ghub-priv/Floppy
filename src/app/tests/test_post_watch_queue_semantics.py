from datetime import timedelta
from importlib import import_module
from itertools import count
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from app import post_watch
from app.models import (
    Episode,
    Item,
    MediaTypes,
    Movie,
    MoviePlay,
    PostWatchDismissal,
    Season,
    Sources,
    TV,
)

post_watch_migration = import_module("app.migrations.0179_postwatchdismissal")


class PostWatchQueueSemanticsTests(TestCase):
    """Keep the Git-native queue aligned with the accepted r13 behaviour."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_ids = count(90000)

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="post-watch-queue-user",
            password="test-password",
        )

    def _item(self, **overrides):
        values = {
            "media_id": str(next(self._media_ids)),
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.MOVIE.value,
            "library_media_type": MediaTypes.MOVIE.value,
            "title": "Example",
        }
        values.update(overrides)
        return Item.objects.create(**values)

    def _movie(self):
        item = self._item(title="Repeat Movie")
        movie = Movie(item=item, user=self.user, status=None, score=None)
        Movie.save_base(movie, force_insert=True)
        return movie

    def _season(self):
        media_id = str(next(self._media_ids))
        tv_item = self._item(
            media_id=media_id,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.TV.value,
            title="Repeat Show",
        )
        tv = TV(item=tv_item, user=self.user, status=None)
        TV.save_base(tv, force_insert=True)

        season_item = self._item(
            media_id=media_id,
            media_type=MediaTypes.SEASON.value,
            library_media_type=MediaTypes.TV.value,
            title="Repeat Show",
            season_number=1,
        )
        season = Season(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=None,
        )
        Season.save_base(season, force_insert=True)
        return season, media_id

    @patch("app.post_watch._movie_date_suggestions", return_value=[])
    def test_repeat_movie_surfaces_only_newest_play_and_dismissal_hides_item(
        self,
        _suggestions,
    ):
        movie = self._movie()
        older = MoviePlay.objects.create(
            movie=movie,
            end_date=timezone.now() - timedelta(hours=2),
        )
        newer = MoviePlay.objects.create(
            movie=movie,
            end_date=timezone.now() - timedelta(hours=1),
        )

        cards = post_watch.build_post_watch_cards(self.user)

        self.assertEqual(
            [card["watch_key"] for card in cards],
            [f"movie:{newer.pk}"],
        )
        self.assertNotEqual(cards[0]["watch_key"], f"movie:{older.pk}")

        PostWatchDismissal.objects.create(
            user=self.user,
            watch_key=f"movie:{newer.pk}",
        )
        self.assertEqual(post_watch.build_post_watch_cards(self.user), [])

    @patch("app.post_watch._episode_next_url", return_value="")
    def test_repeat_episode_surfaces_only_newest_play_and_dropped_is_excluded(
        self,
        _next_url,
    ):
        season, media_id = self._season()
        episode_item = self._item(
            media_id=media_id,
            media_type=MediaTypes.EPISODE.value,
            library_media_type=MediaTypes.TV.value,
            title="Episode 1",
            season_number=1,
            episode_number=1,
        )
        older = Episode(
            item=episode_item,
            related_season=season,
            end_date=timezone.now() - timedelta(hours=2),
            score=None,
        )
        Episode.save_base(older, force_insert=True)
        newer = Episode(
            item=episode_item,
            related_season=season,
            end_date=timezone.now() - timedelta(hours=1),
            score=None,
        )
        Episode.save_base(newer, force_insert=True)

        dropped_item = self._item(
            media_id=media_id,
            media_type=MediaTypes.EPISODE.value,
            library_media_type=MediaTypes.TV.value,
            title="Episode 2",
            season_number=1,
            episode_number=2,
        )
        dropped = Episode(
            item=dropped_item,
            related_season=season,
            end_date=timezone.now(),
            score=None,
            dropped=True,
        )
        Episode.save_base(dropped, force_insert=True)

        cards = post_watch.build_post_watch_cards(self.user)

        self.assertEqual(
            [card["watch_key"] for card in cards],
            [f"episode:{newer.pk}"],
        )
        card_keys = {card["watch_key"] for card in cards}
        self.assertNotIn(f"episode:{older.pk}", card_keys)
        self.assertNotIn(f"episode:{dropped.pk}", card_keys)

    def test_legacy_movie_without_plays_is_backfilled_for_post_watch(self):
        movie = self._movie()
        watched_at = timezone.now() - timedelta(days=2)
        Movie.objects.filter(pk=movie.pk).update(end_date=watched_at)
        self.assertFalse(MoviePlay.objects.filter(movie=movie).exists())

        schema_editor = SimpleNamespace(connection=connection)
        post_watch_migration.backfill_legacy_movie_plays(
            django_apps,
            schema_editor,
        )

        play = MoviePlay.objects.get(movie=movie)
        movie.refresh_from_db()
        self.assertEqual(play.end_date, movie.end_date)

    def test_legacy_movie_dismissal_key_translates_to_movie_play_key(self):
        movie = self._movie()
        watched_at = timezone.now() - timedelta(days=1)
        Movie.objects.filter(pk=movie.pk).update(end_date=watched_at)
        play = MoviePlay.objects.create(movie=movie, end_date=watched_at)
        legacy_key = f"movie:{movie.pk}:{int(watched_at.timestamp())}"

        translated = post_watch_migration._translate_legacy_watch_key(
            legacy_key,
            Movie,
            MoviePlay,
            connection.alias,
        )

        self.assertEqual(translated, f"movie:{play.pk}")
