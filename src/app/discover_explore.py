"""Interactive movie catalogue browser embedded in the Discover page."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from app.discover import filters as discover_filters
from app.discover.providers.tmdb_adapter import GENRE_DISCOVERY_TTL, TMDbDiscoverAdapter
from app.discover_explore_core import (
    DEFAULT_MIN_RATING_OPTIONS,
    DEFAULT_SORT_OPTIONS,
    ProviderExploreConfig,
    candidate_from_library_item,
    card_dict,
    explore_decades,
    fetch_provider_candidates,
    identity_keys,
    library_item_matches,
    paginate_library_candidates,
    parse_filter_state,
    selected_genre_name_set,
    selected_genre_names,
    sort_library_candidates,
    state_url,
)
from app.models import MediaTypes, Movie, Status

EXPLORE_MIN_DECADE = 1920
EXPLORE_MIN_YEAR = 1888

SORT_TO_TMDB = {
    "popular": "popularity.desc",
    "rating": "vote_average.desc",
    "newest": "primary_release_date.desc",
    "oldest": "primary_release_date.asc",
    "random": "popularity.desc",
}
WATCH_STATUS_OPTIONS = (
    ("unwatched", "Unwatched"),
    ("watched", "Watched"),
    ("all", "All"),
)
WATCH_STATUS_LABELS = dict(WATCH_STATUS_OPTIONS)
RUNTIME_OPTIONS = (
    ("", "Any runtime"),
    ("short", "Under 90 min"),
    ("standard", "90-120 min"),
    ("long", "121-150 min"),
    ("epic", "Over 150 min"),
)
RUNTIME_RANGES = {
    "": (None, None),
    "short": (None, 89),
    "standard": (90, 120),
    "long": (121, 150),
    "epic": (151, None),
}
MOVIE_EXPLORE_CONFIG = ProviderExploreConfig(
    media_type=MediaTypes.MOVIE.value,
    row_key="explore_movies",
    endpoint="/discover/movie",
    route_name="discover_explore",
    date_field="primary_release_date",
    sort_to_provider=SORT_TO_TMDB,
    min_year=EXPLORE_MIN_YEAR,
    min_decade=EXPLORE_MIN_DECADE,
    extra_params={"include_adult": "false", "include_video": "false"},
)


def _watched_movie_rows(user):
    """Return Movie rows that represent genuine watch activity."""
    watched_statuses = {
        Status.COMPLETED.value,
        Status.DROPPED.value,
        Status.IN_PROGRESS.value,
        Status.PAUSED.value,
    }
    return list(
        Movie.objects.filter(user=user)
        .filter(Q(status__in=watched_statuses) | Q(plays__isnull=False))
        .select_related("item")
        .distinct()
    )


def _movie_state_url(request, **kwargs) -> str:
    return state_url(
        request,
        route_name=MOVIE_EXPLORE_CONFIG.route_name,
        **kwargs,
    )


@login_required
@require_GET
def discover_explore(request):
    """Render the movie catalogue explorer for Discover/Movies."""
    adapter = TMDbDiscoverAdapter()
    today = timezone.localdate()
    genre_map = adapter._genre_id_to_name_map(MediaTypes.MOVIE.value)
    genre_options = [
        {"id": str(genre_id), "name": name}
        for genre_id, name in sorted(genre_map.items(), key=lambda entry: entry[1])
    ]

    state = parse_filter_state(
        request,
        today=today,
        genre_map=genre_map,
        config=MOVIE_EXPLORE_CONFIG,
        watch_status_labels=WATCH_STATUS_LABELS,
        default_watch_status="unwatched",
        runtime_ranges=RUNTIME_RANGES,
        min_rating_options=DEFAULT_MIN_RATING_OPTIONS,
    )
    decades = explore_decades(today, min_decade=MOVIE_EXPLORE_CONFIG.min_decade)
    include_genre_names = selected_genre_name_set(state.selected_genres, genre_map)
    exclude_genre_names = selected_genre_name_set(
        state.selected_excluded_genres,
        genre_map,
    )

    watched_rows = _watched_movie_rows(request.user)
    watched_keys = identity_keys(watched_rows)
    hidden_keys = discover_filters.get_feedback_keys_by_media_type(
        request.user,
        MediaTypes.MOVIE.value,
    )

    candidates = []
    provider_total_results = 0
    local_total_results = 0
    has_previous = False
    has_next = False

    if state.watch_status == "watched":
        seen: set[tuple[str, str, str]] = set()
        library_candidates = []
        for movie_row in watched_rows:
            item = movie_row.item
            if not item:
                continue
            identity = (str(item.media_type), str(item.source), str(item.media_id))
            if identity in seen or identity in hidden_keys:
                continue
            seen.add(identity)
            if not library_item_matches(
                item,
                include_genres=include_genre_names,
                exclude_genres=exclude_genre_names,
                decade=state.decade,
                from_year=state.from_year,
                to_year=state.to_year,
                min_rating=state.min_rating,
                runtime_min=state.runtime_min,
                runtime_max=state.runtime_max,
            ):
                continue
            library_candidates.append(
                candidate_from_library_item(
                    item,
                    row_key=MOVIE_EXPLORE_CONFIG.row_key,
                )
            )

        sort_library_candidates(library_candidates, state.sort_key, state.seed)
        local_total_results = len(library_candidates)
        candidates, has_previous, has_next = paginate_library_candidates(
            library_candidates,
            state=state,
            results_per_chunk=MOVIE_EXPLORE_CONFIG.results_per_chunk,
        )
    else:
        blocked_keys = hidden_keys | (
            watched_keys if state.watch_status == "unwatched" else set()
        )
        candidates, provider_total_results, has_previous, has_next = (
            fetch_provider_candidates(
                adapter,
                state=state,
                config=MOVIE_EXPLORE_CONFIG,
                today=today,
                ttl_seconds=GENRE_DISCOVERY_TTL,
                blocked_keys=blocked_keys,
                include_genre_names=include_genre_names,
                exclude_genre_names=exclude_genre_names,
            )
        )

    cards = [card_dict(candidate, watched_keys) for candidate in candidates]
    for card in cards:
        card["status_badge"] = "Watched" if card["is_watched"] else ""
        card["can_plan"] = not card["is_watched"]

    current_seed = state.seed if state.sort_key == "random" else None
    refresh_url = _movie_state_url(
        request,
        page=state.chunk,
        seed=current_seed,
        surprise=state.surprise_mode,
    )
    previous_url = (
        _movie_state_url(
            request,
            page=state.chunk - 1,
            seed=current_seed,
            surprise=False,
        )
        if has_previous
        else ""
    )
    next_url = (
        _movie_state_url(
            request,
            page=state.chunk + 1,
            seed=current_seed,
            surprise=False,
        )
        if has_next
        else ""
    )

    context = {
        "explore_title": "Explore Movies",
        "explore_description": (
            "Filter released films by genre, year, runtime and watch history, "
            "or let Floppy pick one for you."
        ),
        "explore_target_id": "discover-explore-shell",
        "explore_id_prefix": "movie-explore",
        "form_url": reverse(MOVIE_EXPLORE_CONFIG.route_name),
        "active_media_type": MediaTypes.MOVIE.value,
        "row_key": MOVIE_EXPLORE_CONFIG.row_key,
        "runtime_label": "Runtime",
        "show_production_status_filter": False,
        "production_status_options": (),
        "selected_production_status": "",
        "using_local_library": state.watch_status == "watched",
        "result_singular": "title",
        "result_plural": "titles",
        "local_result_singular": "watched title",
        "local_result_plural": "watched titles",
        "empty_message": (
            "No films matched these filters. Broaden the filters or try "
            "another combination."
        ),
        "genre_options": genre_options,
        "selected_genres": state.selected_genres,
        "selected_genre_names": selected_genre_names(
            state.selected_genres,
            genre_map,
        ),
        "selected_excluded_genres": state.selected_excluded_genres,
        "selected_excluded_genre_names": selected_genre_names(
            state.selected_excluded_genres,
            genre_map,
        ),
        "decades": decades,
        "selected_decade": state.decade,
        "from_year": state.from_year,
        "to_year": state.to_year,
        "min_year": MOVIE_EXPLORE_CONFIG.min_year,
        "max_year": today.year,
        "custom_year_active": state.custom_year_active,
        "sort_options": DEFAULT_SORT_OPTIONS,
        "selected_sort": state.sort_key,
        "random_seed": current_seed,
        "min_rating_options": DEFAULT_MIN_RATING_OPTIONS,
        "selected_min_rating": state.min_rating_raw,
        "runtime_options": RUNTIME_OPTIONS,
        "selected_runtime": state.runtime_key,
        "watch_status_options": WATCH_STATUS_OPTIONS,
        "selected_watch_status": state.watch_status,
        "watch_status_label": WATCH_STATUS_LABELS[state.watch_status],
        "cards": cards,
        "chunk": state.chunk,
        "provider_total_results": provider_total_results,
        "local_total_results": local_total_results,
        "has_previous": has_previous and not state.surprise_mode,
        "has_next": has_next and not state.surprise_mode,
        "previous_url": previous_url,
        "next_url": next_url,
        "clear_url": reverse(MOVIE_EXPLORE_CONFIG.route_name),
        "refresh_url": refresh_url,
        "surprise_mode": state.surprise_mode,
        "surprise_again_url": _movie_state_url(
            request,
            page=1,
            seed=current_seed,
            surprise=True,
        ),
        "exit_surprise_url": _movie_state_url(
            request,
            page=1,
            seed=current_seed,
            surprise=False,
        ),
    }
    return render(request, "app/components/discover_explore.html", context)
