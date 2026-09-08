from django.urls import path

from app.smart_watched_dates_views import smart_watched_dates_view


urlpatterns = [
    path(
        "smart-watched-dates",
        smart_watched_dates_view,
        name="smart_watched_dates",
    ),
]
