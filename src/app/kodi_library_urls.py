from django.urls import path

from app.kodi_library import kodi_library


urlpatterns = [
    path("kodi-library/", kodi_library, name="kodi_library"),
]
