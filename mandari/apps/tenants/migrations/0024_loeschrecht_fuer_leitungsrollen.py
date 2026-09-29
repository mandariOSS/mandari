# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rollen mit „Alle Anträge bearbeiten“ erhalten „Anträge anderer löschen“.

Das Löschen fremder Dokumente verlangt jetzt ``motions.delete``; bisher genügte für das endgültige
Löschen ``motions.edit_all``. Damit Rollen mit diesem Recht (Vorsitz, Stellv. Vorsitz,
Geschäftsführung und angepasste Rollen) ihr bisheriges Verhalten behalten, bekommen alle Rollen mit
``motions.edit_all`` zusätzlich ``motions.delete``.

Rein additiv und wiederholbar (M2M-``add`` legt keine Doppelten an); der Rückweg ändert nichts, weil
sich nicht unterscheiden lässt, welche Rollen das Recht vorher schon hatten.
"""

from django.db import migrations


def loeschrecht_ergaenzen(apps, schema_editor):
    Permission = apps.get_model("tenants", "Permission")
    Role = apps.get_model("tenants", "Role")

    if not Role.objects.filter(permissions__codename="motions.edit_all").exists():
        return
    loeschen, _ = Permission.objects.get_or_create(
        codename="motions.delete", defaults={"name": "Anträge anderer löschen", "category": "motions"}
    )
    for rolle in Role.objects.filter(permissions__codename="motions.edit_all").distinct():
        rolle.permissions.add(loeschen)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0023_zugangstoken_als_hash"),
    ]

    operations = [
        migrations.RunPython(loeschrecht_ergaenzen, migrations.RunPython.noop),
    ]
