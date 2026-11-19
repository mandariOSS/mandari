# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigene Termine anlegen, ansehen, ändern und löschen (Issue #1489, Schritt 1).

Eine Seite mit einem Formular statt eines Dialogs: Sie funktioniert ohne JavaScript, am Handy und mit der Tastatur.
Wer den Termin nur sehen darf, bekommt dieselbe Seite als Angaben ohne Formular. Sichtbarkeit und Rechte:
``apps.work.termine.termine``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin
from apps.work.rahmen import neues_design

from . import termine

SEITE = "work/termine/termin.html"


def _kalender(ansicht: Any) -> HttpResponse:
    return redirect("work:meetings_calendar", org_slug=ansicht.organization.slug)


class TerminNeuView(WorkViewMixin, TemplateView):
    """Neuen Termin anlegen; ``?tag=2026-11-24`` (Klick im Kalender) füllt den Tag vor."""

    template_name = SEITE
    permission_required = ["meetings.view", termine.ANLEGEN]

    def _formular(self, daten: Any = None) -> termine.TerminForm:
        ansicht: Any = self
        tag = None
        try:
            tag = date.fromisoformat(str(self.request.GET["tag"])[:10]) if "tag" in self.request.GET else None
        except ValueError:
            tag = None
        return termine.TerminForm(daten, membership=ansicht.membership, initial=termine.anfangswerte(None, tag))

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ansicht: Any = super()
        context: dict[str, Any] = ansicht.get_context_data(**kwargs)
        context["active_nav"] = "meetings"
        context["form"] = kwargs.get("form") or self._formular()
        context["darf_aendern"] = True
        return context

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self
        if not neues_design(ansicht.organization):
            return _kalender(ansicht)
        return self.render_to_response(self.get_context_data(**kwargs))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self
        form = self._formular(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form, **kwargs), status=422)
        form.speichern(termine.neu(ansicht.organization, ansicht.membership))
        messages.success(request, "Termin angelegt.")
        return _kalender(ansicht)


class TerminView(WorkViewMixin, TemplateView):
    """Termin ansehen und, wer darf, ändern."""

    template_name = SEITE
    permission_required = "meetings.view"

    def _termin(self) -> Any:
        ansicht: Any = self
        termin = termine.finden(ansicht.membership, self.kwargs["termin_id"])
        if termin is None:
            raise Http404("Termin nicht gefunden.")
        return termin

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ansicht: Any = super()
        context: dict[str, Any] = ansicht.get_context_data(**kwargs)
        eigene: Any = self
        termin = kwargs.get("termin") or self._termin()
        context["active_nav"] = "meetings"
        context["termin"] = termin
        context["darf_aendern"] = termine.darf_aendern(eigene.membership, termin)
        context["form"] = kwargs.get("form") or termine.TerminForm(
            membership=eigene.membership, initial=termine.anfangswerte(termin)
        )
        context["beschreibung"] = context["form"].initial.get("description", "")
        context["zeit"] = termine.zeitangabe(termin)
        context["teilnahmen"] = termine.teilnahmen(termin)
        context["meine_teilnahme"] = termine.meine_teilnahme(termin, eigene.membership)
        return context

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self
        termin = self._termin()
        if not termine.darf_aendern(ansicht.membership, termin):
            raise PermissionDenied("Keine Berechtigung, diesen Termin zu ändern.")
        form = termine.TerminForm(request.POST, membership=ansicht.membership, initial=termine.anfangswerte(termin))
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form, termin=termin, **kwargs), status=422)
        form.speichern(termin)
        messages.success(request, "Termin gespeichert.")
        return _kalender(ansicht)


class TerminLoeschenView(WorkViewMixin, View):
    """Termin löschen (nach Rückfrage auf der Seite)."""

    permission_required = "meetings.view"

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self
        termin = termine.finden(ansicht.membership, kwargs["termin_id"])
        if termin is None:
            raise Http404("Termin nicht gefunden.")
        if not termine.darf_aendern(ansicht.membership, termin):
            raise PermissionDenied("Keine Berechtigung, diesen Termin zu löschen.")
        termin.delete()
        messages.success(request, "Termin gelöscht.")
        return _kalender(ansicht)


class TerminAntwortView(WorkViewMixin, View):
    """Zusage, „vielleicht“ oder Absage der eingeladenen Person (nur die eigene Einladung)."""

    permission_required = "meetings.view"

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self
        termin = termine.finden(ansicht.membership, kwargs["termin_id"])
        if termin is None:
            raise Http404("Termin nicht gefunden.")
        if termine.antworten(
            termin, ansicht.membership, str(request.POST["antwort"]) if "antwort" in request.POST else ""
        ):
            messages.success(request, "Ihre Antwort ist gespeichert.")
        else:
            messages.error(request, "Die Antwort ließ sich nicht speichern.")
        return redirect("work:termin", org_slug=ansicht.organization.slug, termin_id=termin.pk)
