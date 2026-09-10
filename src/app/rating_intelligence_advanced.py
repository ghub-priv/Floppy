from __future__ import annotations

import math
from collections import Counter, defaultdict
from statistics import mean

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
from app.discover.scoring import blended_world_quality
from app.rating_intelligence import (
    COMBINED_EXTENSION_VERSION,
    TV_EXTENSION_VERSION,
    _credit_maps,
    _load_movies,
    _load_tv_shows,
    _normalise_media_kind,
    _studio_map,
)

ADVANCED_ENGINE_VERSION = "2.3.0"
ADVANCED_SCHEMA_VERSION = 2
CACHE_SECONDS = 900
RIDGE_LAMBDA = 18.0
RIDGE_MAX_ITER = 250
RIDGE_TOLERANCE = 1e-5

MOVIE_FAMILY_CONFIG = {
    "genres": {"label": "Genres", "min_samples": 20, "description": "Shows which genres you tend to rate higher or lower than expected once public ratings and the other known details about a film are taken into account."},
    "decades": {"label": "Decades", "min_samples": 20, "description": "Shows whether films from particular decades tend to work better or worse for you than expected."},
    "directors": {"label": "Directors", "min_samples": 4, "description": "Shows which directors are linked with you rating films higher or lower than expected. Only actual director credits are counted."},
    "lead_cast": {"label": "Lead cast", "min_samples": 5, "description": "Shows which frequently seen lead actors are linked with you rating films higher or lower than expected."},
    "studios": {"label": "Studios", "min_samples": 10, "description": "Shows whether films from particular production companies tend to score better or worse with you than expected."},
    "collections": {"label": "Collections / franchises", "min_samples": 4, "description": "Shows whether films in a franchise or collection tend to work better or worse for you than expected."},
}
MOVIE_DISPLAY_FAMILY_ORDER = ("genres", "decades", "directors", "lead_cast", "studios", "collections")

TV_FAMILY_CONFIG = {
    "genres": {"label": "Genres", "min_samples": 10, "description": "Shows which TV genres you tend to rate higher or lower than expected. Each show counts once, regardless of how many episodes it has."},
    "decades": {"label": "Decades", "min_samples": 10, "description": "Shows whether TV from particular decades tends to work better or worse for you than expected."},
    "lead_cast": {"label": "Lead cast", "min_samples": 3, "description": "Shows which frequently seen lead actors are linked with you rating TV shows higher or lower than expected. Shows with only a few rated episodes are treated cautiously."},
    "studios": {"label": "Studios", "min_samples": 5, "description": "Shows whether TV programmes from particular production companies tend to score better or worse with you than expected."},
}
TV_DISPLAY_FAMILY_ORDER = ("genres", "decades", "lead_cast", "studios")

# Backwards-compatible constants for imports or diagnostics expecting v2 names.
FAMILY_CONFIG = MOVIE_FAMILY_CONFIG
DISPLAY_FAMILY_ORDER = MOVIE_DISPLAY_FAMILY_ORDER

VALIDATION_SNAPSHOT = {
    "rated_titles": 5230,
    "folds": 5,
    "source": "frozen-movie-v2.0.0",
    "public": {"label": "Public rating only", "rmse": 1.498672, "mae": 1.087389, "pearson": 0.278929, "top_decile_7plus_pct": 56.597},
    "personal": {"label": "Personal features only", "rmse": 1.459891, "mae": 1.062819, "pearson": 0.353790, "top_decile_7plus_pct": 45.315},
    "hybrid": {"label": "Advanced hybrid", "rmse": 1.415312, "mae": 1.029680, "pearson": 0.421729, "top_decile_7plus_pct": 61.185},
    "interactions": {"rmse": 1.414384, "gain_vs_hybrid": 0.000928, "decision": "Deferred: negligible predictive gain for extra complexity."},
}


def _cache_key(user_id, media_kind="movies"):
    media_kind = _normalise_media_kind(media_kind)
    return f"rating-intelligence:advanced:v2.3.0:{media_kind}:{user_id}"


def invalidate_advanced_rating_intelligence_cache(user_id):
    cache.delete(_cache_key(user_id, "movies"))
    cache.delete(_cache_key(user_id, "tv"))
    cache.delete(_cache_key(user_id, "combined"))


def _clamp(value, lo=1.0, hi=10.0):
    return min(hi, max(lo, float(value)))


