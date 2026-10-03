# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Import des Kommunenverzeichnisses (Issue #783, Stufe 2; Format in docs/INSIGHT_KOMMUNENWECHSEL.md).

Eine CSV-Datei (UTF-8, Semikolon, Kopfzeile) je Gemeinde, Gemeindeverband oder kreisfreie Stadt:

    schluessel;name;art;kreis;breite;laenge;plz;ortsteile
    059990000000;Beispielstadt;Kreisfreie Stadt;;51.9625;7.6256;48143|48145;Nordviertel|Heidekamp

``schluessel`` ist der Regionalschlüssel (12 Stellen) oder der Amtliche Gemeindeschlüssel (8 Stellen), Land und
Kreis ergeben sich daraus. ``plz`` und ``ortsteile`` trennt ``|``. Der Import ist idempotent: vorhandene Einträge
werden aktualisiert, ihre Suchbegriffe neu geschrieben; mit ``ersetzen`` entfallen Einträge, die nicht mehr in
der Datei stehen.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TextIO

from django.db import transaction

from ..models import Municipality, MunicipalityTerm, OParlBody
from .kommunenverzeichnis import LAENDER, get_kind_label_for_body, varianten

SPALTEN = ("schluessel", "name", "art", "kreis", "breite", "laenge", "plz", "ortsteile")
_SCHLUESSEL = re.compile(r"^\d{8}(\d{4})?$")
_PLZ = re.compile(r"^\d{5}$")
_TRENNER = re.compile(r"[|,]")
#: Arten, die im Stöbern eine Stufe zwischen Kreis und Gemeinde bilden
_VERBAND = re.compile(
    r"samtgemeinde|verbandsgemeinde|^amt\b|verwaltungsgemeinschaft|verwaltungsverband|gemeindeverwaltungsverband", re.I
)
_STAPEL = 1000


@dataclass
class Ergebnis:
    neu: int = 0
    aktualisiert: int = 0
    entfernt: int = 0
    uebersprungen: list[str] = field(default_factory=list)


@dataclass
class _Zeile:
    eintrag: Municipality
    ortsteile: list[str]
    postleitzahlen: list[str]


def ags_aus_schluessel(schluessel: str) -> str:
    """AGS (8 Stellen) aus Regional- oder Gemeindeschlüssel: Land, Bezirk, Kreis und die letzten drei Stellen."""
    return schluessel[:5] + schluessel[9:] if len(schluessel) == 12 else schluessel


def _zahl(wert: str) -> float | None:
    wert = wert.strip().replace(",", ".")
    if not wert:
        return None
    try:
        return float(wert)
    except ValueError:
        return None


def _liste(wert: str) -> list[str]:
    return [teil.strip() for teil in _TRENNER.split(wert or "") if teil.strip()]


def _zeile_lesen(nummer: int, zeile: dict[str, str]) -> _Zeile | str:
    schluessel = (zeile.get("schluessel") or "").strip()
    name = (zeile.get("name") or "").strip()
    if not _SCHLUESSEL.match(schluessel) or schluessel[:2] not in LAENDER:
        return f"Zeile {nummer}: Schlüssel fehlt oder ist ungültig"
    if not name:
        return f"Zeile {nummer}: Name fehlt"
    art = (zeile.get("art") or "").strip()[:60]
    breite, laenge = _zahl(zeile.get("breite") or ""), _zahl(zeile.get("laenge") or "")
    if breite is None or laenge is None or not (-90 <= breite <= 90 and -180 <= laenge <= 180):
        breite = laenge = None
    verband = bool(_VERBAND.search(art))
    eintrag = Municipality(
        key=schluessel,
        # Gemeindeverbände haben keinen Gemeindeschlüssel; ein abgeleiteter wäre erfunden
        ags="" if verband else ags_aus_schluessel(schluessel),
        name=name[:200],
        kind=art,
        is_association=verband,
        district_key=schluessel[:5],
        district=(zeile.get("kreis") or "").strip()[:200],
        state_key=schluessel[:2],
        latitude=breite,
        longitude=laenge,
    )
    postleitzahlen = [plz for plz in _liste(zeile.get("plz") or "") if _PLZ.match(plz)]
    return _Zeile(eintrag, _liste(zeile.get("ortsteile") or ""), postleitzahlen)


def _begriffe(eintrag: Municipality, ortsteile: Iterable[str], postleitzahlen: Iterable[str]) -> list[MunicipalityTerm]:
    begriffe: dict[tuple[str, str], MunicipalityTerm] = {}

    def dazu(art: str, anzeige: str) -> None:
        for wert in varianten(anzeige):
            begriffe.setdefault(
                (art, wert[:200]),
                MunicipalityTerm(municipality=eintrag, kind=art, label=anzeige[:200], normalized=wert[:200]),
            )

    dazu(MunicipalityTerm.Kind.NAME, eintrag.name)
    for ortsteil in ortsteile:
        dazu(MunicipalityTerm.Kind.DISTRICT_PART, ortsteil)
    for plz in postleitzahlen:
        begriffe.setdefault(
            (MunicipalityTerm.Kind.POSTCODE, plz),
            MunicipalityTerm(municipality=eintrag, kind=MunicipalityTerm.Kind.POSTCODE, label=plz, normalized=plz),
        )
    return list(begriffe.values())


