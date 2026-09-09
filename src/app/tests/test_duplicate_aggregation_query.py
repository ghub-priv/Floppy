from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from app.models import Item, MediaManager, MediaTypes, Movie, Sources, Status


class DuplicateAggregationQueryTests(TestCase):
    def test_duplicate_history_fetch_does_not_join_item_table(self):
        user = get_user_model().objects.create_user(username="duplicate-aggregation")
        item = Item.objects.create(
            media_id="duplicate-aggregation-movie",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Duplicate aggregation movie",
        )
        now = timezone.now()
        Movie.objects.bulk_create(
            [
                Movie(
                    user=user,
                    item=item,
                    status=Status.COMPLETED.value,
                    progress=1,
                    score=7,
                    end_date=now - timedelta(days=1),
                ),
                Movie(
                    user=user,
                    item=item,
                    status=Status.COMPLETED.value,
                    progress=1,
                    score=8,
                    end_date=now,
                ),
            ]
        )
        display_movie = Movie.objects.select_related("item").filter(item=item).latest("id")
        manager = MediaManager()

        with CaptureQueriesContext(connection) as queries:
            result = manager._aggregate_duplicate_data(
                [display_movie],
                user,
                MediaTypes.MOVIE.value,
            )

        self.assertEqual(len(queries), 1)
        aggregation_sql = queries[0]["sql"].lower()
        self.assertIn("app_movie", aggregation_sql)
        self.assertNotIn("join", aggregation_sql)
        self.assertNotIn("app_item", aggregation_sql)

        aggregated = result[0]
        self.assertEqual(aggregated.repeats, 2)
        self.assertEqual(aggregated.aggregated_progress, 2)
        self.assertEqual(aggregated.aggregated_score, 8)
        self.assertEqual(aggregated.aggregated_status, Status.COMPLETED.value)
