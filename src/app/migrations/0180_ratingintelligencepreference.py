import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0179_postwatchdismissal"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RatingIntelligencePreference",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "pri_colour_info",
                    models.CharField(default="#38bdf8", max_length=7),
                ),
                (
                    "pri_colour_positive",
                    models.CharField(default="#34d399", max_length=7),
                ),
                (
                    "pri_colour_negative",
                    models.CharField(default="#fb7185", max_length=7),
                ),
                (
                    "pri_colour_caution",
                    models.CharField(default="#fbbf24", max_length=7),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rating_intelligence_preference",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
    ]
