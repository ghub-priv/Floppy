"""Admin registrations for custom non-media models.

These models do not implement the status/score contract used by app.admin's
MediaAdmin auto-registration. Register them explicitly before Django's admin
autodiscovery reaches the generic fallback.
"""

from django.contrib import admin

from app.models.post_watch import PostWatchDismissal
from app.models.rating_intelligence import RatingIntelligencePreference


class PostWatchDismissalAdmin(admin.ModelAdmin):
    """Admin for per-watch Post-Watch dismissals."""

    search_fields = ["user__username", "watch_key"]
    list_display = ["user", "watch_key", "dismissed_at"]
    list_filter = ["dismissed_at"]
    raw_id_fields = ["user"]


class RatingIntelligencePreferenceAdmin(admin.ModelAdmin):
    """Admin for per-user Rating Intelligence presentation preferences."""

    list_display = ["user", "pri_colour_info", "pri_colour_positive", "pri_colour_negative", "pri_colour_caution"]
    search_fields = ["user__username"]
    raw_id_fields = ["user"]


admin.site.register(PostWatchDismissal, PostWatchDismissalAdmin)
admin.site.register(RatingIntelligencePreference, RatingIntelligencePreferenceAdmin)
