# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Daumen hoch/runter zu KI-Zusammenfassungen im Bürgerportal (Issue #1221): Qualität messen, Modelle vergleichen.

- **Abgeben:** ohne Anmeldung, ohne Cookie. Gespeichert werden Vorgang, Kommune, Wert, Anbieterhinweis und Modell
  der bewerteten Fassung, deren Prüfsumme (``fassung``) und der Tag; keine IP-Adresse, keine Uhrzeit.
- **Eine je Browser und Fassung, änderbar:** Die Antwort enthält ein signiertes Zeichen der Bewertung. Der Browser
  hält es im lokalen Speicher (``frontend/alpine/insight.ts``, ``kiBewertung``) und schickt es bei einer Änderung
  mit; dann ändert sich dieselbe Zeile statt einer neuen.
- **Neue Fassung, neue Zählung:** Die Prüfsumme hängt am Text der Zusammenfassung. Wird sie neu erstellt, passt eine
  alte Fassung nicht mehr (``FassungVeraltetError``), und die neue beginnt ohne Bewertungen.
- **Missbrauch:** ``throttle`` zählt grob je Adresse im Cache (gehasht, eine Stunde), wie die Rückmeldung zu Seiten.
- **Auswertung:** Anteil hilfreich je Modell, je Kommune und je Woche (``auswertung``, Admin). Das ERP holt die
  Einzelwerte ohne Personenbezug über ``/api/insight/ki-bewertungen/`` ab (``fuer_erp``).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Final, cast

from django.core import signing
from django.db.models import Count, Q
from django.db.models.functions import TruncWeek
from django.http import HttpRequest
from django.utils import timezone

from .. import throttle

if TYPE_CHECKING:
    from ..models import OParlPaper, ZusammenfassungBewertung

#: Salz der signierten Zeichen (nur für Bewertungen gültig)
SALZ: Final = "insight.ki_bewertung"
#: Höchstens so viele Bewertungen je Adresse und Stunde (Änderungen zählen mit)
GRENZE_JE_STUNDE: Final = 60
#: Modell aus dem Anbieterhinweis, etwa „Claude von Anthropic (claude-haiku-5-5); …“
_MODELL: Final = re.compile(r"\(([^()]{1,100})\)")
#: Höchstens so viele Einträge je Abruf des ERP
ERP_SEITE_MAX: Final = 1000


class FassungVeraltetError(Exception):
    """Die bewertete Fassung ist nicht (mehr) die gespeicherte Zusammenfassung."""


class ZuVieleBewertungenError(Exception):
    """Zu viele Bewertungen von dieser Adresse in der letzten Stunde."""


def fassung(zusammenfassung: str | None) -> str:
    """Prüfsumme einer Zusammenfassung (16 Zeichen); leer ohne Zusammenfassung."""
    if not zusammenfassung:
        return ""
    return hashlib.sha256(zusammenfassung.encode("utf-8")).hexdigest()[:16]


def modell_aus_hinweis(hinweis: str) -> str:
    """Modell aus dem Anbieterhinweis (erster Ausdruck in Klammern); leer, wenn keiner da ist."""
    treffer = _MODELL.search(hinweis or "")
    return treffer.group(1).strip() if treffer else ""


def abgeben(
    request: HttpRequest, paper: OParlPaper, *, hilfreich: bool, bewertete_fassung: str, zeichen: str = ""
) -> tuple[ZusammenfassungBewertung, str]:
    """
    Bewertung speichern oder (mit gültigem ``zeichen`` derselben Fassung) ändern; Rückgabe: Bewertung und Zeichen.

    Raises:
        FassungVeraltetError: Die Zusammenfassung ist inzwischen eine andere oder fehlt.
        ZuVieleBewertungenError: Grenze je Adresse überschritten.
    """
    from ..models import ZusammenfassungBewertung

    aktuell = fassung(paper.summary)
    if not aktuell or bewertete_fassung != aktuell:
        raise FassungVeraltetError
    if throttle.hit("ki_bewertung", throttle.client_ip(request), limit=GRENZE_JE_STUNDE, window=throttle.HOUR):
        raise ZuVieleBewertungenError
    bewertung = _aus_zeichen(zeichen, paper, aktuell)
    if bewertung is not None:
        if bewertung.hilfreich != hilfreich:
            bewertung.hilfreich = hilfreich
            bewertung.save(update_fields=["hilfreich", "geaendert"])
    else:
        hinweis = paper.summary_hinweis or ""
        bewertung = ZusammenfassungBewertung.objects.create(
            paper=paper,
            body_id=paper.body_id,
            hilfreich=hilfreich,
            fassung=aktuell,
            modell=modell_aus_hinweis(hinweis),
            hinweis=hinweis[:300],
        )
    return bewertung, signing.dumps({"b": bewertung.pk, "f": aktuell}, salt=SALZ, compress=True)


