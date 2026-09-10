from __future__ import annotations

import math
from collections import Counter, defaultdict
from statistics import mean, median, pstdev

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from app.discover.feature_metadata import (
    normalize_collection,
    normalize_features,
    normalize_person_name,
    normalize_studio,
    release_decade_label,
)
from app.discover.scoring import blended_world_quality, weighted_pearson_correlation
from app.models import (
    CreditRoleType,
    Episode,
    ItemPersonCredit,
    ItemStudioCredit,
    Movie,
    Status,
    TV,
)

ENGINE_VERSION = "1.0.0"
TV_EXTENSION_VERSION = "2.1.0"
COMBINED_EXTENSION_VERSION = "2.3.0"
PROFILE_SCHEMA_VERSION = 2
CACHE_SECONDS = 300
MAX_LEAD_CAST = 5
TV_CONFIDENCE_PRIOR_EPISODES = 5.0

MOVIE_FAMILY_CONFIG = {
    "genres": ("Genres", 12.0, 20, "Genre effects against your personal movie-rating baseline."),
    "decades": ("Decades", 20.0, 20, "Release-decade effects against your personal baseline."),
    "directors": ("Directors", 6.0, 4, "Strict Director-role credits only; other Directing-department crew are excluded."),
    "lead_cast": ("Lead cast", 8.0, 5, "Top five cast credits per film, with small samples shrunk towards baseline."),
    "studios": ("Studios", 15.0, 10, "Production-company effects with a stronger evidence threshold."),
    "collections": ("Collections / franchises", 8.0, 4, "Provider collection/franchise effects where metadata is available."),
}
MOVIE_DISPLAY_FAMILY_ORDER = ("genres", "decades", "directors", "lead_cast", "studios", "collections")

TV_FAMILY_CONFIG = {
    "genres": ("Genres", 8.0, 10, "Show-normalised genre effects. Each TV show contributes one observation regardless of episode count."),
    "decades": ("Decades", 10.0, 10, "Show-normalised release-decade effects against your TV-show baseline."),
    "lead_cast": ("Lead cast", 6.0, 3, "Top five show-level cast credits where metadata is available."),
    "studios": ("Studios", 8.0, 5, "Show-level production-company effects with low-sample shrinkage."),
}
TV_DISPLAY_FAMILY_ORDER = ("genres", "decades", "lead_cast", "studios")

# Backwards-compatible names used by the original v1 implementation and by
# rating_intelligence_advanced.py.
FAMILY_CONFIG = MOVIE_FAMILY_CONFIG
DISPLAY_FAMILY_ORDER = MOVIE_DISPLAY_FAMILY_ORDER


def _normalise_media_kind(value):
    value = str(value or "").strip().lower()
    if value == "tv":
        return "tv"
    if value == "combined":
        return "combined"
    return "movies"


def _cache_key(user_id, media_kind="movies"):
    media_kind = _normalise_media_kind(media_kind)
    return f"rating-intelligence:v1:{COMBINED_EXTENSION_VERSION}:{media_kind}:{user_id}"


def invalidate_rating_intelligence_cache(user_id):
    cache.delete(_cache_key(user_id, "movies"))
    cache.delete(_cache_key(user_id, "tv"))
    cache.delete(_cache_key(user_id, "combined"))


def _activity_dt(entry):
    values = [
        getattr(entry, "end_date", None),
        getattr(entry, "progressed_at", None),
        getattr(entry, "created_at", None),
    ]
    values = [v for v in values if v is not None]
    return max(values) if values else None


def _percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * p
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    frac = pos - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _correlation_label(value):
    magnitude = abs(float(value))
    if magnitude < 0.20:
        return "Very weak"
    if magnitude < 0.40:
        return "Weak"
    if magnitude < 0.60:
        return "Moderate"
    if magnitude < 0.80:
        return "Strong"
    return "Very strong"


def _rating_distribution(scores):
    distribution = Counter(scores)
    max_count = max(distribution.values(), default=1)
    return [
        {
            "score": score,
            "count": distribution[score],
            "pct": round((distribution[score] / len(scores) * 100.0) if scores else 0.0, 3),
            "bar_pct": round(distribution[score] / max_count * 100.0, 2),
        }
        for score in sorted(distribution)
    ]


