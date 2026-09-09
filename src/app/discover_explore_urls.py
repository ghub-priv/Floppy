"""URL routes for the catalogue explorers embedded in Discover."""

from django.urls import path

from app.discover_explore import discover_explore
from app.discover_explore_tv import discover_explore_tv

urlpatterns = [
    path("discover/explore", discover_explore, name="discover_explore"),
    path("discover/explore/tv", discover_explore_tv, name="discover_explore_tv"),
]
