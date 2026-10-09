# SPDX-License-Identifier: AGPL-3.0-or-later
"""Eine Position je Organisation und TOP (#926).

Seit der org-weiten Vorbereitung (work.0030) fehlte die Eindeutigkeit für ``AgendaItemPosition``. Zwei gleichzeitige
Erst-Saves zum selben TOP konnten zwei Zeilen anlegen; danach scheiterte jedes Speichern mit
``MultipleObjectsReturned``.

Vor dem Setzen der Bedingung werden vorhandene Dubletten verlustfrei zusammengeführt:

- Erhalten bleibt die zuletzt geänderte Zeile (``updated_at``, dann ``created_at``).
- Felder, die dort leer sind (Position „open“, Begründung, Ergebnis, „Gesetzt von“, Altlast-Vorbereitung), werden aus
  der jüngsten anderen Zeile übernommen, die einen Wert trägt. „Endgültig“ folgt der Zeile, aus der die Position stammt.
- ``created_at`` wird der früheste Anlagezeitpunkt der Gruppe, ``updated_at`` bleibt der der erhaltenen Zeile.
- Die Begründung ist mit dem Schlüssel der Organisation verschlüsselt (ohne Bindung an die Zeile); da alle Zeilen einer
  Gruppe zur selben Organisation gehören, wird der Geheimtext unverändert übernommen.

Auf ``AgendaItemPosition`` zeigen keine Fremdschlüssel, es muss also nichts umgehängt werden. Zeilen ohne Organisation
(Altlast vor work.0038) fallen nicht unter die Bedingung und bleiben unberührt. Produktion hatte am 07.10.2026 keine
Dubletten, Staging am 10.10.2026 ebenfalls nicht.
"""

from django.db import migrations, models
from django.db.models import Count


def _leer(feld: str, wert) -> bool:
    if feld == "position" and wert == "open":
        return True
    # Begründung ist ein BinaryField (bytes bzw. memoryview), die übrigen Felder Text oder Fremdschlüssel-IDs
    return wert is None or (isinstance(wert, (str, bytes, memoryview)) and len(wert) == 0)


# Felder, die bei einer Dublette aus den anderen Zeilen ergänzt werden (Reihenfolge egal)
UEBERNEHMEN = ("position", "reasoning_encrypted", "outcome", "set_by_id", "preparation_id")


def zusammenfuehren(apps, schema_editor):
    AgendaItemPosition = apps.get_model("work", "AgendaItemPosition")
    gruppen = (
        AgendaItemPosition.objects.exclude(organization_id=None)
        .values("organization_id", "agenda_item_id")
        .annotate(anzahl=Count("id"))
        .filter(anzahl__gt=1)
        .order_by()
    )
    for gruppe in list(gruppen):
        zeilen = list(
            AgendaItemPosition.objects.filter(
                organization_id=gruppe["organization_id"], agenda_item_id=gruppe["agenda_item_id"]
            ).order_by("-updated_at", "-created_at", "-pk")
        )
        behalten, andere = zeilen[0], zeilen[1:]
        aenderungen = {}
        for feld in UEBERNEHMEN:
            if not _leer(feld, getattr(behalten, feld)):
                continue
            quelle = next((zeile for zeile in andere if not _leer(feld, getattr(zeile, feld))), None)
            if quelle is None:
                continue
            aenderungen[feld] = getattr(quelle, feld)
            if feld == "position" and not behalten.is_final:
                aenderungen["is_final"] = quelle.is_final
        fruehester = min(zeile.created_at for zeile in zeilen)
        if fruehester != behalten.created_at:
            aenderungen["created_at"] = fruehester
        AgendaItemPosition.objects.filter(pk__in=[zeile.pk for zeile in andere]).delete()
        if aenderungen:
            # update() statt save(): auto_now soll updated_at der erhaltenen Zeile nicht überschreiben
            AgendaItemPosition.objects.filter(pk=behalten.pk).update(**aenderungen)

    if schema_editor.connection.vendor == "postgresql":
        # Aufgeschobene Fremdschlüsselprüfungen jetzt auslösen, sonst lehnt PostgreSQL das folgende ALTER TABLE wegen
        # „pending trigger events“ ab
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0074_sitzungsreihe_automatik"),
    ]

    operations = [
        migrations.RunPython(zusammenfuehren, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="agendaitemposition",
            constraint=models.UniqueConstraint(
                fields=("organization", "agenda_item"), name="agendaitemposition_org_item_unique"
            ),
        ),
    ]