def _load_movies(user):
    rows = list(
        Movie.objects.filter(user=user, score__isnull=False)
        .select_related("item")
        .only(
            "id", "created_at", "progressed_at", "end_date", "score", "item_id",
            "item__title", "item__genres", "item__release_datetime", "item__studios",
            "item__provider_collection_id", "item__provider_collection_name",
            "item__provider_rating", "item__provider_rating_count",
            "item__trakt_rating", "item__trakt_rating_count",
        )
    )
    latest_by_item = {}
    for row in rows:
        current = latest_by_item.get(row.item_id)
        stamp = _activity_dt(row)
        current_stamp = _activity_dt(current) if current is not None else None
        if current is None or (stamp is not None and (current_stamp is None or stamp >= current_stamp)):
            latest_by_item[row.item_id] = row
    return rows, list(latest_by_item.values())


def _load_tv_shows(user):
    """Build one current rating observation per TV show from episode ratings.

    Episode is the atomic rating unit. Rewatches are deduplicated by episode
    item_id using the newest activity row, and Specials (season 0) are excluded
    to match Derived TV Ratings v1.0.1.
    """
    rows = list(
        Episode.objects.filter(
            related_season__related_tv__user=user,
            status=Status.COMPLETED.value,
            item__season_number__gt=0,
        )
        .order_by(
            "related_season__related_tv_id",
            "item_id",
            "-end_date",
            "-created_at",
            "-id",
        )
        .values(
            "related_season__related_tv_id",
            "item_id",
            "item__season_number",
            "score",
        )
    )

    latest_by_episode = {}
    source_rated_rows = 0
    for row in rows:
        if row["score"] is not None:
            source_rated_rows += 1
        key = (row["related_season__related_tv_id"], row["item_id"])
        if key not in latest_by_episode:
            latest_by_episode[key] = row

    grouped = defaultdict(
        lambda: {
            "scores": [],
            "total_episodes": 0,
            "rated_seasons": set(),
        }
    )
    episode_scores = []
    for row in latest_by_episode.values():
        tv_id = row["related_season__related_tv_id"]
        bucket = grouped[tv_id]
        bucket["total_episodes"] += 1
        if row["score"] is None:
            continue
        score = float(row["score"])
        bucket["scores"].append(score)
        episode_scores.append(score)
        season_number = row["item__season_number"]
        if season_number is not None:
            bucket["rated_seasons"].add(int(season_number))

    rated_tv_ids = [tv_id for tv_id, data in grouped.items() if data["scores"]]
    tv_rows = {
        tv.id: tv
        for tv in TV.objects.filter(id__in=rated_tv_ids, user=user)
        .select_related("item")
        .only(
            "id", "user_id", "item_id",
            "item__title", "item__genres", "item__release_datetime", "item__studios",
            "item__provider_collection_id", "item__provider_collection_name",
            "item__provider_rating", "item__provider_rating_count",
            "item__trakt_rating", "item__trakt_rating_count",
        )
    }

    global_episode_mean = mean(episode_scores) if episode_scores else 0.0
    shows = []
    for tv_id in rated_tv_ids:
        tv = tv_rows.get(tv_id)
        if tv is None:
            continue
        data = grouped[tv_id]
        scores = data["scores"]
        raw_rating = mean(scores)
        rated_count = len(scores)
        confidence = rated_count / (rated_count + TV_CONFIDENCE_PRIOR_EPISODES)
        adjusted_rating = (
            confidence * raw_rating
            + (1.0 - confidence) * global_episode_mean
        )
        total_episodes = data["total_episodes"]
        shows.append(
            {
                "tv": tv,
                "tv_id": tv_id,
                "item": tv.item,
                "item_id": tv.item_id,
                "title": tv.item.title,
                "raw_rating": raw_rating,
                "adjusted_rating": adjusted_rating,
                "rated_episodes": rated_count,
                "total_episodes": total_episodes,
                "rated_seasons": len(data["rated_seasons"]),
                "coverage_pct": (rated_count / total_episodes * 100.0) if total_episodes else 0.0,
                "confidence": confidence,
            }
        )

    return {
        "source_rows": len(rows),
        "source_rated_rows": source_rated_rows,
        "deduplicated_rated_rows": source_rated_rows - len(episode_scores),
        "unique_episode_rows": len(latest_by_episode),
        "rated_episodes": len(episode_scores),
        "episode_scores": episode_scores,
        "global_episode_mean": global_episode_mean,
        "shows": shows,
    }