def _speichern(zeilen: list[_Zeile], ergebnis: Ergebnis) -> None:
    vorhanden = {e.key: e for e in Municipality.objects.filter(key__in=[z.eintrag.key for z in zeilen])}
    neu: list[Municipality] = []
    for zeile in zeilen:
        alt = vorhanden.get(zeile.eintrag.key)
        if alt is None:
            neu.append(zeile.eintrag)
        else:
            zeile.eintrag.pk = alt.pk
    Municipality.objects.bulk_create(neu)
    aktualisieren = [z.eintrag for z in zeilen if z.eintrag.key in vorhanden]
    felder = ["ags", "name", "kind", "is_association", "district_key", "district", "state_key", "latitude", "longitude"]
    Municipality.objects.bulk_update(aktualisieren, felder)
    ergebnis.neu += len(neu)
    ergebnis.aktualisiert += len(aktualisieren)

    # bulk_create setzt in SQLite und PostgreSQL die Primärschlüssel; zur Sicherheit neu laden
    ids = dict(Municipality.objects.filter(key__in=[z.eintrag.key for z in zeilen]).values_list("key", "id"))
    for zeile in zeilen:
        zeile.eintrag.pk = ids[zeile.eintrag.key]
    MunicipalityTerm.objects.filter(municipality_id__in=ids.values()).delete()
    MunicipalityTerm.objects.bulk_create(
        [b for z in zeilen for b in _begriffe(z.eintrag, z.ortsteile, z.postleitzahlen)], batch_size=_STAPEL
    )


def importieren(datei: TextIO, *, ersetzen: bool = False) -> Ergebnis:
    """CSV-Datei einlesen und das Verzeichnis aktualisieren (in einer Transaktion)."""
    ergebnis = Ergebnis()
    leser = csv.DictReader(datei, delimiter=";")
    fehlend = [spalte for spalte in ("schluessel", "name") if spalte not in (leser.fieldnames or [])]
    if fehlend:
        raise ValueError("Spalten fehlen: " + ", ".join(fehlend))
    gelesen: dict[str, _Zeile] = {}
    for nummer, zeile in enumerate(leser, start=2):
        gelesen_oder_fehler = _zeile_lesen(nummer, zeile)
        if isinstance(gelesen_oder_fehler, str):
            ergebnis.uebersprungen.append(gelesen_oder_fehler)
        else:
            gelesen[gelesen_oder_fehler.eintrag.key] = gelesen_oder_fehler
    zeilen = list(gelesen.values())
    with transaction.atomic():
        for start in range(0, len(zeilen), _STAPEL):
            _speichern(zeilen[start : start + _STAPEL], ergebnis)
        if ersetzen:
            _, je_modell = Municipality.objects.exclude(key__in=list(gelesen)).delete()
            ergebnis.entfernt = je_modell.get(Municipality._meta.label, 0)
    return ergebnis


def aus_koerperschaften() -> Ergebnis:
    """Gelistete Kommunen mit Regionalschlüssel oder AGS ins Verzeichnis übernehmen, soweit sie dort fehlen.

    Damit ist der Kommunenwechsel auch ohne vollständiges Verzeichnis nutzbar; Kreis, Ortsteile und
    Postleitzahlen kommen erst mit dem Import dazu.
    """
    ergebnis = Ergebnis()
    vorhanden_rs = set(Municipality.objects.values_list("key", flat=True))
    vorhanden_ags = set(Municipality.objects.exclude(ags="").values_list("ags", flat=True))
    zeilen: list[_Zeile] = []
    for body in OParlBody.objects.listed():
        schluessel = body.rgs if body.rgs and len(body.rgs) == 12 else body.ags if len(body.ags or "") == 8 else ""
        if not schluessel or schluessel[:2] not in LAENDER:
            continue
        if schluessel in vorhanden_rs or ags_aus_schluessel(schluessel) in vorhanden_ags:
            continue
        art = get_kind_label_for_body(body)
        verband = bool(_VERBAND.search(body.name or "")) or art == "Gemeindeverband"
        eintrag = Municipality(
            key=schluessel,
            ags="" if verband else ags_aus_schluessel(schluessel),
            name=body.get_display_name()[:200],
            kind=art,
            is_association=verband,
            district_key=schluessel[:5],
            state_key=schluessel[:2],
            latitude=float(body.latitude) if body.latitude is not None else None,
            longitude=float(body.longitude) if body.longitude is not None else None,
        )
        zeilen.append(_Zeile(eintrag, [], []))
        vorhanden_rs.add(schluessel)
    with transaction.atomic():
        if zeilen:
            _speichern(zeilen, ergebnis)
    return ergebnis
