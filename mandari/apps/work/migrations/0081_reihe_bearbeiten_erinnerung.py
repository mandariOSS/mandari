# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsreihe bearbeiten, Erinnerung an die Sitzung, gelöschte Termine (Issue #1071): Ergebnis der Erinnerung je
Person, Ausnahmeart „Gelöscht“ mit Namen der löschenden Person und Audit-Aktion „Sitzungsreihe geändert“. Rein
additiv mit Datenbank-Vorgaben, rückwärts lauffähig (nur die neuen Spalten fallen weg).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0080_offene_erinnern"),
    ]

    operations = [
        migrations.AddField(
            model_name="factionattendance",
            name="reminder_error",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                max_length=300,
                verbose_name="Erinnerung an die Sitzung nicht verschickt, Grund",
            ),
        ),
        migrations.AddField(
            model_name="factionattendance",
            name="reminder_mailed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="An die Sitzung erinnert am"),
        ),
        migrations.AddField(
            model_name="factionmeetingexception",
            name="created_by_name",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                max_length=200,
                verbose_name="Angelegt von",
            ),
        ),
        migrations.AlterField(
            model_name="factionauditlog",
            name="action",
            field=models.CharField(
                choices=[
                    ("create", "Erstellt"),
                    ("update", "Geändert"),
                    ("delete", "Gelöscht"),
                    ("status", "Statuswechsel"),
                    ("invitation_sent", "Einladung versandt"),
                    ("invitation_updated", "Aktualisierte Einladung versandt"),
                    ("reminder_sent", "Erinnerung versandt"),
                    ("protocol_submitted", "Protokoll zur Genehmigung"),
                    ("protocol_approved", "Protokoll genehmigt"),
                    ("participation", "Teilnahme geändert"),
                    ("proposal", "TOP vorgeschlagen"),
                    ("proposal_accepted", "TOP-Vorschlag angenommen"),
                    ("proposal_rejected", "TOP-Vorschlag abgelehnt"),
                    ("decision", "Abstimmung erfasst"),
                    ("generated", "Automatisch erzeugt"),
                    ("auto_cancelled", "Automatisch entfallen"),
                    ("invitation_released", "Einladungsversand freigegeben"),
                    ("release_notice_sent", "Freigabe-Hinweis versandt"),
                    ("addendum", "Nachtrag erfasst"),
                    ("attendance_confirmed", "Teilnahmen bestätigt"),
                    ("certificate_issued", "Teilnahmenachweis ausgestellt"),
                    ("attendance_exported", "Teilnahmen-Sammel-Export erstellt"),
                    ("api_settings_changed", "Öffentliche API konfiguriert"),
                    ("internal_document_stored", "Nichtöffentliche Unterlage abgelegt"),
                    (
                        "internal_document_access",
                        "Nichtöffentliche Unterlage aufgerufen",
                    ),
                    (
                        "agenda_reminder_sent",
                        "Erinnerung zum Eintragen von TOPs versandt",
                    ),
                    ("protocol_sent", "Protokoll versandt"),
                    ("schedule_changed", "Sitzungsreihe geändert"),
                    ("guest_download", "Dokument von Gast heruntergeladen"),
                ],
                max_length=50,
                verbose_name="Aktion",
            ),
        ),
        migrations.AlterField(
            model_name="factionmeetingexception",
            name="exception_type",
            field=models.CharField(
                choices=[
                    ("cancelled", "Abgesagt"),
                    ("rescheduled", "Verschoben"),
                    ("special", "Sondertermin"),
                    ("deleted", "Gelöscht"),
                ],
                max_length=20,
                verbose_name="Art",
            ),
        ),
    ]
