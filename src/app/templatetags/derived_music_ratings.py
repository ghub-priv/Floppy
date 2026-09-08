"""Read-only hierarchical music ratings derived from lower-level ratings."""

from collections import defaultdict
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal

from django import template
from django.utils.translation import gettext as _

from app.models import AlbumTracker, Music, Track


register = template.Library()

DERIVED_MUSIC_RATINGS_VERSION = "1.0.0"
_REQUEST_CACHE_ATTR = "_derived_music_ratings_v1_cache"


def _display_score(user, raw_average):
    """Format a stored 10-point average on the user's configured rating scale."""
    if raw_average is None:
        return None

    try:
        scale_max = int(user.rating_scale_max)
    except (TypeError, ValueError, AttributeError):
        scale_max = 10

    value = raw_average / Decimal("2") if scale_max == 5 else raw_average
    return format(
        value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        ".2f",
    )


def _entity_id(kind, entity):
    """Resolve an artist/album id from a model, tracker, mapping, or integer."""
    if entity is None:
        return None

    if isinstance(entity, Mapping):
        direct = entity.get("id")
        related = entity.get(f"{kind}_id")
        if related:
            return related
        return direct

    related = getattr(entity, f"{kind}_id", None)
    if related:
        return related

    direct = getattr(entity, "id", None)
    if direct:
        return direct

    try:
        return int(entity)
    except (TypeError, ValueError):
        return None


def _normalise_title(value):
    return " ".join(str(value or "").casefold().split())


def _build_result(user, *, kind, raw_score, rated, total, title):
    if total <= 0:
        return None

    unit = "track" if kind == "album" else "album"

    return {
        "kind": kind,
        "score": _display_score(user, raw_score),
        "raw_score": float(raw_score) if raw_score is not None else None,
        "_raw_decimal": raw_score,
        "rated": rated,
        "total": total,
        "coverage_percent": round((rated / total) * 100, 1) if total else 0.0,
        "unit": unit,
        "title": title,
        "version": DERIVED_MUSIC_RATINGS_VERSION,
    }


def _album_results(user, album_ids):
    """Return track-derived rating results for a set of albums."""
    album_ids = {int(value) for value in album_ids if value}
    if not album_ids:
        return {}

    track_rows = list(
        Track.objects.filter(album_id__in=album_ids).values(
            "id",
            "album_id",
            "title",
        )
    )

    total_keys = defaultdict(set)
    title_candidates = defaultdict(lambda: defaultdict(list))

    for row in track_rows:
        album_id = row["album_id"]
        track_id = row["id"]
        total_keys[album_id].add(("track", track_id))
        title = _normalise_title(row["title"])
        if title:
            title_candidates[album_id][title].append(track_id)

    unique_title_track = {}
    for album_id, titles in title_candidates.items():
        unique_title_track[album_id] = {
            title: ids[0]
            for title, ids in titles.items()
            if len(ids) == 1
        }

    music_rows = (
        Music.objects.filter(
            user=user,
            album_id__in=album_ids,
        )
        .order_by(
            "-end_date",
            "-created_at",
            "-id",
        )
        .values(
            "id",
            "album_id",
            "track_id",
            "item_id",
            "item__title",
            "score",
        )
    )

    latest_scores = defaultdict(dict)

    for row in music_rows:
        album_id = row["album_id"]
        if not album_id:
            continue

        track_id = row["track_id"]
        if track_id:
            key = ("track", track_id)
        else:
            title = _normalise_title(row["item__title"])
            mapped_track_id = unique_title_track.get(album_id, {}).get(title)
            if mapped_track_id:
                key = ("track", mapped_track_id)
            elif row["item_id"]:
                key = ("item", row["item_id"])
            else:
                key = ("music", row["id"])

        total_keys[album_id].add(key)

        # Rows are newest-first, so the first occurrence is the authoritative
        # rating for this track after repeated plays/rewatches.
        if key not in latest_scores[album_id]:
            latest_scores[album_id][key] = row["score"]

    results = {}
    for album_id in album_ids:
        keys = total_keys.get(album_id, set())
        total = len(keys)
        if total == 0:
            results[album_id] = None
            continue

        rated = 0
        score_sum = Decimal("0")
        scores = latest_scores.get(album_id, {})

        for key in keys:
            score = scores.get(key)
            if score is None:
                continue
            rated += 1
            score_sum += Decimal(score)

        raw_average = score_sum / rated if rated else None
        results[album_id] = _build_result(
            user,
            kind="album",
            raw_score=raw_average,
            rated=rated,
            total=total,
            title=_(
                "Your read-only album rating derived from track ratings. "
                "Each track counts once; repeated plays use the newest tracked row."
            ),
        )

    return results


