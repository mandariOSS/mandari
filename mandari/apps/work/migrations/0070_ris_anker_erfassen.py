# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anker für alle bestehenden Verknüpfungen mit Tagesordnungspunkten und Vorlagen anlegen, je Organisation (Issue #547).

Legt nur die Zeilen an (Organisation, Art, Objekt); die fachliche Kennung erfasst der erste Abgleich im Worker aus
dem RIS-Bestand (``apps.work.ris.verknuepfungen``), damit die Migration kurz bleibt und keine Logik des Bestands
nachbilden muss. Wiederholbar: vorhandene Anker bleiben unverändert. Rückweg: nichts zu tun (ein älteres Image
kennt die Tabelle nicht).
"""

from django.db import migrations

#: Stand dieser Migration (Modell, Feld, Weg zur Organisation); spätere Verknüpfungen erfasst der Abgleich selbst.
TOP_FELDER = (
    ("AgendaItemPosition", "agenda_item", "organization_id"),
    ("AgendaPrivateNote", "agenda_item", "organization_id"),
    ("AgendaSpeechNote", "agenda_item", "organization_id"),
    ("AgendaItemNote", "agenda_item", "organization_id"),
    ("AgendaSupplementaryDocument", "agenda_item", "organization_id"),
    ("Task", "related_agenda_item", "organization_id"),
    ("FactionAgendaItem", "related_agenda_item", "meeting__organization_id"),
)
VORLAGEN_FELDER = (
    ("PaperComment", "paper", "organization_id"),
    ("AgendaSupplementaryDocument", "paper", "organization_id"),
    ("Motion", "related_paper", "organization_id"),
    ("Motion", "parent_paper", "organization_id"),
)
VORLAGEN_M2M = (("FactionAgendaItem", "related_papers", "meeting__organization_id"),)


def _paare(apps, felder, m2m=()):
    paare = set()
    for modell, feld, organisation in felder:
        model = apps.get_model("work", modell)
        spalte = model._meta.get_field(feld).attname
        werte = model._base_manager.filter(**{f"{spalte}__isnull": False}).values_list(organisation, spalte)
        paare.update(werte.distinct())
    for modell, feld, organisation in m2m:
        m2m_feld = apps.get_model("work", modell)._meta.get_field(feld)
        through = m2m_feld.remote_field.through
        werte = through._base_manager.values_list(
            f"{m2m_feld.m2m_field_name()}__{organisation}", f"{m2m_feld.m2m_reverse_field_name()}_id"
        )
        paare.update(werte.distinct())
    return {(org, objekt) for org, objekt in paare if org is not None and objekt is not None}


def anker_anlegen(apps, schema_editor):
    RisAnker = apps.get_model("work", "RisAnker")
    for art, paare in (
        ("top", _paare(apps, TOP_FELDER)),
        ("vorlage", _paare(apps, VORLAGEN_FELDER, VORLAGEN_M2M)),
    ):
        vorhanden = set(RisAnker.objects.filter(art=art).values_list("organization_id", "objekt"))
        RisAnker.objects.bulk_create(
            [
                RisAnker(organization_id=org, art=art, objekt=objekt, kennung={})
                for org, objekt in sorted(paare - vorhanden, key=str)
            ],
            batch_size=1000,
            ignore_conflicts=True,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0069_ris_anker"),
    ]

    operations = [
        migrations.RunPython(anker_anlegen, migrations.RunPython.noop),
    ]
