"""Template tags for scoped integration-token management."""

from django import template

from users.integration_token_views import integration_token_context

register = template.Library()


@register.inclusion_tag("users/components/integration_tokens.html")
def integration_token_panel(user):
    """Render the current user's scoped integration-token management panel."""
    return integration_token_context(user)