def _credit_maps(item_ids):
    directors = defaultdict(list)
    lead_cast = defaultdict(list)
    director_seen = defaultdict(set)
    cast_seen = defaultdict(set)
    cast_counts = defaultdict(int)

    rows = (
        ItemPersonCredit.objects.filter(item_id__in=item_ids)
        .order_by("item_id", "role_type", "sort_order", "person__name")
        .values_list("item_id", "role_type", "role", "person__name")
    )
    for item_id, role_type, role, raw_name in rows:
        name = normalize_person_name(raw_name or "")
        if not name:
            continue
        if (
            role_type == CreditRoleType.CREW.value
            and normalize_person_name(role or "") == "director"
            and name not in director_seen[item_id]
        ):
            director_seen[item_id].add(name)
            directors[item_id].append(name)
        if (
            role_type == CreditRoleType.CAST.value
            and cast_counts[item_id] < MAX_LEAD_CAST
            and name not in cast_seen[item_id]
        ):
            cast_seen[item_id].add(name)
            lead_cast[item_id].append(name)
            cast_counts[item_id] += 1
    return directors, lead_cast


def _studio_map(item_ids):
    result = defaultdict(list)
    seen = defaultdict(set)
    rows = (
        ItemStudioCredit.objects.filter(item_id__in=item_ids)
        .order_by("item_id", "sort_order", "studio__name")
        .values_list("item_id", "studio__name")
    )
    for item_id, raw_name in rows:
        name = normalize_studio(raw_name or "")
        if name and name not in seen[item_id]:
            seen[item_id].add(name)
            result[item_id].append(name)
    return result


def _summarise_family(family, config, buckets, baseline, coverage_count, title_count):
    label, shrink_k, min_samples, description = config[family]
    rows = []
    for feature_label, raw in buckets.items():
        values = raw["scores"]
        if not values:
            continue
        n = len(values)
        raw_mean = mean(values)
        delta = raw_mean - baseline
        confidence = n / (n + shrink_k)
        residuals = raw["world_residuals"]
        rows.append({
            "label": feature_label,
            "samples": n,
            "mean_rating": round(raw_mean, 4),
            "delta_from_baseline": round(delta, 4),
            "confidence": round(confidence, 4),
            "confidence_pct": round(confidence * 100.0, 1),
            "shrunk_delta": round(delta * confidence, 4),
            "world_residual_mean": round(mean(residuals), 4) if residuals else None,
            "world_samples": len(residuals),
        })

    eligible = [r for r in rows if r["samples"] >= min_samples]
    positive = [r for r in eligible if r["shrunk_delta"] > 0]
    negative = [r for r in eligible if r["shrunk_delta"] < 0]

    return {
        "key": family,
        "label": label,
        "description": description,
        "shrink_k": shrink_k,
        "min_samples": min_samples,
        "titles_with_feature": coverage_count,
        "coverage_pct": round((coverage_count / title_count * 100.0) if title_count else 0.0, 2),
        "distinct_values": len(rows),
        "eligible_rows": sorted(eligible, key=lambda r: (r["label"], r["samples"])),
        "positive": sorted(positive, key=lambda r: (r["shrunk_delta"], r["samples"]), reverse=True)[:20],
        "negative": sorted(negative, key=lambda r: (r["shrunk_delta"], -r["samples"]))[:20],
    }


def _features_for_item(item, item_id, directors_map, lead_cast_map, studios_map, media_kind):
    if media_kind == "tv":
        return {
            "genres": normalize_features(item.genres or [], normalize_person_name),
            "decades": normalize_features([release_decade_label(item.release_datetime)], normalize_person_name),
            "lead_cast": lead_cast_map.get(item_id, []),
            "studios": studios_map.get(item_id) or normalize_features(item.studios or [], normalize_studio),
        }
    return {
        "genres": normalize_features(item.genres or [], normalize_person_name),
        "decades": normalize_features([release_decade_label(item.release_datetime)], normalize_person_name),
        "studios": studios_map.get(item_id) or normalize_features(item.studios or [], normalize_studio),
        "collections": normalize_features([item.provider_collection_name or item.provider_collection_id], normalize_collection),
        "directors": directors_map.get(item_id, []),
        "lead_cast": lead_cast_map.get(item_id, []),
    }


def _world_score(item):
    payload = blended_world_quality(
        provider_rating=getattr(item, "provider_rating", None),
        provider_votes=getattr(item, "provider_rating_count", None),
        trakt_rating=getattr(item, "trakt_rating", None),
        trakt_votes=getattr(item, "trakt_rating_count", None),
    )
    if payload.get("world_source_blend") == "neutral":
        return None
    return float(payload["world_quality"]) * 10.0


