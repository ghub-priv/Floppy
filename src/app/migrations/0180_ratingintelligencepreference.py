import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


LEGACY_COLOUR_FIELDS = (
    "pri_colour_info",
    "pri_colour_positive",
    "pri_colour_negative",
    "pri_colour_caution",
)


def copy_legacy_rating_intelligence_colours(apps, schema_editor):
    """Preserve colours from runtime-patched databases that stored them on User."""
    connection = schema_editor.connection
    db_alias = connection.alias
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    Preference = apps.get_model("app", "RatingIntelligencePreference")
    user_table = User._meta.db_table

    if user_table not in connection.introspection.table_names():
        return

    with connection.cursor() as cursor:
        description = connection.introspection.get_table_description(cursor, user_table)
        available_columns = {column.name for column in description}
        legacy_fields = [
            field for field in LEGACY_COLOUR_FIELDS if field in available_columns
        ]
        if not legacy_fields:
            return

        quoted_columns = ", ".join(
            schema_editor.quote_name(column)
            for column in ("id", *legacy_fields)
        )
        cursor.execute(
            f"SELECT {quoted_columns} FROM {schema_editor.quote_name(user_table)}"
        )
        rows = cursor.fetchall()

    for row in rows:
        values = dict(zip(("id", *legacy_fields), row, strict=False))
        defaults = {
            field: str(values[field]).strip().lower()
            for field in legacy_fields
            if values.get(field)
        }
        if defaults:
            Preference.objects.using(db_alias).update_or_create(
                user_id=values["id"],
                defaults=defaults,
            )


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
        migrations.RunPython(
            copy_legacy_rating_intelligence_colours,
            migrations.RunPython.noop,
        ),
    ]
