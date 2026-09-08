"""Read-only reconciliation between Kodi's video library and Floppy."""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections import defaultdict
from typing import Any, Iterable

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from app.kodi_client import KodiClient, KodiError
from app.models import Episode, Item, ItemProviderLink, Season

logger = logging.getLogger(__name__)

VERSION = "1.0.3"
CACHE_PREFIX = "kodi-library-awareness:v1.0.3"
CACHE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_PAGE_SIZE = 500
SUPPORTED_IDS = ("tmdb", "tvdb", "imdb")


class KodiLibraryAwarenessError(Exception):
    """Raised when a Kodi library snapshot cannot be built safely."""


def _configured_floppy_user_id() -> int | None:
    raw = str(os.getenv("KODI_FLOPPY_USER_ID") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _cache_key(user_id: int) -> str:
    return f"{CACHE_PREFIX}:user:{int(user_id)}"


def _page_size() -> int:
    raw = str(os.getenv("KODI_LIBRARY_PAGE_SIZE") or DEFAULT_PAGE_SIZE).strip()
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return max(50, min(size, 1000))


def _normalise_title(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _normalise_provider(value: Any) -> str | None:
    text = str(value or "").strip().casefold().replace("-", "_")
    aliases = {
        "tmdb": "tmdb", "tmdb_id": "tmdb", "tmdbid": "tmdb",
        "themoviedb": "tmdb", "themoviedb_id": "tmdb",
        "tvdb": "tvdb", "tvdb_id": "tvdb", "tvdbid": "tvdb",
        "thetvdb": "tvdb", "thetvdb_id": "tvdb",
        "imdb": "imdb", "imdb_id": "imdb", "imdbid": "imdb",
    }
    return aliases.get(text)


def _normalise_external_id(provider: str, value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if provider == "imdb":
        return text.casefold()
    if provider in {"tmdb", "tvdb"} and text.isdigit():
        return str(int(text))
    return text.casefold()


def _extract_ids_from_mapping(value: Any) -> dict[str, set[str]]:
    result = {provider: set() for provider in SUPPORTED_IDS}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for raw_key, raw_value in node.items():
                provider = _normalise_provider(raw_key)
                if provider and not isinstance(raw_value, (dict, list, tuple, set)):
                    normalised = _normalise_external_id(provider, raw_value)
                    if normalised:
                        result[provider].add(normalised)
                if isinstance(raw_value, (dict, list, tuple, set)):
                    visit(raw_value)
        elif isinstance(node, (list, tuple, set)):
            for child in node:
                visit(child)

    visit(value)
    return result


def _kodi_unique_ids(row: dict[str, Any]) -> dict[str, set[str]]:
    ids = _extract_ids_from_mapping(row.get("uniqueid") or {})
    if row.get("imdbnumber"):
        normalised = _normalise_external_id("imdb", row["imdbnumber"])
        if normalised:
            ids["imdb"].add(normalised)
    return ids


def _year_from_item(item: Item) -> int | None:
    value = getattr(item, "release_datetime", None)
    return value.year if value is not None else None


def _row_year(row: dict[str, Any]) -> int | None:
    try:
        year = int(row.get("year") or 0)
    except (TypeError, ValueError):
        return None
    return year if year > 0 else None


def _resume_seconds(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    try:
        position = float(value.get("position") or 0)
    except (TypeError, ValueError):
        return None
    return int(position) if position > 0 else None


def _paged_library_call(
    kodi: KodiClient,
    *,
    method: str,
    result_key: str,
    properties: Iterable[str],
) -> list[dict[str, Any]]:
    """Read one Kodi VideoLibrary collection in bounded pages."""
    page_size = _page_size()
    start = 0
    rows: list[dict[str, Any]] = []
    while True:
        result = kodi.call(
            method,
            {
                "properties": list(properties),
                "limits": {"start": start, "end": start + page_size},
                "sort": {"method": "label", "order": "ascending"},
            },
        )
        if not isinstance(result, dict):
            raise KodiLibraryAwarenessError(f"{method} returned a non-object result.")
        page = result.get(result_key) or []
        if not isinstance(page, list):
            raise KodiLibraryAwarenessError(f"{method} returned invalid {result_key} data.")
        rows.extend(row for row in page if isinstance(row, dict))
        limits = result.get("limits") or {}
        try:
            end = int(limits.get("end", start + len(page)))
            total = int(limits.get("total", len(rows)))
        except (TypeError, ValueError):
            end, total = start + len(page), len(rows)
        if not page or end >= total:
            break
        if end <= start:
            raise KodiLibraryAwarenessError(f"{method} pagination did not advance.")
        start = end
    return rows


def _read_kodi_library(kodi: KodiClient) -> tuple[list[dict], list[dict], list[dict]]:
    movies = _paged_library_call(
        kodi,
        method="VideoLibrary.GetMovies",
        result_key="movies",
        properties=("title", "year", "imdbnumber", "uniqueid", "file", "playcount", "lastplayed", "resume"),
    )
    shows = _paged_library_call(
        kodi,
        method="VideoLibrary.GetTVShows",
        result_key="tvshows",
        properties=("title", "year", "imdbnumber", "uniqueid", "file", "playcount", "episode", "watchedepisodes"),
    )
    episodes = _paged_library_call(
        kodi,
        method="VideoLibrary.GetEpisodes",
        result_key="episodes",
        properties=("title", "showtitle", "season", "episode", "tvshowid", "uniqueid", "file", "playcount", "lastplayed", "resume"),
    )
    return movies, shows, episodes


def _build_floppy_indexes() -> dict[str, Any]:
    items = list(
        Item.objects.filter(media_type__in=("movie", "tv", "episode")).only(
            "id", "media_id", "source", "media_type", "title", "original_title",
            "localized_title", "release_datetime", "season_number", "episode_number",
            "provider_external_ids",
        )
    )
    item_by_id = {item.id: item for item in items}
    ids = {
        media_type: {provider: defaultdict(set) for provider in SUPPORTED_IDS}
        for media_type in ("movie", "tv", "episode")
    }
    title_year = {media_type: defaultdict(set) for media_type in ("movie", "tv", "episode")}
    title_only = {media_type: defaultdict(set) for media_type in ("movie", "tv", "episode")}

    def add_id(item: Item, provider: str, raw_value: Any) -> None:
        value = _normalise_external_id(provider, raw_value)
        if value:
            ids[item.media_type][provider][value].add(item.id)

    for item in items:
        provider = _normalise_provider(item.source)
        if provider:
            add_id(item, provider, item.media_id)
        external = _extract_ids_from_mapping(item.provider_external_ids or {})
        for provider_name, values in external.items():
            for value in values:
                add_id(item, provider_name, value)
        year = _year_from_item(item)
        titles = {
            _normalise_title(item.title),
            _normalise_title(item.original_title),
            _normalise_title(item.localized_title),
        }
        titles.discard("")
        for title in titles:
            title_only[item.media_type][title].add(item.id)
            if year:
                title_year[item.media_type][(title, year)].add(item.id)

    if item_by_id:
        links = ItemProviderLink.objects.filter(item_id__in=item_by_id).only(
            "item_id", "provider", "provider_media_id", "metadata"
        )
        for link in links.iterator():
            item = item_by_id.get(link.item_id)
            if item is None:
                continue
            provider = _normalise_provider(link.provider)
            if provider:
                add_id(item, provider, link.provider_media_id)
            external = _extract_ids_from_mapping(link.metadata or {})
            for provider_name, values in external.items():
                for value in values:
                    add_id(item, provider_name, value)

    return {"items": item_by_id, "ids": ids, "title_year": title_year, "title_only": title_only}


def _match_item(*, row: dict[str, Any], media_type: str, indexes: dict[str, Any]) -> dict[str, Any]:
    unique_ids = _kodi_unique_ids(row)
    for provider in SUPPORTED_IDS:
        for external_id in sorted(unique_ids[provider]):
            candidates = indexes["ids"][media_type][provider].get(external_id, set())
            if len(candidates) == 1:
                return {
                    "item_id": next(iter(candidates)), "method": provider, "confidence": "exact",
                    "external_id": external_id, "diagnosis_code": "exact_external_id",
                    "diagnosis": f"Exact {provider.upper()} ID",
                }
            if len(candidates) > 1:
                return {
                    "item_id": None, "method": provider, "confidence": "ambiguous",
                    "external_id": external_id, "candidate_count": len(candidates),
                    "diagnosis_code": "ambiguous_external_id",
                    "diagnosis": f"{provider.upper()} ID matches multiple Floppy items",
                }

    title = _normalise_title(row.get("title") or row.get("label"))
    year = _row_year(row)
    if title and year:
        candidates = indexes["title_year"][media_type].get((title, year), set())
        if len(candidates) == 1:
            return {"item_id": next(iter(candidates)), "method": "title+year", "confidence": "fallback", "diagnosis_code": "fallback_title_year", "diagnosis": "Fallback title + year match"}
        if len(candidates) > 1:
            return {"item_id": None, "method": "title+year", "confidence": "ambiguous", "candidate_count": len(candidates), "diagnosis_code": "ambiguous_title_year", "diagnosis": "Title + year matches multiple Floppy items"}

    if title and year is None:
        candidates = indexes["title_only"][media_type].get(title, set())
        if len(candidates) == 1:
            return {"item_id": next(iter(candidates)), "method": "title-only", "confidence": "fallback", "diagnosis_code": "fallback_title_only", "diagnosis": "Fallback title-only match"}
        if len(candidates) > 1:
            return {"item_id": None, "method": "title-only", "confidence": "ambiguous", "candidate_count": len(candidates), "diagnosis_code": "ambiguous_title_only", "diagnosis": "Title matches multiple Floppy items"}

    if any(unique_ids[provider] for provider in SUPPORTED_IDS) or title:
        diagnosis = "Movie not found in Floppy" if media_type == "movie" else "TV show not found in Floppy"
        return {"item_id": None, "method": "none", "confidence": "unresolved", "diagnosis_code": "not_found_in_floppy", "diagnosis": diagnosis}
    return {"item_id": None, "method": "none", "confidence": "unresolved", "diagnosis_code": "missing_kodi_identity", "diagnosis": "Kodi item has no usable identity"}


def _build_episode_hierarchy(user_id: int) -> dict[str, Any]:
    episodes: dict[tuple[int, int, int], set[int]] = defaultdict(set)
    seasons: set[tuple[int, int]] = set()
    for season in Season.objects.filter(user_id=user_id).select_related("item", "related_tv__item").iterator():
        tv_item = getattr(getattr(season, "related_tv", None), "item", None)
        season_item = getattr(season, "item", None)
        season_number = getattr(season_item, "season_number", None)
        if tv_item is not None and season_number is not None:
            seasons.add((int(tv_item.id), int(season_number)))

    queryset = Episode.objects.filter(related_season__user_id=user_id).select_related(
        "item", "related_season__item", "related_season__related_tv__item"
    )
    for episode in queryset.iterator():
        season = episode.related_season
        tv_item = getattr(getattr(season, "related_tv", None), "item", None)
        season_item = getattr(season, "item", None)
        episode_item = getattr(episode, "item", None)
        if tv_item is None or season_item is None or episode_item is None:
            continue
        season_number = getattr(season_item, "season_number", None)
        episode_number = getattr(episode_item, "episode_number", None)
        if season_number is None or episode_number is None:
            continue
        key = (int(tv_item.id), int(season_number), int(episode_number))
        seasons.add(key[:2])
        episodes[key].add(int(episode_item.id))
    return {"seasons": seasons, "episodes": episodes}


def _match_episode(
    *,
    row: dict[str, Any],
    indexes: dict[str, Any],
    show_matches: dict[int, dict[str, Any]],
    episode_hierarchy: dict[str, Any],
) -> dict[str, Any]:
    """Match episodes only through the authoritative parent-show + SxxEyy identity."""
    try:
        kodi_tvshow_id = int(row.get("tvshowid"))
        season_number = int(row.get("season"))
        episode_number = int(row.get("episode"))
    except (TypeError, ValueError):
        return {"item_id": None, "method": "none", "confidence": "unresolved", "diagnosis_code": "invalid_kodi_episode_identity", "diagnosis": "Kodi episode identity incomplete"}

    show_match = show_matches.get(kodi_tvshow_id)
    if not show_match:
        return {"item_id": None, "method": "none", "confidence": "unresolved", "diagnosis_code": "unmatched_parent_show", "diagnosis": "Parent TV show not matched"}
    floppy_show_id = show_match.get("item_id")
    if not floppy_show_id:
        if show_match.get("confidence") == "ambiguous":
            return {"item_id": None, "method": show_match.get("method") or "parent-show", "confidence": "ambiguous", "candidate_count": show_match.get("candidate_count"), "diagnosis_code": "ambiguous_parent_show", "diagnosis": "Parent TV show match ambiguous"}
        return {"item_id": None, "method": "parent-show", "confidence": "unresolved", "diagnosis_code": "unmatched_parent_show", "diagnosis": "Parent TV show not matched"}

    floppy_show_id = int(floppy_show_id)
    if (floppy_show_id, season_number) not in episode_hierarchy["seasons"]:
        return {"item_id": None, "method": "show+season", "confidence": "unresolved", "diagnosis_code": "missing_floppy_season", "diagnosis": "Season missing from Floppy"}
    candidates = episode_hierarchy["episodes"].get((floppy_show_id, season_number, episode_number), set())
    if len(candidates) == 1:
        return {"item_id": next(iter(candidates)), "method": "show+SxxEyy", "confidence": "strong", "diagnosis_code": "matched_show_season_episode", "diagnosis": "Matched by parent show + S/E"}
    if len(candidates) > 1:
        return {"item_id": None, "method": "show+SxxEyy", "confidence": "ambiguous", "candidate_count": len(candidates), "diagnosis_code": "duplicate_floppy_episode", "diagnosis": "Duplicate Floppy episode records"}
    return {"item_id": None, "method": "show+SxxEyy", "confidence": "unresolved", "diagnosis_code": "missing_floppy_episode", "diagnosis": "Episode missing from Floppy"}


def _compact_kodi_row(row: dict[str, Any], *, kind: str, match: dict[str, Any]) -> dict[str, Any]:
    id_field = {"movie": "movieid", "tv": "tvshowid", "episode": "episodeid"}[kind]
    compact = {
        "kind": kind,
        "kodi_id": row.get(id_field),
        "title": str(row.get("title") or row.get("label") or "").strip(),
        "year": _row_year(row),
        "file": str(row.get("file") or ""),
        "playcount": int(row.get("playcount") or 0),
        "lastplayed": str(row.get("lastplayed") or ""),
        "resume_seconds": _resume_seconds(row.get("resume")),
        "item_id": match.get("item_id"),
        "method": match.get("method"),
        "confidence": match.get("confidence"),
        "diagnosis_code": match.get("diagnosis_code"),
        "diagnosis": match.get("diagnosis"),
    }
    if kind == "tv":
        compact["episode_count"] = int(row.get("episode") or 0)
        compact["watched_episode_count"] = int(row.get("watchedepisodes") or 0)
    elif kind == "episode":
        compact.update(
            showtitle=str(row.get("showtitle") or "").strip(),
            season=row.get("season"),
            episode=row.get("episode"),
            tvshowid=row.get("tvshowid"),
        )
    if match.get("external_id"):
        compact["external_id"] = match["external_id"]
    if match.get("candidate_count"):
        compact["candidate_count"] = match["candidate_count"]
    return compact


def _summary_bucket(rows: list[dict[str, Any]]) -> dict[str, int]:
    result = {"total": len(rows), "matched": 0, "exact": 0, "strong": 0, "fallback": 0, "ambiguous": 0, "unresolved": 0}
    for row in rows:
        confidence = row.get("confidence") or "unresolved"
        if confidence in {"exact", "strong", "fallback"}:
            result["matched"] += 1
        result[confidence if confidence in result else "unresolved"] += 1
    return result


def refresh_kodi_library(user) -> dict[str, Any]:
    """Build and cache a read-only reconciliation snapshot."""
    configured_user_id = _configured_floppy_user_id()
    if configured_user_id is not None and int(user.id) != configured_user_id:
        raise KodiLibraryAwarenessError("This Kodi instance is assigned to a different Floppy user.")

    kodi = KodiClient.from_env()
    movies_raw, shows_raw, episodes_raw = _read_kodi_library(kodi)
    indexes = _build_floppy_indexes()
    hierarchy = _build_episode_hierarchy(int(user.id))

    shows: list[dict[str, Any]] = []
    show_matches: dict[int, dict[str, Any]] = {}
    for row in shows_raw:
        match = _match_item(row=row, media_type="tv", indexes=indexes)
        shows.append(_compact_kodi_row(row, kind="tv", match=match))
        try:
            show_matches[int(row.get("tvshowid"))] = match
        except (TypeError, ValueError):
            pass

    movies = [
        _compact_kodi_row(row, kind="movie", match=_match_item(row=row, media_type="movie", indexes=indexes))
        for row in movies_raw
    ]
    episodes = [
        _compact_kodi_row(
            row,
            kind="episode",
            match=_match_episode(row=row, indexes=indexes, show_matches=show_matches, episode_hierarchy=hierarchy),
        )
        for row in episodes_raw
    ]

    by_floppy_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    review_rows: list[dict[str, Any]] = []
    for row in movies + shows + episodes:
        if row.get("item_id"):
            by_floppy_item[str(row["item_id"])].append(row)
        if row.get("confidence") in {"fallback", "ambiguous", "unresolved"}:
            review_rows.append(row)

    confidence_order = {"ambiguous": 0, "unresolved": 1, "fallback": 2}
    review_rows.sort(key=lambda row: (
        confidence_order.get(row.get("confidence"), 9),
        row.get("kind") or "",
        (row.get("showtitle") or row.get("title") or "").casefold(),
        row.get("season") or -1,
        row.get("episode") or -1,
    ))
    review_rows_by_kind = {
        kind: [row for row in review_rows if row.get("kind") == kind][:500]
        for kind in ("movie", "tv", "episode")
    }
    review_counts = {
        kind: sum(1 for row in review_rows if row.get("kind") == kind)
        for kind in ("movie", "tv", "episode")
    }
    review_counts["all"] = len(review_rows)

    snapshot = {
        "version": VERSION,
        "user_id": int(user.id),
        "refreshed_at": timezone.now().isoformat(),
        "summary": {
            "movie": _summary_bucket(movies),
            "tv": _summary_bucket(shows),
            "episode": _summary_bucket(episodes),
        },
        "review_rows": review_rows[:500],
        "review_rows_by_kind": review_rows_by_kind,
        "review_counts": review_counts,
        "by_floppy_item": dict(by_floppy_item),
    }
    cache.set(_cache_key(user.id), snapshot, timeout=CACHE_TTL_SECONDS)
    return snapshot


def get_kodi_library_snapshot(user_id: int) -> dict[str, Any] | None:
    snapshot = cache.get(_cache_key(user_id))
    return snapshot if isinstance(snapshot, dict) else None


def get_kodi_library_entries_for_item(user_id: int, item_id: int) -> list[dict[str, Any]]:
    snapshot = get_kodi_library_snapshot(user_id)
    if not snapshot:
        return []
    entries = (snapshot.get("by_floppy_item") or {}).get(str(int(item_id))) or []
    return entries if isinstance(entries, list) else []


@login_required
@require_http_methods(["GET", "POST"])
def kodi_library(request):
    error = None
    snapshot = get_kodi_library_snapshot(request.user.id)
    review_filter = str(request.GET.get("review_type") or "all").strip().lower()
    if review_filter not in {"all", "movie", "tv", "episode"}:
        review_filter = "all"
    if request.method == "POST" or snapshot is None:
        try:
            snapshot = refresh_kodi_library(request.user)
        except (KodiError, KodiLibraryAwarenessError) as exc:
            logger.warning("Kodi library refresh failed: %s", exc)
            error = str(exc)
        except Exception:
            logger.exception("Unexpected Kodi library awareness refresh failure")
            error = "Kodi library refresh failed unexpectedly."

    review_rows = []
    review_rows_total = review_rows_displayed = 0
    review_counts = {"all": 0, "movie": 0, "tv": 0, "episode": 0}
    if snapshot:
        stored_counts = snapshot.get("review_counts") or {}
        for key in review_counts:
            try:
                review_counts[key] = int(stored_counts.get(key) or 0)
            except (TypeError, ValueError):
                review_counts[key] = 0
        review_rows = (
            snapshot.get("review_rows") or []
            if review_filter == "all"
            else (snapshot.get("review_rows_by_kind") or {}).get(review_filter) or []
        )
        review_rows_total = review_counts[review_filter]
        review_rows_displayed = len(review_rows)

    return render(
        request,
        "app/kodi_library.html",
        {
            "snapshot": snapshot,
            "error": error,
            "patch_version": VERSION,
            "review_filter": review_filter,
            "review_rows": review_rows,
            "review_rows_total": review_rows_total,
            "review_rows_displayed": review_rows_displayed,
            "review_counts": review_counts,
        },
    )
