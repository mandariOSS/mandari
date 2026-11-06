# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsarten (Issue #1231, Epic #1226), Teil 2: Jede Organisation bekommt die Standardart „Fraktionssitzung“
(Teilnehmende alle Mitglieder, Sichtbarkeit Mitglieder, Standard-Tagesordnung, nichtöffentlicher Teil und
Abstimmungen an, Startnummer 1). Alle Reihen, Sitzungen und Standard-TOPs werden ihr zugeordnet; danach ist die
Spalte ``meeting_type`` Pflicht.

Kein Datenverlust: Es ändert sich nur die neue Spalte. Nummern, ``updated_at`` und ``organization.settings`` bleiben
unverändert (gesammelte ``UPDATE``-Anweisungen ohne ``save()`` und ohne Änderungshistorie). Die Fraktionssitzungen
zählen damit weiter wie bisher. Laufzeit: drei ``UPDATE``-Anweisungen und ein gesammeltes Anlegen, auch bei
Tausenden Sitzungen im Bereich von Sekunden.

PostgreSQL: Die drei Tabellen sind bis zum Ende der Migration gesperrt. Legt der alte Stand während des Deploys
eine Sitzung an, wartet er und scheitert danach an der Pflichtspalte, statt eine Zeile ohne Art zu hinterlassen.
Die Fremdschlüssel werden sofort geprüft; sonst scheitert das folgende ``ALTER TABLE`` an offenen Trigger-Ereignissen.

Rückwärts wird die Spalte wieder optional; die Zuordnung bleibt stehen, bis ``0083_sitzungsarten`` rückwärts Spalten
und Tabelle entfernt. Die Datenmigration ist idempotent: Sie legt je Organisation höchstens eine Standardart an und
ordnet nur Zeilen ohne Art zu.
"""

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import OuterRef, Subquery

STANDARDART = "Fraktionssitzung"
MODELLE = ("FactionMeetingSchedule", "FactionMeeting", "FactionStandardAgendaItem")


def zuordnen(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor == "postgresql":
        tabellen = ", ".join(connection.ops.quote_name(apps.get_model("work", name)._meta.db_table) for name in MODELLE)
        with connection.cursor() as cursor:
            cursor.execute(f"LOCK TABLE {tabellen} IN ACCESS EXCLUSIVE MODE")
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    organization_model = apps.get_model("tenants", "Organization")
    meeting_type_model = apps.get_model("work", "FactionMeetingType")
    vorhanden = set(meeting_type_model.objects.filter(is_default=True).values_list("organization_id", flat=True))
    meeting_type_model.objects.bulk_create(
        [
            meeting_type_model(
                organization_id=organization_id,
                name=STANDARDART,
                position=0,
                is_default=True,
                is_active=True,
                number_start=1,
                participants_mode="all",
                visibility="members",
                agenda_mode="standard",
                non_public_enabled=True,
                voting_enabled=True,
                settings={},
            )
            for organization_id in organization_model.objects.values_list("pk", flat=True)
            if organization_id not in vorhanden
        ],
        batch_size=500,
    )

    standardart = meeting_type_model.objects.filter(organization=OuterRef("organization"), is_default=True).values(
        "pk"
    )[:1]
    for name in MODELLE:
        apps.get_model("work", name).objects.filter(meeting_type__isnull=True).update(
            meeting_type=Subquery(standardart)
        )


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0083_sitzungsarten"),
    ]

    operations = [
        migrations.RunPython(zuordnen, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="factionmeeting",
            name="meeting_type",
            field=models.ForeignKey(
                blank=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="meetings",
                to="work.factionmeetingtype",
                verbose_name="Sitzungsart",
            ),
        ),
        migrations.AlterField(
            model_name="factionmeetingschedule",
            name="meeting_type",
            field=models.ForeignKey(
                blank=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="schedules",
                to="work.factionmeetingtype",
                verbose_name="Sitzungsart",
            ),
        ),
        migrations.AlterField(
            model_name="factionstandardagendaitem",
            name="meeting_type",
            field=models.ForeignKey(
                blank=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="standard_agenda_items",
                to="work.factionmeetingtype",
                verbose_name="Sitzungsart",
            ),
        ),
    ]
