from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0037_gpodderaccount_last_full_resync_at"),
        ("integrations", "0050_oauth_token_exchange"),
    ]

    operations = []
