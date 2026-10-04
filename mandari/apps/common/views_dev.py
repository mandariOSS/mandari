# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Entwicklungsansichten: Vorschau der Komponentenbibliothek (UI-Kit).

Die Seite wird nur bei ``DEBUG=True`` (oder ``UI_KIT_PREVIEW=True``, z. B. E2E-Tests) registriert (siehe ``mandari/urls.py``)
und dient als lebende Dokumentation aller Cotton-Komponenten unter
``templates/cotton/``. Der Snapshot-Test in ``apps/common/tests/test_components.py``
rendert dieselbe Vorlage, damit Komponentenänderungen nicht unbemerkt brechen.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from django import forms
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render


class UiKitDemoForm(forms.Form):
    """Formular mit gezielt gesetzten Fehlern für die Vorschau."""

    email = forms.EmailField(label="E-Mail-Adresse")
    password = forms.CharField(label="Passwort", widget=forms.PasswordInput)
    newsletter = forms.BooleanField(label="Newsletter", required=False)
    role = forms.ChoiceField(
        label="Rolle",
        choices=[("member", "Fraktionsmitglied"), ("chair", "Fraktionsvorsitz"), ("staff", "Fraktionspersonal")],
    )
    message = forms.CharField(label="Nachricht", widget=forms.Textarea, required=False)


def build_demo_form() -> UiKitDemoForm:
    """Gebundenes Formular mit Feld- und Formularfehlern für die Vorschau."""
    form = UiKitDemoForm(data={"email": "keine-adresse", "password": "", "role": "chair", "message": "Beispieltext"})
    form.is_valid()
    form.add_error(None, "Beispiel für einen formularweiten Fehler.")
    return form


def ui_kit(request: HttpRequest) -> HttpResponse:
    """Vorschau der Komponentenbibliothek – nur für Entwicklung."""
    from django.conf import settings

    if not (settings.DEBUG or getattr(settings, "UI_KIT_PREVIEW", False)):
        raise Http404
    return render(request, "dev/ui_kit.html", ui_kit_context())


def ui_kit_context() -> dict[str, object]:
    """Beispieldaten der Vorschau (auch für den Snapshot-Test)."""
    return {
        "form": build_demo_form(),
        "empty_form": UiKitDemoForm(),
        "demo_file": _demo_file("Antrag der Verwaltung"),
        "demo_file_barriere": _demo_file("Stellungnahme zum Antrag (nicht barrierefrei)"),
        "demo_treffer": _demo_treffer(),
        "demo_reiter": [
            {"key": "", "label": "Alle", "count": "94", "url": "#", "active": True},
            {"key": "papers", "label": "Vorgänge", "count": "79", "url": "#", "active": False},
            {"key": "files", "label": "Dokumente", "count": "126", "url": "#", "active": False},
        ],
        "demo_zeitraum": [
            {"value": "", "label": "Beliebig", "count": None, "checked": True},
            {"value": "2y", "label": "Letzte 2 Jahre", "count": "31", "checked": False},
        ],
        "demo_art": [
            {"value": "Vorlage", "count": "64", "checked": True},
            {"value": "Antrag", "count": "9", "checked": False},
        ],
        "demo_sortierung": [
            {"value": "relevance", "label": "Relevanz", "checked": True},
            {"value": "newest", "label": "Neueste", "checked": False},
        ],
    }


def _demo_treffer() -> dict[str, object]:
    """Suchtreffer für die Vorschau von c-suche.treffer-vorgang (Form wie search_presentation.present_groups)."""
    from django.utils.safestring import mark_safe

    return {
        "kind": "vorgang",
        "url": "#",
        "title": "Goerdelerstraße / Delpstraße / Von-Witzleben-Straße – VBP Nr. 571",
        "context": ["Vorlage", "V/0226/2018", "Bezirksvertretung Münster-Mitte", "08.05.2018"],
        "status": "Am 08.05.2018 in der Bezirksvertretung Münster-Mitte beschlossen.",
        "status_kind": "decided",
        # feste Beispieldaten ohne Fremdinhalt; nur die Markierung ist HTML
        "snippet": mark_safe(
            'Ausbau der nördlichen <mark class="bg-yellow-200 dark:bg-yellow-800">Von-Witzleben-Straße</mark>'
        ),
        "fundstelle": {"label": "Anlage 2 – Begründung", "url": "#"},
        "others": [{"label": "Anlage 3 – Lageplan", "url": "#"}],
    }


def _demo_file(name: str) -> SimpleNamespace:
    """Anlage für die Vorschau der Dokumentzeile (Attribute wie OParlFile)."""
    return SimpleNamespace(
        id=uuid.uuid5(uuid.NAMESPACE_URL, name),
        name=name,
        file_name="",
        mime_type="application/pdf",
        size=245_760,
        size_human="240 KB",
        text_content="Der Rat möge beschließen:\n\n1. Die Verwaltung wird beauftragt, …",
        download_url="https://ris.example/datei.pdf",
        access_url=None,
    )
