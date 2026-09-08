"""HTTP endpoint for lazy Smart Watched Dates resolution."""

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from app import smart_watched_dates
from app.providers import services


_SUGGESTION_LABELS = (
    ("premiere", _("Premiere")),
    ("theatrical", _("First Theatrical")),
    ("digital", _("Digital")),
    ("physical", _("Physical")),
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

    try:
        resolved = smart_watched_dates.suggestions_for_media(
            source=source,
            media_type=media_type,
            media_id=media_id,
            preferred_region=getattr(request.user, "watch_provider_region", ""),
            language=getattr(request.user, "metadata_language", None),
        )
    except services.ProviderAPIError:
        return JsonResponse(
            {"error": "provider_unavailable", "suggestions": []},
            status=503,
        )

    suggestions = [
        {"key": key, "label": label, "date": resolved.get(key) or ""}
        for key, label in _SUGGESTION_LABELS
        if resolved.get(key)
    ]
    return JsonResponse(
        {
            "version": smart_watched_dates.SMART_WATCHED_DATES_VERSION,
            "suggestions": suggestions,
        }
    )