def _empty_baseline():
    return {
        "mean": 0.0,
        "median": None,
        "stddev": 0.0,
        "min": None,
        "max": None,
        "p10": None,
        "p25": None,
        "p75": None,
        "p90": None,
        "p95": None,
        "distribution": [],
        "high_rating_7_plus_pct": 0.0,
        "elite_rating_8_plus_pct": 0.0,
    }


def _eligible_basic_rows(card):
    source = card.get("eligible_rows")
    if source is None:
        source = card.get("positive", []) + card.get("negative", [])
    return {row["label"]: row for row in source}


def _combine_basic_family_cards(movie_profile, tv_profile):
    shared_order = ("genres", "decades", "lead_cast", "studios")
    cards = []
    for family in shared_order:
        movie_card = movie_profile["families"].get(family, {})
        tv_card = tv_profile["families"].get(family, {})
        movie_rows = _eligible_basic_rows(movie_card)
        tv_rows = _eligible_basic_rows(tv_card)
        rows = []
        for label in sorted(set(movie_rows) & set(tv_rows)):
            movie_row = movie_rows[label]
            tv_row = tv_rows[label]
            movie_effect = float(movie_row["shrunk_delta"])
            tv_effect = float(tv_row["shrunk_delta"])
            combined_effect = (movie_effect + tv_effect) / 2.0
            rows.append({
                "label": label,
                "movie_effect": round(movie_effect, 4),
                "tv_effect": round(tv_effect, 4),
                "combined_effect": round(combined_effect, 4),
                "divergence": round(tv_effect - movie_effect, 4),
                "absolute_divergence": round(abs(tv_effect - movie_effect), 4),
                "movie_samples": movie_row["samples"],
                "tv_samples": tv_row["samples"],
                "same_direction": movie_effect * tv_effect > 0,
                "opposite_direction": movie_effect * tv_effect < 0,
            })
        cards.append({
            "key": family,
            "label": movie_card.get("label") or tv_card.get("label") or family.title(),
            "shared_values": len(rows),
            "positive": sorted(
                (r for r in rows if r["movie_effect"] > 0 and r["tv_effect"] > 0),
                key=lambda r: (r["combined_effect"], r["movie_samples"] + r["tv_samples"]),
                reverse=True,
            )[:10],
            "negative": sorted(
                (r for r in rows if r["movie_effect"] < 0 and r["tv_effect"] < 0),
                key=lambda r: (r["combined_effect"], -(r["movie_samples"] + r["tv_samples"])),
            )[:10],
            "divergent": sorted(
                (r for r in rows if r["opposite_direction"]),
                key=lambda r: (r["absolute_divergence"], r["movie_samples"] + r["tv_samples"]),
                reverse=True,
            )[:10],
        })
    return cards


def _compute_combined_profile(user):
    movies = compute_rating_intelligence_profile(user, media_kind="movies")
    tv = compute_rating_intelligence_profile(user, media_kind="tv")
    movie_mean = movies["baseline"]["mean"]
    tv_mean = tv["baseline"]["mean"]
    balanced = mean([movie_mean, tv_mean]) if movies["unique_rated_titles"] and tv["unique_rated_titles"] else (movie_mean or tv_mean or 0.0)
    cross_media_cards = _combine_basic_family_cards(movies, tv)
    shared = sum(card["shared_values"] for card in cross_media_cards)
    consistent = sum(len(card["positive"]) + len(card["negative"]) for card in cross_media_cards)
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "tv_extension_version": TV_EXTENSION_VERSION,
        "combined_extension_version": COMBINED_EXTENSION_VERSION,
        "media_kind": "combined",
        "media_label": "Combined",
        "source_rows": movies["source_rows"] + tv["source_rows"],
        "unique_rated_titles": movies["unique_rated_titles"] + tv["unique_rated_titles"],
        "deduplicated_rows": movies["deduplicated_rows"] + tv["deduplicated_rows"],
        "movies": movies,
        "tv": tv,
        "cross_media_cards": cross_media_cards,
        "combined_summary": {
            "rated_movies": movies["unique_rated_titles"],
            "rated_shows": tv["unique_rated_titles"],
            "rated_episodes": tv.get("tv_summary", {}).get("rated_episodes", 0),
            "movie_baseline": movie_mean,
            "tv_baseline": tv_mean,
            "media_balanced_baseline": round(balanced, 4),
            "tv_minus_movie_baseline": round(tv_mean - movie_mean, 4),
            "movie_7_plus_pct": movies["baseline"]["high_rating_7_plus_pct"],
            "tv_show_7_plus_pct": tv["baseline"]["high_rating_7_plus_pct"],
            "movie_public_pearson": movies["world_alignment"]["pearson"],
            "tv_public_pearson": tv["world_alignment"]["pearson"],
            "shared_signals": shared,
            "consistent_signals": consistent,
        },
        "baseline": {"mean": round(balanced, 4)},
        "world_alignment": {},
        "families": {},
        "family_cards": [],
    }


