from django.conf import settings
from django.db import models


class RatingIntelligencePreference(models.Model):
    """Per-user presentation preferences for Rating Intelligence."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="rating_intelligence_preference",
    )
    pri_colour_info = models.CharField(max_length=7, default="#38bdf8")
    pri_colour_positive = models.CharField(max_length=7, default="#34d399")
    pri_colour_negative = models.CharField(max_length=7, default="#fb7185")
    pri_colour_caution = models.CharField(max_length=7, default="#fbbf24")

    def __str__(self):
        """Return a human-readable description of the preference row."""
        return f"Rating Intelligence preferences for {self.user}"
