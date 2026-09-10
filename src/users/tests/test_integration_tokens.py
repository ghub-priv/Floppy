from http import HTTPStatus as HTTP  # noqa: N814

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from integrations.models import IntegrationToken


class IntegrationTokenSettingsTests(TestCase):
    """Verify scoped-token creation, display, validation and revocation."""

    def setUp(self):
        """Create two users and authenticate the primary settings client."""
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="token-settings-user")
        self.other_user = user_model.objects.create_user(username="token-settings-other")
        self.client.force_login(self.user)

    def test_settings_page_exposes_scope_choices(self):
        """The management page exposes the accepted human-readable capabilities."""
        response = self.client.get(reverse("integration_tokens"))

        self.assertEqual(response.status_code, HTTP.OK)
        self.assertContains(response, "Integration Tokens")
        self.assertContains(response, "Library State Read")
        self.assertContains(response, "Library State Write")
        self.assertContains(response, "Scrobble Write")

    def test_create_displays_plaintext_once_and_persists_digest_only(self):
        """New plaintext credentials are returned by the creation response only."""
        response = self.client.post(
            reverse("create_integration_token"),
            {
                "name": "Kodi Living Room",
                "client_identifier": "kodi-living-room",
                "scopes": [
                    "catalog:read",
                    "progress:read",
                    "watchlist:read",
                ],
                "expires_in_days": "30",
            },
        )

        self.assertEqual(response.status_code, HTTP.OK)
        self.assertTemplateUsed(response, "users/integration_token_created.html")
        raw_token = response.context["raw_integration_token"]
        integration_token = IntegrationToken.objects.get(user=self.user)
        self.assertTrue(raw_token.startswith("flp_"))
        self.assertContains(response, raw_token)
        self.assertNotEqual(integration_token.token_digest, raw_token)
        self.assertNotIn(raw_token, integration_token.token_digest)
        self.assertEqual(integration_token.token_prefix, raw_token[:12])
        self.assertIsNotNone(integration_token.expires_at)

        settings_response = self.client.get(reverse("integration_tokens"))
        self.assertNotContains(settings_response, raw_token)
        self.assertContains(settings_response, integration_token.token_prefix)

    def test_create_rejects_unknown_scope(self):
        """A submitted permission outside the supported set cannot be persisted."""
        response = self.client.post(
            reverse("create_integration_token"),
            {
                "name": "Overprivileged",
                "scopes": ["catalog:read", "admin:everything"],
                "expires_in_days": "",
            },
        )

        self.assertRedirects(response, reverse("integration_tokens"))
        self.assertFalse(IntegrationToken.objects.filter(user=self.user).exists())

    def test_create_requires_at_least_one_scope(self):
        """Empty-scope credentials are rejected by the user-facing creation flow."""
        response = self.client.post(
            reverse("create_integration_token"),
            {"name": "No permissions", "expires_in_days": ""},
        )

        self.assertRedirects(response, reverse("integration_tokens"))
        self.assertFalse(IntegrationToken.objects.filter(user=self.user).exists())

    def test_owner_can_revoke_token(self):
        """A user may revoke their own integration token."""
        integration_token, _raw = IntegrationToken.generate(
            user=self.user,
            name="Kodi Bedroom",
            scopes=["catalog:read"],
        )

        response = self.client.post(
            reverse("revoke_integration_token", args=(integration_token.pk,)),
        )

        self.assertRedirects(response, reverse("integration_tokens"))
        integration_token.refresh_from_db()
        self.assertIsNotNone(integration_token.revoked_at)

    def test_user_cannot_revoke_another_users_token(self):
        """Token revocation is isolated to credentials owned by the current user."""
        integration_token, _raw = IntegrationToken.generate(
            user=self.other_user,
            name="Other user's client",
            scopes=["catalog:read"],
        )

        response = self.client.post(
            reverse("revoke_integration_token", args=(integration_token.pk,)),
        )

        self.assertEqual(response.status_code, HTTP.NOT_FOUND)
        integration_token.refresh_from_db()
        self.assertIsNone(integration_token.revoked_at)
