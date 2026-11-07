# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Adresspunkte von Privatanschriften im Bestand auf die Straße zurückstufen (Issue #1269), umkehrbar.

Seit #1269 verortet die Pipeline Eingaben aus der Einwohnerschaft und Bauanträge nie mehr auf einen Adresspunkt
(``ortsschutz``). Ältere Verortungen dieser Vorgänge können noch einen tragen. ``zurueckstufen`` ändert je Vorgang:

* Ein Adresspunkt (Herkunft „Adresse“ oder Hausnummer im Namen) mit bekannter Straße rückt auf den Punkt der Straße,
  der Name verliert die Hausnummer, die Herkunft „Adresse“ wird „Straße“.
* Ist die Straße im Verzeichnis unbekannt oder liegt dort schon ein Ort desselben Vorgangs, wird die Zeile
  **ausgeblendet** (Status ``hidden``): Sie bleibt mit allen Angaben in der Tabelle, erscheint aber nirgends öffentlich.

Gelöscht wird nichts. Die alten Angaben stehen in ``PaperLocation.vorher``; ``rueckgaengig`` stellt Punkt, Name,
Herkunft, Konfidenz und Status wieder her und nimmt den Eintrag wieder in ``paper.locations`` auf. Entfernte und
schon ausgeblendete Zeilen bleiben unberührt. Ohne ``ausfuehren`` wird nur gezählt (Probelauf).

Findet ein späterer Verortungslauf die Straße einer zurückgestuften Zeile nicht mehr, löscht ``sync_paper_locations``
sie nicht, sondern blendet sie aus (``vorher`` bleibt); ``rueckgaengig`` stellt auch dann den alten Stand her.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from insight_core.services import ortsschutz

if TYPE_CHECKING:
    from insight_core.models import OParlBody, OParlPaper, PaperLocation

VERSCHOBEN: Final = "verschoben"
AUSGEBLENDET: Final = "ausgeblendet"


@dataclass
class Bilanz:
    """Zähler eines Laufs (je Vorgang oder gesamt)."""

    vorgaenge: int = 0
    verschoben: int = 0
    ausgeblendet: int = 0
    wiederhergestellt: int = 0
    uebersprungen: int = 0

    def __iadd__(self, andere: Bilanz) -> Bilanz:
        for feld in ("vorgaenge", "verschoben", "ausgeblendet", "wiederhergestellt", "uebersprungen"):
            setattr(self, feld, getattr(self, feld) + getattr(andere, feld))
        return self


def ist_adresspunkt(zeile: PaperLocation) -> bool:
    """Herkunft „Adresse“ oder eine Hausnummer im Namen."""
    from insight_core.services.gazetteer import strip_house_number

    name = zeile.name or ""
    return zeile.source == "address_match" or ortsschutz.hat_hausnummer(name) or bool(strip_house_number(name)[1])


def kandidaten(body: OParlBody | None = None) -> QuerySet[OParlPaper]:
    """Vorgänge mit sichtbaren Orten, die ein Adresspunkt sein können (Vorfilter; ``zurueckstufen`` prüft genau)."""
    from insight_core.models import OParlPaper, PaperLocation

    zeilen = PaperLocation.objects.filter(
        status__in=(PaperLocation.STATUS_AUTO, PaperLocation.STATUS_CONFIRMED)
    ).filter(Q(source="address_match") | Q(name__regex=r"[0-9]"))
    if body is not None:
        zeilen = zeilen.filter(body=body)
    return OParlPaper.objects.filter(pk__in=zeilen.values("paper_id")).order_by("pk")


def zurueckgestufte(body: OParlBody | None = None) -> QuerySet[OParlPaper]:
    """Vorgänge mit zurückgestuften Zeilen (für ``rueckgaengig``)."""
    from insight_core.models import OParlPaper, PaperLocation

    zeilen = PaperLocation.objects.filter(vorher__isnull=False)
    if body is not None:
        zeilen = zeilen.filter(body=body)
    return OParlPaper.objects.filter(pk__in=zeilen.values("paper_id")).order_by("pk")


def _eintrag(zeile: PaperLocation) -> dict[str, Any]:
    eintrag: dict[str, Any] = {
        "lat": zeile.latitude,
        "lon": zeile.longitude,
        "name": zeile.name,
        "source": zeile.source,
    }
    if zeile.confidence is not None:
        eintrag["confidence"] = zeile.confidence
    return eintrag


