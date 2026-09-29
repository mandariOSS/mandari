# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Benachrichtigungsarten zurücksetzen, die das Einstellungsformular nie angezeigt hat (Issue #423).

Bis zu diesem Stand schrieb das Speichern der Benachrichtigungseinstellungen alle Arten, auch neun,
die das Formular nicht zeigte. Deren Checkboxen fehlten im POST, die Arten wurden damit für jeden
abgeschaltet, der einmal gespeichert hatte. Die Migration entfernt diese Einträge aus
``type_settings``; es gilt wieder der Standard (an; Registrierungsanfragen sind immer aktiv).

Wiederholbar: Einträge, die schon fehlen, bleiben unberührt. Rückwärts ändert sie nichts; eine ältere
Version liest fehlende Einträge ebenfalls als „an“.
"""

from django.db import migrations

#: Arten, die das Formular bis Issue #423 nicht angezeigt hat (fest eingefroren, nicht aus dem Modell)
NIE_ANGEZEIGT = (
    "motion_assigned",
    "motion_due_soon",
    "motion_approval_req",
    "motion_approval_dec",
    "faction_inv_release",
    "faction_invitation",
    "faction_prop_decided",
    "faction_prot_approved",
    "registration_request",
)


def bereinigt(type_settings):
    """``type_settings`` ohne die nie angezeigten Arten, oder ``None``, wenn nichts zu tun ist."""
    if not isinstance(type_settings, dict) or not any(key in type_settings for key in NIE_ANGEZEIGT):
        return None
    return {key: value for key, value in type_settings.items() if key not in NIE_ANGEZEIGT}


def zuruecksetzen(apps, schema_editor):
    model = apps.get_model("work", "NotificationPreference")
    geaendert = []
    for pk, type_settings in model.objects.values_list("pk", "type_settings").iterator(chunk_size=500):
        neu = bereinigt(type_settings)
        if neu is not None:
            geaendert.append(model(pk=pk, type_settings=neu))
    model.objects.bulk_update(geaendert, ["type_settings"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0059_mitglied_entfernen_inhalte_erhalten"),
    ]

    operations = [
        migrations.RunPython(zuruecksetzen, migrations.RunPython.noop),
    ]
