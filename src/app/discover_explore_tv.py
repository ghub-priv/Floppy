"""Interactive TV catalogue browser embedded in the Discover page."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
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
from app.models import MediaTypes, TV, Status

EXPLORE_MIN_DECADE = 1930
EXPLORE_MIN_YEAR = 1930

SORT_TO_TMDB = {
    "popular": "popularity.desc",
    "rating": "vote_average.desc",
    "newest": "first_air_date.desc",
    "oldest": "first_air_date.asc",
    "random": "popularity.desc",
}
WATCH_STATUS_OPTIONS = (
    ("unwatched", "Unwatched"),
    ("watching", "Watching"),
    ("completed", "Completed"),
    ("planning", "Planning"),
    ("paused", "Paused"),
    ("dropped", "Dropped"),
    ("all", "All"),
)
WATCH_STATUS_LABELS = dict(WATCH_STATUS_OPTIONS)
LOCAL_WATCH_STATUS_FILTERS = {
    "watching": {Status.IN_PROGRESS.value},
    "completed": {Status.COMPLETED.value},
    "planning": {Status.PLANNING.value},
    "paused": {Status.PAUSED.value},
    "dropped": {Status.DROPPED.value},
}
LOCAL_RESULT_LABELS = {
    "watching": ("watching show", "watching shows"),
    "completed": ("completed show", "completed shows"),
    "planning": ("planned show", "planned shows"),
    "paused": ("paused show", "paused shows"),
    "dropped": ("dropped show", "dropped shows"),
}
STATUS_BADGES = {
    Status.IN_PROGRESS.value: "Watching",
    Status.COMPLETED.value: "Completed",
    Status.PLANNING.value: "Planning",
    Status.PAUSED.value: "Paused",
    Status.DROPPED.value: "Dropped",
}
RUNTIME_OPTIONS = (
    ("", "Any episode runtime"),
    ("short", "Under 30 min"),
    ("half", "30-44 min"),
    ("hour", "45-69 min"),
    ("long", "70+ min"),
)
RUNTIME_RANGES = {
    "": (None, None),
    "short": (None, 29),
    "half": (30, 44),
    "hour": (45, 69),
    "long": (70, None),
}
PRODUCTION_STATUS_OPTIONS = (
    ("", "Any show status"),
    ("returning", "Returning series"),
    ("ended", "Ended"),
    ("cancelled", "Cancelled"),
    ("limited", "Limited series / miniseries"),
)
PRODUCTION_STATUS_LABELS = dict(PRODUCTION_STATUS_OPTIONS)
PRODUCTION_STATUS_PROVIDER_PARAMS = {
    "": {},
    "returning": {"with_status": "0"},
    "ended": {"with_status": "3"},
    "cancelled": {"with_status": "4"},
    "limited": {"with_type": "2"},
}
TV_EXPLORE_CONFIG = ProviderExploreConfig(
    media_type=MediaTypes.TV.value,
    row_key="explore_tv",
    endpoint="/discover/tv",
    route_name="discover_explore_tv",
    date_field="first_air_date",
    sort_to_provider=SORT_TO_TMDB,
    min_year=EXPLORE_MIN_YEAR,
    min_decade=EXPLORE_MIN_DECADE,
    extra_params={
        "include_adult": "false",
        "include_null_first_air_dates": "false",
    },
)


def _all_tv_rows(user):
    return list(TV.objects.filter(user=user).select_related("item"))


def _watched_tv_rows(rows):
    watched_statuses = {
        Status.COMPLETED.value,
        Status.DROPPED.value,
        Status.IN_PROGRESS.value,
        Status.PAUSED.value,
    }
    return [row for row in rows if row.status in watched_statuses]


def _local_status_rows(rows, watch_status: str):
    statuses = LOCAL_WATCH_STATUS_FILTERS.get(watch_status)
    if not statuses:
        return []
    return [row for row in rows if row.status in statuses]


def _production_status_key(request) -> str:
    value = (request.GET.get("production_status") or "").strip().lower()
    return value if value in PRODUCTION_STATUS_LABELS else ""


def _local_production_status_matches(item, key: str) -> bool:
    if not key:
        return True

    status_text = str(getattr(item, "status", "") or "").strip().casefold()
    format_text = str(getattr(item, "format", "") or "").strip().casefold()

    if key == "returning":
        return status_text in {"returning series", "returning"}
    if key == "ended":
        return status_text == "ended"
    if key == "cancelled":
        return status_text in {"canceled", "cancelled"}
    if key == "limited":
        return (
            format_text in {"miniseries", "mini series", "limited series"}
            or "miniseries" in status_text
            or "limited series" in status_text
        )
    return True


def _tv_state_url(request, **kwargs) -> str:
    return state_url(
        request,
        route_name=TV_EXPLORE_CONFIG.route_name,
        **kwargs,
    )


@login_required
@require_GET
def discover_explore_tv(request):
    """Render the TV catalogue explorer for Discover/TV."""
    adapter = TMDbDiscoverAdapter()
    today = timezone.localdate()
    genre_map = adapter._genre_id_to_name_map(MediaTypes.TV.value)
    genre_options = [
        {"id": str(genre_id), "name": name}
        for genre_id, name in sorted(genre_map.items(), key=lambda entry: entry[1])
    ]

    state = parse_filter_state(
        request,
        today=today,
        genre_map=genre_map,
        config=TV_EXPLORE_CONFIG,
        watch_status_labels=WATCH_STATUS_LABELS,
        default_watch_status="unwatched",
        runtime_ranges=RUNTIME_RANGES,
        min_rating_options=DEFAULT_MIN_RATING_OPTIONS,
    )
    production_status = _production_status_key(request)
    provider_filter_params = PRODUCTION_STATUS_PROVIDER_PARAMS[production_status]

    decades = explore_decades(today, min_decade=TV_EXPLORE_CONFIG.min_decade)
    include_genre_names = selected_genre_name_set(state.selected_genres, genre_map)
    exclude_genre_names = selected_genre_name_set(
        state.selected_excluded_genres,
        genre_map,
    )

    all_rows = _all_tv_rows(request.user)
    watched_rows = _watched_tv_rows(all_rows)
    watched_keys = identity_keys(watched_rows)
    tracked_keys = identity_keys(all_rows)
    row_by_identity = {
        (str(row.item.media_type), str(row.item.source), str(row.item.media_id)): row
        for row in all_rows
        if getattr(row, "item_id", None) and getattr(row, "item", None)
    }
    hidden_keys = discover_filters.get_feedback_keys_by_media_type(
        request.user,
        MediaTypes.TV.value,
    )

    candidates = []
    provider_total_results = 0
    local_total_results = 0
    has_previous = False
    has_next = False
    using_local_library = state.watch_status in LOCAL_WATCH_STATUS_FILTERS

    if using_local_library:
        seen: set[tuple[str, str, str]] = set()
        library_candidates = []
        for tv_row in _local_status_rows(all_rows, state.watch_status):
            item = tv_row.item
            if not item:
                continue
            identity = (str(item.media_type), str(item.source), str(item.media_id))
            if identity in seen or identity in hidden_keys:
                continue
            seen.add(identity)
            if not _local_production_status_matches(item, production_status):
                continue
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
                    row_key=TV_EXPLORE_CONFIG.row_key,
                )
            )

        sort_library_candidates(library_candidates, state.sort_key, state.seed)
        local_total_results = len(library_candidates)
        candidates, has_previous, has_next = paginate_library_candidates(
            library_candidates,
            state=state,
            results_per_chunk=TV_EXPLORE_CONFIG.results_per_chunk,
        )
    else:
        blocked_keys = hidden_keys | (
            watched_keys if state.watch_status == "unwatched" else set()
        )
        candidates, provider_total_results, has_previous, has_next = (
            fetch_provider_candidates(
                adapter,
                state=state,
                config=TV_EXPLORE_CONFIG,
                today=today,
                ttl_seconds=GENRE_DISCOVERY_TTL,
                blocked_keys=blocked_keys,
                include_genre_names=include_genre_names,
                exclude_genre_names=exclude_genre_names,
                provider_filter_params=provider_filter_params,
            )
        )

    cards = [card_dict(candidate, watched_keys) for candidate in candidates]
    for card in cards:
        identity = card["candidate"].identity()
        tracked_row = row_by_identity.get(identity)
        card["status_badge"] = (
            STATUS_BADGES.get(tracked_row.status, "") if tracked_row else ""
        )
        card["can_plan"] = identity not in tracked_keys

    current_seed = state.seed if state.sort_key == "random" else None
    refresh_url = _tv_state_url(
        request,
        page=state.chunk,
        seed=current_seed,
        surprise=state.surprise_mode,
    )
    previous_url = (
        _tv_state_url(
            request,
            page=state.chunk - 1,
            seed=current_seed,
            surprise=False,
        )
        if has_previous
        else ""
    )
    next_url = (
        _tv_state_url(
            request,
            page=state.chunk + 1,
            seed=current_seed,
            surprise=False,
        )
        if has_next
        else ""
    )

    local_singular, local_plural = LOCAL_RESULT_LABELS.get(
        state.watch_status,
        ("show", "shows"),
    )
    context = {
        "explore_title": "Explore TV Shows",
        "explore_description": (
            "Filter premiered shows by genre, first-air year, episode runtime, "
            "watch history and series status, or let Floppy pick one for you."
        ),
        "explore_target_id": "discover-explore-tv-shell",
        "explore_id_prefix": "tv-explore",
        "form_url": reverse(TV_EXPLORE_CONFIG.route_name),
        "active_media_type": MediaTypes.TV.value,
        "row_key": TV_EXPLORE_CONFIG.row_key,
        "runtime_label": "Episode runtime",
        "show_production_status_filter": True,
        "production_status_options": PRODUCTION_STATUS_OPTIONS,
        "selected_production_status": production_status,
        "using_local_library": using_local_library,
        "result_singular": "show",
        "result_plural": "shows",
        "local_result_singular": local_singular,
        "local_result_plural": local_plural,
        "empty_message": (
            "No TV shows matched these filters. Broaden the filters or try "
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
        "min_year": TV_EXPLORE_CONFIG.min_year,
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
        "clear_url": reverse(TV_EXPLORE_CONFIG.route_name),
        "refresh_url": refresh_url,
        "surprise_mode": state.surprise_mode,
        "surprise_again_url": _tv_state_url(
            request,
            page=1,
            seed=current_seed,
            surprise=True,
        ),
        "exit_surprise_url": _tv_state_url(
            request,
            page=1,
            seed=current_seed,
            surprise=False,
        ),
    }
    return render(request, "app/components/discover_explore.html", context)
