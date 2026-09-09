"""Open TMDb items in Kodi via TMDb Helper."""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.utils.html import escape
from django.views.decorators.http import require_POST

from app.kodi_client import (
    KodiAuthenticationError,
    KodiClient,
    KodiConfigurationError,
    KodiConnectionError,
    KodiError,
    KodiProtocolError,
    KodiRPCError,
)
from app.kodi_resume import clear_pending_resume, queue_pending_resume

logger = logging.getLogger(__name__)


def _status(message: str, *, ok: bool = False) -> HttpResponse:
    colour = "text-emerald-400" if ok else "text-red-400"
    return HttpResponse(
        f'<span class="text-sm font-medium {colour}">{escape(message)}</span>'
    )


def _positive_int(value, *, allow_zero: bool = False) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    minimum = 0 if allow_zero else 1
    return number if number >= minimum else None


@login_required
@require_POST
def kodi_play(request):
    """Open a TMDb movie, show, season or episode in Kodi."""
    source = (request.POST.get("source") or "").strip().lower()
    media_type = (request.POST.get("media_type") or "").strip().lower()
    media_id = (request.POST.get("media_id") or "").strip()
    season = None
    episode = None

    if source != "tmdb":
        return _status("Kodi playback currently requires a TMDb item.")
    if not media_id.isdigit():
        return _status("Invalid TMDb ID.")

    if media_type == "movie":
        plugin_params = {"info": "play", "tmdb_type": "movie", "tmdb_id": media_id}
        action = "play"
        description = f"TMDb movie {media_id}"
    elif media_type == "tv":
        plugin_params = {"info": "seasons", "tmdb_id": media_id}
        action = "browse"
        description = f"TMDb TV show {media_id}"
    elif media_type == "season":
        season = _positive_int(request.POST.get("season_number"), allow_zero=True)
        if season is None:
            return _status("Invalid season number.")
        plugin_params = {"info": "episodes", "tmdb_id": media_id, "season": season}
        action = "browse"
        description = f"TMDb TV {media_id} season {season}"
    elif media_type == "episode":
        season = _positive_int(request.POST.get("season_number"), allow_zero=True)
        episode = _positive_int(request.POST.get("episode_number"))
        if season is None or episode is None:
            return _status("Invalid season or episode number.")
        plugin_params = {
            "info": "play",
            "tmdb_type": "tv",
            "tmdb_id": media_id,
            "season": season,
            "episode": episode,
        }
        action = "play"
        description = f"TMDb TV {media_id} S{season:02d}E{episode:02d}"
    else:
        return _status("Unsupported media type.")

    plugin_url = "plugin://plugin.video.themoviedb.helper/?" + urlencode(plugin_params)

    try:
        kodi = KodiClient.from_env()
        if action == "play":
            resume_position = queue_pending_resume(
                request.user,
                "episode" if media_type == "episode" else "movie",
                media_id,
                season_number=season,
                episode_number=episode,
            )
            try:
                kodi.open_file(plugin_url)
            except Exception:
                # Player.Open failed, so this request can never consume the
                # resume queued immediately above.
                clear_pending_resume(request.user.id)
                raise
        else:
            resume_position = None
            kodi.activate_window("videos", [plugin_url, "return"])
    except KodiConfigurationError as exc:
        logger.warning("Kodi configuration error: %s", exc)
        return _status("Kodi is not configured.")
    except KodiAuthenticationError as exc:
        logger.warning("Kodi authentication failed: %s", exc)
        return _status("Kodi authentication failed.")
    except KodiConnectionError as exc:
        logger.warning("Kodi unavailable: %s", exc)
        return _status("Kodi unavailable.")
    except KodiRPCError as exc:
        logger.warning("Kodi JSON-RPC rejected %s: %s", description, exc)
        return _status("Kodi rejected the request.")
    except KodiProtocolError as exc:
        logger.warning("Invalid Kodi response for %s: %s", description, exc)
        return _status("Invalid response from Kodi.")
    except KodiError:
        logger.exception("Unexpected Kodi integration error for %s", description)
        return _status("Kodi request failed.")

    logger.info("Kodi action=%s item=%s url=%s", action, description, plugin_url)
    if action == "browse":
        return _status("✓ Opened in Kodi", ok=True)
    if resume_position:
        minutes, seconds = divmod(int(resume_position), 60)
        return _status(
            f"✓ Sent to Kodi (resume from {minutes}:{seconds:02d})",
            ok=True,
        )
    return _status("✓ Sent to Kodi", ok=True)
