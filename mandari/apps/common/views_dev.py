# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Entwicklungsansichten: Vorschau der Komponentenbibliothek (UI-Kit).

Die Seite wird nur bei ``DEBUG=True`` registriert (siehe ``mandari/urls.py``)
und dient als lebende Dokumentation aller Cotton-Komponenten unter
``templates/cotton/``. Der Snapshot-Test in ``apps/common/tests/test_components.py``
rendert dieselbe Vorlage, damit Komponentenänderungen nicht unbemerkt brechen.
"""

from __future__ import annotations

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

    if not settings.DEBUG:
        raise Http404
    return render(
        request,
        "dev/ui_kit.html",
        {"form": build_demo_form(), "empty_form": UiKitDemoForm()},
    )
