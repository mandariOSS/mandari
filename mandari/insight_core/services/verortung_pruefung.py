# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verortung als zweiter Faktor: Claude prüft die automatisch gefundenen Orte eines Vorgangs (Issue #1222).

Die Prüfung läuft in derselben Anfrage wie die Zusammenfassung im Tagesbatch (``insight_ai.services.
zusammenfassung_batch``), der Text wird also nur einmal bezahlt. Claude bekommt die Orte nummeriert
(``orte_fuer_pruefung``) und antwortet je Ort mit „passt“, „passt nicht“ oder „unsicher“ samt kurzer Begründung;
dazu nennt es fehlende Orte. ``anwenden`` setzt das um:

- Geprüft werden nur automatisch gefundene Orte (``PRUEFBARE_HERKUENFTE``: Adresse, Straße, KI-Extraktion) mit Status
  „automatisch“. Orte aus OParl, manuelle, amtliche Umringe und im Admin bestätigte bleiben unangetastet; sie stehen
  nur als Kontext in der Liste.
- „passt nicht“ blendet den Ort aus (Status „entfernt“, ohne ``reviewed_at``): Er verschwindet von der Karte, sein
  Punkt bleibt gesperrt, und im Admin lässt er sich über den Filter „KI-Prüfung“ finden und mit „Bestätigen“
  zurückholen. „passt“ und „unsicher“ ändern nur Urteil und Begründung.
- Fehlende Orte geokodiert die bestehende Pipeline, nie die KI: zuerst das Straßenverzeichnis der Kommune, sonst
  Photon/Nominatim mit Prüfung, ob der Punkt in der Kommune liegt (wie Pass 2 in ``georeferencing``). Neue Einträge
  tragen die Herkunft ``ai_check`` und bleiben bei einem neuen Verortungslauf erhalten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from django.db import transaction
from django.utils import timezone

if TYPE_CHECKING:
    from insight_core.models import OParlPaper, PaperLocation

logger = logging.getLogger(__name__)

#: Herkunft automatisch gefundener Orte, die Claude beurteilen darf
PRUEFBARE_HERKUENFTE: Final = ("address_match", "street_match", "ai")
#: Herkunft der Orte, die Claude als fehlend nennt und die Pipeline geokodiert
HERKUNFT_KI_PRUEFUNG: Final = "ai_check"
#: Höchstens so viele Orte je Vorgang gehen in die Prüfung (prüfbare zuerst)
MAX_ORTE: Final = 30
#: Höchstens so viele fehlende Orte je Vorgang werden geokodiert
MAX_FEHLENDE: Final = 5
#: Konfidenz ergänzter Orte: mit Treffer im Straßenverzeichnis bzw. über Photon/Nominatim
KONFIDENZ_VERZEICHNIS: Final = 0.7
KONFIDENZ_GEOCODER: Final = 0.6
#: Mindestabstand eines ergänzten Orts zu vorhandenen (wie ``deduplicate_locations``)
ABSTAND_M: Final = 50.0
BEGRUENDUNG_MAX: Final = 300


@dataclass(frozen=True)
class PruefOrt:
    """Ein Ort in der Liste für Claude (Nummer = Position + 1)."""

    pk: str
    name: str
    herkunft: str
    pruefbar: bool


@dataclass
class PruefErgebnis:
    """Zähler einer angewandten Prüfung."""

    passt: int = 0
    passt_nicht: int = 0
    unsicher: int = 0
    ergaenzt: int = 0
    nicht_gefunden: list[str] = field(default_factory=list)


def orte_fuer_pruefung(paper: OParlPaper) -> list[PruefOrt]:
    """Sichtbare Orte des Vorgangs für die Prüfung: prüfbare zuerst, höchstens ``MAX_ORTE``."""
    from insight_core.models import PaperLocation

    zeilen = list(
        PaperLocation.objects.filter(paper=paper)
        .exclude(status=PaperLocation.STATUS_REMOVED)
        .only("pk", "name", "source", "status")
        .order_by("name", "pk")
    )
    orte = [
        PruefOrt(
            pk=str(zeile.pk),
            name=zeile.name or "(ohne Bezeichnung)",
            herkunft=zeile.get_source_display(),
            pruefbar=_pruefbar(zeile),
        )
        for zeile in zeilen
    ]
    orte.sort(key=lambda ort: not ort.pruefbar)
    return orte[:MAX_ORTE]


def _pruefbar(zeile: PaperLocation) -> bool:
    from insight_core.models import PaperLocation

    return zeile.source in PRUEFBARE_HERKUENFTE and zeile.status == PaperLocation.STATUS_AUTO


