from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from app import integration_health
from app.integration_health_telemetry import (
    get_integration_health_telemetry,
    record_integration_health_event,
)
from app.kodi_client import KodiConfigurationError
from integrations.webhooks.base import BaseWebhookProcessor
from integrations.webhooks.kodi import KodiWebhookProcessor
from integrations.webhooks.kodi_runtime import KodiEvent, KodiRuntimeMixin

LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "integration-health-tests",
    }
}


def _healthy(name):
    return integration_health._result(name, "healthy", "Probe succeeded.")


@override_settings(CACHES=LOCMEM_CACHE)
class IntegrationHealthViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_test_user(
            username="integration-health-user",
            password="test-password",
        )
        cache.clear()

    def test_route_requires_authentication(self):
        response = self.client.get(reverse("integration_health"))

        self.assertEqual(response.status_code, 302)

    def test_page_renders_settings_navigation_and_checks(self):
        self.client.force_login(self.user)
        with (
            patch(
                "app.integration_health._check_database",
                return_value=_healthy("Database"),
            ),
            patch(
                "app.integration_health._check_redis",
                return_value=_healthy("Redis / cache"),
            ),
            patch(
                "app.integration_health._check_celery",
                return_value=_healthy("Background tasks"),
            ),
            patch(
                "app.integration_health._check_tmdb",
                return_value=_healthy("TMDb"),
            ),
            patch(
                "app.integration_health._check_kodi",
                return_value=_healthy("Kodi JSON-RPC"),
            ),
        ):
            response = self.client.get(reverse("integration_health"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Integration Health Centre v1.0.0")
        self.assertContains(response, "Integration Health")
        self.assertContains(response, "Kodi HTTP Scrobbler")
        self.assertContains(response, "MDBList ratings")
        self.assertContains(response, "Source integration")

    def test_kodi_not_configured_does_not_expose_exception_detail(self):
        request = SimpleNamespace(user=self.user)
        with patch(
            "app.integration_health.KodiClient.from_env",
            side_effect=KodiConfigurationError("secret-host-value"),
        ):
            result = integration_health._check_kodi(request)

        self.assertEqual(result["status"], "not_configured")
        self.assertNotIn("secret-host-value", str(result))

    def test_tmdb_rejected_credential_is_not_rendered(self):
        request = SimpleNamespace(user=self.user)
        response = Mock(status_code=401, ok=False)
        with (
            patch(
                "app.integration_health.credentials.is_configured",
                return_value=True,
            ),
            patch(
                "app.integration_health.credentials.get",
                return_value="super-secret-api-key",
            ),
            patch("app.integration_health.requests.get", return_value=response),
        ):
            result = integration_health._check_tmdb(request)

        self.assertEqual(result["status"], "degraded")
        self.assertNotIn("super-secret-api-key", str(result))

    def test_tmdb_network_error_is_reported_without_exception_text(self):
        request = SimpleNamespace(user=self.user)
        with (
            patch(
                "app.integration_health.credentials.is_configured",
                return_value=True,
            ),
            patch(
                "app.integration_health.credentials.get",
                return_value="super-secret-api-key",
            ),
            patch(
                "app.integration_health.requests.get",
                side_effect=requests.ConnectionError("secret transport detail"),
            ),
        ):
            result = integration_health._check_tmdb(request)

        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("secret", str(result).lower())

    def test_redis_probe_works_through_configured_django_cache(self):
        result = integration_health._check_redis()

        self.assertEqual(result["status"], "healthy")

    def test_celery_no_workers_is_degraded_not_exception(self):
        inspector = Mock()
        inspector.ping.return_value = None
        with patch(
            "app.integration_health.celery_app.control.inspect",
            return_value=inspector,
        ):
            result = integration_health._check_celery()

        self.assertEqual(result["status"], "degraded")


@override_settings(CACHES=LOCMEM_CACHE)
class IntegrationHealthTelemetryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_test_user(
            username="integration-health-telemetry-user",
        )
        self.other_user = get_user_model().objects.create_test_user(
            username="integration-health-other-user",
        )
        cache.clear()

    def test_rating_success_populates_kodi_and_mdblist_snapshots(self):
        payload = {
            "rating": 8,
            "mediaType": "movie",
            "title": "Example",
        }

        record_integration_health_event(self.user, payload, "received")
        received = get_integration_health_telemetry(self.user.id)
        self.assertEqual(received["kodi_last_received"]["outcome"], "received")
        self.assertIsNone(received["kodi_last_success"])
        self.assertIsNone(received["mdblist_last_rating"])

        record_integration_health_event(self.user, payload, "success")
        success = get_integration_health_telemetry(self.user.id)
        self.assertEqual(success["kodi_last_success"]["outcome"], "success")
        self.assertEqual(success["mdblist_last_rating"]["rating"], 8)

    def test_telemetry_is_scoped_to_current_user(self):
        record_integration_health_event(
            self.user,
            {"event": "start", "mediaType": "movie"},
            "success",
        )

        other = get_integration_health_telemetry(self.other_user.id)

        self.assertIsNone(other["kodi_last_received"])
        self.assertIsNone(other["kodi_last_success"])
        self.assertIsNone(other["mdblist_last_rating"])

    def test_cache_failure_never_escapes_webhook_telemetry(self):
        with patch(
            "app.integration_health_telemetry.cache.set",
            side_effect=RuntimeError("cache down"),
        ):
            record_integration_health_event(
                self.user,
                {"event": "start"},
                "received",
            )


class KodiIntegrationHealthHookTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_test_user(
            username="integration-health-kodi-user",
        )
        self.processor = KodiWebhookProcessor()

    def test_rating_records_received_then_success(self):
        payload = {"rating": 7, "mediaType": "movie"}
        with (
            patch.object(KodiRuntimeMixin, "_process_rating", return_value=None),
            patch(
                "integrations.webhooks.kodi.record_integration_health_event"
            ) as telemetry,
        ):
            self.processor.process_payload(payload, self.user)

        self.assertEqual(
            telemetry.call_args_list,
            [
                call(self.user, payload, "received"),
                call(self.user, payload, "success"),
            ],
        )

    def test_unsupported_event_records_received_only(self):
        payload = {"event": "unsupported"}
        with patch(
            "integrations.webhooks.kodi.record_integration_health_event"
        ) as telemetry:
            self.processor.process_payload(payload, self.user)

        telemetry.assert_called_once_with(self.user, payload, "received")

    def test_live_only_event_records_success_after_runtime_handling(self):
        payload = {"event": KodiEvent.PLAYBACK_PAUSE}
        with (
            patch.object(KodiRuntimeMixin, "process_payload", return_value=None),
            patch(
                "integrations.webhooks.kodi.record_integration_health_event"
            ) as telemetry,
        ):
            self.processor.process_payload(payload, self.user)

        self.assertEqual(
            telemetry.call_args_list,
            [
                call(self.user, payload, "received"),
                call(self.user, payload, "success"),
            ],
        )

    def test_media_processing_records_success(self):
        payload = {"event": KodiEvent.PLAYBACK_STOP, "mediaType": "movie"}
        ids = {"tmdb_id": "123"}
        with (
            patch.object(BaseWebhookProcessor, "_process_media", return_value=None),
            patch(
                "integrations.webhooks.kodi.record_integration_health_event"
            ) as telemetry,
        ):
            self.processor._process_media(payload, self.user, ids)

        telemetry.assert_called_once_with(self.user, payload, "success")
