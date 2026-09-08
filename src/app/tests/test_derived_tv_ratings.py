from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models import MediaTypes
from app.templatetags import derived_tv_ratings


def _user(*, scale=10, authenticated=True):
    return SimpleNamespace(
        is_authenticated=authenticated,
        rating_scale_max=scale,
    )


def test_tv_rating_deduplicates_rewatches_and_uses_newest_row(monkeypatch):
    monkeypatch.setattr(
        derived_tv_ratings,
        "_episode_rows",
        lambda *_: [
            {"item_id": 1, "score": Decimal("8")},
            {"item_id": 1, "score": Decimal("2")},
            {"item_id": 2, "score": None},
        ],
    )

    result = derived_tv_ratings.derived_tv_rating(
        _user(),
        MediaTypes.TV.value,
        {"media_id": "123", "source": "tmdb"},
    )

    assert result["score"] == "8.00"
    assert result["raw_score"] == 8.0
    assert result["rated"] == 1
    assert result["total"] == 2
    assert result["coverage_percent"] == 50.0
    assert result["specials_excluded"] is True


def test_rating_uses_five_point_display_scale(monkeypatch):
    monkeypatch.setattr(
        derived_tv_ratings,
        "_episode_rows",
        lambda *_: [{"item_id": 1, "score": Decimal("8")}],
    )

    result = derived_tv_ratings.derived_tv_rating(
        _user(scale=5),
        MediaTypes.TV.value,
        {"media_id": "123", "source": "tmdb"},
    )

    assert result["score"] == "4.00"


def test_unrated_completed_episodes_return_coverage_without_average(monkeypatch):
    monkeypatch.setattr(
        derived_tv_ratings,
        "_episode_rows",
        lambda *_: [
            {"item_id": 1, "score": None},
            {"item_id": 2, "score": None},
        ],
    )

    result = derived_tv_ratings.derived_tv_rating(
        _user(),
        MediaTypes.SEASON.value,
        {"media_id": "123", "source": "tmdb", "season_number": 2},
    )

    assert result["score"] is None
    assert result["rated"] == 0
    assert result["total"] == 2
    assert result["coverage_percent"] == 0.0
    assert str(result["label"]) == "Season 2"


def test_specials_mapping_uses_specials_label(monkeypatch):
    monkeypatch.setattr(
        derived_tv_ratings,
        "_episode_rows",
        lambda *_: [{"item_id": 1, "score": Decimal("7")}],
    )

    result = derived_tv_ratings.derived_tv_rating(
        _user(),
        MediaTypes.SEASON.value,
        {"media_id": "123", "source": "tmdb", "season_number": 0},
    )

    assert str(result["label"]) == "Specials"


def test_anonymous_and_non_tv_media_are_not_supported(monkeypatch):
    monkeypatch.setattr(derived_tv_ratings, "_episode_rows", lambda *_: [])

    assert (
        derived_tv_ratings.derived_tv_rating(
            _user(authenticated=False),
            MediaTypes.TV.value,
            {"media_id": "123", "source": "tmdb"},
        )
        is None
    )
    assert (
        derived_tv_ratings.derived_tv_rating(
            _user(),
            MediaTypes.MOVIE.value,
            {"media_id": "123", "source": "tmdb"},
        )
        is None
    )


def test_episode_rows_excludes_specials_for_tv(monkeypatch):
    qs = MagicMock()
    qs.filter.return_value = qs
    qs.order_by.return_value = qs
    expected = object()
    qs.values.return_value = expected

    manager = MagicMock()
    manager.filter.return_value = qs
    fake_episode = SimpleNamespace(objects=manager)
    monkeypatch.setattr(derived_tv_ratings, "Episode", fake_episode)

    result = derived_tv_ratings._episode_rows(
        _user(),
        MediaTypes.TV.value,
        {"media_id": "123", "source": "tmdb"},
    )

    assert result is expected
    qs.filter.assert_called_once_with(item__season_number__gt=0)
    qs.order_by.assert_called_once_with("item_id", "-end_date", "-created_at", "-id")


def test_episode_rows_filters_exact_season_from_mapping(monkeypatch):
    qs = MagicMock()
    qs.filter.return_value = qs
    qs.order_by.return_value = qs
    expected = object()
    qs.values.return_value = expected

    manager = MagicMock()
    manager.filter.return_value = qs
    fake_episode = SimpleNamespace(objects=manager)
    monkeypatch.setattr(derived_tv_ratings, "Episode", fake_episode)

    result = derived_tv_ratings._episode_rows(
        _user(),
        MediaTypes.SEASON.value,
        {"media_id": "123", "source": "tmdb", "season_number": "3"},
    )

    assert result is expected
    qs.filter.assert_called_once_with(item__season_number=3)
