"""Post-Watch Workflow: recent unrated movie and episode review inbox."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_POST
from requests import RequestException

from app import history_cache, providers
from app.models import Episode, Item, MediaTypes, MoviePlay, PostWatchDismissal, Sources
from app.smart_watched_dates import suggestions_for_media

logger = logging.getLogger(__name__)

VERSION = "1.1.0"
POST_WATCH_LOOKBACK_DAYS = 7
POST_WATCH_MAX_CARDS = 200
POST_WATCH_KEY_RE = re.compile(r"^(movie|episode):(\d+)$")


def _watch_key(kind: str, instance_id: int) -> str:
    return f"{kind}:{int(instance_id)}"


def _watch_datetime(end_date, created_at):
    return end_date or created_at


def _history_day_keys(*values):
    return [
        key
        for key in (history_cache.history_day_key(value) for value in values)
        if key
    ]


def _invalidate_history_days(user_id: int, *values, reason: str):
    keys = list(dict.fromkeys(_history_day_keys(*values)))
    if keys:
        history_cache.invalidate_history_days(
            user_id,
            day_keys=keys,
            logging_styles=("sessions", "repeats"),
            reason=reason,
        )


def _parse_watch_key(watch_key: str) -> tuple[str, int] | None:
    match = POST_WATCH_KEY_RE.fullmatch(str(watch_key or "").strip())
    if not match:
        return None
    return match.group(1), int(match.group(2))


def _lookup_watch(user, watch_key: str):
    parsed = _parse_watch_key(watch_key)
    if parsed is None:
        return None, None
    kind, instance_id = parsed
    if kind == "movie":
        watch = (
            MoviePlay.objects.select_related("movie", "movie__item")
            .filter(pk=instance_id, movie__user=user)
            .first()
        )
    else:
        watch = (
            Episode.objects.select_related(
                "item",
                "related_season",
                "related_season__item",
                "related_season__related_tv",
                "related_season__related_tv__item",
            )
            .filter(pk=instance_id, related_season__user=user)
            .first()
        )
    return kind, watch


def _movie_details_url(play: MoviePlay) -> str:
    item = play.movie.item
    return reverse(
        "media_details",
        kwargs={
            "source": item.source,
            "media_type": MediaTypes.MOVIE.value,
            "media_id": item.media_id,
            "title": item.title,
        },
    )


def _episode_details_url(episode: Episode, season_number: int, episode_number: int) -> str:
    tv = episode.related_season.related_tv
    parent_type = (
        MediaTypes.ANIME.value
        if tv.item.library_media_type == MediaTypes.ANIME.value
        else MediaTypes.TV.value
    )
    route_name = "anime_episode_details" if parent_type == MediaTypes.ANIME.value else "episode_details"
    return reverse(
        route_name,
        kwargs={
            "source": tv.item.source,
            "media_id": tv.item.media_id,
            "title": tv.item.title,
            "season_number": int(season_number),
            "episode_number": int(episode_number),
        },
    )


def _metadata_episode_numbers(media_id, source, season_number):
    try:
        metadata = providers.services.get_media_metadata(
            MediaTypes.SEASON.value,
            media_id,
            source,
            [season_number],
        )
    except (providers.services.ProviderAPIError, RequestException, KeyError, TypeError, ValueError):
        return []
    numbers = []
    for row in (metadata or {}).get("episodes") or []:
        try:
            number = int(row.get("episode_number"))
        except (AttributeError, TypeError, ValueError):
            continue
        if number > 0:
            numbers.append(number)
    return sorted(set(numbers))


def _episode_next_url(episode: Episode) -> str:
    """Return the next chronological episode detail URL when it can be resolved."""
    season = episode.related_season
    tv = season.related_tv
    item = episode.item
    season_number = int(item.season_number or season.item.season_number or 0)
    episode_number = int(item.episode_number or 0)
    if season_number < 0 or episode_number < 1:
        return ""

    # Prefer already-known local episode coordinates.
    next_item = (
        Item.objects.filter(
            media_id=item.media_id,
            source=item.source,
            media_type=MediaTypes.EPISODE.value,
            season_number=season_number,
            episode_number__gt=episode_number,
        )
        .order_by("episode_number")
        .first()
    )
    if next_item is not None:
        return _episode_details_url(
            episode,
            season_number,
            next_item.episode_number,
        )

    # Metadata lets us distinguish a missing local row from the end of a season.
    episode_numbers = _metadata_episode_numbers(item.media_id, item.source, season_number)
    later_numbers = [number for number in episode_numbers if number > episode_number]
    if later_numbers:
        return _episode_details_url(episode, season_number, later_numbers[0])

    # Bridge into the immediate next real tracked season first.
    next_season = (
        tv.seasons.filter(item__season_number__gt=season_number)
        .exclude(item__season_number=0)
        .select_related("item")
        .order_by("item__season_number")
        .first()
    )
    if next_season is not None:
        next_season_number = int(next_season.item.season_number)
        next_numbers = _metadata_episode_numbers(
            item.media_id,
            item.source,
            next_season_number,
        )
        if next_numbers:
            return _episode_details_url(episode, next_season_number, next_numbers[0])

    # Finally consult show metadata for a season not yet represented locally.
    try:
        tv_metadata = providers.services.get_media_metadata(
            MediaTypes.TV.value,
            tv.item.media_id,
            tv.item.source,
        )
    except (providers.services.ProviderAPIError, RequestException, KeyError, TypeError, ValueError):
        return ""

    candidate_seasons = []
    for row in (tv_metadata or {}).get("related", {}).get("seasons", []) or []:
        try:
            number = int(row.get("season_number"))
        except (AttributeError, TypeError, ValueError):
            continue
        if number > season_number:
            candidate_seasons.append(number)
    for next_season_number in sorted(set(candidate_seasons)):
        next_numbers = _metadata_episode_numbers(
            tv.item.media_id,
            tv.item.source,
            next_season_number,
        )
        if next_numbers:
            return _episode_details_url(episode, next_season_number, next_numbers[0])
    return ""


def _movie_date_suggestions(user, movie) -> list[dict[str, str]]:
    item = getattr(movie, "item", None)
    if item is None or item.source != Sources.TMDB.value:
        return []
    preferred_region = str(
        getattr(user, "preferred_region", "")
        or getattr(user, "country", "")
        or ""
    )
    try:
        values = suggestions_for_media(
            source=item.source,
            media_type=MediaTypes.MOVIE.value,
            media_id=item.media_id,
            preferred_region=preferred_region,
        )
    except (providers.services.ProviderAPIError, RequestException, KeyError, TypeError, ValueError):
        logger.info("Post-Watch date suggestions unavailable for movie %s", item.media_id)
        return []
    labels = {
        "premiere": "Premiere",
        "theatrical": "Theatrical",
        "digital": "Digital",
        "physical": "Physical",
    }
    return [
        {"kind": key, "label": labels[key], "date": value}
        for key, value in values.items()
        if key in labels and value
    ]


def _movie_card(play: MoviePlay) -> dict[str, Any]:
    item = play.movie.item
    watched_at = _watch_datetime(play.end_date, play.created_at)
    return {
        "kind": "movie",
        "watch_key": _watch_key("movie", play.id),
        "watch_id": play.id,
        "title": item.title,
        "subtitle": "Movie",
        "image": item.image,
        "watched_at": watched_at,
        "watched_date": timezone.localdate(watched_at).isoformat() if watched_at else "",
        "details_url": _movie_details_url(play),
        "date_suggestions": _movie_date_suggestions(play.movie.user, play.movie),
        "next_url": "",
    }


def _episode_card(episode: Episode) -> dict[str, Any]:
    item = episode.item
    season = episode.related_season
    tv = season.related_tv
    watched_at = _watch_datetime(episode.end_date, episode.created_at)
    season_number = int(item.season_number or season.item.season_number or 0)
    episode_number = int(item.episode_number or 0)
    return {
        "kind": "episode",
        "watch_key": _watch_key("episode", episode.id),
        "watch_id": episode.id,
        "title": item.title,
        "subtitle": f"{tv.item.title} · S{season_number:02d}E{episode_number:02d}",
        "image": item.image or season.item.image or tv.item.image,
        "watched_at": watched_at,
        "watched_date": timezone.localdate(watched_at).isoformat() if watched_at else "",
        "details_url": _episode_details_url(episode, season_number, episode_number),
        "date_suggestions": [],
        "next_url": _episode_next_url(episode),
    }


def build_post_watch_cards(user):
    """Build the derived seven-day inbox of recently watched unrated items."""
    cutoff = timezone.now() - timezone.timedelta(days=POST_WATCH_LOOKBACK_DAYS)
    recent = Q(end_date__gte=cutoff) | Q(end_date__isnull=True, created_at__gte=cutoff)

    movie_plays = list(
        MoviePlay.objects.filter(recent, movie__user=user, movie__score__isnull=True)
        .select_related("movie", "movie__item", "movie__user")
        .order_by("-end_date", "-created_at")[:POST_WATCH_MAX_CARDS]
    )
    episodes = list(
        Episode.objects.filter(recent, related_season__user=user, score__isnull=True)
        .select_related(
            "item",
            "related_season",
            "related_season__item",
            "related_season__related_tv",
            "related_season__related_tv__item",
        )
        .order_by("-end_date", "-created_at")[:POST_WATCH_MAX_CARDS]
    )

    candidate_keys = [
        *(_watch_key("movie", play.id) for play in movie_plays),
        *(_watch_key("episode", episode.id) for episode in episodes),
    ]
    dismissed = set(
        PostWatchDismissal.objects.filter(
            user=user,
            watch_key__in=candidate_keys,
        ).values_list("watch_key", flat=True)
    )

    cards = [
        *(_movie_card(play) for play in movie_plays if _watch_key("movie", play.id) not in dismissed),
        *(
            _episode_card(episode)
            for episode in episodes
            if _watch_key("episode", episode.id) not in dismissed
        ),
    ]
    cards.sort(
        key=lambda card: card.get("watched_at") or timezone.make_aware(datetime.min),
        reverse=True,
    )
    return cards[:POST_WATCH_MAX_CARDS]


def _parse_display_score(user, raw_value):
    try:
        score = Decimal(str(raw_value or "").strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    return user.scale_score_for_storage(score)


def _datetime_on_selected_date(value: str, original):
    selected = parse_date(str(value or "").strip())
    if selected is None:
        return None
    if original is None:
        original = timezone.now()
    local_original = timezone.localtime(original) if timezone.is_aware(original) else original
    naive = datetime.combine(selected, local_original.time().replace(tzinfo=None))
    return timezone.make_aware(naive, timezone.get_current_timezone())


@login_required
@require_GET
def post_watch(request):
    cards = build_post_watch_cards(request.user)
    return render(
        request,
        "app/post_watch.html",
        {
            "cards": cards,
            "post_watch_version": VERSION,
            "lookback_days": POST_WATCH_LOOKBACK_DAYS,
        },
    )


@login_required
@require_POST
def post_watch_dismiss(request):
    watch_key = str(request.POST.get("watch_key") or "").strip()
    _kind, watch = _lookup_watch(request.user, watch_key)
    if watch is None:
        return HttpResponseBadRequest("Invalid watch.")
    PostWatchDismissal.objects.get_or_create(user=request.user, watch_key=watch_key)
    return redirect("post_watch")


@login_required
@require_POST
def post_watch_rate(request):
    watch_key = str(request.POST.get("watch_key") or "").strip()
    kind, watch = _lookup_watch(request.user, watch_key)
    if watch is None:
        return HttpResponseBadRequest("Invalid watch.")
    score = _parse_display_score(request.user, request.POST.get("score"))
    if score is None:
        return HttpResponseBadRequest("Invalid score.")

    if kind == "movie":
        watch.movie.score = score
        watch.movie.save(update_fields=["score"])
    else:
        episodes = Episode.objects.filter(
            related_season=watch.related_season,
            item__episode_number=watch.item.episode_number,
        )
        old_dates = list(episodes.values_list("end_date", flat=True))
        episodes.update(score=score)
        _invalidate_history_days(
            request.user.id,
            *old_dates,
            reason="post_watch_episode_rating",
        )
    return redirect("post_watch")


@login_required
@require_POST
def post_watch_date(request):
    watch_key = str(request.POST.get("watch_key") or "").strip()
    kind, watch = _lookup_watch(request.user, watch_key)
    if watch is None:
        return HttpResponseBadRequest("Invalid watch.")

    old_date = watch.end_date
    new_date = _datetime_on_selected_date(request.POST.get("watched_date"), old_date or watch.created_at)
    if new_date is None:
        return HttpResponseBadRequest("Invalid watched date.")

    if kind == "movie":
        MoviePlay.objects.filter(pk=watch.pk).update(end_date=new_date)
        latest = (
            MoviePlay.objects.filter(movie=watch.movie, end_date__isnull=False)
            .order_by("-end_date", "-id")
            .values_list("end_date", flat=True)
            .first()
        )
        type(watch.movie).objects.filter(pk=watch.movie_id).update(end_date=latest)
        reason = "post_watch_movie_date"
    else:
        Episode.objects.filter(pk=watch.pk).update(end_date=new_date)
        reason = "post_watch_episode_date"

    _invalidate_history_days(
        request.user.id,
        old_date,
        new_date,
        reason=reason,
    )
    return redirect("post_watch")
