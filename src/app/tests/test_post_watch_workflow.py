from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
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


class PostWatchWorkflowTests(TestCase):
    """Regression coverage for the Git-native Post-Watch Workflow v1.1.0."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_ids = count(1000)

    def setUp(self):
        self.factory = RequestFactory()

    def _user(self, username="post-watch-user"):
        return get_user_model().objects.create_user(
            username=username,
            password="test-password",
        )

    def _next_media_id(self):
        return str(next(self._media_ids))

    def _item(self, **overrides):
        values = {
            "media_id": self._next_media_id(),
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.MOVIE.value,
            "library_media_type": MediaTypes.MOVIE.value,
            "title": "Example",
        }
        values.update(overrides)
        return Item.objects.create(**values)

    def _movie_watch(
        self,
        user,
        *,
        watched_at=None,
        score=None,
        release_datetime=None,
    ):
        item = self._item(release_datetime=release_datetime)
        movie = Movie(item=item, user=user, status=None, score=score)
        Movie.save_base(movie, force_insert=True)
        play = MoviePlay.objects.create(
            movie=movie,
            end_date=watched_at or timezone.now(),
        )
        Movie.objects.filter(pk=movie.pk).update(end_date=play.end_date)
        movie.refresh_from_db()
        return movie, play

    def _episode_watch(
        self,
        user,
        *,
        watched_at=None,
        score=None,
        episode_number=1,
        release_datetime=None,
    ):
        show_media_id = self._next_media_id()
        tv_item = self._item(
            media_id=show_media_id,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.TV.value,
            title="Example Show",
        )
        tv = TV(item=tv_item, user=user, status=None)
        TV.save_base(tv, force_insert=True)

        season_item = self._item(
            media_id=show_media_id,
            media_type=MediaTypes.SEASON.value,
            library_media_type=MediaTypes.TV.value,
            title="Example Show",
            season_number=1,
        )
        season = Season(item=season_item, user=user, related_tv=tv, status=None)
        Season.save_base(season, force_insert=True)

        episode_item = self._item(
            media_id=show_media_id,
            media_type=MediaTypes.EPISODE.value,
            library_media_type=MediaTypes.TV.value,
            title=f"Episode {episode_number}",
            season_number=1,
            episode_number=episode_number,
            release_datetime=release_datetime,
        )
        episode = Episode(
            item=episode_item,
            related_season=season,
            end_date=watched_at or timezone.now(),
            score=score,
        )
        Episode.save_base(episode, force_insert=True)
        return tv, season, episode

    def _post_request(self, user, path, data):
        request = self.factory.post(path, data)
        request.user = user
        return request

    def test_watch_key_parser_is_strict(self):
        self.assertEqual(post_watch._parse_watch_key("movie:12"), ("movie", 12))
        self.assertEqual(
            post_watch._parse_watch_key("episode:99"),
            ("episode", 99),
        )
        self.assertIsNone(post_watch._parse_watch_key("tv:12"))
        self.assertIsNone(post_watch._parse_watch_key("movie:not-a-number"))

    def test_managed_dismissal_is_user_scoped_and_unique(self):
        user = self._user()
        other = self._user("other-post-watch-user")
        first, created = PostWatchDismissal.objects.get_or_create(
            user=user,
            watch_key="movie:1",
        )
        duplicate, created_again = PostWatchDismissal.objects.get_or_create(
            user=user,
            watch_key="movie:1",
        )
        other_row = PostWatchDismissal.objects.create(
            user=other,
            watch_key="movie:1",
        )

        self.assertEqual(first.pk, duplicate.pk)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertNotEqual(other_row.pk, first.pk)
        self.assertTrue(PostWatchDismissal._meta.managed)

    @patch("app.post_watch._movie_date_suggestions", return_value=[])
    def test_recent_unrated_movie_is_in_queue(self, _suggestions):
        user = self._user()
        movie, play = self._movie_watch(user)

        cards = post_watch.build_post_watch_cards(user)

        self.assertEqual(
            [card["watch_key"] for card in cards],
            [f"movie:{play.pk}"],
        )
        self.assertEqual(cards[0]["title"], movie.item.title)

    @patch("app.post_watch._movie_date_suggestions", return_value=[])
    def test_rated_old_and_dismissed_movie_watches_are_not_in_queue(
        self,
        _suggestions,
    ):
        user = self._user()
        self._movie_watch(user, score=Decimal("8.0"))
        self._movie_watch(user, watched_at=timezone.now() - timedelta(days=8))
        _movie, dismissed_play = self._movie_watch(user)
        PostWatchDismissal.objects.create(
            user=user,
            watch_key=f"movie:{dismissed_play.pk}",
        )

        self.assertEqual(post_watch.build_post_watch_cards(user), [])

    @patch("app.post_watch._episode_next_url", return_value="/next/")
    def test_recent_unrated_episode_is_in_queue(self, _next_url):
        user = self._user()
        _tv, _season, episode = self._episode_watch(user)

        cards = post_watch.build_post_watch_cards(user)

        self.assertEqual(
            [card["watch_key"] for card in cards],
            [f"episode:{episode.pk}"],
        )
        self.assertEqual(cards[0]["next_url"], "/next/")

    def test_dismiss_endpoint_rejects_another_users_watch(self):
        owner = self._user("owner")
        attacker = self._user("attacker")
        _movie, play = self._movie_watch(owner)
        request = self._post_request(
            attacker,
            "/post-watch/dismiss/",
            {"watch_key": f"movie:{play.pk}"},
        )

        response = post_watch.post_watch_dismiss(request)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PostWatchDismissal.objects.filter(user=attacker).exists())

    def test_dismiss_endpoint_persists_exact_watch(self):
        user = self._user()
        _movie, play = self._movie_watch(user)
        request = self._post_request(
            user,
            "/post-watch/dismiss/",
            {"watch_key": f"movie:{play.pk}"},
        )

        response = post_watch.post_watch_dismiss(request)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            PostWatchDismissal.objects.filter(
                user=user,
                watch_key=f"movie:{play.pk}",
            ).exists()
        )

    @patch("app.post_watch._movie_date_suggestions", return_value=[])
    def test_movie_rating_uses_user_scale_and_removes_item_from_queue(
        self,
        _suggestions,
    ):
        user = self._user()
        _movie, play = self._movie_watch(user)
        request = self._post_request(
            user,
            "/post-watch/rate/",
            {"watch_key": f"movie:{play.pk}", "score": "8"},
        )

        response = post_watch.post_watch_rate(request)

        play.movie.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(play.movie.score, Decimal("8.0"))
        self.assertEqual(post_watch.build_post_watch_cards(user), [])

    @patch("app.post_watch.history_cache.invalidate_history_days")
    def test_episode_rating_updates_all_plays_for_same_episode(
        self,
        _invalidate,
    ):
        user = self._user()
        _tv, season, episode = self._episode_watch(user)
        repeat = Episode(
            item=episode.item,
            related_season=season,
            end_date=timezone.now() - timedelta(hours=1),
            score=None,
        )
        Episode.save_base(repeat, force_insert=True)
        request = self._post_request(
            user,
            "/post-watch/rate/",
            {"watch_key": f"episode:{episode.pk}", "score": "7"},
        )

        response = post_watch.post_watch_rate(request)

        scores = set(
            Episode.objects.filter(
                related_season=season,
                item__episode_number=1,
            ).values_list("score", flat=True)
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(scores, {Decimal("7.0")})

    @patch("app.post_watch.history_cache.invalidate_history_days")
    def test_movie_date_edit_updates_exact_play_and_parent_last_watched(
        self,
        _invalidate,
    ):
        user = self._user()
        movie, older = self._movie_watch(
            user,
            watched_at=timezone.now() - timedelta(days=2),
        )
        newer = MoviePlay.objects.create(
            movie=movie,
            end_date=timezone.now() - timedelta(days=1),
        )
        Movie.objects.filter(pk=movie.pk).update(end_date=newer.end_date)
        request = self._post_request(
            user,
            "/post-watch/date/",
            {"watch_key": f"movie:{newer.pk}", "watched_date": "2020-01-02"},
        )

        response = post_watch.post_watch_update_date(request)

        newer.refresh_from_db()
        movie.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(timezone.localdate(newer.end_date).isoformat(), "2020-01-02")
        self.assertEqual(movie.end_date, older.end_date)

    @patch("app.post_watch.history_cache.invalidate_history_days")
    def test_episode_date_edit_updates_only_selected_play(self, _invalidate):
        user = self._user()
        _tv, season, episode = self._episode_watch(user)
        repeat = Episode(
            item=episode.item,
            related_season=season,
            end_date=timezone.now() - timedelta(hours=1),
        )
        Episode.save_base(repeat, force_insert=True)
        repeat_original = repeat.end_date
        request = self._post_request(
            user,
            "/post-watch/date/",
            {"watch_key": f"episode:{episode.pk}", "watched_date": "2020-01-03"},
        )

        response = post_watch.post_watch_update_date(request)

        episode.refresh_from_db()
        repeat.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(timezone.localdate(episode.end_date).isoformat(), "2020-01-03")
        self.assertEqual(repeat.end_date, repeat_original)

    @patch("app.post_watch.suggestions_for_media")
    def test_movie_suggestions_delegate_to_smart_watched_dates(self, resolver):
        user = self._user()
        movie, _play = self._movie_watch(user)
        resolver.return_value = {
            "premiere": "2024-01-01",
            "theatrical": "2024-02-01",
            "digital": "",
            "physical": "2024-04-01",
        }

        result = post_watch._movie_date_suggestions(movie, user)

        self.assertEqual(
            [row["kind"] for row in result],
            ["premiere", "theatrical", "physical"],
        )
        self.assertEqual(
            [row["label"] for row in result],
            ["Premiere", "First Theatrical Release", "Physical Release"],
        )
        resolver.assert_called_once()

    @patch("app.post_watch.suggestions_for_media", return_value={})
    def test_movie_suggestions_include_persisted_release_date(self, _resolver):
        user = self._user()
        release_datetime = datetime(2024, 5, 6, 12, 0, tzinfo=UTC)
        movie, _play = self._movie_watch(
            user,
            release_datetime=release_datetime,
        )

        result = post_watch._movie_date_suggestions(movie, user)

        self.assertEqual(
            result[0],
            {
                "kind": "release",
                "label": "Release Date",
                "date": "2024-05-06",
            },
        )

    def test_episode_suggestions_include_air_date(self):
        user = self._user()
        air_date = datetime(2024, 6, 7, 12, 0, tzinfo=UTC)
        _tv, _season, episode = self._episode_watch(
            user,
            release_datetime=air_date,
        )

        self.assertEqual(
            post_watch._episode_date_suggestions(episode),
            [{"kind": "air", "label": "Air Date", "date": "2024-06-07"}],
        )

    def test_next_episode_prefers_known_same_season_item(self):
        user = self._user()
        _tv, _season, episode = self._episode_watch(user, episode_number=1)
        self._item(
            media_id=episode.item.media_id,
            source=episode.item.source,
            media_type=MediaTypes.EPISODE.value,
            library_media_type=MediaTypes.TV.value,
            title="Episode 2",
            season_number=1,
            episode_number=2,
        )

        url = post_watch._episode_next_url(episode)

        self.assertIn("/season/1/episode/2", url)
