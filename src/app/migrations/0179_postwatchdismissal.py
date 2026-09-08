import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


LEGACY_TABLE = "floppy_local_post_watch_dismissal"


def copy_legacy_dismissals(apps, schema_editor):
    """Copy dismissals from the runtime-patch table when upgrading in place."""
    connection = schema_editor.connection
    if LEGACY_TABLE not in connection.introspection.table_names():
        return

    PostWatchDismissal = apps.get_model("app", "PostWatchDismissal")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    quoted_table = schema_editor.quote_name(LEGACY_TABLE)

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {quoted_table}")
        columns = [column[0] for column in cursor.description]
        rows = cursor.fetchall()

    valid_user_ids = set(User.objects.values_list("id", flat=True))
    for row in rows:
        values = dict(zip(columns, row, strict=False))
        user_id = values.get("user_id")
        watch_key = str(values.get("watch_key") or "").strip()
        if user_id not in valid_user_ids or not watch_key:
            continue
        dismissed_at = (
            values.get("dismissed_at")
            or values.get("created_at")
            or timezone.now()
        )
        PostWatchDismissal.objects.get_or_create(
            user_id=user_id,
            watch_key=watch_key,
            defaults={"dismissed_at": dismissed_at},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0178_watchstate_watchstatechange_watchstatesequence"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PostWatchDismissal",
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
                ("watch_key", models.CharField(max_length=64)),
                ("dismissed_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="post_watch_dismissals",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-dismissed_at"],
                "indexes": [
                    models.Index(
                        fields=["user", "watch_key"],
                        name="app_postwat_user_id_b7c1a8_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "watch_key"),
                        name="app_postwatchdismissal_unique_user_watch",
                    ),
                ],
            },
        ),
        migrations.RunPython(copy_legacy_dismissals, migrations.RunPython.noop),
    ]
