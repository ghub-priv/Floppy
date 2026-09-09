"""Post-Watch Workflow: recent unrated movie and episode review inbox."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_POST
from requests import RequestException

from app import history_cache, providers
from app.models import (
    Episode,
    Item,
    MediaTypes,
    Movie,
    MoviePlay,
    PostWatchDismissal,
    Sources,
)
from app.providers import tmdb
from app.services import metadata_resolution
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
            .filter(
                pk=instance_id,
                related_season__user=user,
                item__isnull=False,
            )
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
    route_name = (
        "anime_episode_details"
        if parent_type == MediaTypes.ANIME.value
        else "episode_details"
    )
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


def _metadata_next_episode_number(
    media_id,
    source,
    season_number,
    current_episode_number,
):
    """Resolve the next valid episode using Floppy's canonical helper."""
    try:
        metadata = providers.services.get_media_metadata(
            MediaTypes.SEASON.value,
            media_id,
            source,
            [season_number],
        )
    except (
        providers.services.ProviderAPIError,
        RequestException,
        KeyError,
        TypeError,
        ValueError,
    ):
        return None
    if not isinstance(metadata, dict):
        return None
    return tmdb.find_next_episode(
        current_episode_number,
        metadata.get("episodes") or [],
    )


def _episode_next_url(episode: Episode) -> str:
    """Return the next chronological episode detail URL when it can be resolved."""
    season = episode.related_season
    tv = season.related_tv
    item = episode.item
    season_number = int(item.season_number or season.item.season_number or 0)
    episode_number = int(item.episode_number or 0)
    if season_number < 0 or episode_number < 1:
        return ""

    # Prefer already-known local episode coordinates in the same library bucket.
    next_item = (
        Item.objects.filter(
            media_id=item.media_id,
            source=item.source,
            media_type=MediaTypes.EPISODE.value,
            library_media_type=item.library_media_type,
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

    next_episode_number = _metadata_next_episode_number(
        item.media_id,
        item.source,
        season_number,
        episode_number,
    )
    if next_episode_number is not None:
        return _episode_details_url(
            episode,
            season_number,
            next_episode_number,
        )

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
        next_episode_number = _metadata_next_episode_number(
            next_season.item.media_id,
            next_season.item.source,
            next_season_number,
            0,
        )
        if next_episode_number is not None:
            return _episode_details_url(
                episode,
                next_season_number,
                next_episode_number,
            )

    # Finally consult show metadata for a season not yet represented locally.
    try:
        tv_metadata = providers.services.get_media_metadata(
            MediaTypes.TV.value,
            tv.item.media_id,
            tv.item.source,
        )
    except (
        providers.services.ProviderAPIError,
        RequestException,
        KeyError,
        TypeError,
        ValueError,
    ):
        return ""
    if not isinstance(tv_metadata, dict):
        return ""

    candidate_seasons = []
    for row in (tv_metadata.get("related") or {}).get("seasons", []) or []:
        try:
            number = int(row.get("season_number"))
        except (AttributeError, TypeError, ValueError):
            continue
        if number > season_number:
            candidate_seasons.append(number)
    for next_season_number in sorted(set(candidate_seasons)):
        next_episode_number = _metadata_next_episode_number(
            tv.item.media_id,
            tv.item.source,
            next_season_number,
            0,
        )
        if next_episode_number is not None:
            return _episode_details_url(
                episode,
                next_season_number,
                next_episode_number,
            )
    return ""


def _date_value(value) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        parsed = parse_date(value[:10])
        return parsed.isoformat() if parsed else ""
    date_method = getattr(value, "date", None)
    if callable(date_method):
        try:
            return date_method().isoformat()
        except (TypeError, ValueError):
            return ""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except (TypeError, ValueError):
            return ""
    return ""


def _movie_date_suggestions(movie: Movie, user) -> list[dict[str, str]]:
    """Return the accepted v1.1 movie date choices using current Smart Dates."""
    item = getattr(movie, "item", None)
    if item is None:
        return []

    suggestions: list[dict[str, str]] = []
    release_date = _date_value(getattr(item, "release_datetime", None))
    if release_date:
        suggestions.append(
            {"kind": "release", "label": "Release Date", "date": release_date}
        )

    if item.source != Sources.TMDB.value:
        return suggestions

    preferred_region = str(getattr(user, "watch_provider_region", "") or "")
    try:
        values = suggestions_for_media(
            source=item.source,
            media_type=MediaTypes.MOVIE.value,
            media_id=item.media_id,
            preferred_region=preferred_region,
            language=metadata_resolution.metadata_language_default(user),
        )
    except (
        providers.services.ProviderAPIError,
        RequestException,
        KeyError,
        TypeError,
        ValueError,
    ):
        logger.info("Post-Watch date suggestions unavailable for movie %s", item.media_id)
        return suggestions

    labels = {
        "premiere": "Premiere",
        "theatrical": "First Theatrical Release",
        "digital": "Digital Release",
        "physical": "Physical Release",
    }
    suggestions.extend(
        {"kind": key, "label": labels[key], "date": value}
        for key, value in values.items()
        if key in labels and value
    )

    # Different release classifications can legitimately share a date. Keep
    # each labelled choice, but suppress exact duplicate label/date pairs.
    unique = []
    seen = set()
    for suggestion in suggestions:
        key = (suggestion["label"], suggestion["date"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(suggestion)
    return unique


def _episode_date_suggestions(episode: Episode) -> list[dict[str, str]]:
    air_date = _date_value(getattr(episode.item, "release_datetime", None))
    return (
        [{"kind": "air", "label": "Air Date", "date": air_date}]
        if air_date
        else []
    )


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
        "watched_date": timezone.localdate(watched_at).isoformat(),
        "details_url": _movie_details_url(play),
        "date_suggestions": _movie_date_suggestions(play.movie, play.movie.user),
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
        "watched_date": timezone.localdate(watched_at).isoformat(),
        "details_url": _episode_details_url(episode, season_number, episode_number),
        "date_suggestions": _episode_date_suggestions(episode),
        "next_url": _episode_next_url(episode),
    }


def _latest_unique_movie_plays(plays: list[MoviePlay]) -> list[MoviePlay]:
    """Keep only the newest candidate play for each movie."""
    plays.sort(
        key=lambda play: _watch_datetime(play.end_date, play.created_at),
        reverse=True,
    )
    seen_movie_ids = set()
    unique = []
    for play in plays:
        if play.movie_id in seen_movie_ids:
            continue
        seen_movie_ids.add(play.movie_id)
        unique.append(play)
    return unique


def _latest_unique_episode_plays(episodes: list[Episode]) -> list[Episode]:
    """Keep only the newest candidate play for each tracked episode."""
    episodes.sort(
        key=lambda episode: _watch_datetime(episode.end_date, episode.created_at),
        reverse=True,
    )
    seen_episode_ids = set()
    unique = []
    for episode in episodes:
        identity = (episode.related_season_id, episode.item_id)
        if identity in seen_episode_ids:
            continue
        seen_episode_ids.add(identity)
        unique.append(episode)
    return unique


def build_post_watch_cards(user):
    """Build the derived seven-day inbox of recently watched unrated items."""
    cutoff = timezone.now() - timedelta(days=POST_WATCH_LOOKBACK_DAYS)
    recent = Q(end_date__gte=cutoff) | Q(
        end_date__isnull=True,
        created_at__gte=cutoff,
    )

    movie_plays = _latest_unique_movie_plays(
        list(
            MoviePlay.objects.filter(
                recent,
                movie__user=user,
                movie__score__isnull=True,
            ).select_related("movie", "movie__item", "movie__user")
        )
    )[:POST_WATCH_MAX_CARDS]
    episodes = _latest_unique_episode_plays(
        list(
            Episode.objects.filter(
                recent,
                related_season__user=user,
                item__isnull=False,
                dropped=False,
                score__isnull=True,
            ).select_related(
                "item",
                "related_season",
                "related_season__item",
                "related_season__related_tv",
                "related_season__related_tv__item",
            )
        )
    )[:POST_WATCH_MAX_CARDS]

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
        *(
            _movie_card(play)
            for play in movie_plays
            if _watch_key("movie", play.id) not in dismissed
        ),
        *(
            _episode_card(episode)
            for episode in episodes
            if _watch_key("episode", episode.id) not in dismissed
        ),
    ]
    # MoviePlay.created_at and Episode.created_at are non-null, so every card
    # has an aware watched_at even when the explicit end_date is absent.
    cards.sort(key=lambda card: card["watched_at"], reverse=True)
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
    local_original = (
        timezone.localtime(original) if timezone.is_aware(original) else original
    )
    naive = datetime.combine(selected, local_original.time().replace(tzinfo=None))
    return timezone.make_aware(naive, timezone.get_current_timezone())


@login_required
@require_GET
def post_watch(request):
    """Render the recent unrated Post-Watch inbox."""
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
    """Dismiss one concrete watch from the current user's inbox."""
    watch_key = str(request.POST.get("watch_key") or "").strip()
    _kind, watch = _lookup_watch(request.user, watch_key)
    if watch is None:
        return HttpResponseBadRequest("Invalid watch.")
    PostWatchDismissal.objects.get_or_create(user=request.user, watch_key=watch_key)
    return redirect("post_watch")


@login_required
@require_POST
def post_watch_rate(request):
    """Rate one Post-Watch item using the user's configured score scale."""
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
def post_watch_update_date(request):
    """Update the watched date for one concrete Post-Watch entry."""
    watch_key = str(request.POST.get("watch_key") or "").strip()
    kind, watch = _lookup_watch(request.user, watch_key)
    if watch is None:
        return HttpResponseBadRequest("Invalid watch.")

    old_date = watch.end_date
    new_date = _datetime_on_selected_date(
        request.POST.get("watched_date"),
        old_date or watch.created_at,
    )
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
