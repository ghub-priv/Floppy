from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from app import rapid_rating


def _user(scale=10):
    return SimpleNamespace(rating_scale_max=scale)


def test_display_factor_respects_five_point_scale():
    assert rapid_rating._display_factor(_user(5)) == Decimal("0.5")
    assert rapid_rating._display_factor(_user(10)) == Decimal("1")


def test_display_factor_falls_back_to_ten_point_scale():
    assert rapid_rating._display_factor(SimpleNamespace(rating_scale_max="bad")) == Decimal("1")


def test_safe_int_rejects_invalid_filter_values():
    assert rapid_rating._safe_int("42") == 42
    assert rapid_rating._safe_int(7) == 7
    assert rapid_rating._safe_int("") is None
    assert rapid_rating._safe_int("not-an-id") is None


def test_show_aggregate_excludes_specials(monkeypatch):
    queryset = MagicMock()
    queryset.values.return_value = queryset
    queryset.annotate.return_value = []
    manager = MagicMock()
    manager.filter.return_value = queryset
    monkeypatch.setattr(
        rapid_rating,
        "Episode",
        SimpleNamespace(objects=manager),
    )

    rapid_rating._stats_by_show(_user(), {11, 12})

    manager.filter.assert_called_once_with(
        related_season__user=rapid_rating._stats_by_show.__globals__["user"]
        if "user" in rapid_rating._stats_by_show.__globals__
        else _user(),
        related_season__related_tv__user=rapid_rating._stats_by_show.__globals__["user"]
        if "user" in rapid_rating._stats_by_show.__globals__
        else _user(),
        related_season__related_tv_id__in={11, 12},
        status="Completed",
        item__season_number__gt=0,
    )


def test_tv_queue_limit_remains_500():
    assert rapid_rating.TV_QUEUE_LIMIT == 500


def test_supported_filter_contracts_remain_stable():
    assert rapid_rating.VALID_RATING_STATES == {"unrated", "rated", "all"}
    assert rapid_rating.VALID_ORDERS == {"recent", "oldest", "episode", "random"}
