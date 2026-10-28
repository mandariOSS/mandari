# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Live-Mitschreiben und @-Erwähnen im Protokoll der Fraktionssitzung (Issue #874).

Neue Tabelle für den verschlüsselten Zustand des gemeinsamen Editors je TOP und die Benachrichtigungsart
„Erwähnung im Protokoll“ (nur die Auswahlliste). Rein additiv, rückwärts lauffähig: Ohne die Tabelle bauen die
Editoren den Zustand aus den Notizen am TOP neu auf; die Notizen selbst bleiben unverändert.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0081_reihe_bearbeiten_erinnerung"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="notification_type",
            field=models.CharField(
                choices=[
                    ("meeting_reminder", "Sitzungserinnerung"),
                    ("meeting_updated", "Sitzung aktualisiert"),
                    ("meeting_cancelled", "Sitzung abgesagt"),
                    ("task_assigned", "Aufgabe zugewiesen"),
                    ("task_due_soon", "Aufgabe fällig"),
                    ("task_completed", "Aufgabe erledigt"),
                    ("task_comment", "Kommentar zur Aufgabe"),
                    ("motion_shared", "Antrag geteilt"),
                    ("motion_comment", "Kommentar zum Antrag"),
                    ("motion_status", "Antragsstatus geändert"),
                    ("motion_assigned", "Dokument zugewiesen"),
                    ("motion_due_soon", "Dokument fällig"),
                    ("motion_approval_req", "Freigabe angefragt"),
                    ("motion_approval_dec", "Freigabe entschieden"),
                    ("faction_reminder", "Fraktionssitzung"),
                    ("faction_updated", "Fraktionssitzung aktualisiert"),
                    ("faction_inv_release", "Einladungsfreigabe erforderlich"),
                    ("faction_invitation", "Einladung zur Fraktionssitzung"),
                    ("faction_prop_decided", "TOP-Vorschlag entschieden"),
                    ("faction_prot_approved", "Fraktionsprotokoll genehmigt"),
                    ("faction_mention", "Erwähnung im Protokoll"),
                    ("member_joined", "Neues Mitglied"),
                    ("registration_request", "Registrierungsanfrage"),
                    ("role_changed", "Rolle geändert"),
                    ("support_created", "Support-Ticket erstellt"),
                    ("support_reply", "Neue Antwort auf Ticket"),
                    ("support_status", "Ticketstatus geändert"),
                    ("support_resolved", "Ticket gelöst"),
                    ("support_escalated", "Ticket eskaliert"),
                    ("change_request_new", "Neuer Änderungsantrag"),
                    ("change_request_decided", "Änderungsantrag entschieden"),
                    ("absence_deputy", "Stellvertretung zugewiesen"),
                    ("system", "Systemnachricht"),
                    ("announcement", "Ankündigung"),
                ],
                default="system",
                max_length=30,
                verbose_name="Typ",
            ),
        ),
        migrations.CreateModel(
            name="FactionNotesState",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "yjs_encrypted",
                    models.BinaryField(verbose_name="Editor-Zustand (verschlüsselt)"),
                ),
                (
                    "stand",
                    models.CharField(
                        max_length=16,
                        verbose_name="Fingerabdruck der zugehörigen Notizen",
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "item",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notizen_zustand",
                        to="work.factionagendaitem",
                        verbose_name="TOP",
                    ),
                ),
            ],
            options={
                "verbose_name": "Zustand der Live-Notizen",
                "verbose_name_plural": "Zustände der Live-Notizen",
            },
        ),
    ]