def anwenden(
    vorgang_id: Any, orte_ids: list[str], urteile: list[dict[str, Any]], fehlende: list[dict[str, Any]]
) -> PruefErgebnis:
    """
    Urteile und fehlende Orte aus Claudes Antwort übernehmen (siehe Moduldokumentation); Rückgabe: Zähler.

    ``orte_ids``: Kennungen der Orte in der Reihenfolge, in der sie Claude bekam (Nummer 1 = erste).
    Unbekannte Nummern und Urteile werden übergangen. Idempotent bis auf ergänzte Orte, die per Abstand dedupliziert
    werden.
    """
    from insight_core.models import OParlPaper, PaperLocation
    from insight_core.services.paper_locations import sync_paper_locations

    ergebnis = PruefErgebnis()
    paper = OParlPaper.objects.filter(pk=vorgang_id, deleted=False).select_related("body").first()
    if paper is None:
        return ergebnis
    # Geokodieren vor der Transaktion (Netz: Photon/Nominatim), geschrieben wird danach in einem Zug
    ergaenzt = _ergaenzen(paper, fehlende[:MAX_FEHLENDE], ergebnis)
    jetzt = timezone.now()
    gueltig = {PaperLocation.KI_PASST, PaperLocation.KI_PASST_NICHT, PaperLocation.KI_UNSICHER}
    with transaction.atomic():
        for eintrag in urteile:
            nr, urteil = eintrag.get("nr"), eintrag.get("urteil")
            if not isinstance(nr, int) or not 1 <= nr <= len(orte_ids) or urteil not in gueltig:
                continue
            zeile = PaperLocation.objects.select_for_update().filter(pk=orte_ids[nr - 1], paper=paper).first()
            if zeile is None or not _pruefbar(zeile):
                continue
            zeile.ki_urteil = urteil
            zeile.ki_begruendung = " ".join(str(eintrag.get("begruendung") or "").split())[:BEGRUENDUNG_MAX]
            zeile.ki_geprueft_am = jetzt
            felder = ["ki_urteil", "ki_begruendung", "ki_geprueft_am", "updated_at"]
            if urteil == PaperLocation.KI_PASST_NICHT:
                zeile.status = PaperLocation.STATUS_REMOVED
                felder.append("status")
            zeile.save(update_fields=felder)
            setattr(ergebnis, urteil, getattr(ergebnis, urteil) + 1)
        if ergebnis.passt_nicht or ergaenzt:
            gesperrt = OParlPaper.objects.select_for_update().get(pk=paper.pk)
            if ergaenzt:
                bisher = gesperrt.locations if isinstance(gesperrt.locations, list) else []
                gesperrt.locations = bisher + ergaenzt
                gesperrt.save(update_fields=["locations", "updated_at"])
            sync_paper_locations(gesperrt)
            if ergaenzt:
                PaperLocation.objects.filter(paper=paper, source=HERKUNFT_KI_PRUEFUNG, ki_urteil="").update(
                    ki_urteil=PaperLocation.KI_PASST,
                    ki_begruendung="Von Claude als fehlender Ort genannt.",
                    ki_geprueft_am=jetzt,
                )
    logger.info(
        "Verortung KI-geprüft: Vorgang %s, passt %d, passt nicht %d, unsicher %d, ergänzt %d, nicht gefunden %d",
        vorgang_id,
        ergebnis.passt,
        ergebnis.passt_nicht,
        ergebnis.unsicher,
        ergebnis.ergaenzt,
        len(ergebnis.nicht_gefunden),
    )
    return ergebnis


def _ergaenzen(paper: OParlPaper, fehlende: list[dict[str, Any]], ergebnis: PruefErgebnis) -> list[dict[str, Any]]:
    """Fehlende Orte über das Straßenverzeichnis bzw. Photon/Nominatim geokodieren; Rückgabe: neue JSON-Einträge."""
    from insight_core.models import PaperLocation
    from insight_core.services.gazetteer import StreetGazetteer
    from insight_core.services.georeferencing import _haversine_distance, geocode_address, is_within_body

    namen = [" ".join(str(eintrag.get("bezeichnung") or "").split())[:200] for eintrag in fehlende]
    namen = [name for name in namen if name]
    if not namen or paper.body is None:
        return []
    vorhanden = [
        (lat, lon) for lat, lon in PaperLocation.objects.filter(paper=paper).values_list("latitude", "longitude")
    ]
    gazetteer = StreetGazetteer(paper.body)
    neu: list[dict[str, Any]] = []
    for name in namen:
        treffer = gazetteer.lookup(name)
        if treffer:
            treffer = gazetteer.refine(treffer)
            lat, lon = float(treffer["lat"]), float(treffer["lon"])
            bezeichnung = treffer["name"] + (f" {treffer['house_number']}" if treffer.get("house_number") else "")
            konfidenz = KONFIDENZ_VERZEICHNIS
        else:
            gefunden = geocode_address(name, paper.body)
            if not gefunden or not is_within_body(gefunden["lat"], gefunden["lon"], paper.body):
                ergebnis.nicht_gefunden.append(name)
                continue
            lat, lon = float(gefunden["lat"]), float(gefunden["lon"])
            bezeichnung = gefunden.get("name") or name
            konfidenz = KONFIDENZ_GEOCODER
        if any(_haversine_distance(lat, lon, a, b) < ABSTAND_M for a, b in vorhanden):
            continue
        vorhanden.append((lat, lon))
        neu.append(
            {
                "lat": round(lat, 7),
                "lon": round(lon, 7),
                "name": bezeichnung,
                "source": HERKUNFT_KI_PRUEFUNG,
                "confidence": konfidenz,
            }
        )
        ergebnis.ergaenzt += 1
    return neu
