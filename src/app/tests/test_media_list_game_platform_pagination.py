"""Regression coverage for game-platform filtering around SQL pagination."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.media_list_filters import MediaListFilters
from app.media_list_pagination import can_paginate_in_sql
from app.models import BasicMedia, Game, Item, MediaTypes, Sources, Status
from users.models import MediaStatusChoices


class GamePlatformSqlPaginationTests(TestCase):
    """Keep JSON-backed game platform filters on their alias-safe path."""

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

    def test_platform_filter_uses_alias_safe_fallback(self):
        """Platform JSON predicates must not enter alias-sensitive subqueries."""
        self.assertFalse(
            can_paginate_in_sql(
                MediaListFilters(platforms=("PC",)),
                MediaTypes.GAME.value,
                "title",
            )
        )

        media = list(
            BasicMedia.objects.get_media_list(
                user=self.user,
                media_type=MediaTypes.GAME.value,
                status_filter=MediaStatusChoices.ALL,
                sort_filter="title",
                list_sql_filters={
                    "platform_values": ("PC",),
                    "platform_mode": "or",
                },
            )
        )

        self.assertEqual(len(media), 20)
        self.assertTrue(all("PC" in entry.item.platforms for entry in media))
