"""Template tags for scoped integration-token management."""

from django import template

from users.integration_token_views import integration_token_context

register = template.Library()


@register.inclusion_tag("users/components/integration_tokens.html", takes_context=True)
def integration_token_panel(context, user):
    """Render the current user's scoped integration-token management panel."""
    panel_context = integration_token_context(user)
    panel_context["csrf_token"] = context.get("csrf_token")
    return panel_context