def zurueckstufen(paper: OParlPaper, *, ausfuehren: bool = False) -> Bilanz:
    """Adresspunkte eines Vorgangs mit Privatanschrift auf die Straße zurückstufen bzw. ausblenden."""
    from insight_core.models import PaperLocation
    from insight_core.services.georeferencing import CONFIDENCE
    from insight_core.services.paper_locations import point_key

    bilanz = Bilanz()
    if not ortsschutz.grund(paper):
        return bilanz
    zeilen = list(PaperLocation.objects.filter(paper=paper).order_by("pk"))
    belegt: dict[tuple[float, float], PaperLocation] = {point_key(z.latitude, z.longitude): z for z in zeilen}
    eintraege: list[Any] = list(paper.locations) if isinstance(paper.locations, list) else []
    jetzt = timezone.now().isoformat()
    geaendert: list[PaperLocation] = []

    for zeile in zeilen:
        if zeile.status not in (PaperLocation.STATUS_AUTO, PaperLocation.STATUS_CONFIRMED) or not ist_adresspunkt(
            zeile
        ):
            continue
        alt = point_key(zeile.latitude, zeile.longitude)
        vorher = {**_eintrag(zeile), "status": zeile.status, "am": jetzt}
        strasse = ortsschutz.strassenpunkt(paper.body, zeile.name)
        ziel = point_key(strasse["lat"], strasse["lon"]) if strasse else None
        index = next(
            (i for i, e in enumerate(eintraege) if isinstance(e, dict) and _schluessel(e, point_key) == alt),
            None,
        )
        if strasse is not None and ziel is not None and ziel not in belegt:
            vorher["aktion"] = VERSCHOBEN
            zeile.latitude, zeile.longitude = ziel
            zeile.name = str(strasse["name"])[:500]
            if zeile.source == "address_match":
                zeile.source = "street_match"
                zeile.confidence = CONFIDENCE["street_match"]
            del belegt[alt]
            belegt[ziel] = zeile
            if index is not None:
                eintraege[index] = {**eintraege[index], **_eintrag(zeile)}
            bilanz.verschoben += 1
        else:
            vorher["aktion"] = AUSGEBLENDET
            zeile.status = PaperLocation.STATUS_HIDDEN
            if index is not None:
                eintraege.pop(index)
            bilanz.ausgeblendet += 1
        zeile.vorher = vorher
        geaendert.append(zeile)

    if not geaendert:
        return bilanz
    bilanz.vorgaenge = 1
    if ausfuehren:
        with transaction.atomic():
            for zeile in geaendert:
                zeile.save(
                    update_fields=[
                        "latitude",
                        "longitude",
                        "name",
                        "source",
                        "confidence",
                        "status",
                        "vorher",
                        "updated_at",
                    ]
                )
            paper.locations = eintraege or None
            paper.save(update_fields=["locations", "updated_at"])
    return bilanz


def _schluessel(eintrag: dict[str, Any], point_key: Any) -> tuple[float, float] | None:
    try:
        schluessel: tuple[float, float] = point_key(eintrag["lat"], eintrag["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    return schluessel


def rueckgaengig(paper: OParlPaper, *, ausfuehren: bool = False) -> Bilanz:
    """Rückstufungen eines Vorgangs aufheben: alte Angaben aus ``vorher`` zurück, Eintrag zurück ins JSON."""
    from insight_core.models import PaperLocation
    from insight_core.services.paper_locations import point_key

    bilanz = Bilanz()
    zeilen = list(PaperLocation.objects.filter(paper=paper).order_by("pk"))
    belegt = {point_key(z.latitude, z.longitude): z for z in zeilen}
    eintraege: list[Any] = list(paper.locations) if isinstance(paper.locations, list) else []
    geaendert: list[PaperLocation] = []

    for zeile in zeilen:
        vorher = zeile.vorher
        if not isinstance(vorher, dict) or vorher.get("aktion") not in (VERSCHOBEN, AUSGEBLENDET):
            continue
        try:
            alt = point_key(vorher["lat"], vorher["lon"])
        except (KeyError, TypeError, ValueError):
            bilanz.uebersprungen += 1
            continue
        jetzt = point_key(zeile.latitude, zeile.longitude)
        if alt != jetzt and alt in belegt:
            # Am alten Punkt liegt inzwischen ein anderer Ort des Vorgangs
            bilanz.uebersprungen += 1
            continue
        index = next(
            (i for i, e in enumerate(eintraege) if isinstance(e, dict) and _schluessel(e, point_key) == jetzt),
            None,
        )
        original = {k: vorher[k] for k in ("lat", "lon", "name", "source", "confidence") if k in vorher}
        if vorher["aktion"] == VERSCHOBEN and index is not None:
            eintraege[index] = {**eintraege[index], **original}
        elif index is None:
            eintraege.append(original)
        zeile.latitude, zeile.longitude = alt
        zeile.name = str(vorher.get("name") or "")[:500]
        zeile.source = str(vorher.get("source") or zeile.source)
        zeile.confidence = vorher.get("confidence")
        zeile.status = str(vorher.get("status") or PaperLocation.STATUS_AUTO)
        zeile.vorher = None
        belegt.pop(jetzt, None)
        belegt[alt] = zeile
        geaendert.append(zeile)
        bilanz.wiederhergestellt += 1

    if not geaendert:
        return bilanz
    bilanz.vorgaenge = 1
    if ausfuehren:
        with transaction.atomic():
            for zeile in geaendert:
                zeile.save(
                    update_fields=[
                        "latitude",
                        "longitude",
                        "name",
                        "source",
                        "confidence",
                        "status",
                        "vorher",
                        "updated_at",
                    ]
                )
            paper.locations = eintraege or None
            paper.save(update_fields=["locations", "updated_at"])
    return bilanz
