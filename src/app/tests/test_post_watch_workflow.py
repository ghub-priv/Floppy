from datetime import timedelta
from decimal import Decimal
from itertools import count
from unittest.mock import MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory
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


pytestmark = pytest.mark.django_db
_media_ids = count(1000)


def _user(username="post-watch-user"):
    return get_user_model().objects.create_user(
        username=username,
        password="test-password",
    )


def _next_media_id():
    return str(next(_media_ids))


def _item(**overrides):
    values = {
        "media_id": _next_media_id(),
        "source": Sources.TMDB.value,
        "media_type": MediaTypes.MOVIE.value,
        "library_media_type": MediaTypes.MOVIE.value,
        "title": "Example",
    }
    values.update(overrides)
    return Item.objects.create(**values)


def _movie_watch(user, *, watched_at=None, score=None, release_datetime=None):
    item = _item(release_datetime=release_datetime)
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
    user,
    *,
    watched_at=None,
    score=None,
    episode_number=1,
    release_datetime=None,
):
    show_media_id = _next_media_id()
    tv_item = _item(
        media_id=show_media_id,
        media_type=MediaTypes.TV.value,
        library_media_type=MediaTypes.TV.value,
        title="Example Show",
    )
    tv = TV(item=tv_item, user=user, status=None)
    TV.save_base(tv, force_insert=True)

    season_item = _item(
        media_id=show_media_id,
        media_type=MediaTypes.SEASON.value,
        library_media_type=MediaTypes.TV.value,
        title="Example Show",
        season_number=1,
    )
    season = Season(item=season_item, user=user, related_tv=tv, status=None)
    Season.save_base(season, force_insert=True)

    episode_item = _item(
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


def _post_request(user, path, data):
    request = RequestFactory().post(path, data)
    request.user = user
    return request


def test_watch_key_parser_is_strict():
    assert post_watch._parse_watch_key("movie:12") == ("movie", 12)
    assert post_watch._parse_watch_key("episode:99") == ("episode", 99)
    assert post_watch._parse_watch_key("tv:12") is None
    assert post_watch._parse_watch_key("movie:not-a-number") is None


def test_managed_dismissal_is_user_scoped_and_unique():
    user = _user()
    other = _user("other-post-watch-user")
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

    assert first.pk == duplicate.pk
    assert created is True
    assert created_again is False
    assert other_row.pk != first.pk
    assert PostWatchDismissal._meta.managed is True


def test_recent_unrated_movie_is_in_queue(monkeypatch):
    user = _user()
    movie, play = _movie_watch(user)
    monkeypatch.setattr(post_watch, "_movie_date_suggestions", lambda *_args: [])

    cards = post_watch.build_post_watch_cards(user)

    assert [card["watch_key"] for card in cards] == [f"movie:{play.pk}"]
    assert cards[0]["title"] == movie.item.title


def test_rated_old_and_dismissed_movie_watches_are_not_in_queue(monkeypatch):
    user = _user()
    _movie_watch(user, score=Decimal("8.0"))
    _movie_watch(user, watched_at=timezone.now() - timedelta(days=8))
    _movie, dismissed_play = _movie_watch(user)
    PostWatchDismissal.objects.create(
        user=user,
        watch_key=f"movie:{dismissed_play.pk}",
    )
    monkeypatch.setattr(post_watch, "_movie_date_suggestions", lambda *_args: [])

    assert post_watch.build_post_watch_cards(user) == []


def test_recent_unrated_episode_is_in_queue(monkeypatch):
    user = _user()
    _tv, _season, episode = _episode_watch(user)
    monkeypatch.setattr(post_watch, "_episode_next_url", lambda *_args: "/next/")

    cards = post_watch.build_post_watch_cards(user)

    assert [card["watch_key"] for card in cards] == [f"episode:{episode.pk}"]
    assert cards[0]["next_url"] == "/next/"


def test_dismiss_endpoint_rejects_another_users_watch():
    owner = _user("owner")
    attacker = _user("attacker")
    _movie, play = _movie_watch(owner)
    request = _post_request(
        attacker,
        "/post-watch/dismiss/",
        {"watch_key": f"movie:{play.pk}"},
    )

    response = post_watch.post_watch_dismiss(request)

    assert response.status_code == 400
    assert not PostWatchDismissal.objects.filter(user=attacker).exists()


def test_dismiss_endpoint_persists_exact_watch():
    user = _user()
    _movie, play = _movie_watch(user)
    request = _post_request(
        user,
        "/post-watch/dismiss/",
        {"watch_key": f"movie:{play.pk}"},
    )

    response = post_watch.post_watch_dismiss(request)

    assert response.status_code == 302
    assert PostWatchDismissal.objects.filter(
        user=user,
        watch_key=f"movie:{play.pk}",
    ).exists()


def test_movie_rating_uses_user_scale_and_removes_item_from_queue(monkeypatch):
    user = _user()
    _movie, play = _movie_watch(user)
    monkeypatch.setattr(post_watch, "_movie_date_suggestions", lambda *_args: [])
    request = _post_request(
        user,
        "/post-watch/rate/",
        {"watch_key": f"movie:{play.pk}", "score": "8"},
    )

    response = post_watch.post_watch_rate(request)

    play.movie.refresh_from_db()
    assert response.status_code == 302
    assert play.movie.score == Decimal("8.0")
    assert post_watch.build_post_watch_cards(user) == []


def test_episode_rating_updates_all_plays_for_same_episode(monkeypatch):
    user = _user()
    _tv, season, episode = _episode_watch(user)
    repeat = Episode(
        item=episode.item,
        related_season=season,
        end_date=timezone.now() - timedelta(hours=1),
        score=None,
    )
    Episode.save_base(repeat, force_insert=True)
    monkeypatch.setattr(
        post_watch.history_cache,
        "invalidate_history_days",
        MagicMock(),
    )
    request = _post_request(
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
    assert response.status_code == 302
    assert scores == {Decimal("7.0")}


def test_movie_date_edit_updates_exact_play_and_parent_last_watched(monkeypatch):
    user = _user()
    movie, older = _movie_watch(
        user,
        watched_at=timezone.now() - timedelta(days=2),
    )
    newer = MoviePlay.objects.create(
        movie=movie,
        end_date=timezone.now() - timedelta(days=1),
    )
    Movie.objects.filter(pk=movie.pk).update(end_date=newer.end_date)
    monkeypatch.setattr(
        post_watch.history_cache,
        "invalidate_history_days",
        MagicMock(),
    )
    request = _post_request(
        user,
        "/post-watch/date/",
        {"watch_key": f"movie:{newer.pk}", "watched_date": "2020-01-02"},
    )

    response = post_watch.post_watch_update_date(request)

    newer.refresh_from_db()
    movie.refresh_from_db()
    assert response.status_code == 302
    assert timezone.localdate(newer.end_date).isoformat() == "2020-01-02"
    assert movie.end_date == older.end_date


def test_episode_date_edit_updates_only_selected_play(monkeypatch):
    user = _user()
    _tv, season, episode = _episode_watch(user)
    repeat = Episode(
        item=episode.item,
        related_season=season,
        end_date=timezone.now() - timedelta(hours=1),
    )
    Episode.save_base(repeat, force_insert=True)
    repeat_original = repeat.end_date
    monkeypatch.setattr(
        post_watch.history_cache,
        "invalidate_history_days",
        MagicMock(),
    )
    request = _post_request(
        user,
        "/post-watch/date/",
        {"watch_key": f"episode:{episode.pk}", "watched_date": "2020-01-03"},
    )

    response = post_watch.post_watch_update_date(request)

    episode.refresh_from_db()
    repeat.refresh_from_db()
    assert response.status_code == 302
    assert timezone.localdate(episode.end_date).isoformat() == "2020-01-03"
    assert repeat.end_date == repeat_original


def test_movie_suggestions_delegate_to_smart_watched_dates(monkeypatch):
    user = _user()
    movie, _play = _movie_watch(user)
    resolver = MagicMock(
        return_value={
            "premiere": "2024-01-01",
            "theatrical": "2024-02-01",
            "digital": "",
            "physical": "2024-04-01",
        }
    )
    monkeypatch.setattr(post_watch, "suggestions_for_media", resolver)

    result = post_watch._movie_date_suggestions(movie, user)

    assert [row["kind"] for row in result] == [
        "premiere",
        "theatrical",
        "physical",
    ]
    assert [row["label"] for row in result] == [
        "Premiere",
        "First Theatrical Release",
        "Physical Release",
    ]
    resolver.assert_called_once()


def test_movie_suggestions_include_persisted_release_date(monkeypatch):
    user = _user()
    release_datetime = timezone.now() - timedelta(days=30)
    movie, _play = _movie_watch(
        user,
        release_datetime=release_datetime,
    )
    monkeypatch.setattr(post_watch, "suggestions_for_media", MagicMock(return_value={}))

    result = post_watch._movie_date_suggestions(movie, user)

    assert result[0] == {
        "kind": "release",
        "label": "Release Date",
        "date": timezone.localdate(release_datetime).isoformat(),
    }


def test_episode_suggestions_include_air_date():
    user = _user()
    air_date = timezone.now() - timedelta(days=14)
    _tv, _season, episode = _episode_watch(
        user,
        release_datetime=air_date,
    )

    assert post_watch._episode_date_suggestions(episode) == [
        {
            "kind": "air",
            "label": "Air Date",
            "date": timezone.localdate(air_date).isoformat(),
        }
    ]


def test_next_episode_prefers_known_same_season_item():
    user = _user()
    _tv, _season, episode = _episode_watch(user, episode_number=1)
    _item(
        media_id=episode.item.media_id,
        source=episode.item.source,
        media_type=MediaTypes.EPISODE.value,
        library_media_type=MediaTypes.TV.value,
        title="Episode 2",
        season_number=1,
        episode_number=2,
    )

    url = post_watch._episode_next_url(episode)

    assert "/season/1/episode/2" in url
