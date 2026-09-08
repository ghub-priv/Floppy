"""Authenticated Kodi playback controls for Floppy's Now Playing card."""

import logging

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.http import require_POST

from app.kodi_client import KodiClient, KodiError
from app.kodi_state import is_kodi_state

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"pause", "resume", "stop", "seek_back", "seek_forward"}


def _kodi_time_to_seconds(value):
    """Convert a Kodi JSON-RPC time object to whole seconds."""
    if not isinstance(value, dict):
        return None
    try:
        hours = int(value.get("hours") or 0)
        minutes = int(value.get("minutes") or 0)
        seconds = int(value.get("seconds") or 0)
    except (TypeError, ValueError):
        return None
    return max(0, (hours * 3600) + (minutes * 60) + seconds)


def _active_video_player(kodi):
    """Return Kodi's active video player or None."""
    for player in kodi.get_active_players():
        if player.get("type") != "video":
            continue
        try:
            return int(player["playerid"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


@login_required
@require_POST
def kodi_control(request):
    """Control the Kodi session represented by Floppy's live playback state."""
    action = (request.POST.get("action") or "").strip().lower()
    if action not in VALID_ACTIONS:
        return HttpResponseBadRequest("Invalid Kodi control action.")
    if not is_kodi_state(request.user.id):
        return HttpResponse("No active Kodi playback session.", status=409)

    try:
        kodi = KodiClient.from_env()
        player_id = _active_video_player(kodi)
        if player_id is None:
            return HttpResponse("No active Kodi video player.", status=409)

        if action == "pause":
            kodi.play_pause(player_id, play=False)
        elif action == "resume":
            kodi.play_pause(player_id, play=True)
        elif action == "stop":
            kodi.stop(player_id)
        else:
            properties = kodi.get_player_properties(
                player_id,
                properties=("time", "totaltime"),
            )
            current = _kodi_time_to_seconds(properties.get("time"))
            duration = _kodi_time_to_seconds(properties.get("totaltime"))
            if current is None:
                return HttpResponse("Kodi player time unavailable.", status=409)

            delta = -30 if action == "seek_back" else 30
            target = max(0, current + delta)
            if duration is not None and duration > 0:
                target = min(target, duration)
            kodi.seek_seconds(player_id, target)

        logger.info(
            "Kodi control executed: user=%s action=%s player=%s",
            request.user.id,
            action,
            player_id,
        )
    except KodiError as exc:
        logger.warning(
            "Kodi control failed: user=%s action=%s error=%s",
            request.user.id,
            action,
            exc,
        )
        return HttpResponse("Kodi control failed.", status=502)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "Invalid Kodi control state: user=%s action=%s error=%s",
            request.user.id,
            action,
            exc,
        )
        return HttpResponse("Invalid Kodi player state.", status=409)

    # Do not mutate live_playback or PlaybackProgress here. HTTP Scrobbler
    # reports the resulting Kodi state back through the normal webhook path.
    response = HttpResponse(status=204)
    response["HX-Trigger"] = "kodiControlComplete"
    return response
