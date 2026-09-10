from datetime import timedelta
from http import HTTPStatus as HTTP  # noqa: N814

from django.urls import reverse
from django.utils import timezone

from integrations.models import IntegrationToken

from .base import FloppyApiTestCase


class ScopedIntegrationTokenPolicyTests(FloppyApiTestCase):
    """Verify central deny-by-default enforcement and usage telemetry."""

    def test_allowed_route_requires_every_mapped_scope(self):
        """A mapped method is allowed only when every configured scope is present."""
        token, raw = IntegrationToken.generate(
            user=self.user1,
            name="Read library",
            scopes=["catalog:read", "progress:read", "watchlist:read"],
        )

        response = self.call_api(
            "get",
            "api_media_type_list",
            args=("movie",),
            headers={"HTTP_AUTHORIZATION": f"Bearer {raw}"},
        )

        self.assertEqual(response.status_code, HTTP.OK)
        token.refresh_from_db()
        self.assertIsNotNone(token.last_used_at)

    def test_missing_scope_is_denied_without_usage_update(self):
        """Denied scoped requests do not count as successful credential use."""
        token, raw = IntegrationToken.generate(
            user=self.user1,
            name="Incomplete reader",
            scopes=["catalog:read", "progress:read"],
        )

        response = self.call_api(
            "get",
            "api_media_type_list",
            args=("movie",),
            headers={"HTTP_X_API_KEY": raw},
        )

        self.assertEqual(response.status_code, HTTP.FORBIDDEN)
        token.refresh_from_db()
        self.assertIsNone(token.last_used_at)

    def test_unlisted_route_is_denied_by_default(self):
        """A valid scoped credential cannot reach a route absent from the policy."""
        _token, raw = IntegrationToken.generate(
            user=self.user1,
            name="Default scoped token",
        )

        response = self.call_api(
            "get",
            "api_media_list",
            headers={"HTTP_X_API_KEY": raw},
        )

        self.assertEqual(response.status_code, HTTP.FORBIDDEN)

    def test_legacy_account_token_bypasses_scoped_policy(self):
        """Legacy account credentials retain access to routes outside scoped policy."""
        response = self.call_api(
            "get",
            "api_media_list",
            headers={"HTTP_X_API_KEY": self.user1.token},
        )

        self.assertEqual(response.status_code, HTTP.OK)

    def test_last_used_write_is_throttled_for_five_minutes(self):
        """Successful requests inside the telemetry window do not rewrite last_used_at."""
        token, raw = IntegrationToken.generate(
            user=self.user1,
            name="Recently used",
            scopes=["scrobble:write"],
        )
        recent = timezone.now() - timedelta(minutes=1)
        token.last_used_at = recent
        token.save(update_fields=["last_used_at"])

        response = self.call_api(
            "post",
            "api_scrobble",
            payload={
                "action": "start",
                "media_type": "movie",
                "ids": {"tmdb": "603"},
            },
            headers={"HTTP_AUTHORIZATION": f"Bearer {raw}"},
        )

        self.assertEqual(response.status_code, HTTP.OK)
        token.refresh_from_db()
        self.assertEqual(token.last_used_at, recent)

    def test_listenbrainz_validate_accepts_scrobble_scope(self):
        """v1.0.1 permits ListenBrainz validation with scrobble:write."""
        _token, raw = IntegrationToken.generate(
            user=self.user1,
            name="ListenBrainz client",
            scopes=["scrobble:write"],
        )

        response = self.client.get(
            reverse("listenbrainz_validate_token"),
            HTTP_AUTHORIZATION=f"Token {raw}",
        )

        self.assertEqual(response.status_code, HTTP.OK)
        self.assertTrue(response.json()["valid"])
