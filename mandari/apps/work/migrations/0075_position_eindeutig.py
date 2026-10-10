# SPDX-License-Identifier: AGPL-3.0-or-later
"""Eine Position je Organisation und TOP (#926).

Seit der org-weiten Vorbereitung (work.0030) fehlte die Eindeutigkeit für ``AgendaItemPosition``. Zwei gleichzeitige
Erst-Saves zum selben TOP konnten zwei Zeilen anlegen; danach scheiterte jedes Speichern mit
``MultipleObjectsReturned``.

Vor dem Setzen der Bedingung werden vorhandene Dubletten zusammengeführt, ohne dass ein Wert verloren geht:

- Erhalten bleibt die zuletzt geänderte Zeile (``updated_at``, dann ``created_at``).
- Felder, die dort leer sind, werden aus der jüngsten anderen Zeile übernommen, die einen Wert trägt: Position
  („open“ gilt als leer), Begründung, Ergebnis, „Gesetzt von“, Altlast-Vorbereitung. „Endgültig“ gehört zur Position
  und kommt aus der Zeile, aus der die Position stammt.
- ``created_at`` wird der früheste Anlagezeitpunkt der Gruppe, ``updated_at`` bleibt der der erhaltenen Zeile.
- Die Begründung ist mit dem Schlüssel der Organisation verschlüsselt (ohne Bindung an die Zeile); da alle Zeilen einer
  Gruppe zur selben Organisation gehören, wird der Geheimtext unverändert übernommen.
- **Widerspruch:** Tragen zwei Zeilen einer Gruppe verschiedene, nicht leere Werte in Position, Ergebnis oder
  Begründung (Geheimtext byteweise verglichen), oder ist eine andere Zeile als die, aus der die Position stammt,
  „endgültig“, bricht die Migration mit einer Liste der Gruppen ab, bevor irgendetwas geändert oder gelöscht wird. Solche Dubletten führt ein Mensch von Hand zusammen, dann läuft die Migration erneut.

Auf ``AgendaItemPosition`` zeigen keine Fremdschlüssel, es muss also nichts umgehängt werden. Zeilen ohne Organisation
(Altlast vor work.0038) fallen nicht unter die Bedingung und bleiben unberührt. Produktion hatte am 07.10.2026 keine
Dubletten, Staging am 10.10.2026 ebenfalls nicht.

Rückweg: Rückwärts wird nur die Bedingung entfernt; zusammengeführte Dubletten kommen nicht zurück.
"""

from django.db import migrations, models
from django.db.models import Count

#: Inhaltliche Felder: zwei verschiedene, nicht leere Werte sind ein Widerspruch („Endgültig“ gesondert, an der Position)
INHALT = ("position", "outcome", "reasoning_encrypted")
#: Verwaltungsangaben: aus der jüngsten Zeile mit Wert ergänzt
VERWALTUNG = ("set_by_id", "preparation_id")
NAMEN = {"position": "Position", "is_final": "Endgültig", "outcome": "Ergebnis", "reasoning_encrypted": "Begründung"}


def _leer(feld: str, wert) -> bool:
    if feld == "position" and wert == "open":
        return True
    # Begründung ist ein BinaryField (bytes bzw. memoryview), die übrigen Felder Text oder Fremdschlüssel-IDs
    return wert is None or (isinstance(wert, (str, bytes, memoryview)) and len(wert) == 0)


def _wert(wert):
    """Vergleichbarer Wert (Geheimtext aus PostgreSQL kommt als memoryview)."""
    return bytes(wert) if isinstance(wert, memoryview) else wert


def _plan(zeilen):
    """(Änderungen an der erhaltenen Zeile, Felder mit Widerspruch) für eine Gruppe, jüngste Zeile zuerst."""
    behalten, andere = zeilen[0], zeilen[1:]
    widerspruch = [
        feld
        for feld in INHALT
        if len({_wert(getattr(z, feld)) for z in zeilen if not _leer(feld, getattr(z, feld))}) > 1
    ]
    # „Endgültig“ gehört zur Position: Es kommt aus der Zeile, aus der die Position stammt (die erhaltene, wenn
    # keine Zeile eine Position trägt). Ist eine andere Zeile endgültig, wäre unklar, was endgültig ist.
    quelle_position = next((z for z in zeilen if not _leer("position", z.position)), behalten)
    if any(z.is_final for z in zeilen if z is not quelle_position):
        widerspruch.insert(1 if "position" in widerspruch else 0, "is_final")
    aenderungen = {}
    if quelle_position.is_final != behalten.is_final:
        aenderungen["is_final"] = quelle_position.is_final
    for feld in INHALT + VERWALTUNG:
        if not _leer(feld, getattr(behalten, feld)):
            continue
        quelle = next((zeile for zeile in andere if not _leer(feld, getattr(zeile, feld))), None)
        if quelle is not None:
            aenderungen[feld] = getattr(quelle, feld)
    fruehester = min(zeile.created_at for zeile in zeilen)
    if fruehester != behalten.created_at:
        aenderungen["created_at"] = fruehester
    return aenderungen, widerspruch


def zusammenfuehren(apps, schema_editor):
    AgendaItemPosition = apps.get_model("work", "AgendaItemPosition")
    gruppen = (
        AgendaItemPosition.objects.exclude(organization_id=None)
        .values("organization_id", "agenda_item_id")
        .annotate(anzahl=Count("id"))
        .filter(anzahl__gt=1)
        .order_by()
    )
    plaene, widersprueche = [], []
    for gruppe in list(gruppen):
        zeilen = list(
            AgendaItemPosition.objects.filter(
                organization_id=gruppe["organization_id"], agenda_item_id=gruppe["agenda_item_id"]
            ).order_by("-updated_at", "-created_at", "-pk")
        )
        aenderungen, widerspruch = _plan(zeilen)
        if widerspruch:
            felder = ", ".join(NAMEN[feld] for feld in widerspruch)
            widersprueche.append(
                f"Organisation {gruppe['organization_id']}, TOP {gruppe['agenda_item_id']}: "
                f"{len(zeilen)} Zeilen, widersprüchlich in {felder}"
            )
        plaene.append((zeilen[0], zeilen[1:], aenderungen))
    if widersprueche:
        raise RuntimeError(
            "work.0075 abgebrochen, nichts geändert: Positionen mit widersprüchlichen Dubletten (#926). Bitte von "
            "Hand zusammenführen und die Migration erneut starten.\n" + "\n".join(widersprueche)
        )

    for behalten, andere, aenderungen in plaene:
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