def _artist_result_from_albums(
    user,
    *,
    artist_id,
    album_ids,
    direct_scores,
    album_results,
):
    """Build one artist result, giving direct album ratings precedence."""
    album_ids = list(dict.fromkeys(album_ids))
    total = len(album_ids)
    if total == 0:
        return None

    rated = 0
    direct_count = 0
    derived_count = 0
    score_sum = Decimal("0")

    for album_id in album_ids:
        direct = direct_scores.get(album_id)
        if direct is not None:
            value = Decimal(direct)
            direct_count += 1
        else:
            album_result = album_results.get(album_id)
            value = album_result.get("_raw_decimal") if album_result else None
            if value is not None:
                derived_count += 1

        if value is None:
            continue

        rated += 1
        score_sum += value

    raw_average = score_sum / rated if rated else None
    result = _build_result(
        user,
        kind="artist",
        raw_score=raw_average,
        rated=rated,
        total=total,
        title=_(
            "Your read-only artist rating derived from albums. "
            "A direct album rating takes priority; otherwise that album's "
            "track-derived rating is used. Each album counts once."
        ),
    )
    if result is not None:
        result["direct_album_count"] = direct_count
        result["derived_album_count"] = derived_count
        result["artist_id"] = artist_id
    return result


def _artist_results(user, artist_ids):
    """Return album-derived rating results for a set of artists."""
    artist_ids = {int(value) for value in artist_ids if value}
    if not artist_ids:
        return {}

    direct_rows = list(
        AlbumTracker.objects.filter(
            user=user,
            album__artist_id__in=artist_ids,
        ).values(
            "album_id",
            "album__artist_id",
            "score",
        )
    )

    played_rows = list(
        Music.objects.filter(
            user=user,
            album__artist_id__in=artist_ids,
            album_id__isnull=False,
        )
        .values(
            "album_id",
            "album__artist_id",
        )
        .distinct()
    )

    albums_by_artist = defaultdict(set)
    direct_scores = {}

    for row in direct_rows:
        artist_id = row["album__artist_id"]
        album_id = row["album_id"]
        if not artist_id or not album_id:
            continue
        albums_by_artist[artist_id].add(album_id)
        direct_scores[album_id] = row["score"]

    for row in played_rows:
        artist_id = row["album__artist_id"]
        album_id = row["album_id"]
        if not artist_id or not album_id:
            continue
        albums_by_artist[artist_id].add(album_id)

    all_album_ids = {
        album_id
        for album_ids in albums_by_artist.values()
        for album_id in album_ids
    }
    album_results = _album_results(user, all_album_ids)

    return {
        artist_id: _artist_result_from_albums(
            user,
            artist_id=artist_id,
            album_ids=sorted(albums_by_artist.get(artist_id, set())),
            direct_scores=direct_scores,
            album_results=album_results,
        )
        for artist_id in artist_ids
    }


def _request_cache(context):
    request = context.get("request")
    if request is not None:
        cache = getattr(request, _REQUEST_CACHE_ATTR, None)
        if cache is None:
            cache = {
                "artist": {},
                "album": {},
                "artist_done": set(),
                "album_done": set(),
            }
            setattr(request, _REQUEST_CACHE_ATTR, cache)
        return cache

    cache = context.get("_derived_music_ratings_cache")
    if cache is None:
        cache = {
            "artist": {},
            "album": {},
            "artist_done": set(),
            "album_done": set(),
        }
        try:
            context["_derived_music_ratings_cache"] = cache
        except TypeError:
            pass
    return cache


def _page_ids(context, kind):
    media_list = context.get("media_list")
    if media_list is None:
        return set()

    ids = set()
    try:
        rows = list(media_list)
    except TypeError:
        return ids

    for row in rows:
        value = getattr(row, f"{kind}_id", None)
        if not value:
            related = getattr(row, kind, None)
            value = getattr(related, "id", None)
        if value:
            ids.add(int(value))
    return ids


@register.simple_tag(takes_context=True)
def derived_music_rating(context, user, kind, entity):
    """Return a read-only hierarchical rating for an album or artist."""
    if not getattr(user, "is_authenticated", False):
        return None

    kind = str(kind or "").strip().lower()
    if kind not in {"album", "artist"}:
        return None

    entity_id = _entity_id(kind, entity)
    if not entity_id:
        return None
    entity_id = int(entity_id)

    cache = _request_cache(context)
    result_cache = cache[kind]
    done = cache[f"{kind}_done"]

    if entity_id not in done:
        scope_ids = _page_ids(context, kind)
        scope_ids.add(entity_id)
        pending = scope_ids - done

        if kind == "album":
            results = _album_results(user, pending)
        else:
            results = _artist_results(user, pending)

        for pending_id in pending:
            result_cache[pending_id] = results.get(pending_id)
        done.update(pending)

    return result_cache.get(entity_id)