def _public_calibration(samples):
    pairs = [(s["world_score"], s["rating"]) for s in samples if s["world_score"] is not None]
    fallback = mean(s["rating"] for s in samples) if samples else 0.0
    if len(pairs) < 2:
        return {"slope": 0.0, "intercept": fallback, "fallback": fallback, "sample_size": len(pairs)}
    xs = [x for x, _ in pairs]
    ys = [y for _, y in pairs]
    mx, my = mean(xs), mean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in pairs) / denom if denom else 0.0
    return {"slope": slope, "intercept": my - slope * mx, "fallback": fallback, "sample_size": len(pairs)}


def _public_prediction(calibration, sample):
    if sample["world_score"] is None:
        return calibration["fallback"]
    return calibration["intercept"] + calibration["slope"] * sample["world_score"]


def _world_score(item):
    world = blended_world_quality(
        provider_rating=getattr(item, "provider_rating", None),
        provider_votes=getattr(item, "provider_rating_count", None),
        trakt_rating=getattr(item, "trakt_rating", None),
        trakt_votes=getattr(item, "trakt_rating_count", None),
    )
    return None if world.get("world_source_blend") == "neutral" else float(world["world_quality"]) * 10.0


def _build_samples(user, media_kind="movies"):
    media_kind = _normalise_media_kind(media_kind)
    if media_kind == "tv":
        tv_data = _load_tv_shows(user)
        entries = tv_data["shows"]
        item_ids = [entry["item_id"] for entry in entries]
        _, lead_cast_map = _credit_maps(item_ids)
        studios_map = _studio_map(item_ids)
        samples = []
        for entry in entries:
            item = entry["item"]
            item_id = entry["item_id"]
            families = {
                "genres": normalize_features(item.genres or [], normalize_person_name),
                "decades": normalize_features([release_decade_label(item.release_datetime)], normalize_person_name),
                "lead_cast": lead_cast_map.get(item_id, []),
                "studios": studios_map.get(item_id) or normalize_features(item.studios or [], normalize_studio),
            }
            features = {f"{family}:{label}" for family, labels in families.items() for label in labels}
            samples.append({
                "item_id": item_id,
                "title": entry["title"],
                # Confidence-adjusted target prevents a one-episode 10/10 from
                # carrying the same certainty as a fully rated limited series.
                "rating": float(entry["adjusted_rating"]),
                "observed_rating": float(entry["raw_rating"]),
                "world_score": _world_score(item),
                "features": features,
                "rated_episodes": entry["rated_episodes"],
                "total_episodes": entry["total_episodes"],
                "coverage_pct": entry["coverage_pct"],
                "confidence": entry["confidence"],
            })
        return tv_data["source_rated_rows"], tv_data["deduplicated_rated_rows"], samples, tv_data

    source_rows, movies = _load_movies(user)
    item_ids = [movie.item_id for movie in movies]
    directors_map, lead_cast_map = _credit_maps(item_ids)
    studios_map = _studio_map(item_ids)
    samples = []
    for movie in movies:
        item = movie.item
        item_id = movie.item_id
        families = {
            "genres": normalize_features(item.genres or [], normalize_person_name),
            "decades": normalize_features([release_decade_label(item.release_datetime)], normalize_person_name),
            "directors": directors_map.get(item_id, []),
            "lead_cast": lead_cast_map.get(item_id, []),
            "studios": studios_map.get(item_id) or normalize_features(item.studios or [], normalize_studio),
            "collections": normalize_features([item.provider_collection_name or item.provider_collection_id], normalize_collection),
        }
        features = {f"{family}:{label}" for family, labels in families.items() for label in labels}
        rating = float(movie.score)
        samples.append({
            "item_id": item_id,
            "title": item.title,
            "rating": rating,
            "observed_rating": rating,
            "world_score": _world_score(item),
            "features": features,
        })
    return len(source_rows), len(source_rows) - len(samples), samples, None


def _allowed_features(samples, family_config):
    support = Counter()
    for sample in samples:
        for feature in sample["features"]:
            support[feature] += 1
    allowed = set()
    for feature, count in support.items():
        family, _ = feature.split(":", 1)
        config = family_config.get(family)
        if config and count >= config["min_samples"]:
            allowed.add(feature)
    return support, allowed


