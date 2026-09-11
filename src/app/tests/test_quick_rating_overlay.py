from types import SimpleNamespace

from django.conf import settings

from app.models import MediaTypes
from app.templatetags import quick_rating


def _user():
    return SimpleNamespace(is_authenticated=True)


def _fake_reverse(name, args):
    return f"/{name}/" + "/".join(str(value) for value in args)


def test_history_music_routes_album_aggregate(monkeypatch):
    monkeypatch.setattr(quick_rating, "reverse", _fake_reverse)

    result = quick_rating.quick_rating_context(
        {
            "user": _user(),
            "quick_rating_media_type": MediaTypes.MUSIC.value,
            "quick_rating_instance_id": 101,
            "quick_rating_value": 8,
            "entry": {"album": {"id": 202}},
        }
    )

    assert result["enabled"] is True
    assert result["target_kind"] == "album"
    assert result["instance_id"] == 202
    assert result["url"] == "/update_album_score/202"


def test_direct_music_row_routes_track_before_album(monkeypatch):
    monkeypatch.setattr(quick_rating, "reverse", _fake_reverse)

    Music = type("Music", (), {})
    music = Music()
    music.id = 303
    music.score = 7
    music.album = SimpleNamespace(id=404)
    music.artist = SimpleNamespace(id=505)
    music.track = SimpleNamespace(id=606)

    result = quick_rating.quick_rating_context(
        {
            "user": _user(),
            "media": music,
            "resolved_media_type": MediaTypes.MUSIC.value,
        }
    )

    assert result["enabled"] is True
    assert result["target_kind"] == "track"
    assert result["instance_id"] == 303
    assert result["url"] == "/update_track_score/303"


def test_explicit_artist_grid_target_wins(monkeypatch):
    monkeypatch.setattr(quick_rating, "reverse", _fake_reverse)

    result = quick_rating.quick_rating_context(
        {
            "user": _user(),
            "quick_rating_media_type": MediaTypes.MUSIC.value,
            "quick_rating_music_kind": "artist",
            "quick_rating_music_id": 707,
            "quick_rating_value": 6,
        }
    )

    assert result["target_kind"] == "artist"
    assert result["url"] == "/update_artist_score/707"


def test_standard_media_uses_generic_score_route(monkeypatch):
    monkeypatch.setattr(quick_rating, "reverse", _fake_reverse)

    result = quick_rating.quick_rating_context(
        {
            "user": _user(),
            "quick_rating_media_type": MediaTypes.MOVIE.value,
            "quick_rating_instance_id": 808,
        }
    )

    assert result["enabled"] is True
    assert result["target_kind"] == "standard"
    assert result["url"] == f"/update_media_score/{MediaTypes.MOVIE.value}/808"


def test_podcast_remains_excluded(monkeypatch):
    monkeypatch.setattr(quick_rating, "reverse", _fake_reverse)

    result = quick_rating.quick_rating_context(
        {
            "user": _user(),
            "quick_rating_media_type": MediaTypes.PODCAST.value,
            "quick_rating_instance_id": 909,
        }
    )

    assert result["enabled"] is False
    assert result["reason"] == "specialised_score_path"


def test_history_card_uses_shared_quick_rating_component():
    template = (
        settings.BASE_DIR / "templates" / "app" / "components" / "history_card.html"
    ).read_text(encoding="utf-8")

    assert "quick_rating_overlay_v4_history_shared_component" in template  # noqa: S101
    assert 'include "app/components/media_card_rating.html"' in template  # noqa: S101
    assert "quick_rating_media_type=entry.media_type" in template  # noqa: S101
    assert "quick_rating_instance_id=entry.instance_id" in template  # noqa: S101
