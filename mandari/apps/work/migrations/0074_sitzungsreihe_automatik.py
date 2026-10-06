# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsreihe und Automatik für Fraktionssitzungen (Issue #871).

Neue Spalten (rein additiv, Wahrheitswerte mit Datenbank-Vorgabe ``False``, damit ein älteres Abbild
weiter Sitzungen und Reihen anlegen kann):

- Reihe: ``rsvp_enabled`` (Zu- und Absagen), ``auto_invite`` mit Wochentag und Uhrzeit (automatische
  Einladung zum festen Zeitpunkt), ``generated_until`` (bis wohin die Reihe Termine angelegt hat).
- Sitzung: ``rsvp_enabled``, ``agenda_reminder_sent_at`` (Erinnerung zum Eintragen von TOPs),
  ``protocol_sent_at`` (automatischer Protokollversand), ``invitation_claimed_at`` (Erstversand
  beansprucht; hängende Ansprüche gibt der Einladungslauf nach einer Frist frei).
- Änderungshistorie: zwei neue Aktionen (nur Auswahlliste, keine Schemaänderung).

Bestand (Entscheidung vom 05.10.2026: Zu- und Absagen sind auch für bestehende Organisationen und Reihen
aus): Reihen und Sitzungen starten mit ``rsvp_enabled = False``. Ausgenommen sind kommende Sitzungen, deren
Einladung schon verschickt ist – die Mail hat um Zu- oder Absage gebeten, das bleibt bis zur Sitzung möglich.
``generated_until`` übernimmt je Reihe den spätesten schon angelegten Solltermin, damit vor dem Update
gelöschte Termine nicht zurückkommen. Kein bestehender Wert wird überschrieben oder gelöscht; die
Zu- und Absagen selbst (Status der Teilnahmen) bleiben unverändert. Wiederholbar, Rückweg ohne Wirkung.
"""

from django.db import migrations, models
from django.db.models import Max
from django.utils import timezone


def bestand_uebernehmen(apps, schema_editor):
    """Erzeugt-bis je Reihe setzen; kommende, schon eingeladene Sitzungen behalten Zu- und Absagen."""
    schedule_model = apps.get_model("work", "FactionMeetingSchedule")
    meeting_model = apps.get_model("work", "FactionMeeting")

    letzte = (
        meeting_model.objects.filter(schedule__isnull=False, scheduled_date__isnull=False)
        .order_by()
        .values("schedule_id")
        .annotate(letzter=Max("scheduled_date"))
    )
    for zeile in letzte:
        schedule_model.objects.filter(pk=zeile["schedule_id"], generated_until__isnull=True).update(
            generated_until=zeile["letzter"]
        )

    meeting_model.objects.filter(invitation_sent=True, start__gt=timezone.now()).exclude(
        status__in=["completed", "cancelled"]
    ).update(rsvp_enabled=True)


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0073_standard_tagesordnung"),
    ]

    operations = [
        migrations.AddField(
            model_name="factionmeeting",
            name="agenda_reminder_sent_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="TOP-Erinnerung versandt am"),
        ),
        migrations.AddField(
            model_name="factionmeeting",
            name="invitation_claimed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Einladungsversand beansprucht am"),
        ),
        migrations.AddField(
            model_name="factionmeeting",
            name="protocol_sent_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Protokoll versandt am"),
        ),
        migrations.AddField(
            model_name="factionmeeting",
            name="rsvp_enabled",
            field=models.BooleanField(db_default=False, default=False, verbose_name="Zu- und Absagen"),
        ),
        migrations.AddField(
            model_name="factionmeetingschedule",
            name="auto_invite",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Einladungen gehen zum festen Zeitpunkt ohne Freigabe raus, mit der Tagesordnung von diesem Zeitpunkt",
                verbose_name="Automatisch einladen",
            ),
        ),
        migrations.AddField(
            model_name="factionmeetingschedule",
            name="auto_invite_time",
            field=models.TimeField(blank=True, null=True, verbose_name="Einladung um"),
        ),
        migrations.AddField(
            model_name="factionmeetingschedule",
            name="auto_invite_weekday",
            field=models.PositiveSmallIntegerField(
                blank=True,
                choices=[
                    (0, "Montag"),
                    (1, "Dienstag"),
                    (2, "Mittwoch"),
                    (3, "Donnerstag"),
                    (4, "Freitag"),
                    (5, "Samstag"),
                    (6, "Sonntag"),
                ],
                null=True,
                verbose_name="Einladung am Wochentag",
            ),
        ),
        migrations.AddField(
            model_name="factionmeetingschedule",
            name="generated_until",
            field=models.DateField(blank=True, null=True, verbose_name="Termine erzeugt bis"),
        ),
        migrations.AddField(
            model_name="factionmeetingschedule",
            name="rsvp_enabled",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Erzeugte Sitzungen sammeln Zu- und Absagen",
                verbose_name="Zu- und Absagen",
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
                ],
                max_length=50,
                verbose_name="Aktion",
            ),
        ),
        migrations.RunPython(bestand_uebernehmen, migrations.RunPython.noop, elidable=True),
    ]
