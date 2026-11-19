# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neue Rechte für eigene Termine im Kalender von Work (Issue #1489).

``calendar.create`` (Termine anlegen, eigene ändern und löschen) bekommt jede Rolle, die schon Aufgaben anlegen darf;
``calendar.manage`` (alle Termine ändern und löschen) jede Rolle, die alle Aufgaben verwalten darf. Dazu wie immer
Administrator-Rollen und Rollen mit ``organization.admin``. So kann nach dem Update anlegen, wer bisher schon
gemeinsame Arbeit eintragen durfte; die Organisation ändert es in der Rollenverwaltung unter „Kalender“.

Rein additiv und wiederholbar (M2M-``add`` legt keine Doppelten an); der Rückweg ändert nichts.
"""

from django.db import migrations
from django.db.models import Q

RECHTE = {
    "calendar.create": ("Termine im Kalender anlegen", "tasks.create"),
    "calendar.manage": ("Alle Termine verwalten", "tasks.manage"),
}


def rechte_vergeben(apps, schema_editor):
    Permission = apps.get_model("tenants", "Permission")
    Role = apps.get_model("tenants", "Role")

    for codename, (name, wie) in RECHTE.items():
        # Katalogeintrag immer anlegen: Neue Organisationen erhalten ihre Standardrollen nur mit vorhandenen Rechten
        recht, _ = Permission.objects.get_or_create(codename=codename, defaults={"name": name, "category": "calendar"})
        rollen = Role.objects.filter(
            Q(permissions__codename=wie) | Q(is_admin=True) | Q(permissions__codename="organization.admin")
        ).distinct()
        for rolle in rollen:
            rolle.permissions.add(recht)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0031_demo_laufzeit"),
    ]

    operations = [
        migrations.RunPython(rechte_vergeben, migrations.RunPython.noop),
    ]
