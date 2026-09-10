from django.contrib.auth.decorators import login_required

from app.rating_intelligence import rating_intelligence as _rating_intelligence
from app.rating_intelligence_advanced import (
    advanced_rating_intelligence as _advanced_rating_intelligence,
)
from app.rating_intelligence_preferences import attach_rating_intelligence_colours


@login_required
def rating_intelligence(request):
    """Render Rating Intelligence with the user's semantic colour preferences."""
    attach_rating_intelligence_colours(request.user)
    return _rating_intelligence(request)


@login_required
def advanced_rating_intelligence(request):
    """Render Advanced Rating Intelligence with semantic colour preferences."""
    attach_rating_intelligence_colours(request.user)
    return _advanced_rating_intelligence(request)
