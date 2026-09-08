from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import kodi_library


def _indexes():
    return {
        "items": {},
        "ids": {
            media_type: {
                provider: defaultdict(set)
                for provider in kodi_library.SUPPORTED_IDS
            }
            for media_type in ("movie", "tv", "episode")
        },
        "title_year": {
            media_type: defaultdict(set)
            for media_type in ("movie", "tv", "episode")
        },
        "title_only": {
            media_type: defaultdict(set)
            for media_type in ("movie", "tv", "episode")
        },
    }


def test_provider_and_external_id_normalisation():
    assert kodi_library._normalise_provider("TMDB_ID") == "tmdb"
    assert kodi_library._normalise_provider("thetvdb") == "tvdb"
    assert kodi_library._normalise_provider("IMDb") == "imdb"
    assert kodi_library._normalise_provider("unknown") is None
    assert kodi_library._normalise_external_id("tmdb", "000123") == "123"
    assert kodi_library._normalise_external_id("imdb", "TT1234567") == "tt1234567"


def test_exact_external_id_wins_over_title_fallback():
    indexes = _indexes()
    indexes["ids"]["movie"]["tmdb"]["123"].add(7)
    indexes["title_year"]["movie"][("different title", 2020)].add(99)

    result = kodi_library._match_item(
        row={
            "title": "Different Title",
            "year": 2020,
            "uniqueid": {"tmdb": "123"},
        },
        media_type="movie",
        indexes=indexes,
    )

    assert result["item_id"] == 7
    assert result["confidence"] == "exact"
    assert result["method"] == "tmdb"


def test_ambiguous_external_id_is_never_guessed_by_title():
    indexes = _indexes()
    indexes["ids"]["movie"]["tmdb"]["123"].update({7, 8})
    indexes["title_year"]["movie"][("the movie", 2020)].add(7)

    result = kodi_library._match_item(
        row={
            "title": "The Movie",
            "year": 2020,
            "uniqueid": {"tmdb": "123"},
        },
        media_type="movie",
        indexes=indexes,
    )

    assert result["item_id"] is None
    assert result["confidence"] == "ambiguous"
    assert result["candidate_count"] == 2
    assert result["diagnosis_code"] == "ambiguous_external_id"


def test_title_year_fallback_requires_unique_candidate():
    indexes = _indexes()
    key = ("the movie", 2020)
    indexes["title_year"]["movie"][key].add(11)

    matched = kodi_library._match_item(
        row={"title": "The Movie", "year": 2020},
        media_type="movie",
        indexes=indexes,
    )
    assert matched["item_id"] == 11
    assert matched["method"] == "title+year"
    assert matched["confidence"] == "fallback"

    indexes["title_year"]["movie"][key].add(12)
    ambiguous = kodi_library._match_item(
        row={"title": "The Movie", "year": 2020},
        media_type="movie",
        indexes=indexes,
    )
    assert ambiguous["item_id"] is None
    assert ambiguous["confidence"] == "ambiguous"


def test_title_only_fallback_is_used_only_when_kodi_year_is_missing():
    indexes = _indexes()
    indexes["title_only"]["movie"]["the movie"].add(11)

    missing_year = kodi_library._match_item(
        row={"title": "The Movie"},
        media_type="movie",
        indexes=indexes,
    )
    assert missing_year["item_id"] == 11
    assert missing_year["method"] == "title-only"

    wrong_year = kodi_library._match_item(
        row={"title": "The Movie", "year": 2021},
        media_type="movie",
        indexes=indexes,
    )
    assert wrong_year["item_id"] is None
    assert wrong_year["diagnosis_code"] == "not_found_in_floppy"


def test_episode_match_requires_parent_show_and_exact_season_episode():
    hierarchy = {
        "seasons": {(101, 2)},
        "episodes": {(101, 2, 4): {555}},
    }
    result = kodi_library._match_episode(
        row={"tvshowid": 9, "season": 2, "episode": 4},
        indexes=_indexes(),
        show_matches={9: {"item_id": 101, "confidence": "exact", "method": "tmdb"}},
        episode_hierarchy=hierarchy,
    )

    assert result["item_id"] == 555
    assert result["confidence"] == "strong"
    assert result["method"] == "show+SxxEyy"


