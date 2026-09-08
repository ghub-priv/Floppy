"""Region-aware movie watched-date suggestions.

This is the Git-native successor to the Smart Watched Dates v1.0.3 runtime
patch. It keeps the release-date resolver isolated from the large TMDB movie
metadata payload and lets the date picker request suggestions only when they
are actually needed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import requests
from django.core.cache import cache

from app.models import MediaTypes, Sources
from app.providers import services, tmdb

logger = logging.getLogger(__name__)

SMART_WATCHED_DATES_VERSION = "1.0.3"
SMART_RELEASE_CACHE_TIMEOUT = 60 * 60 * 24

_RELEASE_FIELDS = (
    ("premiere", "premiere_release_date"),
    ("theatrical", "theatrical_release_date"),
    ("digital", "digital_release_date"),
    ("physical", "physical_release_date"),
)


def _empty_release_fields():
    return {field_name: "" for _name, field_name in _RELEASE_FIELDS}


def _valid_iso_date(value):
    """Return a YYYY-MM-DD prefix when *value* contains a valid-looking ISO date."""
    raw = str(value or "").strip()
    iso_date = raw[:10]
    if (
        len(iso_date) != 10
        or iso_date[4:5] != "-"
        or iso_date[7:8] != "-"
        or not iso_date[:4].isdigit()
        or not iso_date[5:7].isdigit()
        or not iso_date[8:10].isdigit()
    ):
        return ""
    return iso_date


def parse_release_dates(payload):
    """Parse TMDB release-date rows into worldwide and per-region earliest dates.

    TMDB release types are mapped exactly as in the accepted r13 implementation:
    1 = premiere, 2/3 = theatrical, 4 = digital, 5 = physical.
    """
    results = payload.get("results") if isinstance(payload, Mapping) else None
    if not isinstance(results, list):
        results = []

    resolved = _empty_release_fields()
    by_region = {}

    def update(target, field_name, iso_date):
        current = target.get(field_name) or ""
        if not current or iso_date < current:
            target[field_name] = iso_date

    for result in results:
        if not isinstance(result, Mapping):
            continue

        region = str(result.get("iso_3166_1") or "").strip().upper()
        region_resolved = None
        if region:
            region_resolved = by_region.setdefault(region, _empty_release_fields())

        release_rows = result.get("release_dates") or []
        if not isinstance(release_rows, list):
            continue

        for release in release_rows:
            if not isinstance(release, Mapping):
                continue
            try:
                release_type = int(release.get("type"))
            except (TypeError, ValueError):
                continue

            iso_date = _valid_iso_date(release.get("release_date"))
            if not iso_date:
                continue

            if release_type == 1:
                field_name = "premiere_release_date"
            elif release_type in (2, 3):
                field_name = "theatrical_release_date"
            elif release_type == 4:
                field_name = "digital_release_date"
            elif release_type == 5:
                field_name = "physical_release_date"
            else:
                continue

            update(resolved, field_name, iso_date)
            if region_resolved is not None:
                update(region_resolved, field_name, iso_date)

    by_region = {
        region: values for region, values in by_region.items() if any(values.values())
    }
    return {
        **resolved,
        "smart_release_dates_by_region": by_region,
    }


def _cache_key(media_id, language=None):
    language = language or ""
    return f"smart_watched_dates_v{SMART_WATCHED_DATES_VERSION}:{media_id}:{language}"


def movie_release_dates(media_id, *, language=None):
    """Return parsed TMDB movie release dates, using a dedicated cache entry."""
    media_id = str(media_id or "").strip()
    if not media_id:
        return {
            **_empty_release_fields(),
            "smart_release_dates_by_region": {},
        }

    key = _cache_key(media_id, language)
    cached = cache.get(key)
    if isinstance(cached, Mapping):
        return dict(cached)

    url = f"{tmdb.base_url}/movie/{media_id}/release_dates"
    try:
        response = services.api_request(
            Sources.TMDB.value,
            "GET",
            url,
            params=tmdb.base_params(language),
        )
    except requests.exceptions.HTTPError as error:
        tmdb.handle_error(error)
        raise
    except services.ProviderAPIError:
        raise

    parsed = parse_release_dates(response)
    cache.set(key, parsed, SMART_RELEASE_CACHE_TIMEOUT)
    return parsed


def resolve_movie_suggestions(preferred_region, release_dates):
    """Resolve r13-compatible watched-date suggestions for one preferred region."""
    release_dates = release_dates if isinstance(release_dates, Mapping) else {}
    preferred_region = str(preferred_region or "").strip().upper()
    if preferred_region == "UNSET":
        preferred_region = ""

    region_map = release_dates.get("smart_release_dates_by_region") or {}
    regional = {}
    if preferred_region and isinstance(region_map, Mapping):
        candidate = region_map.get(preferred_region) or {}
        if isinstance(candidate, Mapping):
            regional = candidate

    resolved = {}
    for name, field_name in _RELEASE_FIELDS:
        resolved[name] = _valid_iso_date(
            regional.get(field_name) or release_dates.get(field_name)
        )
    return resolved


def suggestions_for_media(
    *,
    source,
    media_type,
    media_id,
    preferred_region="",
    language=None,
):
    """Return suggestions only for TMDB movies; other media get an empty mapping."""
    if source != Sources.TMDB.value or media_type != MediaTypes.MOVIE.value:
        return {}
    return resolve_movie_suggestions(
        preferred_region,
        movie_release_dates(media_id, language=language),
    )
