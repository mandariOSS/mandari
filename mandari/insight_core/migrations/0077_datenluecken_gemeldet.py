# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenlücken getrennt vom Alarm melden (Nachtrag zu Issue #1525).

Der Betriebsmonitor schickt Befunde des Datenabgleichs nicht mehr als „Quelle kritisch“, sondern in einer täglichen
Übersicht; deren Wiederholung zählt die neue Spalte ``datenabgleich_alert_sent_at``. Bisher stand jeder Hinweis in
``health_alert_sent_at``. Der Datenschritt übernimmt den Zeitstempel für Quellen ohne Störung des Abrufs (keine
Fehlerklasse, weniger als drei Fehlversuche, Abgleich jünger als die kritische Schwelle): Sonst meldete der erste Lauf
für jede dieser Quellen eine Entwarnung, obwohl sie nie gestört war, und die Lücken kämen sofort erneut. Quellen mit
Störung behalten ihren Alarm. Rückweg: Spalte entfernen (der Rückwärtsschritt schreibt die Zeitstempel zurück).
"""

from datetime import timedelta

from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone

# Stand von ERROR_KIND_INFO in insight_core/services/source_health.py bei Anlage der Migration
FEHLERKLASSEN = ("ua_blocked", "robots_blocked", "server_error_series")


def uebernehmen(apps, schema_editor):
    OParlSource = apps.get_model("insight_core", "OParlSource")
    tage = int(getattr(settings, "INSIGHT_SOURCE_STALE_CRITICAL_DAYS", 7))
    stoerung = (
        Q(last_error_kind__in=FEHLERKLASSEN)
        | Q(consecutive_failures__gte=3)
        | Q(last_sync__isnull=True)
        | Q(last_sync__lt=timezone.now() - timedelta(days=tage))
    )
    for quelle in OParlSource.objects.filter(health_alert_sent_at__isnull=False).exclude(stoerung):
        quelle.datenabgleich_alert_sent_at = quelle.health_alert_sent_at
        quelle.health_alert_sent_at = None
        quelle.save(update_fields=["datenabgleich_alert_sent_at", "health_alert_sent_at"])


def zurueck(apps, schema_editor):
    OParlSource = apps.get_model("insight_core", "OParlSource")
    for quelle in OParlSource.objects.filter(
        health_alert_sent_at__isnull=True, datenabgleich_alert_sent_at__isnull=False
    ):
        quelle.health_alert_sent_at = quelle.datenabgleich_alert_sent_at
        quelle.save(update_fields=["health_alert_sent_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0076_beratung_punkt_abgeleitet"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlsource",
            name="datenabgleich_alert_sent_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Datenlücken gemeldet am"),
        ),
        migrations.RunPython(uebernehmen, zurueck, elidable=True),
    ]
