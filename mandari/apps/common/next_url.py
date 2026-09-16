# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Absicherung benutzergelieferter Weiterleitungsziele ("next").

Bisher wurde ein ``next``-Wert quer durch die Anmeldung gereicht und erst ganz
am Ende geprüft (``LoginView.get_success_url``). Das trägt, solange jeder Pfad
dort endet — verlässt ein Wert die Kette früher, fehlt die Prüfung ersatzlos.
Deshalb wird hier an der Annahmestelle geprüft: Was nicht auf die eigene
Herkunft zeigt, wird gar nicht erst weitergereicht.

Das Gegenstück im Frontend ist ``frontend/js/navigation.ts``. Das Session-RIS
hat mit ``apps/session/views/nexturl.py`` eine engere Variante, die zusätzlich
den Mandantenpfad erzwingt.
"""

from django.http import HttpRequest
from django.utils.http import url_has_allowed_host_and_scheme


def safe_next_url(request: HttpRequest, candidate: object, fallback: str = "") -> str:
    """
    Das Ziel, wenn es auf dieselbe Herkunft zeigt, sonst ``fallback``.

    Abgewiesen werden fremde Hosts, schemarelative URLs wie ``//example.org``
    und alles, was kein nichtleerer Text ist.
    """
    if not isinstance(candidate, str) or not candidate:
        return fallback
    if not url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return fallback
    return candidate
