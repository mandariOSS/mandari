# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sichtschutz für Nichtöffentliches in ganz Work (Issues #1166, #1201).

Titel und Inhalte nichtöffentlicher TOPs stehen in Work zunächst verdeckt auf dem Bildschirm, auch für Personen, die
sie sehen dürfen: Titel weichgezeichnet, Unterlagen, Protokoll und Beschluss hinter einer Abdeckung, Vorleser
bekommen einen Ersatztext. Erst aktives Einblenden zeigt sie, damit beim Teilen des Bildschirms oder am Beamer nichts
mitzulesen ist.

Ein Schalter für ganz Work, gespeichert als Cookie ``COOKIE`` im Browser: Es gilt für alle Seiten und Tabs. Sein Wert
ist eine Marke der Anmeldung (HMAC über Sitzungsschlüssel und eine Kennung, die jede Anmeldung neu setzt, ``marke``);
der Server zeigt Nichtöffentliches nur eingeblendet, wenn das Cookie genau diese Marke trägt. Damit beginnt jede
Anmeldung verdeckt, auch wenn Django den Sitzungsschlüssel weiterführt (erneute Anmeldung desselben Kontos). Der
Browser setzt und löscht das Cookie selbst (Alpine-Store ``noe``, ``frontend/alpine/noe-sichtschutz.ts``), sofort und
ohne Anfrage an den Server. Der Schalter selbst steht bewusst nicht in der Django-Session: ``SESSION_SAVE_EVERY_REQUEST``
schreibt bei gleichzeitigen Anfragen ältere Fassungen zurück, ein Verdecken könnte so verloren gehen. Die Session hält
nur die Kennung der Anmeldung; geht sie so verloren, ändert sich die Marke und alles ist wieder verdeckt.

Der Kontextprozessor ``sichtschutz_kontext`` gibt Zustand und Marke allen Vorlagen, damit der Server jede Seite schon
richtig ausliefert (Klasse ``noe-verdeckt`` am ``<body>`` der beiden Work-Rahmen, kein Aufblitzen).

Nur Sichtschutz gegen Mitlesen: Wer den nichtöffentlichen Teil nicht sehen darf, bekommt ihn wie bisher gar nicht vom
Server (``apps.work.faction.visibility``). Die Marke ist kein Geheimnis und gibt keine Rechte. Darstellung: Abschnitt
„Sichtschutz“ in ``static/css/fraktionssitzung.css``.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any

from django.http import HttpRequest
from django.utils.crypto import constant_time_compare, get_random_string, salted_hmac

#: Name des Cookies; fehlt es oder passt es nicht zur Anmeldung, ist Nichtöffentliches verdeckt
COOKIE = "mandari_noe"
#: Kennung der Anmeldung in der Session; geht sie verloren (ältere Fassung zurückgeschrieben), ist wieder verdeckt
ANMELDUNG = "work_noe_anmeldung"
_SALZ = "apps.work.sichtschutz"


def marke_fuer(session_key: str, anmeldung: str = "") -> str:
    """Wert des Cookies für eine Anmeldung (Sitzungsschlüssel und Kennung); verrät beides nicht."""
    return salted_hmac(_SALZ, f"{session_key}:{anmeldung}", algorithm="sha256").hexdigest()


def marke(request: HttpRequest | None) -> str:
    """Marke der angemeldeten Sitzung; ohne Anmeldung oder gespeicherte Session leer (dann bleibt alles verdeckt)."""
    if request is None:
        return ""
    user = getattr(request, "user", None)
    session = getattr(request, "session", None)
    if session is None or user is None or not user.is_authenticated or not session.session_key:
        return ""
    return marke_fuer(session.session_key, str(session.get(ANMELDUNG, "")))


def ist_sichtbar(request: HttpRequest | None) -> bool:
    """Hat die angemeldete Person Nichtöffentliches eingeblendet (Cookie mit der Marke dieser Anmeldung)?"""
    erwartet = marke(request)
    if not erwartet or request is None:
        return False
    return constant_time_compare(request.COOKIES.get(COOKIE, ""), erwartet)


class Sichtschutz:
    """Kontext ``noe_schutz`` für die Rahmen: Rechteprüfung erst, wenn eine Vorlage den Knopf zeichnet."""

    def __init__(self, request: HttpRequest) -> None:
        self._request = request

    @cached_property
    def knopf(self) -> bool:
        """Knopf in der Kopfleiste nur für Personen, die in dieser Organisation Nichtöffentliches sehen dürfen."""
        from apps.work.faction.visibility import can_view_internal

        return can_view_internal(getattr(self._request, "membership", None))


def sichtschutz_kontext(request: HttpRequest) -> dict[str, Any]:
    """
    Kontextprozessor: ``noe_sichtbar`` (bool) und ``noe_marke`` für alle Vorlagen, in Work zusätzlich ``noe_schutz``.

    Ohne Anfrage (etwa beim Zeichnen für WebSocket-Nachrichten) gibt es keinen Kontextprozessor; ``noe_sichtbar``
    fehlt dann und gilt in den Vorlagen als verdeckt. Der Store im Browser gleicht danach ab.
    """
    kontext: dict[str, Any] = {"noe_sichtbar": ist_sichtbar(request), "noe_marke": marke(request)}
    if getattr(request, "organization", None) is not None:
        kontext["noe_schutz"] = Sichtschutz(request)
    return kontext


def bei_anmeldung(sender: Any, request: HttpRequest | None = None, **kwargs: Any) -> None:
    """Jede Anmeldung bekommt eine neue Kennung und damit eine neue Marke: Sie beginnt verdeckt."""
    session = getattr(request, "session", None)
    if session is not None:
        session[ANMELDUNG] = get_random_string(12)


def register() -> None:
    """Signal verbinden (idempotent über dispatch_uid); aufgerufen in ``WorkConfig.ready``."""
    from django.contrib.auth.signals import user_logged_in

    user_logged_in.connect(bei_anmeldung, dispatch_uid="work_noe_sichtschutz_anmeldung")
