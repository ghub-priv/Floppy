from django.urls import path

from app.post_watch import (
    post_watch,
    post_watch_dismiss,
    post_watch_rate,
    post_watch_update_date,
)


urlpatterns = [
    path("post-watch/", post_watch, name="post_watch"),
    path("post-watch/dismiss/", post_watch_dismiss, name="post_watch_dismiss"),
    path("post-watch/rate/", post_watch_rate, name="post_watch_rate"),
    path(
        "post-watch/date/",
        post_watch_update_date,
        name="post_watch_update_date",
    ),
]
