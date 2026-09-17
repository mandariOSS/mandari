# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Monatlicher Verfügbarkeitsbericht aus der Statusseite (Gatus), Issue #231.

Datenquelle ist die Gatus-API:

- ``GET /api/v1/endpoints/statuses?page=N&pageSize=100`` liefert je überwachtem Endpunkt
  ``name``, ``group``, ``key``, die letzten ``results`` und die ``events`` (``START``,
  ``HEALTHY``, ``UNHEALTHY`` mit Zeitstempel).
- ``GET /api/v1/endpoints/<key>/uptimes/30d`` liefert die Verfügbarkeit der letzten 30 Tage
  als Bruchzahl (``0.998750``). Gatus kennt nur 1h/24h/7d/30d, keinen Kalendermonat – der
  Bericht nimmt 30d als Näherung und ist am Monatsersten für den Vormonat am genauesten.

Störungen (Anzahl, Dauer) werden aus den Ereignissen des Monats berechnet: ein
``UNHEALTHY`` eröffnet eine Störung, das nächste ``HEALTHY`` beendet sie. Fehlen Ereignisse,
dienen die Einzelergebnisse als Ersatz (Gatus hält davon nur die letzten ~100).

Je Mandant lässt sich nichts trennen: Bürgerportal, Work, Session und OParl-API laufen auf
derselben Instanz; ein Ausfall trifft alle Mandanten gleichermaßen. Der Bericht gilt daher
je Dienst und für alle Mandanten zusammen.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

ZIEL_PROZENT = 99.5
_FRAKTION_RE = re.compile(r"(\.\d{6})\d+")


@dataclass(frozen=True)
class Stoerung:
    beginn: datetime
    ende: datetime

    @property
    def dauer(self) -> timedelta:
        return self.ende - self.beginn


@dataclass
class Dienst:
    name: str
    gruppe: str
    key: str
    uptime_30d: float | None  # 0–1
    stoerungen: list[Stoerung] = field(default_factory=list)

    @property
    def ausfallzeit(self) -> timedelta:
        return sum((s.dauer for s in self.stoerungen), timedelta())


class GatusError(RuntimeError):
    """Statusseite nicht erreichbar oder Antwort unbrauchbar."""


# ---------------------------------------------------------------------------
# Gatus-API
# ---------------------------------------------------------------------------


