# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bestand den Körperschaften zuordnen (Issue #756) – ohne Verhaltensänderung.

1. Jeder Mandant ohne Standardkörperschaft bekommt genau eine – aus Name, Kurzname, Art und AGS des Mandanten,
   mit dessen Kurzkennung (``slug``).
2. Alle Gremien ohne Körperschaft gehören danach zur Standardkörperschaft ihres Mandanten, alle Vorlagen ohne
   Körperschaft zur Körperschaft ihres federführenden Gremiums, sonst ebenfalls zur Standardkörperschaft.

Ohne Änderungszeitpunkt (``update()``): OParl-Listen, Änderungsfeed und Bürgerportal sehen keine Änderung.
Idempotent: Ein zweiter Lauf findet nichts mehr. Dieselbe Zuordnung läuft nach jedem ``migrate`` für Nachzügler
eines älteren Images (``body_service.assign_missing``); hier steht eine eingefrorene Kopie, damit die Migration
nicht vom späteren Stand des Dienstes abhängt. Rückwärts ist nichts zu tun (die Spalten entfallen mit 0056).
Nicht ``elidable``: Beim Zusammenfassen der Migrationen muss der Schritt erhalten bleiben.
"""

from django.db import migrations
from django.db.models import OuterRef, Subquery
from django.utils.text import slugify


def _freie_kennung(Body, tenant_id, basis):
    basis = (slugify(basis) or "koerperschaft")[:90]
    belegt = set(Body.objects.filter(tenant_id=tenant_id, slug__startswith=basis).values_list("slug", flat=True))
    kennung, n = basis, 1
    while kennung in belegt:
        n += 1
        kennung = f"{basis}-{n}"
    return kennung


def zuordnen(apps, schema_editor):
    Tenant = apps.get_model("session", "SessionTenant")
    Body = apps.get_model("session", "SessionBody")
    Organization = apps.get_model("session", "SessionOrganization")
    Paper = apps.get_model("session", "SessionPaper")

    mit_standard = Body.objects.filter(is_default=True).values("tenant_id")
    for tenant in Tenant.objects.exclude(pk__in=mit_standard).iterator():
        Body.objects.create(
            tenant_id=tenant.pk,
            is_default=True,
            slug=_freie_kennung(Body, tenant.pk, tenant.slug),
            name=tenant.name,
            short_name=tenant.short_name or "",
            body_type=tenant.body_type or "",
            ags=tenant.ags or "",
        )

    standard = dict(Body.objects.filter(is_default=True).values_list("tenant_id", "pk"))
    for tenant_id in set(Organization.objects.filter(body__isnull=True).values_list("tenant_id", flat=True)):
        Organization.objects.filter(tenant_id=tenant_id, body__isnull=True).update(body_id=standard[tenant_id])

    federfuehrend = Organization.objects.filter(pk=OuterRef("main_organization_id")).values("body_id")[:1]
    Paper.objects.filter(body__isnull=True, main_organization__body__isnull=False).update(
        body_id=Subquery(federfuehrend)
    )
    for tenant_id in set(Paper.objects.filter(body__isnull=True).values_list("tenant_id", flat=True)):
        Paper.objects.filter(tenant_id=tenant_id, body__isnull=True).update(body_id=standard[tenant_id])


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0056_koerperschaften"),
    ]

    operations = [
        migrations.RunPython(zuordnen, migrations.RunPython.noop),
    ]