def _fit_sparse_ridge(samples, targets, family_config):
    support, allowed = _allowed_features(samples, family_config)
    columns = defaultdict(list)
    for i, sample in enumerate(samples):
        for feature in sample["features"]:
            if feature in allowed:
                columns[feature].append(i)
    ordered = sorted(columns, key=lambda feature: (-len(columns[feature]), feature))
    intercept = mean(targets) if targets else 0.0
    residual = [target - intercept for target in targets]
    weights = {feature: 0.0 for feature in ordered}
    converged = False
    final_max_delta = 0.0
    iterations = 0

    for iteration in range(1, RIDGE_MAX_ITER + 1):
        iterations = iteration
        max_delta = 0.0
        intercept_delta = sum(residual) / len(residual) if residual else 0.0
        if intercept_delta:
            intercept += intercept_delta
            residual = [value - intercept_delta for value in residual]
            max_delta = abs(intercept_delta)
        for feature in ordered:
            indexes = columns[feature]
            old = weights[feature]
            numerator = sum(residual[i] + old for i in indexes)
            new = numerator / (len(indexes) + RIDGE_LAMBDA)
            delta = new - old
            if delta:
                weights[feature] = new
                for i in indexes:
                    residual[i] -= delta
                max_delta = max(max_delta, abs(delta))
        final_max_delta = max_delta
        if max_delta < RIDGE_TOLERANCE:
            converged = True
            break

    return {
        "intercept": intercept,
        "weights": weights,
        "support": support,
        "allowed": allowed,
        "iterations": iterations,
        "converged": converged,
        "final_max_delta": final_max_delta,
    }


def _family_cards(samples, model, calibration, family_config, family_order):
    raw_ratings = defaultdict(list)
    raw_residuals = defaultdict(list)
    for sample in samples:
        residual = sample["rating"] - _public_prediction(calibration, sample)
        for feature in sample["features"]:
            if feature in model["allowed"]:
                raw_ratings[feature].append(sample.get("observed_rating", sample["rating"]))
                raw_residuals[feature].append(residual)

    grouped = defaultdict(list)
    for feature, coefficient in model["weights"].items():
        family, label = feature.split(":", 1)
        ratings = raw_ratings.get(feature, [])
        residuals = raw_residuals.get(feature, [])
        grouped[family].append({
            "label": label,
            "samples": len(ratings),
            "mean_rating": round(mean(ratings), 4) if ratings else None,
            "raw_public_residual": round(mean(residuals), 4) if residuals else None,
            "adjustment": round(float(coefficient), 4),
        })

    cards = []
    for family in family_order:
        config = family_config[family]
        rows = grouped.get(family, [])
        positive = sorted((r for r in rows if r["adjustment"] > 0), key=lambda r: (r["adjustment"], r["samples"]), reverse=True)[:20]
        negative = sorted((r for r in rows if r["adjustment"] < 0), key=lambda r: (r["adjustment"], -r["samples"]))[:20]
        cards.append({
            "key": family,
            "label": config["label"],
            "description": config["description"],
            "min_samples": config["min_samples"],
            "feature_count": len(rows),
            "modelled_rows": sorted(rows, key=lambda r: (r["label"], r["samples"])),
            "positive": positive,
            "negative": negative,
        })
    return cards


def _predict_personal(model, sample):
    return model["intercept"] + sum(model["weights"].get(feature, 0.0) for feature in sample["features"])


def _pearson(actual, predicted):
    if len(actual) < 2:
        return 0.0
    ma, mp = mean(actual), mean(predicted)
    numerator = sum((a - ma) * (p - mp) for a, p in zip(actual, predicted))
    da = sum((a - ma) ** 2 for a in actual)
    dp = sum((p - mp) ** 2 for p in predicted)
    denominator = math.sqrt(da * dp)
    return numerator / denominator if denominator else 0.0


def _metrics(actual, predicted, label):
    if not actual:
        return {"label": label, "rmse": 0.0, "mae": 0.0, "pearson": 0.0, "top_decile_7plus_pct": 0.0}
    errors = [a - p for a, p in zip(actual, predicted)]
    top_n = max(1, math.ceil(len(actual) * 0.10))
    top_indexes = sorted(range(len(predicted)), key=lambda i: predicted[i], reverse=True)[:top_n]
    return {
        "label": label,
        "rmse": round(math.sqrt(mean(error * error for error in errors)), 6),
        "mae": round(mean(abs(error) for error in errors), 6),
        "pearson": round(_pearson(actual, predicted), 6),
        "top_decile_7plus_pct": round(sum(actual[i] >= 7.0 for i in top_indexes) / len(top_indexes) * 100.0, 3),
    }


