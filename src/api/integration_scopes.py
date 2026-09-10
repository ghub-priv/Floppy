"""Scope policy for third-party IntegrationToken credentials.

Legacy account tokens remain unrestricted for backwards compatibility.

IntegrationToken credentials are deny-by-default: a route and HTTP method must
be explicitly listed before a scoped credential may reach it.

The existing watchlist:* scope names are retained for compatibility. They cover
user library state: Planning/Watching/On Hold/Completed/Dropped, Collection and
custom lists.
"""

from __future__ import annotations

from rest_framework.exceptions import PermissionDenied


INTEGRATION_SCOPE_POLICY = {
    # Media catalogue + tracked-state surfaces.
    "api_media_type_list": {
        "GET": ("catalog:read", "progress:read", "watchlist:read"),
        "POST": ("progress:write", "watchlist:write"),
    },
    "api_media_detail": {
        "GET": ("catalog:read", "progress:read", "watchlist:read"),
        "PATCH": ("progress:write", "watchlist:write"),
        "DELETE": ("progress:write", "watchlist:write"),
    },
    "api_media_seasons": {
        "GET": ("catalog:read", "progress:read"),
    },
    "api_media_season_detail": {
        "GET": ("catalog:read", "progress:read", "watchlist:read"),
        "PATCH": ("progress:write", "watchlist:write"),
        "DELETE": ("progress:write", "watchlist:write"),
    },
    "api_media_season_episodes": {
        "GET": ("catalog:read", "progress:read"),
    },
    "api_media_episode_detail": {
        "GET": ("catalog:read", "progress:read"),
        "PATCH": ("progress:write", "watchlist:write"),
        "DELETE": ("progress:write",),
    },
    # Watched/progress/history.
    "api_media_consumption_history": {
        "GET": ("progress:read",),
    },
    "api_media_season_consumption_history": {
        "GET": ("progress:read",),
    },
    "api_media_episode_consumption_history": {
        "GET": ("progress:read",),
    },
    "api_media_progress": {
        "GET": ("progress:read",),
    },
    "api_media_season_progress": {
        "GET": ("progress:read",),
    },
    "api_history": {
        "GET": ("progress:read",),
    },
    "api_history_record": {
        "GET": ("progress:read",),
        "DELETE": ("progress:write",),
    },
    "api_media_episode_watch": {
        "POST": ("progress:write",),
        "DELETE": ("progress:write",),
    },
    "api_media_episode_drop": {
        "POST": ("progress:write",),
    },
    "api_media_movie_watch": {
        "POST": ("progress:write",),
        "DELETE": ("progress:write",),
    },
    "api_playback_progress": {
        "GET": ("progress:read",),
        "PUT": ("progress:write",),
        "DELETE": ("progress:write",),
    },
    "api_scrobble": {
        "POST": ("scrobble:write",),
    },
    # ListenBrainz-compatible ingest, added in v1.0.1.
    "listenbrainz_submit_listens": {
        "POST": ("scrobble:write",),
    },
    "listenbrainz_validate_token": {
        "GET": ("scrobble:write",),
    },
    # Collection. Existing watchlist:* capability represents mutable
    # user-library membership/state, so Collection belongs here too.
    "api_collection": {
        "GET": ("watchlist:read",),
        "POST": ("watchlist:write",),
    },
    "api_collection_entry": {
        "GET": ("watchlist:read",),
        "DELETE": ("watchlist:write",),
    },
    # Custom lists.
    "api_lists": {
        "GET": ("watchlist:read",),
        "POST": ("watchlist:write",),
    },
    "api_list_detail": {
        "GET": ("watchlist:read",),
        "PUT": ("watchlist:write",),
        "PATCH": ("watchlist:write",),
        "DELETE": ("watchlist:write",),
    },
    "api_list_add_item": {
        "GET": ("watchlist:read",),
        "POST": ("watchlist:write",),
        "PUT": ("watchlist:write",),
    },
    "api_list_remove_item": {
        "DELETE": ("watchlist:write",),
    },
    "api_media_lists": {
        "GET": ("watchlist:read",),
    },
    "api_media_list_detail": {
        "PUT": ("watchlist:write",),
        "POST": ("watchlist:write",),
        "DELETE": ("watchlist:write",),
    },
    "api_media_season_lists": {
        "GET": ("watchlist:read",),
    },
    "api_media_season_list_detail": {
        "PUT": ("watchlist:write",),
        "POST": ("watchlist:write",),
        "DELETE": ("watchlist:write",),
    },
    "api_media_episode_lists": {
        "GET": ("watchlist:read",),
    },
    "api_media_episode_list_detail": {
        "PUT": ("watchlist:write",),
        "POST": ("watchlist:write",),
        "DELETE": ("watchlist:write",),
    },
    # Provider-backed catalogue search.
    "api_search_provider": {
        "GET": ("catalog:read",),
    },
}


def _route_name(request):
    """Return the resolved Django URL name for a DRF request."""
    match = getattr(request, "resolver_match", None)
    if match is None:
        raw_request = getattr(request, "_request", None)
        match = getattr(raw_request, "resolver_match", None)
    return getattr(match, "url_name", None)


def authorize_integration_request(request, token):
    """Enforce the route/method scope policy for an IntegrationToken.

    Unknown routes and unknown methods are denied. All scopes listed for the
    method are required. ``IntegrationToken.has_scope`` already supports the
    ``*`` wildcard.
    """
    route_name = _route_name(request)
    method = request.method.upper()
    method_policy = INTEGRATION_SCOPE_POLICY.get(route_name)
    required_scopes = method_policy.get(method) if method_policy else None

    if not required_scopes:
        msg = "This endpoint is not available to scoped integration tokens."
        raise PermissionDenied(msg)

    missing = [scope for scope in required_scopes if not token.has_scope(scope)]
    if missing:
        msg = "Integration token lacks required scope(s): " + ", ".join(missing)
        raise PermissionDenied(msg)
