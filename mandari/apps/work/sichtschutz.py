# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sichtschutz für Nichtöffentliches in ganz Work (Issues #1166, #1201).

Titel und Inhalte nichtöffentlicher TOPs stehen in Work zunächst verdeckt auf dem Bildschirm, auch für Personen, die
sie sehen dürfen: Titel weichgezeichnet, Unterlagen, Protokoll und Beschluss hinter einer Abdeckung, Vorleser
bekommen einen Ersatztext. Erst aktives Einblenden zeigt sie, damit beim Teilen des Bildschirms oder am Beamer nichts
mitzulesen ist.

Ein Schalter für ganz Work: Der Zustand liegt in der Django-Session (``SITZUNGS_SCHLUESSEL``) und gilt damit für alle
Seiten und Tabs der angemeldeten Sitzung. Standard ist verdeckt, und jede Anmeldung beginnt wieder verdeckt
(``bei_anmeldung_verdecken``). Der Kontextprozessor ``sichtschutz_kontext`` gibt den Zustand allen Vorlagen, damit
der Server jede Seite schon richtig ausliefert (Klasse ``noe-verdeckt`` am ``<body>`` der beiden Work-Rahmen, kein
Aufblitzen). Umschalten: Knopf in der Kopfleiste und die Schalter der Sitzungsansicht, Alpine-Store ``noe``
(``frontend/alpine/noe-sichtschutz.ts``), der den Endpunkt ``sichtschutz_setzen`` aufruft und offene Tabs per
BroadcastChannel mitnimmt.

Nur Sichtschutz gegen Mitlesen: Wer den nichtöffentlichen Teil nicht sehen darf, bekommt ihn wie bisher gar nicht vom
Server (``apps.work.faction.visibility``). Darstellung: Abschnitt „Sichtschutz“ in ``static/css/fraktionssitzung.css``.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_http_methods

#: Schlüssel in der Django-Session; fehlt er, ist Nichtöffentliches verdeckt
SITZUNGS_SCHLUESSEL = "work_noe_sichtbar"


def ist_sichtbar(request: HttpRequest | None) -> bool:
    """Hat die angemeldete Person Nichtöffentliches eingeblendet? Ohne Session oder Anmeldung: verdeckt."""
    if request is None:
        return False
    user = getattr(request, "user", None)
    session = getattr(request, "session", None)
    if session is None or user is None or not user.is_authenticated:
        return False
    return session.get(SITZUNGS_SCHLUESSEL) is True


def setzen(request: HttpRequest, sichtbar: bool) -> None:
    """Zustand der Session setzen; verdeckt heißt: Schlüssel entfernen (wie nach der Anmeldung)."""
    if sichtbar:
        request.session[SITZUNGS_SCHLUESSEL] = True
    else:
        request.session.pop(SITZUNGS_SCHLUESSEL, None)


class Sichtschutz:
    """Kontext ``noe_schutz`` für die Rahmen: Rechteprüfung erst, wenn eine Vorlage den Knopf zeichnet."""

    def __init__(self, request: HttpRequest) -> None:
        self._request = request
        self.sichtbar = ist_sichtbar(request)

    @cached_property
    def knopf(self) -> bool:
        """Knopf in der Kopfleiste nur für Personen, die in dieser Organisation Nichtöffentliches sehen dürfen."""
        from apps.work.faction.visibility import can_view_internal

        return can_view_internal(getattr(self._request, "membership", None))

    @cached_property
    def url(self) -> str:
        return reverse("work:noe_sichtschutz")


def sichtschutz_kontext(request: HttpRequest) -> dict[str, Any]:
    """
    Kontextprozessor: ``noe_sichtbar`` (bool) für alle Vorlagen, in Work zusätzlich ``noe_schutz`` (Knopf, Adresse).

    Ohne Anfrage (etwa beim Zeichnen für WebSocket-Nachrichten) gibt es keinen Kontextprozessor; ``noe_sichtbar``
    fehlt dann und gilt in den Vorlagen als verdeckt. Der Store im Browser gleicht danach ab.
    """
    kontext: dict[str, Any] = {"noe_sichtbar": ist_sichtbar(request)}
    if getattr(request, "organization", None) is not None:
        kontext["noe_schutz"] = Sichtschutz(request)
    return kontext


@login_required
@require_http_methods(["GET", "POST"])
def sichtschutz_setzen(request: HttpRequest) -> HttpResponse:
    """
    GET: Zustand abfragen (Abgleich nach „Zurück“ aus dem Browser-Cache). POST ``sichtbar=1`` blendet ein, jeder andere
    Wert verdeckt. Unabhängig von der Organisation, CSRF-geschützt; die Rechte ändert das nicht.
    """
    if request.method == "POST":
        setzen(request, request.POST.get("sichtbar") == "1")
    antwort = JsonResponse({"sichtbar": ist_sichtbar(request)})
    antwort["Cache-Control"] = "no-store"
    return antwort


def bei_anmeldung_verdecken(sender: Any, request: HttpRequest | None = None, **kwargs: Any) -> None:
    """Jede Anmeldung beginnt verdeckt, auch wenn Django die bisherige Session weiterführt (gleiches Konto)."""
    session = getattr(request, "session", None)
    if session is not None:
        session.pop(SITZUNGS_SCHLUESSEL, None)


def register() -> None:
    """Signal verbinden (idempotent über dispatch_uid); aufgerufen in ``WorkConfig.ready``."""
    from django.contrib.auth.signals import user_logged_in

    user_logged_in.connect(bei_anmeldung_verdecken, dispatch_uid="work_noe_sichtschutz_anmeldung")
