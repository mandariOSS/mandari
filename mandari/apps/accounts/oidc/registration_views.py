# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Oberfläche zum Registrieren eigener Anwendungen für eine Kommune (Session) oder Fraktion (Work), Issue #1591.

Die Fachschicht stellt die Seite mit ihrer View bereit, prüft dort Recht und Bereich und nutzt
:class:`OwnedApplicationsMixin` für Formular, Liste und Aktionen; die Vorlage bindet
``accounts/oidc/_anwendungen_verwalten.html`` ein. Die Regeln selbst prüft :mod:`.registration`.

Ein neues Client-Secret steht nur in der Antwort auf das Anlegen bzw. auf „Neues Secret“ und wird nirgends
zwischengespeichert (auch nicht in der Sitzung). Sperren, Entsperren und Löschen leiten danach auf die Seite zurück.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django import forms
from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect

from apps.common.hilfe import docs_url

from . import bindings, catalog, provider_enabled, registration
from .models import OAuthApplication

#: Art der Anwendung im Formular
CONFIDENTIAL = "vertraulich"
PUBLIC = "oeffentlich"

#: Aktionen des Formulars (``aktion``)
ACTION_CREATE = "anlegen"
ACTION_LOCK = "sperren"
ACTION_UNLOCK = "entsperren"
ACTION_SECRET = "secret"
ACTION_DELETE = "loeschen"


#: Auswahlfelder wie ``c-form.radio`` und ``c-form.checkbox``
_CHOICE_CLASSES = (
    "mt-0.5 shrink-0 w-4 h-4 border-gray-300 dark:border-gray-600 text-primary-600 focus:ring-primary-500 "
    "dark:bg-gray-700"
)


class RegistrationForm(forms.Form):
    """Anwendung im Namen von Personen anlegen; Scopes nur aus denen, die der Bereich vergeben darf."""

    name = forms.CharField(
        label="Name",
        max_length=255,
        help_text="Erscheint auf der Zustimmungsseite und unter „Verbundene Anwendungen“.",
    )
    art = forms.ChoiceField(
        label="Art",
        choices=[
            (CONFIDENTIAL, "Vertraulich: Anwendung auf einem Server, mit Client-Secret"),
            (PUBLIC, "Öffentlich: App im Browser oder auf dem Telefon, ohne Secret"),
        ],
        initial=CONFIDENTIAL,
        widget=forms.RadioSelect(attrs={"class": _CHOICE_CLASSES}),
    )
    redirect_uris = forms.CharField(
        label="Redirect-URIs",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Eine Adresse je Zeile, nur https; sie muss beim Aufruf genau übereinstimmen.",
    )
    scopes = forms.MultipleChoiceField(
        label="Scopes", widget=forms.CheckboxSelectMultiple(attrs={"class": f"rounded-sm {_CHOICE_CLASSES}"})
    )

    def __init__(self, *args: Any, owner: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        scope_field = self.fields["scopes"]
        assert isinstance(scope_field, forms.MultipleChoiceField)
        scope_field.choices = registration.scope_choices(owner)
        scope_field.initial = [catalog.OPENID]

    def redirect_uri_list(self) -> list[str]:
        return [line.strip() for line in str(self.cleaned_data.get("redirect_uris", "")).split() if line.strip()]


@dataclass(frozen=True)
class OwnedApplication:
    """Eine Anwendung des Bereichs für die Anzeige."""

    application: OAuthApplication
    scopes: list[str]
    redirect_uris: list[str]
    confidential: bool


def _display(application: OAuthApplication) -> OwnedApplication:
    return OwnedApplication(
        application=application,
        scopes=list(application.scope_list),
        redirect_uris=str(application.redirect_uris).split(),
        confidential=application.client_type == OAuthApplication.CLIENT_CONFIDENTIAL,
    )


def _nur_eingeschaltet() -> None:
    """Die Seite gibt es nur bei eingeschaltetem Anbieter."""
    if not provider_enabled():
        raise Http404


class OwnedApplicationsMixin:
    """
    Anwendungen eines Bereichs anzeigen, anlegen, sperren, entsperren, mit neuem Secret versehen und löschen.

    Die View liefert :meth:`oidc_owner` (fachfreie Kennung ihres Bereichs) und prüft vorher das Recht; ``get_context_data``
    nimmt :meth:`oidc_context` auf, ``post`` ruft :meth:`oidc_post`. Ohne eingeschalteten Anbieter antworten beide mit
    404, aber erst nach der Prüfung des Rechts: Wer die Seite nicht öffnen darf, bekommt wie überall 403.
    """

    request: HttpRequest

    def oidc_owner(self) -> str:
        raise NotImplementedError

    def oidc_context(
        self, form: RegistrationForm | None = None, neu: registration.Registration | None = None
    ) -> dict[str, Any]:
        _nur_eingeschaltet()
        owner = self.oidc_owner()
        site = str(getattr(settings, "SITE_URL", "") or "").rstrip("/")
        return {
            "oidc_bereich": bindings.describe(owner) or "",
            "oidc_anwendungen": [_display(application) for application in registration.owned(owner)],
            "oidc_form": form if form is not None else RegistrationForm(owner=owner),
            "oidc_neu": neu,
            "oidc_discovery": f"{site}/.well-known/openid-configuration",
            "oidc_doku": docs_url("eigene_anwendungen"),
        }

    def oidc_post(self, request: HttpRequest) -> HttpResponse | dict[str, Any]:
        """Aktion ausführen; Ergebnis: Weiterleitung oder Kontext für die Seite (Formular mit Fehlern, neues Secret)."""
        _nur_eingeschaltet()
        owner = self.oidc_owner()
        action = request.POST.get("aktion", "")
        user = request.user
        application_id = request.POST.get("anwendung", "")
        if action == ACTION_CREATE:
            form = RegistrationForm(request.POST, owner=owner)
            if not form.is_valid():
                return self.oidc_context(form=form)
            try:
                neu = registration.register(
                    request,
                    owner,
                    user=user,
                    name=str(form.cleaned_data["name"]),
                    confidential=form.cleaned_data["art"] == CONFIDENTIAL,
                    redirect_uris=form.redirect_uri_list(),
                    scopes=list(form.cleaned_data["scopes"]),
                )
            except registration.RegistrationError as exc:
                for message in exc.messages:
                    form.add_error(None, message)
                return self.oidc_context(form=form)
            return self.oidc_context(neu=neu)
        if action == ACTION_SECRET:
            erneuert = registration.new_secret(request, owner, application_id, user=user)
            if erneuert is not None:
                return self.oidc_context(neu=erneuert)
            messages.error(request, "Für diese Anwendung gibt es kein Client-Secret.")
        elif action in (ACTION_LOCK, ACTION_UNLOCK):
            active = action == ACTION_UNLOCK
            application = registration.set_active(request, owner, application_id, user=user, active=active)
            if application is None:
                messages.error(request, "Diese Anwendung gibt es hier nicht.")
            elif active:
                messages.success(request, f"„{application.name}“ ist wieder eingeschaltet. Personen stimmen erneut zu.")
            else:
                messages.success(request, f"„{application.name}“ ist gesperrt; alle ihre Zugänge sind widerrufen.")
        elif action == ACTION_DELETE:
            name = registration.delete(request, owner, application_id, user=user)
            if name is None:
                messages.error(request, "Diese Anwendung gibt es hier nicht.")
            else:
                messages.success(request, f"„{name}“ ist gelöscht; alle ihre Zugänge sind widerrufen.")
        else:
            messages.error(request, "Unbekannte Aktion.")
        return redirect(request.path)