def compute_rating_intelligence_profile(user, media_kind="movies"):
    media_kind = _normalise_media_kind(media_kind)
    if media_kind == "combined":
        return _compute_combined_profile(user)
    is_tv = media_kind == "tv"

    if is_tv:
        tv_data = _load_tv_shows(user)
        entries = tv_data["shows"]
        item_ids = [entry["item_id"] for entry in entries]
        scores = [float(entry["raw_rating"]) for entry in entries]
        source_rows_count = tv_data["source_rated_rows"]
        deduplicated_rows = tv_data["deduplicated_rated_rows"]
        config = TV_FAMILY_CONFIG
        family_order = TV_DISPLAY_FAMILY_ORDER
    else:
        source_rows, movies = _load_movies(user)
        entries = [
            {
                "item": movie.item,
                "item_id": movie.item_id,
                "title": movie.item.title,
                "raw_rating": float(movie.score),
            }
            for movie in movies
        ]
        item_ids = [entry["item_id"] for entry in entries]
        scores = [entry["raw_rating"] for entry in entries]
        source_rows_count = len(source_rows)
        deduplicated_rows = len(source_rows) - len(entries)
        tv_data = None
        config = MOVIE_FAMILY_CONFIG
        family_order = MOVIE_DISPLAY_FAMILY_ORDER

    directors_map, lead_cast_map = _credit_maps(item_ids)
    studios_map = _studio_map(item_ids)
    baseline = mean(scores) if scores else 0.0

    world_user, world_scores, world_residuals = [], [], []
    world_by_item = {}
    for entry in entries:
        world_score = _world_score(entry["item"])
        if world_score is None:
            continue
        user_score = float(entry["raw_rating"])
        world_by_item[entry["item_id"]] = world_score
        world_user.append(user_score / 10.0)
        world_scores.append(world_score / 10.0)
        world_residuals.append(user_score - world_score)

    alignment = weighted_pearson_correlation(
        world_user, world_scores, [1.0] * len(world_scores)
    ) if world_scores else 0.0

    family_values = {
        family: defaultdict(lambda: {"scores": [], "world_residuals": []})
        for family in config
    }
    coverage = Counter()

    for entry in entries:
        item = entry["item"]
        item_id = entry["item_id"]
        score = float(entry["raw_rating"])
        features = _features_for_item(
            item, item_id, directors_map, lead_cast_map, studios_map, media_kind
        )
        for family, labels in features.items():
            if family not in family_values:
                continue
            if labels:
                coverage[family] += 1
            for feature_label in labels:
                bucket = family_values[family][feature_label]
                bucket["scores"].append(score)
                if item_id in world_by_item:
                    bucket["world_residuals"].append(score - world_by_item[item_id])

    if scores:
        baseline_payload = {
            "mean": round(baseline, 4),
            "median": round(median(scores), 4),
            "stddev": round(pstdev(scores), 4) if len(scores) > 1 else 0.0,
            "min": min(scores),
            "max": max(scores),
            "p10": round(_percentile(scores, 0.10), 4),
            "p25": round(_percentile(scores, 0.25), 4),
            "p75": round(_percentile(scores, 0.75), 4),
            "p90": round(_percentile(scores, 0.90), 4),
            "p95": round(_percentile(scores, 0.95), 4),
            "distribution": _rating_distribution(scores),
            "high_rating_7_plus_pct": round(sum(s >= 7.0 for s in scores) / len(scores) * 100.0, 3),
            "elite_rating_8_plus_pct": round(sum(s >= 8.0 for s in scores) / len(scores) * 100.0, 3),
        }
    else:
        baseline_payload = _empty_baseline()

    families = {
        family: _summarise_family(
            family, config, family_values[family], baseline, coverage[family], len(entries)
        )
        for family in config
    }

    profile = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "tv_extension_version": TV_EXTENSION_VERSION,
        "combined_extension_version": COMBINED_EXTENSION_VERSION,
        "media_kind": media_kind,
        "media_label": "TV" if is_tv else "Movies",
        "source_rows": source_rows_count,
        "unique_rated_titles": len(entries),
        "deduplicated_rows": deduplicated_rows,
        "baseline": baseline_payload,
        "world_alignment": {
            "sample_size": len(world_scores),
            "pearson": round(alignment, 6),
            "strength": _correlation_label(alignment),
            "mean_residual_points": round(mean(world_residuals), 4) if world_residuals else None,
            "median_residual_points": round(median(world_residuals), 4) if world_residuals else None,
        },
        "families": families,
        "family_cards": [families[key] for key in family_order],
    }

    if is_tv:
        episode_scores = tv_data["episode_scores"]
        episode_distribution = _rating_distribution(episode_scores)
        show_rankings = [
            {
                "title": entry["title"],
                "rating": round(entry["raw_rating"], 4),
                "adjusted_rating": round(entry["adjusted_rating"], 4),
                "rated_episodes": entry["rated_episodes"],
                "total_episodes": entry["total_episodes"],
                "rated_seasons": entry["rated_seasons"],
                "coverage_pct": round(entry["coverage_pct"], 1),
                "confidence_pct": round(entry["confidence"] * 100.0, 1),
            }
            for entry in entries
        ]
        profile["tv_summary"] = {
            "rated_episodes": tv_data["rated_episodes"],
            "known_completed_episodes": tv_data["unique_episode_rows"],
            "episode_mean": round(mean(episode_scores), 4) if episode_scores else None,
            "episode_median": round(median(episode_scores), 4) if episode_scores else None,
            "episode_stddev": round(pstdev(episode_scores), 4) if len(episode_scores) > 1 else 0.0,
            "episode_7_plus_pct": round(sum(s >= 7.0 for s in episode_scores) / len(episode_scores) * 100.0, 3) if episode_scores else 0.0,
            "episode_8_plus_pct": round(sum(s >= 8.0 for s in episode_scores) / len(episode_scores) * 100.0, 3) if episode_scores else 0.0,
            "distribution": episode_distribution,
            "confidence_prior_episodes": TV_CONFIDENCE_PRIOR_EPISODES,
            "top_shows": sorted(show_rankings, key=lambda r: (r["adjusted_rating"], r["rated_episodes"], r["rating"]), reverse=True)[:10],
            "bottom_shows": sorted(show_rankings, key=lambda r: (r["adjusted_rating"], -r["rated_episodes"], r["rating"]))[:10],
        }

    return profile


