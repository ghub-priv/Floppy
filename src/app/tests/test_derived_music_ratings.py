from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.templatetags import derived_music_ratings


def _user(*, scale=10, authenticated=True):
    return SimpleNamespace(
        is_authenticated=authenticated,
        rating_scale_max=scale,
    )


def test_artist_uses_direct_album_rating_before_track_derived_rating():
    result = derived_music_ratings._artist_result_from_albums(
        _user(),
        artist_id=1,
        album_ids=[10, 20, 30],
        direct_scores={
            10: Decimal("9.0"),
            30: Decimal("8.0"),
        },
        album_results={
            10: {"_raw_decimal": Decimal("2.0")},
            20: {"_raw_decimal": Decimal("7.6")},
            30: {"_raw_decimal": Decimal("10.0")},
        },
    )

    assert result["score"] == "8.20"
    assert result["raw_score"] == 8.2
    assert result["rated"] == 3
    assert result["total"] == 3
    assert result["direct_album_count"] == 2
    assert result["derived_album_count"] == 1


def test_artist_excludes_unrated_albums_without_treating_them_as_zero():
    result = derived_music_ratings._artist_result_from_albums(
        _user(),
        artist_id=1,
        album_ids=[10, 20],
        direct_scores={},
        album_results={
            10: {"_raw_decimal": Decimal("8.0")},
            20: {"_raw_decimal": None},
        },
    )

    assert result["score"] == "8.00"
    assert result["rated"] == 1
    assert result["total"] == 2
    assert result["coverage_percent"] == 50.0


def test_display_score_respects_five_point_scale():
    result = derived_music_ratings._build_result(
        _user(scale=5),
        kind="album",
        raw_score=Decimal("8.0"),
        rated=1,
        total=1,
        title="test",
    )

    assert result["score"] == "4.00"


def test_entity_id_accepts_mapping_tracker_and_integer():
    assert derived_music_ratings._entity_id("album", {"album_id": "12"}) == "12"
    assert derived_music_ratings._entity_id("artist", {"id": 13}) == 13
    assert (
        derived_music_ratings._entity_id(
            "album",
            SimpleNamespace(album_id=14, id=99),
        )
        == 14
    )
    assert derived_music_ratings._entity_id("artist", "15") == 15


def test_album_results_uses_newest_music_row_for_repeated_track(monkeypatch):
    track_qs = MagicMock()
    track_qs.values.return_value = [
        {"id": 101, "album_id": 10, "title": "Track One"},
        {"id": 102, "album_id": 10, "title": "Track Two"},
    ]
    track_manager = MagicMock()
    track_manager.filter.return_value = track_qs
    monkeypatch.setattr(
        derived_music_ratings,
        "Track",
        SimpleNamespace(objects=track_manager),
    )

    music_qs = MagicMock()
    music_qs.order_by.return_value = music_qs
    music_qs.values.return_value = [
        {
            "id": 3,
            "album_id": 10,
            "track_id": 101,
            "item_id": 1001,
            "item__title": "Track One",
            "score": Decimal("9"),
        },
        {
            "id": 2,
            "album_id": 10,
            "track_id": 101,
            "item_id": 1001,
            "item__title": "Track One",
            "score": Decimal("3"),
        },
        {
            "id": 1,
            "album_id": 10,
            "track_id": 102,
            "item_id": 1002,
            "item__title": "Track Two",
            "score": Decimal("7"),
        },
    ]
    music_manager = MagicMock()
    music_manager.filter.return_value = music_qs
    monkeypatch.setattr(
        derived_music_ratings,
        "Music",
        SimpleNamespace(objects=music_manager),
    )

    result = derived_music_ratings._album_results(_user(), {10})[10]

    assert result["score"] == "8.00"
    assert result["rated"] == 2
    assert result["total"] == 2
    music_qs.order_by.assert_called_once_with("-end_date", "-created_at", "-id")


def test_album_results_maps_unique_legacy_title_to_catalog_track(monkeypatch):
    track_qs = MagicMock()
    track_qs.values.return_value = [
        {"id": 101, "album_id": 10, "title": "Track One"},
    ]
    track_manager = MagicMock()
    track_manager.filter.return_value = track_qs
    monkeypatch.setattr(
        derived_music_ratings,
        "Track",
        SimpleNamespace(objects=track_manager),
    )

    music_qs = MagicMock()
    music_qs.order_by.return_value = music_qs
    music_qs.values.return_value = [
        {
            "id": 1,
            "album_id": 10,
            "track_id": None,
            "item_id": 1001,
            "item__title": "  TRACK   ONE ",
            "score": Decimal("8"),
        },
    ]
    music_manager = MagicMock()
    music_manager.filter.return_value = music_qs
    monkeypatch.setattr(
        derived_music_ratings,
        "Music",
        SimpleNamespace(objects=music_manager),
    )

    result = derived_music_ratings._album_results(_user(), {10})[10]

    assert result["score"] == "8.00"
    assert result["rated"] == 1
    assert result["total"] == 1


def test_anonymous_user_and_unknown_kind_are_not_supported():
    assert (
        derived_music_ratings.derived_music_rating(
            {},
            _user(authenticated=False),
            "album",
            1,
        )
        is None
    )
    assert (
        derived_music_ratings.derived_music_rating(
            {},
            _user(),
            "podcast",
            1,
        )
        is None
    )
