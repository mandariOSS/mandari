# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Demo-Instanz (Issue #99).

Die Demo läuft als eigene Installation mit eigener Datenbank und nur synthetischen Daten
(``setup_demo_environment``); die Zugangsdaten stehen öffentlich auf der Website. Der Schalter
``DEMO_INSTANCE`` macht aus einer normalen Installation diese Demo:

- Kein Mailversand: jedes Backend wird zum Speicher-Backend (``mail_backends.build_backend``).
- Keine Aufrufe an frei eintragbare Adressen: KI-Funktionen sind abgeschaltet.
- Besucher dürfen das gemeinsame Konto nicht kapern: Passwort, zweiter Faktor,
  Sicherheitsschlüssel, Registrierung, „Passwort vergessen“ und Kontolöschung sind gesperrt.
- Ein Hinweis auf jeder Seite sagt, dass alles erfunden ist und wann die Demo neu aufgebaut wird
  (``DEMO_NEUAUFBAU_UTC``).
- Die Anmeldeseite zeigt alle Demo-Zugänge mit dem öffentlichen Passwort und meldet mit einem Klick an.

Außerhalb der Demo-Instanz ist nichts davon aktiv.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.utils import timezone

from apps.common.demo_daten import (
    DEMO_LEITSTELLE_EMAIL,
    DEMO_OPARL_PREFIX,
    DEMO_ORG_SLUG,
    DEMO_SESSION_SLUG,
    DEMO_USERS,
)

#: Seiten, deren POST in der Demo gesperrt ist (``namespace:name``).
GESPERRT_IN_DER_DEMO = frozenset(
    {
        "work:security",  # Passwort, zweiter Faktor, Sitzungen
        "work:profile_data",  # Datenauskunft und Kontolöschung
        "accounts:two_factor_enroll",
        "accounts:security_keys",
        "accounts:webauthn_register_options",
        "accounts:webauthn_register",
        "accounts:register",
        "accounts:self_register",
        "accounts:self_register_confirm",
        "accounts:password_reset",
        "accounts:password_reset_confirm",
    }
)

HINWEIS_GESPERRT = (
    "In der Demo-Umgebung nicht möglich: Alle Besucher teilen sich dieses Konto. "
    "Passwort, zweiter Faktor und Konto lassen sich deshalb nicht ändern."
)


def ist_demo() -> bool:
    return bool(getattr(settings, "DEMO_INSTANCE", False))


