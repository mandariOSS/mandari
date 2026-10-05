# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ordner „Nichtöffentliche Vorgänge“ im Dokumentenspeicher (Issue #873).

1. ``DocumentFolder.sworn_in_only``: neue Spalte mit Datenbank-Standardwert ``false``. Auf PostgreSQL ist
   ``ADD COLUMN … DEFAULT false NOT NULL`` ohne Umschreiben der Tabelle; eine ältere Version (Rückfall ohne
   Migrationsrückbau) legt Ordner ohne die Spalte an und erhält den Standardwert.
2. Höchstens ein solcher Ordner je Organisation (Teilindex über gesetzte Zeilen; die Tabelle ist klein).
3. Zwei neue Aktionen der Änderungshistorie (nur Auswahlliste, keine Datenbankänderung).

Rein additiv: Bestehende Ordner, Dokumente und Einträge bleiben unverändert.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0025_recht_dokumente_ehemaliger_mitglieder"),
        ("work", "0069_ris_anker"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentfolder",
            name="sworn_in_only",
            field=models.BooleanField(
                db_default=False, default=False, editable=False, verbose_name="Nur für vereidigte Mitglieder"
            ),
        ),
        migrations.AddConstraint(
            model_name="documentfolder",
            constraint=models.UniqueConstraint(
                condition=models.Q(("sworn_in_only", True)),
                fields=("organization",),
                name="uniq_document_folder_sworn_in_only",
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
                ],
                max_length=50,
                verbose_name="Aktion",
            ),
        ),
    ]
