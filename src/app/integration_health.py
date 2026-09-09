"""Human-readable, read-only diagnostics for Floppy integrations.

The machine-oriented django-health-check endpoints remain authoritative for
container health. This page adds operator-facing context without becoming a
runtime dependency of the integrations it observes.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache, caches
from django.db import connection
from django.shortcuts import render

from app.integration_health_telemetry import get_integration_health_telemetry
from app.kodi_client import KodiConfigurationError, KodiError, KodiClient
from app.providers import credentials
from config.celery import app as celery_app

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 3.0
STATUS_LABELS = {
    "healthy": "Healthy",
    "degraded": "Degraded",
    "unavailable": "Unavailable",
    "not_configured": "Not configured",
    "no_data": "No recent data",
    "info": "Info",
}


def _result(name: str, status: str, summary: str, details=None) -> dict:
    return {
        "name": name,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "summary": summary,
        "details": list(details or []),
    }


def _human_bytes(value) -> str | None:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if abs(amount) < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return None


def _check_database() -> dict:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
        if not row or row[0] != 1:
            return _result(
                "Database",
                "degraded",
                "The database responded unexpectedly to a read-only probe.",
            )
        return _result(
            "Database",
            "healthy",
            "Read-only database probe succeeded.",
            [f"Backend: {connection.vendor}"],
        )
    except Exception:
        logger.warning("Integration Health database probe failed", exc_info=True)
        return _result(
            "Database",
            "unavailable",
            "The database could not complete a read-only probe.",
        )


def _redis_details() -> list[str]:
    """Best-effort Redis diagnostics after the Django cache probe succeeds."""
    details = []
    try:
        backend = caches["default"]
        cache_client = getattr(backend, "_cache", None)
        get_client = getattr(cache_client, "get_client", None)
        if not callable(get_client):
            return details
        redis_client = get_client(write=False)
        info = redis_client.info("memory") or {}
        used = _human_bytes(info.get("used_memory"))
        maximum = _human_bytes(info.get("maxmemory"))
        if used:
            details.append(f"Used memory: {used}")
        if maximum and int(info.get("maxmemory") or 0) > 0:
            details.append(f"Max memory: {maximum}")
        policy = info.get("maxmemory_policy")
        if not policy:
            config = redis_client.config_get("maxmemory-policy") or {}
            policy = config.get("maxmemory-policy")
        if policy:
            details.append(f"Eviction policy: {policy}")
    except Exception:
        # Cache availability is established by the round-trip below. Low-level
        # memory metadata is useful, but lack of access to it is not a failure.
        logger.debug("Could not read optional Redis memory diagnostics", exc_info=True)
    return details


def _check_redis() -> dict:
    probe_key = f"integration-health:probe:{uuid4().hex}"
    probe_value = uuid4().hex
    try:
        cache.set(probe_key, probe_value, timeout=10)
        observed = cache.get(probe_key)
        cache.delete(probe_key)
    except Exception:
        logger.warning("Integration Health Redis/cache probe failed", exc_info=True)
        return _result(
            "Redis / cache",
            "unavailable",
            "The configured Django cache could not complete a round-trip.",
        )

    if observed != probe_value:
        return _result(
            "Redis / cache",
            "degraded",
            "The cache accepted the probe but did not return the expected value.",
        )
    return _result(
        "Redis / cache",
        "healthy",
        "Django cache round-trip succeeded.",
        _redis_details(),
    )


def _check_celery() -> dict:
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return _result(
            "Background tasks",
            "healthy",
            "Celery is running in eager mode; tasks execute in-process.",
        )
    try:
        replies = celery_app.control.inspect(timeout=PROBE_TIMEOUT).ping() or {}
    except Exception:
        logger.warning("Integration Health Celery probe failed", exc_info=True)
        return _result(
            "Background tasks",
            "unavailable",
            "The Celery broker/worker probe failed.",
        )
    if not replies:
        return _result(
            "Background tasks",
            "degraded",
            "The broker was contacted but no Celery worker answered the ping.",
        )
    return _result(
        "Background tasks",
        "healthy",
        f"{len(replies)} Celery worker(s) answered the ping.",
    )


def _check_tmdb(request) -> dict:
    if not credentials.is_configured("tmdb", user=request.user):
        return _result(
            "TMDb",
            "not_configured",
            "No TMDb API credential is configured for this user/instance.",
        )

    api_key = credentials.get("tmdb", "api_key", user=request.user)
    try:
        response = requests.get(
            "https://api.themoviedb.org/3/configuration",
            params={"api_key": api_key},
            timeout=PROBE_TIMEOUT,
        )
    except requests.RequestException:
        logger.warning("Integration Health TMDb probe could not reach provider")
        return _result(
            "TMDb",
            "unavailable",
            "TMDb could not be reached with the active credential.",
        )

    if response.status_code in {401, 403}:
        return _result(
            "TMDb",
            "degraded",
            "TMDb rejected the active credential.",
        )
    if not response.ok:
        return _result(
            "TMDb",
            "degraded",
            f"TMDb returned HTTP {response.status_code} to the health probe.",
        )
    return _result(
        "TMDb",
        "healthy",
        "TMDb configuration probe succeeded.",
    )


def _check_kodi(_request) -> dict:
    try:
        client = KodiClient.from_env()
    except KodiConfigurationError:
        return _result(
            "Kodi JSON-RPC",
            "not_configured",
            "Kodi JSON-RPC is not configured.",
        )

    # Health pages should fail fast even if the operational client has a longer
    # timeout configured for normal playback commands.
    client.timeout = min(client.timeout, PROBE_TIMEOUT)
    try:
        reachable = client.ping()
    except KodiError:
        logger.warning("Integration Health Kodi JSON-RPC probe failed", exc_info=True)
        return _result(
            "Kodi JSON-RPC",
            "unavailable",
            "Kodi is configured but the JSON-RPC ping failed.",
        )
    if not reachable:
        return _result(
            "Kodi JSON-RPC",
            "degraded",
            "Kodi responded, but JSONRPC.Ping did not return pong.",
        )
    return _result(
        "Kodi JSON-RPC",
        "healthy",
        "Kodi JSON-RPC ping succeeded.",
    )


def _event_details(event: dict | None) -> list[str]:
    if not isinstance(event, dict):
        return []
    details = []
    if event.get("recorded_at"):
        details.append(f"Recorded: {event['recorded_at']}")
    if event.get("kind"):
        details.append(f"Kind: {event['kind']}")
    if event.get("event"):
        details.append(f"Event: {event['event']}")
    if event.get("media_type"):
        details.append(f"Media type: {event['media_type']}")
    if event.get("title"):
        details.append(f"Title: {event['title']}")
    if event.get("rating") is not None:
        details.append(f"Rating: {event['rating']}")
    return details


def _check_kodi_scrobbler(telemetry: dict) -> dict:
    received = telemetry.get("kodi_last_received")
    success = telemetry.get("kodi_last_success")
    if success:
        return _result(
            "Kodi HTTP Scrobbler",
            "healthy",
            "A successful Kodi webhook event has been observed in the last 30 days.",
            _event_details(success),
        )
    if received:
        return _result(
            "Kodi HTTP Scrobbler",
            "degraded",
            "A Kodi webhook was received, but no successful event is currently recorded.",
            _event_details(received),
        )
    return _result(
        "Kodi HTTP Scrobbler",
        "no_data",
        "No Kodi webhook telemetry has been observed in the last 30 days.",
    )


def _check_mdblist(telemetry: dict) -> dict:
    rating = telemetry.get("mdblist_last_rating")
    if rating:
        return _result(
            "MDBList ratings",
            "healthy",
            "A successful MDBList/Kodi rating event has been observed in the last 30 days.",
            _event_details(rating),
        )
    return _result(
        "MDBList ratings",
        "no_data",
        "No successful MDBList/Kodi rating telemetry has been observed in the last 30 days.",
    )


@login_required
def integration_health(request):
    """Render the read-only Integration Health Centre for the current user."""
    telemetry = get_integration_health_telemetry(request.user.id)
    checks = [
        _check_database(),
        _check_redis(),
        _check_celery(),
        _check_tmdb(request),
        _check_kodi(request),
        _check_kodi_scrobbler(telemetry),
        _check_mdblist(telemetry),
        _result(
            "Source integration",
            "info",
            "Custom behaviour is source-native; Git history and CI are the integration baseline.",
            ["The historical runtime patch-harness probe is intentionally retired."],
        ),
    ]
    return render(
        request,
        "app/integration_health.html",
        {
            "checks": checks,
            "version": "1.0.0",
        },
    )
