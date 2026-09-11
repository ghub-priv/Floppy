import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


LEGACY_TABLE = "floppy_local_post_watch_dismissal"


def backfill_legacy_movie_plays(apps, schema_editor):
    """Materialise pre-MoviePlay watch dates as one concrete play per movie.

    MoviePlay was introduced without a data backfill. Older Movie rows can
    therefore still carry a valid end_date while having no MoviePlay rows. The
    runtime-patched Post-Watch workflow used Movie.end_date directly, whereas
    the Git-native implementation works with concrete MoviePlay records.

    This mirrors Movie.watch()'s existing lazy preservation behaviour once at
    migration time so older watched movies are not lost from Post-Watch and
    legacy dismissal keys can be translated deterministically.
    """
    db_alias = schema_editor.connection.alias
    Movie = apps.get_model("app", "Movie")
    MoviePlay = apps.get_model("app", "MoviePlay")

    movies_with_plays = MoviePlay.objects.using(db_alias).values("movie_id")
    legacy_movies = (
        Movie.objects.using(db_alias)
        .filter(end_date__isnull=False)
        .exclude(pk__in=movies_with_plays)
        .only("pk", "end_date")
    )

    batch = []
    for movie in legacy_movies.iterator(chunk_size=500):
        batch.append(MoviePlay(movie_id=movie.pk, end_date=movie.end_date))
        if len(batch) >= 500:
            MoviePlay.objects.using(db_alias).bulk_create(batch, batch_size=500)
            batch.clear()
    if batch:
        MoviePlay.objects.using(db_alias).bulk_create(batch, batch_size=500)


def _translate_legacy_watch_key(watch_key, Movie, MoviePlay, db_alias):
    """Translate r13 watch keys to the Git-native MoviePlay key format."""
    parts = watch_key.split(":")

    # Episode keys did not change. Also tolerate already-native movie keys so
    # rerunning against a partially migrated local database stays harmless.
    if len(parts) == 2 and parts[0] == "episode":
        return watch_key
    if len(parts) == 2 and parts[0] == "movie":
        try:
            play_id = int(parts[1])
        except (TypeError, ValueError):
            return None
        return (
            watch_key
            if MoviePlay.objects.using(db_alias).filter(pk=play_id).exists()
            else None
        )

    # r13 movie keys were movie:<Movie id>:<integer watched timestamp>.
    if len(parts) != 3 or parts[0] != "movie":
        return None
    try:
        movie_id = int(parts[1])
        watched_timestamp = int(parts[2])
    except (TypeError, ValueError):
        return None

    plays = list(
        MoviePlay.objects.using(db_alias)
        .filter(movie_id=movie_id, end_date__isnull=False)
        .order_by("-end_date", "-created_at", "-pk")
    )
    for play in plays:
        try:
            if int(play.end_date.timestamp()) == watched_timestamp:
                return f"movie:{play.pk}"
        except (AttributeError, OverflowError, OSError, ValueError):
            continue

    # The legacy key was generated from Movie.end_date. If database precision
    # prevented an exact MoviePlay timestamp match, map it to the newest play
    # only when the parent Movie still confirms the same watched second.
    movie = (
        Movie.objects.using(db_alias)
        .filter(pk=movie_id, end_date__isnull=False)
        .only("end_date")
        .first()
    )
    if movie is None or not plays:
        return None
    try:
        if int(movie.end_date.timestamp()) == watched_timestamp:
            return f"movie:{plays[0].pk}"
    except (AttributeError, OverflowError, OSError, ValueError):
        return None
    return None


def copy_legacy_dismissals(apps, schema_editor):
    """Copy and translate dismissals from the runtime-patch table."""
    connection = schema_editor.connection
    if LEGACY_TABLE not in connection.introspection.table_names():
        return

    db_alias = connection.alias
    PostWatchDismissal = apps.get_model("app", "PostWatchDismissal")
    Movie = apps.get_model("app", "Movie")
    MoviePlay = apps.get_model("app", "MoviePlay")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    quoted_table = schema_editor.quote_name(LEGACY_TABLE)

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {quoted_table}")
        columns = [column[0] for column in cursor.description]
        rows = cursor.fetchall()

    valid_user_ids = set(
        User.objects.using(db_alias).values_list("id", flat=True),
    )
    for row in rows:
        values = dict(zip(columns, row, strict=False))
        user_id = values.get("user_id")
        legacy_watch_key = str(values.get("watch_key") or "").strip()
        if user_id not in valid_user_ids or not legacy_watch_key:
            continue

        watch_key = _translate_legacy_watch_key(
            legacy_watch_key,
            Movie,
            MoviePlay,
            db_alias,
        )
        if not watch_key:
            continue

        dismissed_at = (
            values.get("dismissed_at")
            or values.get("created_at")
            or timezone.now()
        )
        dismissal, created = PostWatchDismissal.objects.using(db_alias).get_or_create(
            user_id=user_id,
            watch_key=watch_key,
        )
        if created:
            # auto_now_add sets the insertion time during model save. Restore
            # the legacy value afterwards so an in-place migration retains the
            # user's original dismissal history as well as the dismissal itself.
            PostWatchDismissal.objects.using(db_alias).filter(pk=dismissal.pk).update(
                dismissed_at=dismissed_at,
            )


def migrate_post_watch_legacy_data(apps, schema_editor):
    """Normalise old movie watches, then migrate runtime-patch dismissals."""
    backfill_legacy_movie_plays(apps, schema_editor)
    copy_legacy_dismissals(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0180_item_metadata_refreshed_at"),
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
        migrations.RunPython(
            migrate_post_watch_legacy_data,
            migrations.RunPython.noop,
        ),
    ]
