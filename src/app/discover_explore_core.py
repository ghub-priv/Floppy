"""Reusable primitives for the Movie and TV Discover catalogue explorers."""

from __future__ import annotations

import random
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.urls import reverse
from django.utils.text import slugify

from app.discover.schemas import CandidateItem

EXPLORE_RESULTS_PER_CHUNK = 60
EXPLORE_MAX_PROVIDER_PAGE = 500
RANDOM_SEED_LIMIT = 2_147_483_647
RANDOM_PAGE_SALT = 1_000_003

DEFAULT_SORT_OPTIONS = (
    ("popular", "Most popular"),
    ("rating", "Highest rated"),
    ("newest", "Newest first"),
    ("oldest", "Oldest first"),
    ("random", "Random"),
)
DEFAULT_MIN_RATING_OPTIONS = ("", "6", "7", "8")


@dataclass(slots=True)
class ExploreFilterState:
    """Validated Explore query-string state shared by both explorers."""

    selected_genres: list[str]
    selected_excluded_genres: list[str]
    decade: int | None
    from_year: int | None
    to_year: int | None
    custom_year_active: bool
    sort_key: str
    min_rating_raw: str
    min_rating: float | None
    watch_status: str
    runtime_key: str
    runtime_min: int | None
    runtime_max: int | None
    surprise_mode: bool
    seed: int
    chunk: int


@dataclass(frozen=True, slots=True)
class ProviderExploreConfig:
    """Provider configuration that differs between Movie and TV Explore."""

    media_type: str
    row_key: str
    endpoint: str
    route_name: str
    date_field: str
    sort_to_provider: Mapping[str, str]
    min_year: int
    min_decade: int
    extra_params: Mapping[str, str]
    runtime_supported: bool = True
    max_provider_page: int = EXPLORE_MAX_PROVIDER_PAGE
    results_per_chunk: int = EXPLORE_RESULTS_PER_CHUNK


