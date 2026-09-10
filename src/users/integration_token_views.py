"""User-facing management views for scoped integration tokens."""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from integrations.models import IntegrationToken

INTEGRATION_SCOPE_OPTIONS = (
    ("catalog:read", "Catalog Read", "Read provider-backed catalogue metadata."),
    ("progress:read", "Progress Read", "Read watch history and playback progress."),
    ("progress:write", "Progress Write", "Update watched state and playback progress."),
    (
        "watchlist:read",
        "Library State Read",
        "Read tracked library state, collections and custom lists.",
    ),
    (
        "watchlist:write",
        "Library State Write",
        "Change tracked library state, collections and custom lists.",
    ),
    ("scrobble:write", "Scrobble Write", "Submit playback and ListenBrainz events."),
)
_ALLOWED_SCOPES = frozenset(
    scope for scope, _label, _description in INTEGRATION_SCOPE_OPTIONS
)
MAX_TOKEN_EXPIRY_DAYS = 3650
MAX_TOKEN_FIELD_LENGTH = 255


def integration_token_context(user):
    """Return template context for the scoped-token management panel."""
    return {
        "integration_tokens": user.integration_tokens.order_by("-created_at"),
        "integration_scope_options": INTEGRATION_SCOPE_OPTIONS,
    }


@login_required
@require_GET
def integration_tokens(request):
    """Render the scoped integration-token settings subpage."""
    return render(request, "users/integration_tokens.html")


@login_required
@require_POST
def create_integration_token(request):
    """Create a scoped credential and display its plaintext value exactly once."""
    name = (request.POST.get("name") or "").strip()
    client_identifier = (request.POST.get("client_identifier") or "").strip()
    scopes = list(dict.fromkeys(request.POST.getlist("scopes")))
    expires_in_days = (request.POST.get("expires_in_days") or "").strip()

    errors = []
    if not name:
        errors.append("Token name is required.")
    elif len(name) > MAX_TOKEN_FIELD_LENGTH:
        errors.append("Token name must be 255 characters or fewer.")

    if len(client_identifier) > MAX_TOKEN_FIELD_LENGTH:
        errors.append("Client identifier must be 255 characters or fewer.")

    if not scopes:
        errors.append("Select at least one permission.")
    elif any(scope not in _ALLOWED_SCOPES for scope in scopes):
        errors.append("One or more selected permissions are invalid.")

    expires_at = None
    if expires_in_days:
        try:
            expiry_days = int(expires_in_days)
        except ValueError:
            errors.append("Expiry must be a whole number of days.")
        else:
            if not 1 <= expiry_days <= MAX_TOKEN_EXPIRY_DAYS:
                errors.append(
                    f"Expiry must be between 1 and {MAX_TOKEN_EXPIRY_DAYS} days."
                )
            else:
                expires_at = timezone.now() + timedelta(days=expiry_days)

    if errors:
        for error in errors:
            messages.error(request, error)
        return redirect("integration_tokens")

    integration_token, raw_integration_token = IntegrationToken.generate(
        user=request.user,
        name=name,
        client_identifier=client_identifier,
        scopes=scopes,
        expires_at=expires_at,
    )
    return render(
        request,
        "users/integration_token_created.html",
        {
            "integration_token": integration_token,
            "raw_integration_token": raw_integration_token,
        },
    )


@login_required
@require_POST
def revoke_integration_token(request, token_id):
    """Revoke one scoped credential belonging to the current user."""
    integration_token = get_object_or_404(
        IntegrationToken,
        pk=token_id,
        user=request.user,
    )
    if integration_token.revoked_at is None:
        integration_token.revoked_at = timezone.now()
        integration_token.save(update_fields=["revoked_at"])
        messages.success(
            request, f"Integration token '{integration_token.name}' revoked."
        )
    return redirect("integration_tokens")
