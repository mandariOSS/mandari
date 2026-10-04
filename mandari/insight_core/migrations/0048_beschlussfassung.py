# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlussfassung im RIS-Bestand (Issue #525): Beschlussnummer, Abstimmung, Einzelstimmen und Umsetzungsstand am
Tagesordnungspunkt, Genehmigung der Niederschrift an der Sitzung. Alle Spalten nullable ohne Standardwert:
abwärtskompatibel, ein Image ohne diese Spalten schreibt weiter.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0047_texterkennung_abbrueche"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlagendaitem",
            name="implementation_deadline",
            field=models.DateField(blank=True, null=True, verbose_name="Erledigungsfrist"),
        ),
        migrations.AddField(
            model_name="oparlagendaitem",
            name="implementation_modified",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Umsetzungsstand geändert am"),
        ),
        migrations.AddField(
            model_name="oparlagendaitem",
            name="implementation_note",
            field=models.TextField(
                blank=True,
                null=True,
                verbose_name="Öffentliche Statusmeldung zur Umsetzung",
            ),
        ),
        migrations.AddField(
            model_name="oparlagendaitem",
            name="implementation_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("open", "Offen"),
                    ("in_progress", "In Umsetzung"),
                    ("done", "Erledigt"),
                    ("deferred", "Zurückgestellt"),
                ],
                max_length=20,
                null=True,
                verbose_name="Umsetzungsstand",
            ),
        ),
        migrations.AddField(
            model_name="oparlagendaitem",
            name="resolution_number",
            field=models.CharField(blank=True, max_length=100, null=True, verbose_name="Beschlussnummer"),
        ),
        migrations.AddField(
            model_name="oparlagendaitem",
            name="roll_call",
            field=models.JSONField(
                blank=True,
                help_text="Nur bei namentlicher Abstimmung: Liste aus name und vote.",
                null=True,
                verbose_name="Einzelstimmen",
            ),
        ),
        migrations.AddField(
            model_name="oparlagendaitem",
            name="vote_method",
            field=models.CharField(
                blank=True,
                choices=[
                    ("summary", "Nur Summen"),
                    ("open", "Offen (einzeln erfasst)"),
                    ("roll_call", "Namentlich"),
                    ("secret", "Geheim"),
                ],
                max_length=20,
                null=True,
                verbose_name="Abstimmungsart",
            ),
        ),
        migrations.AddField(
            model_name="oparlagendaitem",
            name="vote_result",
            field=models.CharField(
                blank=True,
                choices=[
                    ("approved", "Angenommen"),
                    ("rejected", "Abgelehnt"),
                    ("deferred", "Vertagt"),
                    ("withdrawn", "Zurückgezogen"),
                    ("noted", "Zur Kenntnis genommen"),
                ],
                max_length=20,
                null=True,
                verbose_name="Ergebnis der Abstimmung",
            ),
        ),
        migrations.AddField(
            model_name="oparlagendaitem",
            name="votes_abstain",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="Enthaltungen"),
        ),
        migrations.AddField(
            model_name="oparlagendaitem",
            name="votes_no",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="Nein-Stimmen"),
        ),
        migrations.AddField(
            model_name="oparlagendaitem",
            name="votes_yes",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="Ja-Stimmen"),
        ),
        migrations.AddField(
            model_name="oparlmeeting",
            name="protocol_approval_mode",
            field=models.CharField(
                blank=True,
                choices=[
                    ("follow_up", "Genehmigt in der Folgesitzung"),
                    ("direct", "Ohne Genehmigungsschritt veröffentlicht"),
                ],
                max_length=20,
                null=True,
                verbose_name="Genehmigungsweg der Niederschrift",
            ),
        ),
        migrations.AddField(
            model_name="oparlmeeting",
            name="protocol_approved_in_external_id",
            field=models.TextField(
                blank=True,
                help_text="OParl-Kennung (URL) der genehmigenden Sitzung.",
                null=True,
                verbose_name="Genehmigt in der Sitzung",
            ),
        ),
        migrations.AddField(
            model_name="oparlmeeting",
            name="protocol_approved_on",
            field=models.DateField(blank=True, null=True, verbose_name="Niederschrift genehmigt am"),
        ),
    ]