def _cross_validate(samples, family_config, folds=5):
    """Deterministic live validation for the TV model.

    The movie model keeps its frozen v2.0.0 validation snapshot. TV has a new
    target construction, so reporting movie validation numbers would be
    misleading. This computes out-of-sample metrics from the current TV data.
    """
    if len(samples) < 20:
        return None
    folds = min(folds, max(2, len(samples) // 4))
    ordered = sorted(samples, key=lambda sample: (str(sample["item_id"]), sample.get("title", "")))
    actual, public_pred, personal_pred, hybrid_pred = [], [], [], []

    for fold in range(folds):
        train = [sample for i, sample in enumerate(ordered) if i % folds != fold]
        test = [sample for i, sample in enumerate(ordered) if i % folds == fold]
        if not train or not test:
            continue

        calibration = _public_calibration(train)
        personal_model = _fit_sparse_ridge(train, [sample["rating"] for sample in train], family_config)
        residual_targets = [sample["rating"] - _public_prediction(calibration, sample) for sample in train]
        hybrid_model = _fit_sparse_ridge(train, residual_targets, family_config)

        for sample in test:
            public_raw = _public_prediction(calibration, sample)
            public_value = _clamp(public_raw)
            personal_value = _clamp(_predict_personal(personal_model, sample))
            hybrid_value = _clamp(public_raw + _predict_personal(hybrid_model, sample))
            actual.append(sample["rating"])
            public_pred.append(public_value)
            personal_pred.append(personal_value)
            hybrid_pred.append(hybrid_value)

    if not actual:
        return None
    return {
        "rated_titles": len(actual),
        "folds": folds,
        "source": "live-tv-v2.1.0",
        "public": _metrics(actual, public_pred, "Public rating only"),
        "personal": _metrics(actual, personal_pred, "Personal features only"),
        "hybrid": _metrics(actual, hybrid_pred, "Advanced hybrid"),
        "interactions": None,
    }


def _advanced_row_map(card):
    source = card.get("modelled_rows")
    if source is None:
        source = card.get("positive", []) + card.get("negative", [])
    return {row["label"]: row for row in source}


def _combine_advanced_family_cards(movie_profile, tv_profile):
    shared_order = ("genres", "decades", "lead_cast", "studios")
    movie_cards = {card["key"]: card for card in movie_profile.get("family_cards", [])}
    tv_cards = {card["key"]: card for card in tv_profile.get("family_cards", [])}
    cards = []
    for family in shared_order:
        movie_card = movie_cards.get(family, {})
        tv_card = tv_cards.get(family, {})
        movie_rows = _advanced_row_map(movie_card)
        tv_rows = _advanced_row_map(tv_card)
        rows = []
        for label in sorted(set(movie_rows) & set(tv_rows)):
            movie_row = movie_rows[label]
            tv_row = tv_rows[label]
            movie_adjustment = float(movie_row["adjustment"])
            tv_adjustment = float(tv_row["adjustment"])
            combined_adjustment = (movie_adjustment + tv_adjustment) / 2.0
            same_direction = (movie_adjustment > 0 and tv_adjustment > 0) or (movie_adjustment < 0 and tv_adjustment < 0)
            rows.append({
                "label": label,
                "movie_adjustment": round(movie_adjustment, 4),
                "tv_adjustment": round(tv_adjustment, 4),
                "combined_adjustment": round(combined_adjustment, 4),
                "divergence": round(tv_adjustment - movie_adjustment, 4),
                "absolute_divergence": round(abs(tv_adjustment - movie_adjustment), 4),
                "movie_samples": movie_row["samples"],
                "tv_samples": tv_row["samples"],
                "same_direction": same_direction,
                "opposite_direction": movie_adjustment * tv_adjustment < 0,
            })
        cards.append({
            "key": family,
            "label": movie_card.get("label") or tv_card.get("label") or family.title(),
            "shared_values": len(rows),
            "positive": sorted(
                (r for r in rows if r["movie_adjustment"] > 0 and r["tv_adjustment"] > 0),
                key=lambda r: (r["combined_adjustment"], r["movie_samples"] + r["tv_samples"]),
                reverse=True,
            )[:10],
            "negative": sorted(
                (r for r in rows if r["movie_adjustment"] < 0 and r["tv_adjustment"] < 0),
                key=lambda r: (r["combined_adjustment"], -(r["movie_samples"] + r["tv_samples"])),
            )[:10],
            "divergent": sorted(
                (r for r in rows if r["opposite_direction"]),
                key=lambda r: (r["absolute_divergence"], r["movie_samples"] + r["tv_samples"]),
                reverse=True,
            )[:10],
        })
    return cards


def _mae_gain(validation):
    if not validation or not validation.get("public") or not validation.get("hybrid"):
        return None
    public = float(validation["public"]["mae"])
    hybrid = float(validation["hybrid"]["mae"])
    return round(((public - hybrid) / public * 100.0) if public else 0.0, 3)


def _compute_combined_advanced(user):
    movies = compute_advanced_rating_intelligence(user, media_kind="movies")
    tv = compute_advanced_rating_intelligence(user, media_kind="tv")
    cards = _combine_advanced_family_cards(movies, tv)
    shared = sum(card["shared_values"] for card in cards)
    directional = sum(len(card["positive"]) + len(card["negative"]) for card in cards)
    return {
        "schema_version": ADVANCED_SCHEMA_VERSION,
        "engine_version": ADVANCED_ENGINE_VERSION,
        "tv_extension_version": TV_EXTENSION_VERSION,
        "combined_extension_version": COMBINED_EXTENSION_VERSION,
        "media_kind": "combined",
        "media_label": "Combined",
        "source_rows": movies.get("source_rows", 0) + tv.get("source_rows", 0),
        "unique_rated_titles": movies.get("unique_rated_titles", 0) + tv.get("unique_rated_titles", 0),
        "deduplicated_rows": movies.get("deduplicated_rows", 0) + tv.get("deduplicated_rows", 0),
        "model": None,
        "validation": None,
        "family_cards": [],
        "movies": movies,
        "tv": tv,
        "cross_media_cards": cards,
        "combined_summary": {
            "movie_titles": movies.get("unique_rated_titles", 0),
            "tv_titles": tv.get("unique_rated_titles", 0),
            "rated_episodes": tv.get("tv_summary", {}).get("rated_episodes", 0),
            "movie_features": movies.get("model", {}).get("feature_count", 0) if movies.get("model") else 0,
            "tv_features": tv.get("model", {}).get("feature_count", 0) if tv.get("model") else 0,
            "movie_cv_mae_gain_pct": _mae_gain(movies.get("validation")),
            "tv_cv_mae_gain_pct": _mae_gain(tv.get("validation")),
            "shared_modelled_signals": shared,
            "consistent_direction_signals": directional,
        },
    }


def compute_advanced_rating_intelligence(user, media_kind="movies"):
    media_kind = _normalise_media_kind(media_kind)
    if media_kind == "combined":
        return _compute_combined_advanced(user)
    is_tv = media_kind == "tv"
    family_config = TV_FAMILY_CONFIG if is_tv else MOVIE_FAMILY_CONFIG
    family_order = TV_DISPLAY_FAMILY_ORDER if is_tv else MOVIE_DISPLAY_FAMILY_ORDER
    source_rows, deduplicated_rows, samples, tv_data = _build_samples(user, media_kind)

    validation = _cross_validate(samples, family_config) if is_tv else VALIDATION_SNAPSHOT
    if not samples:
        return {
            "schema_version": ADVANCED_SCHEMA_VERSION,
            "engine_version": ADVANCED_ENGINE_VERSION,
            "tv_extension_version": TV_EXTENSION_VERSION,
            "combined_extension_version": COMBINED_EXTENSION_VERSION,
            "media_kind": media_kind,
            "media_label": "TV" if is_tv else "Movies",
            "source_rows": 0,
            "unique_rated_titles": 0,
            "deduplicated_rows": 0,
            "model": None,
            "family_cards": [],
            "validation": validation,
        }

    calibration = _public_calibration(samples)
    residual_targets = [sample["rating"] - _public_prediction(calibration, sample) for sample in samples]
    model = _fit_sparse_ridge(samples, residual_targets, family_config)
    family_cards = _family_cards(samples, model, calibration, family_config, family_order)

    public_predictions = [_clamp(_public_prediction(calibration, sample)) for sample in samples]
    hybrid_predictions = []
    for sample in samples:
        personal_adjustment = _predict_personal(model, sample)
        hybrid_predictions.append(_clamp(_public_prediction(calibration, sample) + personal_adjustment))

    public_mae = mean(abs(sample["rating"] - prediction) for sample, prediction in zip(samples, public_predictions))
    hybrid_mae = mean(abs(sample["rating"] - prediction) for sample, prediction in zip(samples, hybrid_predictions))

    profile = {
        "schema_version": ADVANCED_SCHEMA_VERSION,
        "engine_version": ADVANCED_ENGINE_VERSION,
        "tv_extension_version": TV_EXTENSION_VERSION,
        "combined_extension_version": COMBINED_EXTENSION_VERSION,
        "media_kind": media_kind,
        "media_label": "TV" if is_tv else "Movies",
        "source_rows": source_rows,
        "unique_rated_titles": len(samples),
        "deduplicated_rows": deduplicated_rows,
        "validation": validation,
        "public_calibration": {
            "slope": round(calibration["slope"], 6),
            "intercept": round(calibration["intercept"], 6),
            "sample_size": calibration["sample_size"],
        },
        "model": {
            "architecture": "Calibrated public quality + regularised personal residual main effects",
            "ridge_lambda": RIDGE_LAMBDA,
            "feature_count": len(model["weights"]),
            "iterations": model["iterations"],
            "converged": model["converged"],
            "final_max_delta": round(model["final_max_delta"], 9),
            "residual_intercept": round(model["intercept"], 6),
            "current_mean_abs_public_error": round(public_mae, 4),
            "current_mean_abs_hybrid_error": round(hybrid_mae, 4),
            "target_label": "confidence-adjusted show rating" if is_tv else "movie rating",
        },
        "family_cards": family_cards,
    }

    if is_tv and tv_data is not None:
        profile["tv_summary"] = {
            "rated_episodes": tv_data["rated_episodes"],
            "known_completed_episodes": tv_data["unique_episode_rows"],
            "confidence_prior_episodes": 5,
        }
    return profile


def get_advanced_rating_intelligence(user, force=False, media_kind="movies"):
    media_kind = _normalise_media_kind(media_kind)
    key = _cache_key(user.id, media_kind)
    if not force:
        cached = cache.get(key)
        if (
            isinstance(cached, dict)
            and cached.get("engine_version") == ADVANCED_ENGINE_VERSION
            and cached.get("media_kind") == media_kind
        ):
            return cached
    profile = compute_advanced_rating_intelligence(user, media_kind=media_kind)
    cache.set(key, profile, CACHE_SECONDS)
    return profile


@login_required
def advanced_rating_intelligence(request):
    media_kind = _normalise_media_kind(request.GET.get("media"))
    profile = get_advanced_rating_intelligence(request.user, media_kind=media_kind)
    validation = profile.get("validation")
    validation_rows = []
    if validation:
        for key in ("public", "personal", "hybrid"):
            row = dict(validation[key])
            row["key"] = key
            validation_rows.append(row)
    movie_validation_rows = []
    tv_validation_rows = []
    if media_kind == "combined":
        for target, rows in ((profile.get("movies", {}).get("validation"), movie_validation_rows), (profile.get("tv", {}).get("validation"), tv_validation_rows)):
            if target:
                for key in ("public", "personal", "hybrid"):
                    row = dict(target[key])
                    row["key"] = key
                    rows.append(row)
    return render(
        request,
        "app/rating_intelligence_advanced.html",
        {
            "profile": profile,
            "media_kind": media_kind,
            "model": profile.get("model"),
            "validation": validation,
            "validation_rows": validation_rows,
            "family_cards": profile.get("family_cards", []),
            "tv_summary": profile.get("tv_summary"),
            "combined_summary": profile.get("combined_summary"),
            "cross_media_cards": profile.get("cross_media_cards", []),
            "movie_profile": profile.get("movies"),
            "tv_profile": profile.get("tv"),
            "movie_validation_rows": movie_validation_rows,
            "tv_validation_rows": tv_validation_rows,
        },
    )


@login_required
@require_POST
def refresh_advanced_rating_intelligence(request):
    media_kind = _normalise_media_kind(request.POST.get("media"))
    get_advanced_rating_intelligence(request.user, force=True, media_kind=media_kind)
    return redirect(f"{reverse('rating_intelligence_advanced')}?media={media_kind}")
