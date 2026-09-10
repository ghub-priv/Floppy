from django.urls import path

from app.rating_intelligence import refresh_rating_intelligence
from app.rating_intelligence_advanced import refresh_advanced_rating_intelligence
from app.rating_intelligence_preferences import rating_intelligence_colours
from app.rating_intelligence_views import (
    advanced_rating_intelligence,
    rating_intelligence,
)


urlpatterns = [
    path(
        "rating-intelligence/",
        rating_intelligence,
        name="rating_intelligence",
    ),
    path(
        "rating-intelligence/refresh/",
        refresh_rating_intelligence,
        name="rating_intelligence_refresh",
    ),
    path(
        "rating-intelligence/advanced/",
        advanced_rating_intelligence,
        name="rating_intelligence_advanced",
    ),
    path(
        "rating-intelligence/advanced/refresh/",
        refresh_advanced_rating_intelligence,
        name="rating_intelligence_advanced_refresh",
    ),
    path(
        "rating-intelligence/colours/",
        rating_intelligence_colours,
        name="rating_intelligence_colours",
    ),
]
