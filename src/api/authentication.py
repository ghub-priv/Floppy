"""Authentication classes for API requests."""

import hashlib
from datetime import timedelta

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission

from integrations.models import IntegrationToken
from users.models import User

from .integration_scopes import authorize_integration_request


def authenticate_token(raw_token: str):
    """Authenticate raw token against IntegrationToken or fallback to User.token."""
    if not raw_token:
        msg = "Invalid token"
        raise AuthenticationFailed(msg)

    token_digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    try:
        integration_token = IntegrationToken.objects.select_related("user").get(
            token_digest=token_digest
        )
    except IntegrationToken.DoesNotExist:
        pass
    else:
        if not integration_token.is_valid():
            msg = "Invalid token"
            raise AuthenticationFailed(msg)
        return (integration_token.user, integration_token)

    try:
        user = User.objects.get(token=raw_token)
    except User.DoesNotExist:
        msg = "Invalid token"
        raise AuthenticationFailed(msg) from None
    return (user, None)


def _mark_integration_token_used(token):
    """Record recent credential use without writing on every API request."""
    now = timezone.now()
    cutoff = now - timedelta(minutes=5)
    if token.last_used_at is None or token.last_used_at < cutoff:
        IntegrationToken.objects.filter(pk=token.pk).update(last_used_at=now)
        token.last_used_at = now


def authenticate_and_authorize(request, raw_token: str):
    """Authenticate a credential and enforce IntegrationToken scope policy."""
    user, auth = authenticate_token(raw_token)
    if isinstance(auth, IntegrationToken):
        authorize_integration_request(request, auth)
        _mark_integration_token_used(auth)
    return (user, auth)


class BearerAuthentication(BaseAuthentication):
    """Bearer or Token Authorization header authentication."""

    keywords = ("bearer", "token")

    def authenticate(self, request):
        """Authenticate the user with Bearer or Token header."""
        auth = request.headers.get("Authorization")
        if not auth:
            return None
        parts = auth.split()
        if len(parts) != 2 or parts[0].lower() not in self.keywords:  # noqa: PLR2004
            return None
        token = parts[1]
        return authenticate_and_authorize(request, token)


class ListenBrainzTokenAuthentication(BaseAuthentication):
    """ListenBrainz-style `Authorization: Token <token>` authentication.

    Exists so ListenBrainz-compatible scrobble clients (Multi-Scrobbler,
    Navidrome, Pano Scrobbler, ...) can authenticate against the ingest
    endpoints. Supports both IntegrationToken and legacy User.token.
    """

    keyword = "Token"

    def authenticate_header(self, request):
        """Return the WWW-Authenticate value so DRF answers 401, not 403.

        The ListenBrainz protocol specifies 401 for a missing or invalid token,
        and clients branch on it.
        """
        return self.keyword

    def authenticate(self, request):
        """Authenticate the user with a ListenBrainz-style token."""
        auth = request.headers.get("Authorization")
        if not auth:
            return None
        parts = auth.split()
        if len(parts) != 2 or parts[0].lower() != self.keyword.lower():  # noqa: PLR2004
            return None
        token = parts[1]
        return authenticate_and_authorize(request, token)


class APIKeyAuthentication(BaseAuthentication):
    """API Key Authentication via X-API-Key header."""

    def authenticate(self, request):
        """Authenticate the user with API Key."""
        auth = request.headers.get("X-API-Key")
        if not auth:
            return None
        return authenticate_and_authorize(request, auth.strip())


class HasScope(BasePermission):
    """Permission class to check whether request.auth grants a required scope.

    Legacy tokens (request.auth is None) have full access to all scopes.
    """

    required_scope = None

    def __init__(self, required_scope=None):
        """Initialize permission class with an optional required scope."""
        if required_scope is not None:
            self.required_scope = required_scope

    def has_permission(self, request, view):
        """Return True if the request user and token scopes satisfy requirements."""
        if not request.user or not request.user.is_authenticated:
            return False
        if request.auth is None:
            return True
        if hasattr(request.auth, "has_scope"):
            scope = getattr(view, "required_scope", self.required_scope)
            if not scope:
                return True
            return request.auth.has_scope(scope)
        return False
