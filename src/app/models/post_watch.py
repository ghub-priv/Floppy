from django.conf import settings
from django.db import models


class PostWatchDismissal(models.Model):
    """Persist a user's decision to dismiss one concrete watch from Post-Watch."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="post_watch_dismissals",
    )
    watch_key = models.CharField(max_length=64)
    dismissed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Database constraints and ordering for Post-Watch dismissals."""

        ordering = ["-dismissed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "watch_key"],
                name="app_postwatchdismissal_unique_user_watch",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "watch_key"]),
        ]

    def __str__(self):
        """Return a compact user/watch identity for admin and diagnostics."""
        return f"{self.user_id}:{self.watch_key}"
