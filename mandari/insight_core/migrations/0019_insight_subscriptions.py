"""
Migration for Insight Subscriptions (E-Mail-Digest).

Models: InsightSubscriber, SubscriptionAlert, DigestLog
"""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("insight_core", "0018_paper_georef"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # DigestLog first (referenced by SubscriptionAlert FK)
        migrations.CreateModel(
            name="DigestLog",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("sent_at", models.DateTimeField(auto_now_add=True)),
                ("alert_count", models.IntegerField()),
                ("success", models.BooleanField(default=True)),
                ("error", models.TextField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "Digest-Protokoll",
                "verbose_name_plural": "Digest-Protokolle",
                "db_table": "insight_digest_logs",
                "ordering": ["-sent_at"],
            },
        ),
        # InsightSubscriber
        migrations.CreateModel(
            name="InsightSubscriber",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("email", models.EmailField(db_index=True, max_length=254)),
                ("token", models.UUIDField(default=uuid.uuid4, unique=True)),
                ("confirmed", models.BooleanField(default=False)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("neighborhood_active", models.BooleanField(default=False)),
                ("neighborhood_lat", models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True)),
                ("neighborhood_lon", models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True)),
                ("neighborhood_name", models.CharField(blank=True, max_length=300, null=True)),
                ("neighborhood_radius", models.IntegerField(default=500)),
                ("keyword_active", models.BooleanField(default=False)),
                ("keyword", models.CharField(blank=True, max_length=200, null=True)),
                ("bookmarks_active", models.BooleanField(default=False)),
                ("digest_frequency", models.CharField(
                    choices=[("weekly", "Wöchentlich"), ("biweekly", "Alle 2 Wochen")],
                    default="weekly",
                    max_length=10,
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("unsubscribed_at", models.DateTimeField(blank=True, null=True)),
                ("body", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="subscribers",
                    to="insight_core.oparlbody",
                )),
                ("user", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="insight_subscriptions",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "verbose_name": "Insight-Abonnent",
                "verbose_name_plural": "Insight-Abonnenten",
                "db_table": "insight_subscribers",
                "ordering": ["-created_at"],
                "unique_together": {("email", "body")},
            },
        ),
        # DigestLog.subscriber FK (now that InsightSubscriber exists)
        migrations.AddField(
            model_name="digestlog",
            name="subscriber",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="digest_logs",
                to="insight_core.insightsubscriber",
            ),
            preserve_default=False,
        ),
        # SubscriptionAlert
        migrations.CreateModel(
            name="SubscriptionAlert",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("alert_type", models.CharField(
                    choices=[("neighborhood", "Nachbarschaft"), ("keyword", "Suchbegriff"), ("bookmark", "Gemerkt")],
                    max_length=20,
                )),
                ("entity_type", models.CharField(max_length=50)),
                ("entity_id", models.UUIDField()),
                ("entity_title", models.CharField(max_length=500)),
                ("entity_url", models.CharField(max_length=500)),
                ("context", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("subscriber", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="alerts",
                    to="insight_core.insightsubscriber",
                )),
                ("sent_in_digest", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="alerts",
                    to="insight_core.digestlog",
                )),
            ],
            options={
                "verbose_name": "Abo-Benachrichtigung",
                "verbose_name_plural": "Abo-Benachrichtigungen",
                "db_table": "insight_subscription_alerts",
                "ordering": ["-created_at"],
                "unique_together": {("subscriber", "entity_type", "entity_id")},
            },
        ),
    ]
