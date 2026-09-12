from http import HTTPStatus as HTTP  # noqa: N814

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from integrations.oauth_models import OAUTH_DEVICE_CODE_GRANT, OAuthClient


class OAuthApiBearerRegressionTests(TestCase):
    """Protect the OAuth-to-scoped-API credential boundary."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="oauth-api-user")
        self.oauth_client = OAuthClient.register_public_client(
            name="OAuth API regression client",
            allowed_scopes=["catalog:read"],
        )

    def test_issued_bearer_is_enforced_by_api_scopes(self):
        """An OAuth bearer reaches granted API routes and no others."""
        device_response = self.client.post(
            reverse("oauth_device_authorization"),
            {
                "client_id": self.oauth_client.client_id,
                "scope": "catalog:read",
            },
        )
        self.assertEqual(device_response.status_code, HTTP.OK)
        device = device_response.json()

        self.client.force_login(self.user)
        approval = self.client.post(
            reverse("oauth_device"),
            {
                "user_code": device["user_code"],
                "action": "approve",
            },
        )
        self.assertEqual(approval.status_code, HTTP.OK)

        token_response = self.client.post(
            reverse("oauth_token"),
            {
                "client_id": self.oauth_client.client_id,
                "grant_type": OAUTH_DEVICE_CODE_GRANT,
                "device_code": device["device_code"],
            },
        )
        self.assertEqual(token_response.status_code, HTTP.OK)
        access_token = token_response.json()["access_token"]

        # Ensure the API requests are authenticated only by the OAuth bearer.
        self.client.logout()
        bearer = {"HTTP_AUTHORIZATION": f"Bearer {access_token}"}

        allowed = self.client.get(reverse("api_home"), **bearer)
        denied = self.client.get(reverse("api_history"), **bearer)

        self.assertEqual(allowed.status_code, HTTP.OK)
        self.assertEqual(denied.status_code, HTTP.FORBIDDEN)