def _abrufen(url: str, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as antwort:  # noqa: S310 – Basis-URL aus Option/Settings
            return bytes(antwort.read())
    except (OSError, ValueError) as exc:
        raise GatusError(f"Statusseite nicht erreichbar ({url}): {exc}") from exc


def lade_statuses(basis_url: str, timeout: float = 10.0) -> list[dict[str, Any]]:
    basis = basis_url.rstrip("/")
    seiten: list[dict[str, Any]] = []
    seite = 1
    while True:
        roh = _abrufen(f"{basis}/api/v1/endpoints/statuses?page={seite}&pageSize=100", timeout)
        try:
            daten = json.loads(roh)
        except ValueError as exc:
            raise GatusError(f"Antwort der Statusseite ist kein JSON: {exc}") from exc
        if not isinstance(daten, list):
            raise GatusError("Antwort der Statusseite hat nicht die erwartete Form (Liste)")
        seiten.extend(daten)
        if len(daten) < 100:
            return seiten
        seite += 1


def lade_uptime(basis_url: str, key: str, dauer: str = "30d", timeout: float = 10.0) -> float | None:
    try:
        roh = _abrufen(f"{basis_url.rstrip('/')}/api/v1/endpoints/{key}/uptimes/{dauer}", timeout)
        return float(roh.decode("utf-8").strip())
    except (GatusError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Auswertung
# ---------------------------------------------------------------------------


def zeitstempel(wert: str) -> datetime:
    """RFC-3339-Zeitstempel von Gatus (bis zu neun Nachkommastellen, ``Z``) als bewusste UTC-Zeit."""
    text = _FRAKTION_RE.sub(r"\1", wert.strip()).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def monatsgrenzen(monat: str) -> tuple[datetime, datetime]:
    """(Beginn, Ende) des Monats ``YYYY-MM`` in UTC; Ende exklusiv."""
    beginn = datetime.strptime(monat, "%Y-%m").replace(tzinfo=UTC)
    ende = (beginn.replace(day=28) + timedelta(days=4)).replace(day=1)
    return beginn, ende


def _uebergaenge(status: dict[str, Any]) -> list[tuple[datetime, bool]]:
    """Zeitlich sortierte (Zeitpunkt, gesund?)-Übergänge aus Ereignissen, ersatzweise aus Ergebnissen."""
    ereignisse = status.get("events") or []
    if ereignisse:
        paare = [
            (zeitstempel(str(e["timestamp"])), str(e.get("type")) != "UNHEALTHY")
            for e in ereignisse
            if e.get("type") in ("HEALTHY", "UNHEALTHY") and e.get("timestamp")
        ]
    else:
        paare = [
            (zeitstempel(str(r["timestamp"])), bool(r.get("success")))
            for r in status.get("results") or []
            if r.get("timestamp")
        ]
    return sorted(paare)


def stoerungen_im_monat(status: dict[str, Any], beginn: datetime, ende: datetime, jetzt: datetime) -> list[Stoerung]:
    stoerungen: list[Stoerung] = []
    offen: datetime | None = None
    for zeitpunkt, gesund in _uebergaenge(status):
        if not gesund and offen is None:
            offen = zeitpunkt
        elif gesund and offen is not None:
            stoerungen.append(Stoerung(offen, zeitpunkt))
            offen = None
    if offen is not None:
        stoerungen.append(Stoerung(offen, min(jetzt, ende)))
    # Auf den Monat zuschneiden; Störungen ganz außerhalb fallen weg.
    ergebnis = []
    for s in stoerungen:
        von, bis = max(s.beginn, beginn), min(s.ende, ende)
        if bis > von:
            ergebnis.append(Stoerung(von, bis))
    return ergebnis


def sammle_dienste(basis_url: str, monat: str, *, jetzt: datetime | None = None, timeout: float = 10.0) -> list[Dienst]:
    jetzt = jetzt or datetime.now(tz=UTC)
    beginn, ende = monatsgrenzen(monat)
    dienste = []
    for status in lade_statuses(basis_url, timeout):
        key = str(status.get("key") or "")
        dienste.append(
            Dienst(
                name=str(status.get("name") or key),
                gruppe=str(status.get("group") or ""),
                key=key,
                uptime_30d=lade_uptime(basis_url, key, timeout=timeout) if key else None,
                stoerungen=stoerungen_im_monat(status, beginn, ende, jetzt),
            )
        )
    return sorted(dienste, key=lambda d: (d.gruppe, d.name))


# ---------------------------------------------------------------------------
# Bericht
# ---------------------------------------------------------------------------


def _dauer(delta: timedelta) -> str:
    minuten = int(delta.total_seconds() // 60)
    return f"{minuten // 60} h {minuten % 60:02d} min" if minuten >= 60 else f"{minuten} min"


def _prozent(wert: float | None) -> str:
    return "n. v." if wert is None else f"{wert * 100:.3f} %"


def bericht_markdown(monat: str, dienste: list[Dienst], *, ziel: float = ZIEL_PROZENT, quelle: str = "") -> str:
    beginn, ende = monatsgrenzen(monat)
    monatslaenge = ende - beginn
    mit_wert = [d.uptime_30d for d in dienste if d.uptime_30d is not None]
    gesamt = sum(mit_wert) / len(mit_wert) if mit_wert else None
    ausfall_gesamt = sum((d.ausfallzeit for d in dienste), timedelta())
    stoerungen_gesamt = sum(len(d.stoerungen) for d in dienste)

    zeilen = [
        f"# Verfügbarkeitsbericht {monat}",
        "",
        f"- Zeitraum: {beginn:%d.%m.%Y} bis {(ende - timedelta(days=1)):%d.%m.%Y} (UTC)",
        f"- Zielwert: {ziel:g} % je Dienst",
        f"- Gesamtverfügbarkeit (Mittel über alle Dienste, letzte 30 Tage): **{_prozent(gesamt)}**",
        f"- Störungen im Monat: {stoerungen_gesamt}, Ausfallzeit zusammen {_dauer(ausfall_gesamt)}",
        f"- Quelle: Statusseite {quelle or '(Gatus)'}, abgerufen {datetime.now(tz=UTC):%d.%m.%Y %H:%M} UTC",
        "",
        "| Dienst | Gruppe | Verfügbarkeit (30 Tage) | Ziel erreicht | Störungen | Ausfallzeit | Verfügbarkeit aus Ereignissen |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in dienste:
        aus_ereignissen = 1 - d.ausfallzeit / monatslaenge
        erreicht = "n. v." if d.uptime_30d is None else ("ja" if d.uptime_30d * 100 >= ziel else "**nein**")
        zeilen.append(
            f"| {d.name} | {d.gruppe or '–'} | {_prozent(d.uptime_30d)} | {erreicht} | {len(d.stoerungen)} "
            f"| {_dauer(d.ausfallzeit)} | {aus_ereignissen * 100:.3f} % |"
        )
    stoerungsliste = [(d, s) for d in dienste for s in d.stoerungen]
    if stoerungsliste:
        zeilen += ["", "## Störungen", "", "| Dienst | Beginn (UTC) | Ende (UTC) | Dauer |", "|---|---|---|---|"]
        for d, s in sorted(stoerungsliste, key=lambda paar: paar[1].beginn):
            zeilen.append(f"| {d.name} | {s.beginn:%d.%m.%Y %H:%M} | {s.ende:%d.%m.%Y %H:%M} | {_dauer(s.dauer)} |")
    zeilen += [
        "",
        "## Methodik und Grenzen",
        "",
        "- Die Spalte „Verfügbarkeit (30 Tage)“ ist der Gatus-Wert für die letzten 30 Tage ab Abruf, nicht der "
        "Kalendermonat. Am Monatsersten für den Vormonat erzeugt, weicht sie höchstens um einen Tag ab.",
        "- Störungen und Ausfallzeit stammen aus den Ereignissen der Statusseite innerhalb des Monats; "
        "„Verfügbarkeit aus Ereignissen“ ist 1 − Ausfallzeit ÷ Monatslänge.",
        "- Alle Dienste laufen auf derselben Instanz; eine Aufteilung je Mandant ist nicht möglich und "
        "würde nichts anderes zeigen. Der Bericht gilt für alle Mandanten gemeinsam.",
        "",
    ]
    return "\n".join(zeilen)
