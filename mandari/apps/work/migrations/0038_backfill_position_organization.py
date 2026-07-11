# SPDX-License-Identifier: EUPL-1.2
"""Backfill: AgendaItemPosition.organization aus der Altlast-Verknüpfung.

Alt-Positionen (vor dem Sitzungsvorbereitungs-Umbau) hängen nur am
``preparation``-FK und haben ``organization=NULL`` — die neuen, org-
gefilterten Lese-Pfade finden sie dadurch nicht (Symptom: „alle
Positionen weg"). Diese Migration setzt ``organization`` aus
``preparation.organization``.

Kollisionsregel: Existiert für (organization, agenda_item) bereits eine
NEUE Zeile (nach dem Umbau angelegt), bleibt diese führend; steht sie
aber noch auf „open", während die Alt-Zeile eine echte Position trägt,
werden die Alt-Werte in die neue Zeile übernommen. Die Alt-Zeile wird in
beiden Fällen gelöscht (verwaiste Duplikate vermeiden). Idempotent.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    AgendaItemPosition = apps.get_model("work", "AgendaItemPosition")

    null_rows = AgendaItemPosition.objects.filter(organization__isnull=True).select_related("preparation")

    fixed = merged = dropped = 0
    for row in null_rows.iterator():
        prep = row.preparation
        if prep is None or prep.organization_id is None:
            continue
        existing = (
            AgendaItemPosition.objects.filter(
                organization_id=prep.organization_id,
                agenda_item_id=row.agenda_item_id,
            )
            .exclude(pk=row.pk)
            .first()
        )
        if existing is None:
            row.organization_id = prep.organization_id
            row.save(update_fields=["organization"])
            fixed += 1
            continue
        # Kollision: neue Zeile führt; echte Alt-Position ggf. übernehmen.
        if existing.position == "open" and row.position != "open":
            existing.position = row.position
            existing.is_final = row.is_final
            existing.save(update_fields=["position", "is_final"])
            merged += 1
        row.delete()
        dropped += 1
    print(
        f"\n  Positionen-Backfill: {fixed} zugeordnet, {merged} in neue Zeile übernommen, {dropped} Duplikate entfernt."
    )


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0037_notizen_zu_vorgang_kommentaren"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
