# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dialoge im Django-Admin: Ansichten, die der Admin als Dialog einbettet, dürfen von Seiten derselben
Herkunft gerahmt werden (Issue #686).

django-unfold öffnet ab 0.107 die Verweise neben Auswahlfeldern (Bezugsobjekt hinzufügen, ändern,
ansehen, löschen, nachschlagen) in einem Dialog mit ``<iframe>`` statt in einem eigenen Fenster. Die
eingebettete Seite ist eine gewöhnliche Admin-Ansicht mit dem Parameter ``_popup``. Unfold erlaubt das
Einbetten selbst nur für Listen- und Formularansicht; die Löschbestätigung bekäme die Voreinstellung
``X-Frame-Options: DENY`` und bliebe im Dialog leer.

Die Ausnahme gilt nur für Admin-Pfade mit ``_popup`` und nur für dieselbe Herkunft. Alle anderen
Antworten behalten die Voreinstellung der ``XFrameOptionsMiddleware``; fremde Seiten können den Admin
weiterhin nicht einbetten.
"""

from __future__ import annotations

from collections.abc import Callable

from django.contrib.admin.options import IS_POPUP_VAR
from django.http import HttpRequest, HttpResponseBase

ADMIN_PREFIX = "/admin/"


class AdminDialogFrameMiddleware:
    """Setzt ``X-Frame-Options: SAMEORIGIN`` für Admin-Ansichten im Dialog.

    Muss in ``MIDDLEWARE`` **nach** ``XFrameOptionsMiddleware`` stehen: Antworten laufen in
    umgekehrter Reihenfolge zurück, und jene Middleware lässt einen bereits gesetzten Kopf stehen.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        if self._is_admin_dialog(request) and not response.has_header("X-Frame-Options"):
            response["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @staticmethod
    def _is_admin_dialog(request: HttpRequest) -> bool:
        if not request.path.startswith(ADMIN_PREFIX):
            return False
        # Formulare im Dialog senden den Parameter als verstecktes Feld mit (Bestätigung einer Löschung)
        return IS_POPUP_VAR in request.GET or (request.method == "POST" and IS_POPUP_VAR in request.POST)