def get_rating_intelligence_profile(user, force=False, media_kind="movies"):
    media_kind = _normalise_media_kind(media_kind)
    key = _cache_key(user.id, media_kind)
    if not force:
        cached = cache.get(key)
        if (
            isinstance(cached, dict)
            and cached.get("engine_version") == ENGINE_VERSION
            and cached.get("tv_extension_version") == TV_EXTENSION_VERSION
            and cached.get("combined_extension_version") == COMBINED_EXTENSION_VERSION
            and cached.get("media_kind") == media_kind
        ):
            return cached
    profile = compute_rating_intelligence_profile(user, media_kind=media_kind)
    cache.set(key, profile, CACHE_SECONDS)
    return profile


@login_required
def rating_intelligence(request):
    media_kind = _normalise_media_kind(request.GET.get("media"))
    profile = get_rating_intelligence_profile(request.user, media_kind=media_kind)
    return render(request, "app/rating_intelligence.html", {
        "profile": profile,
        "media_kind": media_kind,
        "baseline": profile["baseline"],
        "world": profile["world_alignment"],
        "family_cards": profile["family_cards"],
        "tv_summary": profile.get("tv_summary"),
        "combined_summary": profile.get("combined_summary"),
        "cross_media_cards": profile.get("cross_media_cards", []),
        "movie_profile": profile.get("movies"),
        "tv_profile": profile.get("tv"),
    })


@login_required
@require_POST
def refresh_rating_intelligence(request):
    media_kind = _normalise_media_kind(request.POST.get("media"))
    get_rating_intelligence_profile(request.user, force=True, media_kind=media_kind)
    return redirect(f"{reverse('rating_intelligence')}?media={media_kind}")
