"""URLs for Kodi Control + Sync v2.7."""

from django.urls import path

from app.kodi_control import kodi_control
from app.kodi_playback import kodi_play
from app.kodi_runtime_views import kodi_active_playback_fragment

urlpatterns = [
    path("kodi/play/", kodi_play, name="kodi_play"),
    path("kodi/control/", kodi_control, name="kodi_control"),
    # This path intentionally precedes app.urls in config.urls. The established
    # app.urls route keeps its public name, while requests hit this wrapper so
    # stale/cold Kodi state can be reconciled before rendering the same fragment.
    path(
        "api/active-playback/",
        kodi_active_playback_fragment,
        name="kodi_active_playback_fragment",
    ),
]
