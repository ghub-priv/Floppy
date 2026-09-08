from app import smart_watched_dates
from app.models import MediaTypes, Sources


def _release(region, *rows):
    return {
        "iso_3166_1": region,
        "release_dates": [
            {"type": release_type, "release_date": release_date}
            for release_type, release_date in rows
        ],
    }


def test_parse_release_dates_keeps_earliest_worldwide_and_per_region():
    parsed = smart_watched_dates.parse_release_dates(
        {
            "results": [
                _release(
                    "US",
                    (1, "2026-03-10T00:00:00.000Z"),
                    (3, "2026-03-20T00:00:00.000Z"),
                    (4, "2026-05-01T00:00:00.000Z"),
                ),
                _release(
                    "GB",
                    (1, "2026-03-12T00:00:00.000Z"),
                    (2, "2026-03-18T00:00:00.000Z"),
                    (5, "2026-06-01T00:00:00.000Z"),
                ),
                _release(
                    "FR",
                    (3, "2026-03-15T00:00:00.000Z"),
                    (4, "2026-04-25T00:00:00.000Z"),
                ),
            ]
        }
    )

    assert parsed["premiere_release_date"] == "2026-03-10"
    assert parsed["theatrical_release_date"] == "2026-03-15"
    assert parsed["digital_release_date"] == "2026-04-25"
    assert parsed["physical_release_date"] == "2026-06-01"
    assert parsed["smart_release_dates_by_region"]["GB"] == {
        "premiere_release_date": "2026-03-12",
        "theatrical_release_date": "2026-03-18",
        "digital_release_date": "",
        "physical_release_date": "2026-06-01",
    }


def test_preferred_region_falls_back_worldwide_per_release_type():
    parsed = {
        "premiere_release_date": "2026-03-10",
        "theatrical_release_date": "2026-03-15",
        "digital_release_date": "2026-04-25",
        "physical_release_date": "2026-06-01",
        "smart_release_dates_by_region": {
            "GB": {
                "premiere_release_date": "2026-03-12",
                "theatrical_release_date": "2026-03-18",
                "digital_release_date": "",
                "physical_release_date": "2026-06-10",
            }
        },
    }

    assert smart_watched_dates.resolve_movie_suggestions("gb", parsed) == {
        "premiere": "2026-03-12",
        "theatrical": "2026-03-18",
        "digital": "2026-04-25",
        "physical": "2026-06-10",
    }


def test_unset_region_uses_worldwide_dates():
    parsed = {
        "premiere_release_date": "2026-03-10",
        "theatrical_release_date": "2026-03-15",
        "digital_release_date": "2026-04-25",
        "physical_release_date": "2026-06-01",
        "smart_release_dates_by_region": {
            "GB": {
                "premiere_release_date": "2026-03-12",
                "theatrical_release_date": "2026-03-18",
                "digital_release_date": "2026-05-10",
                "physical_release_date": "2026-06-10",
            }
        },
    }

    assert smart_watched_dates.resolve_movie_suggestions("UNSET", parsed) == {
        "premiere": "2026-03-10",
        "theatrical": "2026-03-15",
        "digital": "2026-04-25",
        "physical": "2026-06-01",
    }


def test_release_types_two_and_three_share_first_theatrical_bucket():
    parsed = smart_watched_dates.parse_release_dates(
        {
            "results": [
                _release(
                    "GB",
                    (3, "2026-04-20T00:00:00.000Z"),
                    (2, "2026-04-10T00:00:00.000Z"),
                )
            ]
        }
    )

    assert parsed["theatrical_release_date"] == "2026-04-10"
    assert (
        parsed["smart_release_dates_by_region"]["GB"][
            "theatrical_release_date"
        ]
        == "2026-04-10"
    )


def test_non_movie_or_non_tmdb_media_get_no_suggestions(monkeypatch):
    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("TMDB release dates should not be fetched")

    monkeypatch.setattr(smart_watched_dates, "movie_release_dates", fail_if_called)

    assert (
        smart_watched_dates.suggestions_for_media(
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            media_id="123",
        )
        == {}
    )
    assert (
        smart_watched_dates.suggestions_for_media(
            source=Sources.MAL.value,
            media_type=MediaTypes.MOVIE.value,
            media_id="123",
        )
        == {}
    )
    assert called is False


def test_cache_key_is_feature_versioned():
    assert "v1.0.3" in smart_watched_dates._cache_key("123", "en-GB")
