# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Test- und Systemkonten aus dem RIS erkennen (Issue #1258).

Ratsinformationssysteme führen neben den Mandatsträgerinnen und Mandatsträgern Konten, die keine Personen der
Kommunalpolitik sind, und liefern sie über OParl mit aus. Das Bürgerportal blendet sie in öffentlichen Listen und
Mitgliederzahlen aus; die Daten selbst bleiben unverändert im Bestand (nichts wird gelöscht, die OParl-Ausgabe bleibt
ein Spiegel der Quelle).

Zwei kleine Regeln, bewusst eng gefasst – lieber ein Konto zu viel sichtbar als eine echte Person versteckt:

**Systemrolle (je Mitgliedschaft):** Die Quelle kennzeichnet einen Zugang zum RIS oder einen Verteiler als Rolle in
einem Gremium, etwa „Informationssystem“ (Münster, Düsseldorf), „Zugriff auf das Ratsinfo“ und „Digitaler Druck“ (Köln)
oder „Vollzugriff“ (Wuppertal). Solche Mitgliedschaften sind kein Sitz im Gremium; ausgeblendet wird nur die
Mitgliedschaft, die Person mit ihren übrigen Mitgliedschaften bleibt. Verwaltungsrollen wie „Schriftführung“ oder
„Gast“ sind echte Funktionen und bleiben sichtbar.

**Testkonto (je Person):** Der Name zeigt ein Test- oder Platzhalterkonto, und es gibt keinen echten Vornamen:

- Vorname ist selbst ein Testwort („Test Person“),
- ohne Vornamen (leer oder nur Ziffern) endet ein Wortteil auf „test“, „dummy“ oder „ausleihe“, gefolgt höchstens
  von Ziffern („ratsinfotest“, „ms-r-test“, „Test Beuel“, „34 Dummy_34“, „CDU-Ausleihe1“), der Name trägt eine
  Kontonummer mit „#“ („Presse#1“) oder der ganze Name ist „Gast“, „Admin“, „Administrator“ bzw. „session“,
- die bekannten Platzhalter „Max Mustermann“ und „Erika Mustermann“.

Wer einen echten Vornamen hat, ist nie ein Testkonto (außer den Platzhaltern): „Hans Test“ bleibt sichtbar.

Beide Regeln gibt es als Python-Prüfung (``ist_testkonto``, ``ist_systemrolle``) und als ``Q``-Ausdruck für die
Datenbank (``testkonto_q``, ``systemrolle_q``, ``ausgeblendet_q``) mit denselben Mustern; reguläre Ausdrücke nur in
der Schnittmenge von Python (SQLite) und PostgreSQL.
"""

from __future__ import annotations

import re
from typing import Any, Final

from django.db.models import Q

#: Buchstaben und Ziffern eines Wortteils; alles andere (Leerzeichen, Bindestrich, Unterstrich) trennt
_WORTZEICHEN: Final = "a-z0-9äöüß"

#: Rollen, mit denen die Quelle einen Zugang zum RIS statt eines Sitzes kennzeichnet (ganze Rolle, ohne Groß/klein)
SYSTEMROLLE: Final = (
    r"^\s*(informationssystem|vollzugriff|zugriff auf das ratsinfo(rmationssystem)?|digitaler druck)\s*$"
)

#: Kein echter Vorname: leer oder nur Ziffern („34 Dummy_34“)
VORNAME_FEHLT: Final = r"^\s*[0-9]*\s*$"
#: Der Vorname selbst ist ein Testwort („Test Person“)
VORNAME_TESTWORT: Final = r"^\s*(test|dummy)[0-9]*\s*$"
#: Ein Wortteil endet auf ein Testwort, danach höchstens Ziffern („ratsinfotest“, „ms-r-test“, „Dummy_34“)
TESTWORT: Final = rf"(test|dummy|ausleihe)[0-9]*([^{_WORTZEICHEN}]|$)"
#: Der ganze Name ist ein Funktionswort statt eines Namens, oder er trägt eine Kontonummer („Presse#1“)
FUNKTIONSKONTO: Final = r"^\s*(gast|admin|administrator|session)\s*$|#[0-9]+"
#: Bekannte Platzhalter
PLATZHALTER_VORNAME: Final = r"^\s*(max|erika)\s*$"
PLATZHALTER_NACHNAME: Final = r"^\s*mustermann\s*$"
PLATZHALTER_NAME: Final = r"^\s*(max|erika)\s+mustermann\s*$"


def _passt(muster: str, wert: str | None) -> bool:
    return re.search(muster, wert or "", re.IGNORECASE) is not None


def ist_systemrolle(rolle: str | None) -> bool:
    """Kennzeichnet die Rolle einen Zugang zum RIS („Informationssystem“) statt eines Sitzes im Gremium?"""
    return _passt(SYSTEMROLLE, rolle)


def ist_testkonto(person: Any) -> bool:
    """Ist die Person ein Test-, Platzhalter- oder Funktionskonto des RIS (Regeln: Moduldokumentation)?"""
    vorname = getattr(person, "given_name", None)
    nachname = getattr(person, "family_name", None)
    name = getattr(person, "name", None)
    if _passt(VORNAME_TESTWORT, vorname):
        return True
    if _passt(PLATZHALTER_NAME, name) or (
        _passt(PLATZHALTER_VORNAME, vorname) and _passt(PLATZHALTER_NACHNAME, nachname)
    ):
        return True
    if not _passt(VORNAME_FEHLT, vorname):
        return False
    return any(_passt(muster, wert) for muster in (TESTWORT, FUNKTIONSKONTO) for wert in (name, nachname))


def _feld(prefix: str, name: str) -> str:
    return f"{prefix}__{name}" if prefix else name


def systemrolle_q(prefix: str = "") -> Q:
    """Mitgliedschaften mit Systemrolle; ``prefix`` für Beziehungen (z. B. ``"memberships"``)."""
    return Q(**{f"{_feld(prefix, 'role')}__iregex": SYSTEMROLLE})


def testkonto_q(prefix: str = "") -> Q:
    """Personen, die Testkonten sind; ``prefix`` für Beziehungen (z. B. ``"person"`` an Mitgliedschaften)."""
    vorname = _feld(prefix, "given_name")
    nachname = _feld(prefix, "family_name")
    name = _feld(prefix, "name")
    vorname_fehlt = Q(**{f"{vorname}__isnull": True}) | Q(**{f"{vorname}__iregex": VORNAME_FEHLT})
    name_zeigt_testkonto = (
        Q(**{f"{name}__iregex": TESTWORT})
        | Q(**{f"{nachname}__iregex": TESTWORT})
        | Q(**{f"{name}__iregex": FUNKTIONSKONTO})
        | Q(**{f"{nachname}__iregex": FUNKTIONSKONTO})
    )
    platzhalter = Q(**{f"{name}__iregex": PLATZHALTER_NAME}) | (
        Q(**{f"{vorname}__iregex": PLATZHALTER_VORNAME}) & Q(**{f"{nachname}__iregex": PLATZHALTER_NACHNAME})
    )
    return Q(**{f"{vorname}__iregex": VORNAME_TESTWORT}) | platzhalter | (vorname_fehlt & name_zeigt_testkonto)


def ausgeblendet_q(prefix: str = "") -> Q:
    """Mitgliedschaften, die öffentliche Listen nicht zeigen: Systemrolle oder Person ist ein Testkonto."""
    return systemrolle_q(prefix) | testkonto_q(_feld(prefix, "person"))