def test_episode_does_not_fallback_when_parent_show_is_unmatched():
    result = kodi_library._match_episode(
        row={"tvshowid": 9, "season": 2, "episode": 4, "title": "Episode Four"},
        indexes=_indexes(),
        show_matches={},
        episode_hierarchy={"seasons": set(), "episodes": {}},
    )

    assert result["item_id"] is None
    assert result["confidence"] == "unresolved"
    assert result["diagnosis_code"] == "unmatched_parent_show"


def test_episode_reports_missing_season_before_missing_episode():
    show_matches = {9: {"item_id": 101, "confidence": "exact", "method": "tmdb"}}

    missing_season = kodi_library._match_episode(
        row={"tvshowid": 9, "season": 3, "episode": 1},
        indexes=_indexes(),
        show_matches=show_matches,
        episode_hierarchy={"seasons": {(101, 2)}, "episodes": {}},
    )
    assert missing_season["diagnosis_code"] == "missing_floppy_season"

    missing_episode = kodi_library._match_episode(
        row={"tvshowid": 9, "season": 2, "episode": 9},
        indexes=_indexes(),
        show_matches=show_matches,
        episode_hierarchy={"seasons": {(101, 2)}, "episodes": {}},
    )
    assert missing_episode["diagnosis_code"] == "missing_floppy_episode"


def test_paged_library_call_advances_until_total_is_read(monkeypatch):
    monkeypatch.setenv("KODI_LIBRARY_PAGE_SIZE", "50")
    kodi = MagicMock()
    kodi.call.side_effect = [
        {"movies": [{"movieid": i} for i in range(50)], "limits": {"start": 0, "end": 50, "total": 75}},
        {"movies": [{"movieid": i} for i in range(50, 75)], "limits": {"start": 50, "end": 75, "total": 75}},
    ]

    rows = kodi_library._paged_library_call(
        kodi,
        method="VideoLibrary.GetMovies",
        result_key="movies",
        properties=("title",),
    )

    assert len(rows) == 75
    assert kodi.call.call_count == 2
    assert kodi.call.call_args_list[0].args[0] == "VideoLibrary.GetMovies"
    assert kodi.call.call_args_list[0].args[1]["limits"] == {"start": 0, "end": 50}
    assert kodi.call.call_args_list[1].args[1]["limits"] == {"start": 50, "end": 100}


def test_paged_library_call_rejects_non_advancing_pagination(monkeypatch):
    monkeypatch.setenv("KODI_LIBRARY_PAGE_SIZE", "50")
    kodi = MagicMock()
    kodi.call.return_value = {
        "movies": [{"movieid": 1}],
        "limits": {"start": 0, "end": 0, "total": 2},
    }

    with pytest.raises(kodi_library.KodiLibraryAwarenessError):
        kodi_library._paged_library_call(
            kodi,
            method="VideoLibrary.GetMovies",
            result_key="movies",
            properties=("title",),
        )


def test_kodi_snapshot_reader_uses_video_library_get_methods_only():
    kodi = MagicMock()
    kodi.call.side_effect = [
        {"movies": [], "limits": {"start": 0, "end": 0, "total": 0}},
        {"tvshows": [], "limits": {"start": 0, "end": 0, "total": 0}},
        {"episodes": [], "limits": {"start": 0, "end": 0, "total": 0}},
    ]

    kodi_library._read_kodi_library(kodi)

    methods = [call.args[0] for call in kodi.call.call_args_list]
    assert methods == [
        "VideoLibrary.GetMovies",
        "VideoLibrary.GetTVShows",
        "VideoLibrary.GetEpisodes",
    ]
    assert all(method.startswith("VideoLibrary.Get") for method in methods)


def test_refresh_rejects_user_not_assigned_to_configured_kodi(monkeypatch):
    monkeypatch.setenv("KODI_FLOPPY_USER_ID", "17")
    monkeypatch.setattr(kodi_library.KodiClient, "from_env", MagicMock())

    with pytest.raises(kodi_library.KodiLibraryAwarenessError):
        kodi_library.refresh_kodi_library(SimpleNamespace(id=18))

    kodi_library.KodiClient.from_env.assert_not_called()


def test_summary_counts_only_exact_strong_and_fallback_as_matched():
    summary = kodi_library._summary_bucket(
        [
            {"confidence": "exact"},
            {"confidence": "strong"},
            {"confidence": "fallback"},
            {"confidence": "ambiguous"},
            {"confidence": "unresolved"},
        ]
    )
    assert summary == {
        "total": 5,
        "matched": 3,
        "exact": 1,
        "strong": 1,
        "fallback": 1,
        "ambiguous": 1,
        "unresolved": 1,
    }
