# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Recht „Dokumente ehemaliger Mitglieder einsehen“ für Administration und Fraktionsvorsitz.

Seit dem Entfernen eines Mitglieds Inhalte der Organisation erhalten bleiben (#420), sah private
Dokumente der Person niemand mehr. Das Recht ``motions.view_former_members`` macht sie (auch
Entwürfe) lesbar. Es bekommen: Rollen namens „Administrator“ oder „Fraktionsvorsitz“ (Standardrollen,
auch angepasste), Administrator-Rollen und Rollen mit ``organization.admin``.

Rein additiv und wiederholbar (M2M-``add`` legt keine Doppelten an); der Rückweg ändert nichts.
"""

from django.db import migrations
from django.db.models import Q

RECHT = "motions.view_former_members"
ROLLEN = ("Administrator", "Fraktionsvorsitz")


def recht_vergeben(apps, schema_editor):
    Permission = apps.get_model("tenants", "Permission")
    Role = apps.get_model("tenants", "Role")

    # Katalogeintrag immer anlegen: Neue Organisationen erhalten ihre Standardrollen nur mit
    # vorhandenen Rechten (Role.create_default_roles)
    recht, _ = Permission.objects.get_or_create(
        codename=RECHT, defaults={"name": "Dokumente ehemaliger Mitglieder einsehen", "category": "motions"}
    )
    rollen = Role.objects.filter(
        Q(name__in=ROLLEN) | Q(is_admin=True) | Q(permissions__codename="organization.admin")
    ).distinct()
    for rolle in rollen:
        rolle.permissions.add(recht)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0024_loeschrecht_fuer_leitungsrollen"),
    ]

    operations = [
        migrations.RunPython(recht_vergeben, migrations.RunPython.noop),
    ]
