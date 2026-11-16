# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin des OpenID-Connect-Anbieters (Issue #1206).

Die Bibliothek registriert ihre Modelle selbst und nimmt dafür die Klassen aus ``OAUTH2_PROVIDER``
(``APPLICATION_ADMIN_CLASS`` usw.); deshalb steht hier keine eigene Registrierung.

- Anwendungen werden hier oder mit ``manage.py oidc_anwendung`` angelegt (dasselbe Formular, #1215). Erlaubt sind
  Code-Ablauf und Client Credentials, Scopes aus dem Katalog, Signatur RS256. Das Client-Secret wird beim Speichern
  gehasht und ist danach nicht mehr lesbar. Dienste können an genau einen Bereich gebunden sein und sich mit
  ``private_key_jwt`` ausweisen (öffentliche Schlüssel als JWKS, #565); gebundene Dienste nur so. Anlegen, Ändern und Löschen stehen im Sicherheitsprotokoll; wird eine
  Anwendung ausgeschaltet, sind alle ihre Tokens widerrufen.
- Tokens, Codes und ID-Tokens sind nur lesbar; Tokens lassen sich widerrufen (mit Eintrag im Protokoll).
  Tokens stehen ohnehin nur als Prüfsumme in der Datenbank.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from oauth2_provider.admin import (
    AccessTokenAdmin,
    ApplicationAdmin,
    GrantAdmin,
    IDTokenAdmin,
    RefreshTokenAdmin,
)
from oauth2_provider.forms import ApplicationForm
from oauth2_provider.models import revoke_access_token
from unfold.admin import ModelAdmin

from apps.common.admin_mixins import ReadOnlyAdminMixin

from . import audit, catalog, services
from .models import ALLOWED_GRANT_TYPES, OAuthApplication


class OAuthApplicationForm(ApplicationForm):  # type: ignore[misc]  # Bibliothek ohne Typen
    allowed_scopes = forms.MultipleChoiceField(
        label="Erlaubte Scopes",
        required=False,
        choices=[(scope.name, f"{scope.name} – {scope.description}") for scope in catalog.CATALOG],
        widget=forms.CheckboxSelectMultiple,
        help_text=(
            "Im Namen einer Person: openid, profile, email, ratsdaten:lesen, einreichung:schreiben, "
            "ratsdaten:nichtoeffentlich (zweiter Faktor Pflicht, höchstens 7 Tage nach der Anmeldung). "
            "Für Dienste: ratsdaten:lesen, einreichung:schreiben, mandari:bereitstellung; "
            "sitzung:lesen nur mit Bindung an einen Mandanten."
        ),
    )
    jwks = forms.JSONField(
        label="Öffentliche Schlüssel (JWKS)",
        required=False,
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text=(
            "Für private_key_jwt (RFC 7523): JWK Set mit den öffentlichen Schlüsseln der Anwendung, nur RS256 "
            "(RSA ab 2048 Bit) oder ES256 (P-256). Dann gilt kein Client-Secret. Leer: Anmeldung mit Client-Secret."
        ),
    )

    class Meta:
        model = OAuthApplication
        fields = (
            "name",
            "client_id",
            "client_secret",
            "client_type",
            "authorization_grant_type",
            "redirect_uris",
            "allowed_scopes",
            "skip_authorization",
            "algorithm",
            "is_active",
            "binding",
            "jwks",
        )
        labels = {
            "name": "Name",
            "client_id": "Client-ID",
            "client_secret": "Client-Secret",
            "client_type": "Art des Clients",
            "authorization_grant_type": "Ablauf",
            "redirect_uris": "Redirect-URIs",
            "skip_authorization": "Vertrauenswürdig (ohne Zustimmungsseite)",
            "algorithm": "Signatur des ID-Tokens",
            "binding": "Bindung",
        }
        help_texts = {
            "name": "Erscheint auf der Zustimmungsseite und unter „Verbundene Anwendungen“.",
            "client_type": "Öffentlich: App ohne Secret (etwa auf dem Telefon), nur mit PKCE. Vertraulich: mit Secret.",
            "redirect_uris": "Durch Leerzeichen getrennt; sie müssen exakt passen. Nur https.",
            "skip_authorization": "Nur für eigene Anwendungen von mandari. Fremde Anwendungen fragen immer nach.",
            "algorithm": "RS256 für den Code-Ablauf, leer für Dienste.",
            "binding": (
                "Nur für Dienste: genau ein Bereich, etwa „session-mandant:<Kennung des Mandanten>“. Am einfachsten mit "
                "manage.py oidc_anwendung anlegen --mandant <Kurzname>."
            ),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        grant_field = self.fields.get("authorization_grant_type")
        if grant_field is not None:
            grant_field.choices = [
                (value, label) for value, label in OAuthApplication.GRANT_TYPES if value in ALLOWED_GRANT_TYPES
            ]
        algorithm_field = self.fields.get("algorithm")
        if algorithm_field is not None:
            algorithm_field.choices = [
                (OAuthApplication.NO_ALGORITHM, "Keine (Dienst)"),
                (OAuthApplication.RS256_ALGORITHM, "RS256"),
            ]
        secret_field = self.fields.get("client_secret")
        if secret_field is not None and not (self.instance and self.instance.pk):
            secret_field.help_text = "Jetzt kopieren und sicher übergeben. Nach dem Speichern ist es nur noch gehasht."
        elif secret_field is not None:
            secret_field.help_text = "Gehasht, nicht mehr lesbar. Für ein neues Secret einen neuen Wert eintragen und vor dem Speichern kopieren."


class OAuthApplicationAdmin(ApplicationAdmin, ModelAdmin):  # type: ignore[misc]  # Bibliothek ohne Typen
    form = OAuthApplicationForm
    list_display = ("name", "client_id", "authorization_grant_type", "client_type", "skip_authorization", "is_active")
    list_filter = ("authorization_grant_type", "client_type", "skip_authorization", "is_active")
    search_fields = ("name", "client_id")
    readonly_fields = ("created", "updated")
    fieldsets = (
        ("Anwendung", {"fields": ("name", "is_active", "skip_authorization")}),
        ("Zugangsdaten", {"fields": ("client_id", "client_secret", "client_type", "jwks")}),
        ("Ablauf", {"fields": ("authorization_grant_type", "redirect_uris", "allowed_scopes", "algorithm", "binding")}),
        ("Info", {"fields": ("created", "updated"), "classes": ("collapse",)}),
    )

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        super().save_model(request, obj, form, change)
        audit.write(
            audit.EVENT_APPLICATION,
            request,
            user_ref=getattr(request.user, "pk", None),
            aktion="geändert" if change else "angelegt",
            anwendung=obj.client_id,
            name=obj.name,
            # Nur Feldnamen, nie Werte (ein neues Secret erscheint als „client_secret“)
            felder=sorted(form.changed_data) if change else None,
        )
        if change and "is_active" in form.changed_data and not obj.is_active:
            # Ausgeschaltet: alle Tokens widerrufen, damit beim Wiedereinschalten kein alter Zugang auflebt (#1215)
            services.revoke_application(request, obj, admin=str(getattr(request.user, "pk", "")))

    def delete_model(self, request: HttpRequest, obj: Any) -> None:
        client_id, name = obj.client_id, obj.name
        super().delete_model(request, obj)
        self._log_deleted(request, client_id, name)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> None:
        deleted = list(queryset.values_list("client_id", "name"))
        super().delete_queryset(request, queryset)
        for client_id, name in deleted:
            self._log_deleted(request, client_id, name)

    @staticmethod
    def _log_deleted(request: HttpRequest, client_id: str, name: str) -> None:
        audit.write(
            audit.EVENT_APPLICATION,
            request,
            user_ref=getattr(request.user, "pk", None),
            aktion="gelöscht",
            anwendung=client_id,
            name=name,
        )


def _log_admin_revocation(request: HttpRequest, token: Any) -> None:
    audit.write(
        audit.EVENT_REVOKED,
        request,
        user_ref=getattr(token, "user_id", None),
        anwendung=getattr(token.application, "client_id", ""),
        durch=audit.BY_ADMINISTRATION,
        admin=str(getattr(request.user, "pk", "")),
    )


class OAuthAccessTokenAdmin(ReadOnlyAdminMixin, AccessTokenAdmin, ModelAdmin):  # type: ignore[misc]
    list_display = ("pk", "user", "application", "scope", "expires", "created")

    @admin.action(description="Ausgewählte Access-Tokens widerrufen", permissions=["delete"])
    def revoke_tokens(self, request: HttpRequest, queryset: QuerySet[Any]) -> None:
        count = 0
        for token in queryset.select_related("application"):
            _log_admin_revocation(request, token)
            revoke_access_token(token)
            count += 1
        self.message_user(request, f"{count} Access-Token(s) widerrufen.")


class OAuthRefreshTokenAdmin(ReadOnlyAdminMixin, RefreshTokenAdmin, ModelAdmin):  # type: ignore[misc]
    list_display = ("pk", "user", "application", "created", "revoked")

    @admin.action(description="Ausgewählte Refresh-Tokens widerrufen", permissions=["delete"])
    def revoke_tokens(self, request: HttpRequest, queryset: QuerySet[Any]) -> None:
        count = 0
        for token in queryset.filter(revoked__isnull=True).select_related("application"):
            _log_admin_revocation(request, token)
            token.revoke()
            count += 1
        self.message_user(request, f"{count} Refresh-Token(s) widerrufen.")


class OAuthIDTokenAdmin(ReadOnlyAdminMixin, IDTokenAdmin, ModelAdmin):  # type: ignore[misc]
    pass


class OAuthGrantAdmin(ReadOnlyAdminMixin, GrantAdmin, ModelAdmin):  # type: ignore[misc]
    pass
