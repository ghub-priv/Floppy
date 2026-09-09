from datetime import date

from django.template.loader import get_template
from django.test import SimpleTestCase

from app.discover.schemas import CandidateItem
from app.discover_explore_core import (
    ExploreFilterState,
    ProviderExploreConfig,
    candidate_matches_genres,
    fetch_provider_candidates,
    paginate_library_candidates,
    sort_library_candidates,
)


class _FakeTMDbAdapter:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def _cache_request(self, endpoint, params, *, ttl_seconds):
        self.calls.append((endpoint, dict(params), ttl_seconds))
        page = int(params.get("page", 1))
        return self.pages.get(
            page,
            {"results": [], "total_results": 0, "total_pages": 0},
        )

    def _normalize_results(self, media_type, results, *, row_key):
        return [
            CandidateItem(
                media_type=media_type,
                source="tmdb",
                media_id=str(item["id"]),
                title=item["title"],
                release_date=item.get("release_date"),
                genres=list(item.get("genres") or []),
                popularity=item.get("popularity"),
                rating=item.get("rating"),
                row_key=row_key,
            )
            for item in results
        ]


def _state(*, sort_key="popular", seed=42, chunk=1, surprise_mode=False):
    return ExploreFilterState(
        selected_genres=[],
        selected_excluded_genres=[],
        decade=None,
        from_year=None,
        to_year=None,
        custom_year_active=False,
        sort_key=sort_key,
        min_rating_raw="",
        min_rating=None,
        watch_status="unwatched",
        runtime_key="",
        runtime_min=None,
        runtime_max=None,
        surprise_mode=surprise_mode,
        seed=seed,
        chunk=chunk,
    )


def _config(*, results_per_chunk=3):
    return ProviderExploreConfig(
        media_type="movie",
        row_key="explore_movies",
        endpoint="/discover/movie",
        route_name="discover_explore",
        date_field="primary_release_date",
        sort_to_provider={
            "popular": "popularity.desc",
            "rating": "vote_average.desc",
            "newest": "primary_release_date.desc",
            "oldest": "primary_release_date.asc",
            "random": "popularity.desc",
        },
        min_year=1888,
        min_decade=1920,
        extra_params={},
        results_per_chunk=results_per_chunk,
    )


class DiscoverExploreCoreTests(SimpleTestCase):
    def test_genre_include_is_all_and_exclude_is_any(self):
        candidate = CandidateItem(
            media_type="movie",
            source="tmdb",
            media_id="1",
            title="Example",
            genres=["Action", "Science Fiction", "Drama"],
        )

        self.assertTrue(
            candidate_matches_genres(
                candidate,
                include_genres={"action", "science fiction"},
                exclude_genres=set(),
            )
        )
        self.assertFalse(
            candidate_matches_genres(
                candidate,
                include_genres={"action", "comedy"},
                exclude_genres=set(),
            )
        )
        self.assertFalse(
            candidate_matches_genres(
                candidate,
                include_genres={"action"},
                exclude_genres={"drama", "horror"},
            )
        )

    def test_provider_scan_backfills_after_local_exclusions(self):
        pages = {
            1: {
                "results": [
                    {"id": 1, "title": "Blocked 1", "release_date": "2020-01-01"},
                    {"id": 2, "title": "Blocked 2", "release_date": "2020-01-01"},
                    {"id": 3, "title": "Blocked 3", "release_date": "2020-01-01"},
                ],
                "total_results": 7,
                "total_pages": 2,
            },
            2: {
                "results": [
                    {"id": 4, "title": "Visible 4", "release_date": "2020-01-01"},
                    {"id": 5, "title": "Visible 5", "release_date": "2020-01-01"},
                    {"id": 6, "title": "Visible 6", "release_date": "2020-01-01"},
                    {"id": 7, "title": "Visible 7", "release_date": "2020-01-01"},
                ],
                "total_results": 7,
                "total_pages": 2,
            },
        }
        adapter = _FakeTMDbAdapter(pages)
        blocked = {("movie", "tmdb", str(media_id)) for media_id in (1, 2, 3)}

        candidates, total, has_previous, has_next = fetch_provider_candidates(
            adapter,
            state=_state(),
            config=_config(),
            today=date(2026, 9, 9),
            ttl_seconds=3600,
            blocked_keys=blocked,
            include_genre_names=set(),
            exclude_genre_names=set(),
        )

        self.assertEqual([item.media_id for item in candidates], ["4", "5", "6"])
        self.assertEqual(total, 7)
        self.assertFalse(has_previous)
        self.assertTrue(has_next)
        self.assertEqual([call[1]["page"] for call in adapter.calls], [1, 2])

    def test_provider_scan_drops_future_releases_and_keeps_backfilling(self):
        pages = {
            1: {
                "results": [
                    {"id": 1, "title": "Future", "release_date": "2027-01-01"},
                    {"id": 2, "title": "Released 2", "release_date": "2026-01-01"},
                ],
                "total_results": 4,
                "total_pages": 2,
            },
            2: {
                "results": [
                    {"id": 3, "title": "Released 3", "release_date": "2025-01-01"},
                    {"id": 4, "title": "Released 4", "release_date": "2024-01-01"},
                ],
                "total_results": 4,
                "total_pages": 2,
            },
        }
        adapter = _FakeTMDbAdapter(pages)

        candidates, _total, _has_previous, _has_next = fetch_provider_candidates(
            adapter,
            state=_state(),
            config=_config(),
            today=date(2026, 9, 9),
            ttl_seconds=3600,
            blocked_keys=set(),
            include_genre_names=set(),
            exclude_genre_names=set(),
        )

        self.assertEqual([item.media_id for item in candidates], ["2", "3", "4"])

    def test_seeded_random_local_pagination_is_stable_and_non_overlapping(self):
        originals = [
            CandidateItem(
                media_type="movie",
                source="tmdb",
                media_id=str(index),
                title=f"Movie {index}",
            )
            for index in range(20)
        ]

        first_order = list(originals)
        second_order = list(originals)
        sort_library_candidates(first_order, "random", 12345)
        sort_library_candidates(second_order, "random", 12345)

        self.assertEqual(
            [candidate.media_id for candidate in first_order],
            [candidate.media_id for candidate in second_order],
        )

        page_one, _previous, next_one = paginate_library_candidates(
            first_order,
            state=_state(sort_key="random", seed=12345, chunk=1),
            results_per_chunk=5,
        )
        page_two, previous_two, _next_two = paginate_library_candidates(
            first_order,
            state=_state(sort_key="random", seed=12345, chunk=2),
            results_per_chunk=5,
        )

        self.assertTrue(next_one)
        self.assertTrue(previous_two)
        self.assertFalse(
            {candidate.media_id for candidate in page_one}
            & {candidate.media_id for candidate in page_two}
        )

    def test_explore_templates_compile(self):
        get_template("app/components/discover_explore.html")
        get_template("app/components/discover_rows.html")
