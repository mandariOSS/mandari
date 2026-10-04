# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Genehmigungsweg an der Niederschrift festhalten (Issue #525).

Bisher leitete die Abbildung „ohne Genehmigungsschritt veröffentlicht“ aus gleichen Zeitpunkten von Genehmigung und
Veröffentlichung ab. Nach Rücknahme und erneuter Veröffentlichung ändert sich aber nur ``published_at``; die
Schnittstelle meldete dann „genehmigt in der Folgesitzung“. Das neue Feld setzt der Workflow beim Genehmigen bzw.
beim Veröffentlichen aus der Prüfung.

- Spalte mit DB-Default ``""``: abwärtskompatibel, ein Image ohne das Feld schreibt weiter.
- Befüllen bestehender genehmigter Niederschriften: gleiche Zeitpunkte oder ein Audit-Eintrag „veröffentlicht aus
  der Prüfung“ heißt ohne Genehmigungsschritt, sonst Folgesitzung (wie bisher ausgegeben). Idempotent (nur leere
  Felder) und ``elidable``: Auf einer neuen Installation gibt es nichts zu befüllen; die Abbildung leitet den Weg
  ohne Angabe ohnehin aus den Zeitpunkten ab. Rückwärts nichts, die Spalte entfernt der Rückbau des Feldes.
"""

from django.db import migrations, models

FOLLOW_UP = "follow_up"
DIRECT = "direct"


def befuellen(apps, schema_editor):
    protocol_model = apps.get_model("session", "SessionProtocol")
    audit_model = apps.get_model("session", "SessionAuditLog")
    offen = list(
        protocol_model.objects.filter(approved_at__isnull=False, approval_mode="").only(
            "pk", "approved_at", "published_at"
        )
    )
    if not offen:
        return
    # Veröffentlichung ohne Genehmigungsschritt: Statuswechsel Prüfung -> veröffentlicht (Aktion „publish“). Ohne
    # Filter auf die Kennungen: keine lange IN-Liste, es sind höchstens so viele Einträge wie Veröffentlichungen.
    direkt = {
        str(object_id)
        for object_id, changes in audit_model.objects.filter(
            model_name="SessionProtocol", action="publish"
        ).values_list("object_id", "changes")
        if isinstance(changes, dict)
        and isinstance(changes.get("status"), dict)
        and changes["status"].get("alt") == "review"
    }
    for protocol in offen:
        gleich = protocol.published_at is not None and protocol.published_at == protocol.approved_at
        protocol.approval_mode = DIRECT if gleich or str(protocol.pk) in direkt else FOLLOW_UP
    protocol_model.objects.bulk_update(offen, ["approval_mode"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0066_ereignisse_drehscheibe"),
    ]

    operations = [
        migrations.AddField(
            model_name="sessionprotocol",
            name="approval_mode",
            field=models.CharField(
                blank=True,
                choices=[
                    ("follow_up", "Genehmigung in der Folgesitzung"),
                    ("direct", "Direkte Veröffentlichung ohne Genehmigungsschritt"),
                ],
                db_default="",
                default="",
                max_length=20,
                verbose_name="Genehmigungsweg",
            ),
        ),
        migrations.RunPython(befuellen, migrations.RunPython.noop, elidable=True),
    ]
