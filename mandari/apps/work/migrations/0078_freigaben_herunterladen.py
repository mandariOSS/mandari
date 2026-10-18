# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Herunterladen je Freigabe abschaltbar, Downloads von Gästen in der Änderungshistorie (Issue #582).

Neue Spalten (rein additiv, Wahrheitswert mit Datenbank-Vorgabe ``True``):

- ``MotionShare.allow_download`` und ``FolderGuestShare.allow_download``: „Herunterladen erlauben“ – Gäste
  exportieren das Dokument als PDF oder DOCX und laden Anhänge herunter.
- Änderungshistorie: eine neue Aktion ``guest_download`` (nur Auswahlliste, keine Schemaänderung).

Bestand: Alle bestehenden Freigaben erhalten ``True`` – Gäste laden weiter herunter wie bisher. Eine ältere
Version (Rückfall) kennt die Spalten nicht und legt Freigaben dank der Datenbank-Vorgabe weiter an. Kein
bestehender Wert wird überschrieben oder gelöscht.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0077_sitzungsansicht"),
    ]

    operations = [
        migrations.AddField(
            model_name="folderguestshare",
            name="allow_download",
            field=models.BooleanField(
                db_default=True,
                default=True,
                help_text="Gäste dürfen das Dokument als PDF oder DOCX exportieren und Anhänge herunterladen.",
                verbose_name="Herunterladen erlauben",
            ),
        ),
        migrations.AddField(
            model_name="motionshare",
            name="allow_download",
            field=models.BooleanField(
                db_default=True,
                default=True,
                help_text="Gäste dürfen das Dokument als PDF oder DOCX exportieren und Anhänge herunterladen.",
                verbose_name="Herunterladen erlauben",
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
                    ("internal_document_access", "Nichtöffentliche Unterlage aufgerufen"),
                    (
                        "agenda_reminder_sent",
                        "Erinnerung zum Eintragen von TOPs versandt",
                    ),
                    ("protocol_sent", "Protokoll versandt"),
                    ("guest_download", "Dokument von Gast heruntergeladen"),
                ],
                max_length=50,
                verbose_name="Aktion",
            ),
        ),
    ]
