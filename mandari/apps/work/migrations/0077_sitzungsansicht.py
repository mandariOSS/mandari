# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsansicht der laufenden Fraktionssitzung (Issue #874).

Neue Spalten, rein additiv (alle leer bzw. mit Datenbank-Vorgabe ``""``, damit ein älteres Abbild weiter
Sitzungen und TOPs anlegen kann):

- Sitzung: ``started_at`` (tatsächlicher Beginn), ``chaired_by`` und ``minute_taker`` (Sitzungsleitung und
  Schriftführung, ``SET_NULL``) mit gesichertem Namen wie bei Anwesenheit und Protokolleinträgen (#591).
- TOP: ``notes_encrypted`` (formatierte Notizen der Sitzung, mit dem Organisationsschlüssel verschlüsselt) und
  ``notes_updated_at``.

Keine Datenmigration: Bestehende Protokolleinträge (auch Wortbeiträge), Beschlüsse, Aufgaben und Anwesenheiten
bleiben unverändert und werden in der neuen Ansicht weiter angezeigt. Rückweg ohne Wirkung auf Bestandsdaten.
"""

import apps.common.encryption
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0025_recht_dokumente_ehemaliger_mitglieder"),
        ("work", "0076_kommentar_vorschlag"),
    ]

    operations = [
        migrations.AddField(
            model_name="factionagendaitem",
            name="notes_encrypted",
            field=apps.common.encryption.EncryptedTextField(verbose_name="Notizen"),
        ),
        migrations.AddField(
            model_name="factionagendaitem",
            name="notes_updated_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Notizen geändert am"
            ),
        ),
        migrations.AddField(
            model_name="factionmeeting",
            name="chaired_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="chaired_faction_meetings",
                to="tenants.membership",
                verbose_name="Sitzungsleitung",
            ),
        ),
        migrations.AddField(
            model_name="factionmeeting",
            name="chaired_by_name_snapshot",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                max_length=301,
                verbose_name="Sitzungsleitung (Name, gesichert)",
            ),
        ),
        migrations.AddField(
            model_name="factionmeeting",
            name="minute_taker",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="minuted_faction_meetings",
                to="tenants.membership",
                verbose_name="Schriftführung",
            ),
        ),
        migrations.AddField(
            model_name="factionmeeting",
            name="minute_taker_name_snapshot",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                max_length=301,
                verbose_name="Schriftführung (Name, gesichert)",
            ),
        ),
        migrations.AddField(
            model_name="factionmeeting",
            name="started_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Gestartet um"
            ),
        ),
    ]
