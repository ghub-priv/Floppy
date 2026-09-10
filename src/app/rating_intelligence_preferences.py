from __future__ import annotations

import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from app.models import RatingIntelligencePreference

PRI_COLOUR_DEFAULTS = {
    "pri_colour_info": "#38bdf8",
    "pri_colour_positive": "#34d399",
    "pri_colour_negative": "#fb7185",
    "pri_colour_caution": "#fbbf24",
}
HEX_COLOUR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def rating_intelligence_colours_for_user(user):
    """Return persisted PRI semantic colours, falling back to v2.3.0 defaults."""
    try:
        preference = user.rating_intelligence_preference
    except RatingIntelligencePreference.DoesNotExist:
        preference = None

    return {
        key: getattr(preference, key) if preference is not None else default
        for key, default in PRI_COLOUR_DEFAULTS.items()
    }


def attach_rating_intelligence_colours(user):
    """Expose PRI colours on the request user for the accepted v2.3.0 templates."""
    colours = rating_intelligence_colours_for_user(user)
    for key, value in colours.items():
        setattr(user, key, value)
    return colours


@login_required
@require_http_methods(["GET", "POST"])
def rating_intelligence_colours(request):
    """View and update presentation-only Rating Intelligence colour settings."""
    colours = rating_intelligence_colours_for_user(request.user)

    if request.method == "POST":
        if request.user.is_demo:
            messages.error(request, "This section is view-only for demo accounts.")
            return redirect("rating_intelligence_colours")

        submitted = {
            key: str(request.POST.get(key, default)).strip()
            for key, default in PRI_COLOUR_DEFAULTS.items()
        }
        if any(not HEX_COLOUR_RE.fullmatch(value) for value in submitted.values()):
            messages.error(
                request,
                "Colours must use #RRGGBB hexadecimal format.",
            )
        else:
            preference, _created = RatingIntelligencePreference.objects.get_or_create(
                user=request.user,
            )
            changed_fields = []
            for key, value in submitted.items():
                normalised = value.lower()
                if getattr(preference, key) != normalised:
                    setattr(preference, key, normalised)
                    changed_fields.append(key)
            if changed_fields:
                preference.save(update_fields=changed_fields)

            messages.success(request, "Rating Intelligence colours updated.")
            return redirect("rating_intelligence_colours")

    return render(
        request,
        "app/rating_intelligence_colours.html",
        {"colours": colours},
    )
