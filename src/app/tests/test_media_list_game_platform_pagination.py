"""Regression coverage for game-platform filtering on the SQL media-list path."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import BasicMedia, Game, Item, MediaTypes, Sources, Status
from users.models import MediaStatusChoices


class GamePlatformSqlPaginationTests(TestCase):
    """Keep JSON-backed game platform filters safe inside SQL subqueries."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="game-platform-pagination",
            password="12345",
        )
        cls.user.game_enabled = True
        cls.user.save(update_fields=["game_enabled"])

        items = [
            Item(
                media_id=f"game-platform-{index}",
                source=Sources.IGDB.value,
                media_type=MediaTypes.GAME.value,
                title=f"Game {index:02d}",
                platforms=["PC"] if index < 20 else ["PlayStation 5"],
            )
            for index in range(40)
        ]
        Item.objects.bulk_create(items)
        created_items = list(
            Item.objects.filter(
                media_id__startswith="game-platform-",
                media_type=MediaTypes.GAME.value,
            ).order_by("id")
        )
        Game.objects.bulk_create(
            [
                Game(
                    item=item,
                    user=cls.user,
                    status=Status.COMPLETED.value,
                )
                for item in created_items
            ]
        )

    def test_platform_filter_works_in_paginated_and_item_projection_queries(self):
        filters = {
            "platform_values": ("PC",),
            "platform_mode": "or",
        }

        page, total = BasicMedia.objects.get_media_list(
            user=self.user,
            media_type=MediaTypes.GAME.value,
            status_filter=MediaStatusChoices.ALL,
            sort_filter="title",
            list_sql_filters=filters,
            sql_limit=32,
            sql_offset=0,
        )

        self.assertEqual(total, 20)
        self.assertEqual(len(page), 20)
        self.assertTrue(all("PC" in media.item.platforms for media in page))

        projected = list(
            BasicMedia.objects.get_media_list_item_values(
                user=self.user,
                media_type=MediaTypes.GAME.value,
                status_filter=MediaStatusChoices.ALL,
                list_sql_filters=filters,
            )
        )
        self.assertEqual(len(projected), 20)
        self.assertTrue(all("PC" in row["platforms"] for row in projected))
