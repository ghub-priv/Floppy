"""HTTP endpoint for lazy Smart Watched Dates resolution."""

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from app import smart_watched_dates
from app.models import MediaTypes, Sources
from app.providers import services
from app.services import metadata_resolution


_SUGGESTION_LABELS = (
    ("premiere", "Premiere"),
    ("theatrical", "First Theatrical"),
    ("digital", "Digital"),
    ("physical", "Physical"),
)


@login_required
@never_cache
@require_GET
def smart_watched_dates_view(request):
    """Return region-aware watched-date suggestions for a track modal movie."""
    source = str(request.GET.get("source") or "").strip()
    media_type = str(request.GET.get("media_type") or "").strip()
    media_id = str(request.GET.get("media_id") or "").strip()

    if not source or not media_type or not media_id:
        return JsonResponse(
            {"error": "missing_media_identity", "suggestions": []},
            status=400,
        )

    if source != Sources.TMDB.value or media_type != MediaTypes.MOVIE.value:
        return JsonResponse(
            {
                "version": smart_watched_dates.SMART_WATCHED_DATES_VERSION,
                "suggestions": [],
            }
        )

    # TMDB movie ids are numeric. Reject malformed path input instead of
    # letting a crafted value alter the provider URL constructed downstream.
    if not media_id.isdigit():
        return JsonResponse(
            {"error": "invalid_media_id", "suggestions": []},
            status=400,
        )

    try:
        resolved = smart_watched_dates.suggestions_for_media(
            source=source,
            media_type=media_type,
            media_id=media_id,
            preferred_region=getattr(request.user, "watch_provider_region", ""),
            language=metadata_resolution.metadata_language_default(request.user),
        )
    except services.ProviderAPIError:
        return JsonResponse(
            {"error": "provider_unavailable", "suggestions": []},
            status=503,
        )

    suggestions = [
        {"key": key, "label": _(label), "date": resolved.get(key) or ""}
        for key, label in _SUGGESTION_LABELS
        if resolved.get(key)
    ]
    return JsonResponse(
        {
            "version": smart_watched_dates.SMART_WATCHED_DATES_VERSION,
            "suggestions": suggestions,
        }
    )
