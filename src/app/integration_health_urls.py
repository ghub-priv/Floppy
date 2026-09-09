from django.urls import path

from app.integration_health import integration_health

urlpatterns = [
    path(
        "settings/integration-health",
        integration_health,
        name="integration_health",
    ),
]