def _aus_zeichen(zeichen: str, paper: OParlPaper, aktuell: str) -> ZusammenfassungBewertung | None:
    """Bewertung zu einem signierten Zeichen derselben Fassung und desselben Vorgangs; sonst ``None``."""
    from ..models import ZusammenfassungBewertung

    if not zeichen:
        return None
    try:
        inhalt = signing.loads(zeichen, salt=SALZ)
    except signing.BadSignature:
        return None
    kennung = inhalt.get("b") if isinstance(inhalt, dict) else None
    if not isinstance(kennung, int) or inhalt.get("f") != aktuell:
        return None
    return ZusammenfassungBewertung.objects.filter(pk=kennung, paper=paper, fassung=aktuell).first()


@dataclass(frozen=True)
class Anteil:
    """Zeile der Auswertung: Gruppe, Zahl der Bewertungen, davon hilfreich, Anteil in Prozent."""

    gruppe: str
    gesamt: int
    hilfreich: int

    @property
    def prozent(self) -> int:
        return round(100 * self.hilfreich / self.gesamt) if self.gesamt else 0


def auswertung(*, tage: int = 84) -> dict[str, list[Anteil]]:
    """Anteil hilfreich je Modell, je Kommune und je Woche der letzten ``tage`` Tage (Admin)."""
    from ..models import ZusammenfassungBewertung

    seit = timezone.localdate() - timedelta(days=tage)
    basis = ZusammenfassungBewertung.objects.filter(tag__gte=seit)
    zaehlung = {"gesamt": Count("id"), "positiv": Count("id", filter=Q(hilfreich=True))}

    def zeilen(feld: str, beschriftung: Any) -> list[Anteil]:
        return [
            Anteil(gruppe=beschriftung(zeile[feld]), gesamt=zeile["gesamt"], hilfreich=zeile["positiv"])
            for zeile in basis.values(feld).annotate(**zaehlung).order_by(feld)
        ]

    # Die Stubs kennen die Zählungen nach values() nicht: als einfache Zeilen lesen
    je_woche = cast(
        "list[dict[str, Any]]",
        list(basis.annotate(woche=TruncWeek("tag")).values("woche").annotate(**zaehlung).order_by("-woche")),
    )
    wochen = [
        Anteil(gruppe=f"Woche ab {zeile['woche']:%d.%m.%Y}", gesamt=zeile["gesamt"], hilfreich=zeile["positiv"])
        for zeile in je_woche
        if zeile["woche"] is not None
    ]
    return {
        "Modell": zeilen("modell", lambda wert: wert or "ohne Angabe"),
        "Kommune": zeilen("body__name", lambda wert: wert or "ohne Kommune"),
        "Woche": wochen,
    }


def fuer_erp(*, seit: date | None = None, nach: int = 0, anzahl: int = ERP_SEITE_MAX) -> dict[str, Any]:
    """
    Einzelwerte für das ERP (ohne Personenbezug): abgegeben oder geändert ab dem Tag ``seit`` (``None``: alle), nach
    Kennung aufsteigend ab ``nach``, höchstens ``anzahl``. ``naechste`` ist die Kennung für den nächsten Abruf mit
    demselben ``seit`` (``None``: keine weiteren). Das ERP übernimmt die Einträge nach ``id`` (neu oder geändert).
    """
    from ..models import ZusammenfassungBewertung

    anzahl = max(1, min(int(anzahl), ERP_SEITE_MAX))
    auswahl = ZusammenfassungBewertung.objects.filter(pk__gt=nach)
    if seit is not None:
        auswahl = auswahl.filter(geaendert__gte=seit)
    zeilen = list(auswahl.select_related("body", "paper").order_by("pk")[: anzahl + 1])
    weitere = len(zeilen) > anzahl
    zeilen = zeilen[:anzahl]
    return {
        "bewertungen": [
            {
                "id": zeile.pk,
                "tag": zeile.tag.isoformat(),
                "geaendert": zeile.geaendert.isoformat(),
                "hilfreich": zeile.hilfreich,
                "modell": zeile.modell,
                "hinweis": zeile.hinweis,
                "fassung": zeile.fassung,
                "vorgang": str(zeile.paper_id) if zeile.paper_id else None,
                "vorgang_nummer": (zeile.paper.reference or "") if zeile.paper else "",
                "kommune": str(zeile.body_id) if zeile.body_id else None,
                "kommune_name": zeile.body.name if zeile.body else "",
            }
            for zeile in zeilen
        ],
        "naechste": zeilen[-1].pk if weitere and zeilen else None,
    }


def stand(paper: OParlPaper) -> dict[str, Any]:
    """Angaben für den Bewertungsschalter unter der Zusammenfassung (Fassung); leer ohne Zusammenfassung."""
    return {"fassung": fassung(paper.summary)} if paper.summary else {}


__all__ = [
    "Anteil",
    "FassungVeraltetError",
    "ZuVieleBewertungenError",
    "abgeben",
    "auswertung",
    "fassung",
    "fuer_erp",
    "modell_aus_hinweis",
    "stand",
]
