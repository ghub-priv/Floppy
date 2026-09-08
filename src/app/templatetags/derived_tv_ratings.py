"""Read-only TV and season ratings derived from episode ratings."""

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal

from django import template
from django.utils.translation import gettext as _

from app.models import Episode, MediaTypes


register = template.Library()

DERIVED_TV_RATINGS_VERSION = "1.1.0"


def _media_value(media, key, default=None):
    """Read a detail-media value from either a metadata mapping or an object."""
    if isinstance(media, Mapping):
        value = media.get(key, default)
    else:
        value = getattr(media, key, default)
    return default if value is None else value


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


def _episode_rows(user, media_type, media):
    """Return completed episode rows in stable episode/recency order."""
    media_id = str(_media_value(media, "media_id", "") or "").strip()
    source = str(_media_value(media, "source", "") or "").strip()

    if not media_id or not source:
        return Episode.objects.none().values("item_id", "score")

    qs = Episode.objects.filter(
        related_season__user=user,
        related_season__related_tv__user=user,
        related_season__related_tv__item__media_id=media_id,
        related_season__related_tv__item__source=source,
        status="Completed",
    )

    if media_type == MediaTypes.TV.value:
        # Specials remain independently rateable, but they do not influence
        # the main show's derived score.
        qs = qs.filter(item__season_number__gt=0)
    elif media_type == MediaTypes.SEASON.value:
        season_number = _media_value(media, "season_number", None)
        try:
            season_number = int(season_number)
        except (TypeError, ValueError):
            return Episode.objects.none().values("item_id", "score")
        qs = qs.filter(item__season_number=season_number)
    else:
        return Episode.objects.none().values("item_id", "score")

    # Episode is the atomic rating unit. Floppy can contain multiple Episode
    # activity rows for rewatches, so order each episode's rows newest-first.
    # The aggregation below takes only the first row for each unique item_id.
    return (
        qs.order_by(
            "item_id",
            "-end_date",
            "-created_at",
            "-id",
        )
        .values(
            "item_id",
            "score",
        )
    )


@register.simple_tag
def derived_tv_rating(user, media_type, media):
    """Return a read-only episode-derived rating for a TV show or season."""
    if not getattr(user, "is_authenticated", False):
        return None

    if media_type not in {
        MediaTypes.TV.value,
        MediaTypes.SEASON.value,
    }:
        return None

    total = 0
    rated = 0
    score_sum = Decimal("0")
    seen_items = set()

    for row in _episode_rows(user, media_type, media):
        item_id = row["item_id"]
        if item_id in seen_items:
            continue
        seen_items.add(item_id)
        total += 1

        score = row["score"]
        if score is None:
            continue

        rated += 1
        score_sum += Decimal(score)

    if total == 0:
        return None

    raw_average = score_sum / rated if rated else None

    is_show = media_type == MediaTypes.TV.value
    season_number = _media_value(media, "season_number", None)

    if is_show:
        label = _("TV Show")
        title = _(
            "Your read-only TV show rating derived from rated episodes. "
            "Each episode counts once; Specials are excluded."
        )
    else:
        label = _("Specials") if season_number == 0 else _("Season %(number)s") % {
            "number": season_number,
        }
        title = _(
            "Your read-only season rating derived from rated episodes. "
            "Each episode counts once."
        )

    return {
        "score": _display_score(user, raw_average),
        "raw_score": float(raw_average) if raw_average is not None else None,
        "rated": rated,
        "total": total,
        "coverage_percent": round((rated / total) * 100, 1) if total else 0.0,
        "label": label,
        "title": title,
        "specials_excluded": is_show,
        "version": DERIVED_TV_RATINGS_VERSION,
    }
