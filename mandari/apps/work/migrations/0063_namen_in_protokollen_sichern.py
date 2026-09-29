# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Namen in Protokollen, Anwesenheit und Teilnahmebestätigungen dauerhaft sichern (Issue #591).

Seit #420 bleiben Fraktionssitzungen beim Entfernen eines Mitglieds erhalten, der Verweis auf die
Mitgliedschaft wird aber geleert. Neue Felder halten den Namen fest; die Anwendung setzt sie beim
Erfassen und spätestens vor dem Entfernen der Mitgliedschaft.

Abwärtskompatibel: neue Spalten mit Datenbank-Vorgabe (ein älteres Image legt Einträge weiter an).
Die Datenübernahme füllt nur leere Namen zu noch vorhandenen Mitgliedschaften und ist damit
wiederholbar. Schon entfernte Mitglieder lassen sich nicht mehr zuordnen; dort bleibt es bei
„Ehemaliges Mitglied“.
"""

from django.db import migrations, models

#: formatting.MEMBER_NAME_MAX_LENGTH, Stand dieser Migration
NAME_MAX_LENGTH = 301

#: Modell → (Verweis auf die Mitgliedschaft, Feld mit dem gesicherten Namen)
SNAPSHOTS = {
    "FactionAttendance": (
        ("membership", "member_name_snapshot"),
        ("confirmed_final_by", "confirmed_final_by_name_snapshot"),
    ),
    "FactionProtocolEntry": (
        ("speaker", "speaker_name_snapshot"),
        ("action_assignee", "action_assignee_name_snapshot"),
    ),
}


def display_name(first_name: str, last_name: str, email: str) -> str:
    """User.get_display_name(), Stand dieser Migration: Vor- und Nachname, sonst der Teil vor dem @."""
    if first_name or last_name:
        return f"{first_name} {last_name}".strip()[:NAME_MAX_LENGTH]
    return (email or "").split("@")[0][:NAME_MAX_LENGTH]


def namen_sichern(apps, schema_editor):
    Membership = apps.get_model("tenants", "Membership")
    for model_name, pairs in SNAPSHOTS.items():
        model = apps.get_model("work", model_name)
        for fk, snapshot_field in pairs:
            offen = model.objects.filter(**{f"{fk}__isnull": False, snapshot_field: ""})
            member_ids = set(offen.order_by().values_list(f"{fk}_id", flat=True))
            if not member_ids:
                continue
            members = Membership.objects.filter(pk__in=member_ids).values_list(
                "pk", "user__first_name", "user__last_name", "user__email"
            )
            for member_id, first_name, last_name, email in members.iterator():
                offen.filter(**{f"{fk}_id": member_id}).update(
                    **{snapshot_field: display_name(first_name, last_name, email)}
                )


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0062_aufgaben_herkunft_protokolleintrag"),
    ]

    operations = [
        migrations.AddField(
            model_name="factionattendance",
            name="confirmed_final_by_name_snapshot",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                max_length=301,
                verbose_name="Final bestätigt von (Name, gesichert)",
            ),
        ),
        migrations.AddField(
            model_name="factionattendance",
            name="member_name_snapshot",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                max_length=301,
                verbose_name="Name des Mitglieds (gesichert)",
            ),
        ),
        migrations.AddField(
            model_name="factionprotocolentry",
            name="action_assignee_name_snapshot",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                max_length=301,
                verbose_name="Verantwortlich (Name, gesichert)",
            ),
        ),
        migrations.AddField(
            model_name="factionprotocolentry",
            name="speaker_name_snapshot",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                max_length=301,
                verbose_name="Redner (Name, gesichert)",
            ),
        ),
        migrations.RunPython(namen_sichern, migrations.RunPython.noop),
    ]