def safe_int(value: str | None, *, default: int | None = None) -> int | None:
    """Parse an integer without allowing bad query strings to fail the view."""
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def safe_float(value: str | None) -> float | None:
    """Parse a float without allowing bad query strings to fail the view."""
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def explore_decades(today: date, *, min_decade: int) -> list[int]:
    """Return selectable decades newest-first."""
    current_decade = (today.year // 10) * 10
    return list(range(current_decade, min_decade - 1, -10))


def selected_years(
    request,
    today: date,
    *,
    min_year: int,
) -> tuple[int | None, int | None]:
    """Return a validated custom year range."""
    from_year = safe_int(request.GET.get("from_year"))
    to_year = safe_int(request.GET.get("to_year"))

    if from_year is not None:
        from_year = min(max(from_year, min_year), today.year)
    if to_year is not None:
        to_year = min(max(to_year, min_year), today.year)
    if from_year is not None and to_year is not None and from_year > to_year:
        from_year, to_year = to_year, from_year
    return from_year, to_year


def parse_filter_state(
    request,
    *,
    today: date,
    genre_map: Mapping[int, str],
    config: ProviderExploreConfig,
    watch_status_labels: Mapping[str, str],
    default_watch_status: str,
    runtime_ranges: Mapping[str, tuple[int | None, int | None]],
    min_rating_options: tuple[str, ...] = DEFAULT_MIN_RATING_OPTIONS,
) -> ExploreFilterState:
    """Parse and validate the shared Explore controls from request.GET."""
    valid_genre_ids = {str(genre_id) for genre_id in genre_map}
    selected_excluded_genres = [
        value
        for value in request.GET.getlist("exclude_genre")
        if value in valid_genre_ids
    ]
    excluded_set = set(selected_excluded_genres)
    selected_genres = [
        value
        for value in request.GET.getlist("genre")
        if value in valid_genre_ids and value not in excluded_set
    ]

    decades = explore_decades(today, min_decade=config.min_decade)
    decade = safe_int(request.GET.get("decade"))
    if decade not in decades:
        decade = None

    from_year, to_year = selected_years(request, today, min_year=config.min_year)

    sort_key = (request.GET.get("sort") or "popular").strip().lower()
    if sort_key not in config.sort_to_provider:
        sort_key = "popular"

    min_rating_raw = (request.GET.get("min_rating") or "").strip()
    if min_rating_raw not in min_rating_options:
        min_rating_raw = ""

    watch_status = (
        request.GET.get("watch_status") or default_watch_status
    ).strip().lower()
    if watch_status not in watch_status_labels:
        watch_status = default_watch_status

    runtime_key = (request.GET.get("runtime") or "").strip().lower()
    if runtime_key not in runtime_ranges:
        runtime_key = ""
    runtime_min, runtime_max = runtime_ranges[runtime_key]

    surprise_mode = request.GET.get("surprise") in {"1", "true", "True"}
    shuffle_requested = request.GET.get("shuffle") in {"1", "true", "True"}
    seed = safe_int(request.GET.get("seed"))
    if seed is None or shuffle_requested:
        seed = secrets.randbelow(RANDOM_SEED_LIMIT)

    chunk = safe_int(request.GET.get("page"), default=1) or 1

    return ExploreFilterState(
        selected_genres=selected_genres,
        selected_excluded_genres=selected_excluded_genres,
        decade=decade,
        from_year=from_year,
        to_year=to_year,
        custom_year_active=from_year is not None or to_year is not None,
        sort_key=sort_key,
        min_rating_raw=min_rating_raw,
        min_rating=safe_float(min_rating_raw),
        watch_status=watch_status,
        runtime_key=runtime_key,
        runtime_min=runtime_min,
        runtime_max=runtime_max,
        surprise_mode=surprise_mode,
        seed=seed,
        chunk=max(chunk, 1),
    )


def selected_genre_name_set(
    genre_ids: list[str],
    genre_map: Mapping[int, str],
) -> set[str]:
    """Return selected genre names case-folded for defensive matching."""
    return {
        genre_map[int(genre_id)].casefold()
        for genre_id in genre_ids
        if genre_id.isdigit() and int(genre_id) in genre_map
    }


def selected_genre_names(
    genre_ids: list[str],
    genre_map: Mapping[int, str],
) -> list[str]:
    """Return display names for selected genre IDs."""
    return [
        genre_map[int(genre_id)]
        for genre_id in genre_ids
        if genre_id.isdigit() and int(genre_id) in genre_map
    ]


def candidate_matches_genres(
    candidate: CandidateItem,
    *,
    include_genres: set[str],
    exclude_genres: set[str],
) -> bool:
    """Enforce include-ALL and exclude-ANY semantics after provider results."""
    candidate_genres = {str(value).casefold() for value in (candidate.genres or [])}
    if include_genres and not include_genres.issubset(candidate_genres):
        return False
    if exclude_genres and candidate_genres & exclude_genres:
        return False
    return True


def candidate_from_library_item(item, *, row_key: str) -> CandidateItem:
    """Normalize a locally tracked Item into a Discover candidate."""
    release_date = (
        item.release_datetime.date().isoformat() if item.release_datetime else None
    )
    rating = item.provider_rating
    rating_count = item.provider_rating_count
    if rating is None and item.imdb_rating is not None:
        rating = item.imdb_rating
        rating_count = item.imdb_rating_count
    if rating is None and item.trakt_rating is not None:
        rating = item.trakt_rating
        rating_count = item.trakt_rating_count

    return CandidateItem(
        media_type=str(item.media_type),
        source=str(item.source),
        media_id=str(item.media_id),
        title=item.title,
        original_title=item.original_title,
        localized_title=item.localized_title,
        image=item.image or None,
        release_date=release_date,
        genres=list(item.genres or item.implied_genres or []),
        studios=list(item.studios or []),
        keywords=list(item.provider_keywords or []),
        certification=item.provider_certification or None,
        popularity=(
            item.provider_popularity
            if item.provider_popularity is not None
            else item.trakt_popularity_score
        ),
        rating=rating,
        rating_count=rating_count,
        row_key=row_key,
    )


def library_item_matches(
    item,
    *,
    include_genres: set[str],
    exclude_genres: set[str],
    decade: int | None,
    from_year: int | None,
    to_year: int | None,
    min_rating: float | None,
    runtime_min: int | None,
    runtime_max: int | None,
) -> bool:
    """Apply Explore filters to a locally tracked Item."""
    item_genres = {
        str(value).casefold() for value in (item.genres or item.implied_genres or [])
    }
    if include_genres and not include_genres.issubset(item_genres):
        return False
    if exclude_genres and item_genres & exclude_genres:
        return False

    release_year = item.release_datetime.year if item.release_datetime else None
    if from_year is not None or to_year is not None:
        if release_year is None:
            return False
        if from_year is not None and release_year < from_year:
            return False
        if to_year is not None and release_year > to_year:
            return False
    elif decade is not None and (
        release_year is None or not decade <= release_year <= decade + 9
    ):
        return False

    rating = item.provider_rating
    if rating is None:
        rating = item.imdb_rating
    if rating is None:
        rating = item.trakt_rating
    if min_rating is not None and (rating is None or rating < min_rating):
        return False

    runtime = item.runtime_minutes
    if runtime_min is not None and (runtime is None or runtime < runtime_min):
        return False
    if runtime_max is not None and (runtime is None or runtime > runtime_max):
        return False
    return True


def sort_library_candidates(
    candidates: list[CandidateItem],
    sort_key: str,
    seed: int,
) -> None:
    """Sort local candidates with the same options exposed for provider data."""
    if sort_key == "random":
        random.Random(seed).shuffle(candidates)  # noqa: S311 - deterministic UI order
        return
    if sort_key == "rating":
        candidates.sort(
            key=lambda candidate: (
                candidate.rating is not None,
                candidate.rating if candidate.rating is not None else -1.0,
                candidate.rating_count or 0,
            ),
            reverse=True,
        )
        return
    if sort_key == "newest":
        candidates.sort(key=lambda candidate: candidate.release_date or "", reverse=True)
        return
    if sort_key == "oldest":
        candidates.sort(key=lambda candidate: candidate.release_date or "9999-12-31")
        return
    candidates.sort(
        key=lambda candidate: (
            candidate.popularity is not None,
            candidate.popularity if candidate.popularity is not None else -1.0,
        ),
        reverse=True,
    )


def paginate_library_candidates(
    candidates: list[CandidateItem],
    *,
    state: ExploreFilterState,
    results_per_chunk: int = EXPLORE_RESULTS_PER_CHUNK,
) -> tuple[list[CandidateItem], bool, bool]:
    """Slice local results or return one random Surprise candidate."""
    if state.surprise_mode:
        return ([secrets.choice(candidates)] if candidates else []), False, False
    start = (state.chunk - 1) * results_per_chunk
    end = start + results_per_chunk
    return candidates[start:end], state.chunk > 1, end < len(candidates)


def identity_keys(rows) -> set[tuple[str, str, str]]:
    """Return stable identity triples from tracked media rows."""
    return {
        (str(row.item.media_type), str(row.item.source), str(row.item.media_id))
        for row in rows
        if getattr(row, "item_id", None) and getattr(row, "item", None)
    }


def release_year(release_date: str | None) -> str:
    """Return the display year from an ISO-like release date."""
    if not release_date:
        return ""
    value = str(release_date)
    return value[:4] if len(value) >= 4 and value[:4].isdigit() else ""


def details_url(candidate: CandidateItem) -> str:
    """Return the canonical Floppy details URL for a candidate."""
    title_slug = slugify(candidate.title) or str(candidate.media_id)
    return reverse(
        "media_details",
        kwargs={
            "source": candidate.source,
            "media_type": candidate.media_type,
            "media_id": str(candidate.media_id),
            "title": title_slug,
        },
    )


def card_dict(
    candidate: CandidateItem,
    watched_keys: set[tuple[str, str, str]],
) -> dict[str, Any]:
    """Build template metadata while retaining the current CandidateItem."""
    return {
        "candidate": candidate,
        "title": candidate.title,
        "image": candidate.image,
        "year": release_year(candidate.release_date),
        "rating": candidate.rating,
        "rating_count": candidate.rating_count,
        "genres": candidate.genres[:3],
        "details_url": details_url(candidate),
        "is_watched": candidate.identity() in watched_keys,
    }


def state_url(
    request,
    *,
    route_name: str,
    page: int | None = None,
    seed: int | None = None,
    surprise: bool | None = None,
) -> str:
    """Preserve active Explore filters while changing navigation state."""
    params = request.GET.copy()
    params.pop("shuffle", None)
    if page is not None:
        params["page"] = str(max(page, 1))
    if seed is not None:
        params["seed"] = str(seed)
    elif params.get("sort") != "random":
        params.pop("seed", None)
    if surprise is True:
        params["surprise"] = "1"
    elif surprise is False:
        params.pop("surprise", None)
    query = params.urlencode()
    base = reverse(route_name)
    return f"{base}?{query}" if query else base


def build_provider_params(
    state: ExploreFilterState,
    *,
    config: ProviderExploreConfig,
    today: date,
    provider_filter_params: Mapping[str, str | int | float] | None = None,
) -> dict[str, str | int | float]:
    """Translate validated Explore state into TMDb Discover parameters."""
    params: dict[str, str | int | float] = dict(config.extra_params)
    params["sort_by"] = config.sort_to_provider[state.sort_key]
    params[f"{config.date_field}.lte"] = today.isoformat()

    if state.selected_genres:
        params["with_genres"] = ",".join(state.selected_genres)
    if state.selected_excluded_genres:
        params["without_genres"] = ",".join(state.selected_excluded_genres)

    if state.custom_year_active:
        if state.from_year is not None:
            params[f"{config.date_field}.gte"] = f"{state.from_year}-01-01"
        if state.to_year is not None:
            year_end = date(state.to_year, 12, 31)
            params[f"{config.date_field}.lte"] = min(year_end, today).isoformat()
    elif state.decade is not None:
        params[f"{config.date_field}.gte"] = f"{state.decade}-01-01"
        decade_end = date(state.decade + 9, 12, 31)
        params[f"{config.date_field}.lte"] = min(decade_end, today).isoformat()

    if state.min_rating is not None:
        params["vote_average.gte"] = state.min_rating
        params["vote_count.gte"] = 25
    elif state.sort_key == "rating":
        params["vote_count.gte"] = 100

    if config.runtime_supported:
        if state.runtime_min is not None:
            params["with_runtime.gte"] = state.runtime_min
        if state.runtime_max is not None:
            params["with_runtime.lte"] = state.runtime_max

    if provider_filter_params:
        params.update(provider_filter_params)

    return params


def fetch_provider_candidates(
    adapter,
    *,
    state: ExploreFilterState,
    config: ProviderExploreConfig,
    today: date,
    ttl_seconds: int,
    blocked_keys: set[tuple[str, str, str]],
    include_genre_names: set[str],
    exclude_genre_names: set[str],
    provider_filter_params: Mapping[str, str | int | float] | None = None,
) -> tuple[list[CandidateItem], int, bool, bool]:
    """Build a full visible UI page after local exclusions and validation."""
    params = build_provider_params(
        state,
        config=config,
        today=today,
        provider_filter_params=provider_filter_params,
    )
    probe = adapter._cache_request(  # noqa: SLF001 - same Discover provider package
        config.endpoint,
        {**params, "page": 1},
        ttl_seconds=ttl_seconds,
    )
    provider_total_results = int(probe.get("total_results") or 0)
    provider_total_pages = min(
        int(probe.get("total_pages") or 0),
        config.max_provider_page,
    )

    if provider_total_pages <= 0:
        return [], provider_total_results, False, False

    provider_pages = list(range(1, provider_total_pages + 1))
    if state.sort_key == "random" or state.surprise_mode:
        random.Random(state.seed).shuffle(  # noqa: S311 - deterministic UI order
            provider_pages
        )

    if state.surprise_mode:
        desired_start = 0
        desired_size = config.results_per_chunk
        target_count = desired_size
    else:
        desired_start = (state.chunk - 1) * config.results_per_chunk
        desired_size = config.results_per_chunk
        target_count = desired_start + desired_size + 1

    eligible: list[CandidateItem] = []
    seen_ids: set[str] = set()
    exhausted_provider_pages = True

    for provider_page in provider_pages:
        payload = (
            probe
            if provider_page == 1
            else adapter._cache_request(  # noqa: SLF001 - same Discover provider package
                config.endpoint,
                {**params, "page": provider_page},
                ttl_seconds=ttl_seconds,
            )
        )
        raw_results = payload.get("results") or []
        if not raw_results:
            continue

        normalized = adapter._normalize_results(  # noqa: SLF001 - shared normalizer
            config.media_type,
            raw_results,
            row_key=config.row_key,
        )
        if state.sort_key == "random" or state.surprise_mode:
            page_seed = state.seed ^ (provider_page * RANDOM_PAGE_SALT)
            random.Random(page_seed).shuffle(  # noqa: S311 - deterministic UI order
                normalized
            )

        for candidate in normalized:
            media_id = str(candidate.media_id)
            if not media_id or media_id in seen_ids:
                continue
            seen_ids.add(media_id)
            if candidate.identity() in blocked_keys:
                continue
            if candidate.release_date and candidate.release_date > today.isoformat():
                continue
            if not candidate_matches_genres(
                candidate,
                include_genres=include_genre_names,
                exclude_genres=exclude_genre_names,
            ):
                continue

            eligible.append(candidate)
            if len(eligible) >= target_count:
                exhausted_provider_pages = False
                break

        if len(eligible) >= target_count:
            break

    if state.surprise_mode:
        candidates = [secrets.choice(eligible)] if eligible else []
        return candidates, provider_total_results, False, False

    page_end = desired_start + desired_size
    candidates = eligible[desired_start:page_end]
    has_previous = state.chunk > 1 and bool(
        candidates or desired_start <= len(eligible)
    )
    has_next = len(eligible) > page_end

    if exhausted_provider_pages and len(eligible) <= page_end:
        has_next = False

    return candidates, provider_total_results, has_previous, has_next
