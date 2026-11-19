# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Terminumfrage anlegen, beantworten, um Vorschläge ergänzen, festlegen und löschen (Issue #1489, Schritt 4).

Zwei Seiten ohne Dialog: das Formular für eine neue Umfrage und die Umfrage selbst, auf der die Gefragten antworten
und die anlegende Person einen Vorschlag festlegt. Sichtbarkeit und Rechte: ``apps.work.termine.umfrage``.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin
from apps.work.rahmen import neues_design

from . import termine, umfrage


def _kalender(ansicht: Any) -> HttpResponse:
    return redirect("work:meetings_calendar", org_slug=ansicht.organization.slug)


def _gefunden(ansicht: Any, umfrage_id: Any) -> Any:
    gefunden = umfrage.finden(ansicht.membership, umfrage_id)
    if gefunden is None:
        raise Http404("Terminumfrage nicht gefunden.")
    return gefunden


class UmfrageNeuView(WorkViewMixin, TemplateView):
    """Neue Terminumfrage mit ihren Vorschlägen anlegen."""

    template_name = "work/termine/umfrage_neu.html"
    permission_required = ["meetings.view", termine.ANLEGEN]

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ansicht: Any = super()
        context: dict[str, Any] = ansicht.get_context_data(**kwargs)
        eigene: Any = self
        context["active_nav"] = "meetings"
        context["form"] = kwargs.get("form") or umfrage.UmfrageForm(membership=eigene.membership)
        return context

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self
        if not neues_design(ansicht.organization):
            return _kalender(ansicht)
        return self.render_to_response(self.get_context_data(**kwargs))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self
        form = umfrage.UmfrageForm(request.POST, membership=ansicht.membership)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form, **kwargs), status=422)
        neue = form.speichern(ansicht.organization)
        messages.success(request, "Terminumfrage angelegt. Die Gefragten sind benachrichtigt.")
        return redirect("work:terminumfrage", org_slug=ansicht.organization.slug, umfrage_id=neue.pk)


class UmfrageView(WorkViewMixin, TemplateView):
    """Umfrage ansehen; die Gefragten speichern hier ihre Antworten."""

    template_name = "work/termine/umfrage.html"
    permission_required = "meetings.view"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ansicht: Any = super()
        context: dict[str, Any] = ansicht.get_context_data(**kwargs)
        eigene: Any = self
        gefunden = _gefunden(eigene, self.kwargs["umfrage_id"])
        context["active_nav"] = "meetings"
        context["umfrage"] = gefunden
        context["beschreibung"] = gefunden.get_description_decrypted() or ""
        context["uebersicht"] = umfrage.uebersicht(gefunden, eigene.membership)
        context["darf_antworten"] = gefunden.abgeschlossen_am is None and umfrage.ist_gefragt(
            gefunden, eigene.membership
        )
        context["darf_aendern"] = umfrage.darf_aendern(eigene.membership, gefunden)
        return context

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self
        gefunden = _gefunden(ansicht, kwargs["umfrage_id"])
        if umfrage.antworten(gefunden, ansicht.membership, request.POST):
            messages.success(request, "Ihre Antwort ist gespeichert.")
        else:
            messages.error(request, "Die Antwort ließ sich nicht speichern.")
        return redirect("work:terminumfrage", org_slug=ansicht.organization.slug, umfrage_id=gefunden.pk)


class UmfrageFestlegenView(WorkViewMixin, View):
    """Einen Vorschlag festlegen: Daraus wird der Termin, die Umfrage ist abgeschlossen."""

    permission_required = "meetings.view"

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self
        gefunden = _gefunden(ansicht, kwargs["umfrage_id"])
        if not umfrage.darf_aendern(ansicht.membership, gefunden):
            raise PermissionDenied("Keine Berechtigung, diese Terminumfrage abzuschließen.")
        termin = umfrage.festlegen(
            gefunden, str(request.POST["vorschlag"]) if "vorschlag" in request.POST else "", ansicht.membership
        )
        if termin is None:
            messages.error(request, "Der Vorschlag ließ sich nicht festlegen.")
            return redirect("work:terminumfrage", org_slug=ansicht.organization.slug, umfrage_id=gefunden.pk)
        messages.success(request, "Termin festgelegt. Die Gefragten sind benachrichtigt.")
        return redirect("work:termin", org_slug=ansicht.organization.slug, termin_id=termin.pk)


class UmfrageVorschlagView(WorkViewMixin, View):
    """Einen weiteren Zeitpunkt in die offene Umfrage aufnehmen; die Gefragten ergänzen danach ihre Antwort."""

    permission_required = "meetings.view"

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self
        gefunden = _gefunden(ansicht, kwargs["umfrage_id"])
        if not umfrage.darf_aendern(ansicht.membership, gefunden):
            raise PermissionDenied("Keine Berechtigung, diese Terminumfrage zu ändern.")
        fehler = umfrage.vorschlag_ergaenzen(gefunden, request.POST, ansicht.membership)
        if fehler:
            messages.error(request, fehler)
        else:
            messages.success(request, "Vorschlag ergänzt. Die Gefragten sind benachrichtigt.")
        return redirect("work:terminumfrage", org_slug=ansicht.organization.slug, umfrage_id=gefunden.pk)


class UmfrageLoeschenView(WorkViewMixin, View):
    """Umfrage löschen (nach Rückfrage auf der Seite); ein daraus entstandener Termin bleibt."""

    permission_required = "meetings.view"

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self
        gefunden = _gefunden(ansicht, kwargs["umfrage_id"])
        if not umfrage.darf_aendern(ansicht.membership, gefunden):
            raise PermissionDenied("Keine Berechtigung, diese Terminumfrage zu löschen.")
        gefunden.delete()
        messages.success(request, "Terminumfrage gelöscht.")
        return _kalender(ansicht)