class DemoInstanceMiddleware:
    """Weist in der Demo-Instanz POST-Anfragen an gesperrte Seiten mit einem Hinweis ab."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_view(
        self, request: HttpRequest, view_func: Any, view_args: Any, view_kwargs: Any
    ) -> HttpResponse | None:
        if not ist_demo() or request.method != "POST":
            return None
        match = getattr(request, "resolver_match", None)
        if match is None or match.view_name not in GESPERRT_IN_DER_DEMO:
            return None
        messages.warning(request, HINWEIS_GESPERRT)
        return HttpResponseRedirect(request.get_full_path())


def neuaufbau_uhrzeiten() -> list[str]:
    """Zeiten des automatischen Neuaufbaus in Ortszeit (``DEMO_NEUAUFBAU_UTC``, z. B. "03:30,15:30" → 4:30, 16:30)."""
    roh = str(getattr(settings, "DEMO_NEUAUFBAU_UTC", "") or "")
    heute = timezone.localdate()
    zeiten: list[datetime] = []
    for teil in roh.split(","):
        try:
            stunde, minute = (int(x) for x in teil.strip().split(":"))
            utc = datetime.combine(heute, time(stunde, minute), tzinfo=UTC)
        except ValueError:
            continue
        zeiten.append(timezone.localtime(utc))
    return [f"{z.hour}:{z.minute:02d}" for z in sorted(zeiten, key=lambda z: (z.hour, z.minute))]


def neuaufbau_text() -> str:
    """„zweimal täglich (4:30 und 16:30 Uhr)“ – für den Demo-Hinweis; ohne Zeitplan „regelmäßig“."""
    zeiten = neuaufbau_uhrzeiten()
    if not zeiten:
        return "regelmäßig"
    liste = zeiten[0] if len(zeiten) == 1 else ", ".join(zeiten[:-1]) + " und " + zeiten[-1]
    haeufigkeit = {1: "täglich", 2: "zweimal täglich"}.get(len(zeiten), f"{len(zeiten)}-mal täglich")
    return f"{haeufigkeit} ({liste} Uhr)"


def demo_context(request: HttpRequest) -> dict[str, Any]:
    """Kontextprozessor: ``demo_instance`` und der Zeitplan für den Hinweis in den Basis-Templates."""
    if not ist_demo():
        return {"demo_instance": False}
    return {"demo_instance": True, "demo_neuaufbau": neuaufbau_text()}


@dataclass(frozen=True)
class DemoZugang:
    email: str
    rolle: str
    hinweis: str
    ziel: str  # Seite nach der Anmeldung


@dataclass(frozen=True)
class DemoZugangsgruppe:
    titel: str
    zugaenge: tuple[DemoZugang, ...]


def demo_zugaenge() -> list[DemoZugangsgruppe]:
    """
    Die öffentlichen Demo-Zugänge für die Anmeldeseite – nur in der Demo-Instanz mit festem Passwort.

    Rollen und Ziele passen zu ``setup_demo_environment`` (Fraktion in Work, Verwaltung in Session) und
    ``setup_demo_praesentation`` (Leitstelle). Außerhalb der Demo-Instanz gibt es keine Liste.
    """
    if demo_passwort() is None:
        return []
    work = f"/work/{DEMO_ORG_SLUG}/"
    session = f"/session/{DEMO_SESSION_SLUG}/"

    def zugang(key: str, rolle: str, hinweis: str, ziel: str) -> DemoZugang:
        return DemoZugang(DEMO_USERS[key]["email"], rolle, hinweis, ziel)

    return [
        DemoZugangsgruppe(
            "Fraktion (Work)",
            (
                zugang("vorsitz", "Fraktionsvorsitz", "Alle Rechte in der Musterfraktion", work),
                zugang("mitglied", "Fraktionsmitglied", "Anträge, Sitzungen, Aufgaben", work),
                zugang("sachkundig", "Sachkundige Bürgerin", "Mitarbeit im Bauausschuss", work),
                zugang("unvereidigt", "Neues Mitglied", "Noch nicht vereidigt, ohne nicht-öffentliche Inhalte", work),
                zugang("gast", "Gast", "Nur ein freigegebener Ordner", work),
            ),
        ),
        DemoZugangsgruppe(
            "Verwaltung (Session)",
            (
                zugang("verwaltung", "Administration", "Sitzungsdienst mit allen Rechten", session),
                zugang("sachbearbeitung", "Sachbearbeitung", "Vorlagen und Anträge bearbeiten", session),
                zugang("protokoll", "Protokollführung", "Sitzungen durchführen, Niederschriften", session),
                zugang("lesezugriff", "Lesezugriff", "Alles ansehen, nichts ändern", session),
                DemoZugang(
                    DEMO_LEITSTELLE_EMAIL, "Leitstelle", "Zwei Verwaltungen, Wechsel zwischen ihnen", "/session/"
                ),
            ),
        ),
    ]


def demo_kommune_url() -> str:
    """Bürgerportal der Demo-Kommune Musterstadt (ungelistet, Kennung aus der festen Demo-Kennung)."""
    from mandari_oparl.ids import canonical_id

    return f"/insight/kommune/{canonical_id(f'{DEMO_OPARL_PREFIX}/body/musterstadt')}/"


def demo_passwort() -> str | None:
    """Festes Passwort für die Demo-Konten – nur in der Demo-Instanz und nur, wenn gesetzt.

    Die öffentliche Demo braucht Zugangsdaten, die auf der Website stehen können
    (``DEMO_PASSWORD``). Außerhalb der Demo-Instanz gibt es nie ein festes Passwort: Dort
    erzeugen ``setup_demo_environment`` und ``setup_demo_praesentation`` bei jedem Lauf neue.
    """
    import os

    passwort = os.environ.get("DEMO_PASSWORD", "").strip()
    if not ist_demo() or not passwort:
        return None
    return passwort
