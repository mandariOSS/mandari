# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstellungen › Anwendungen: eigene Anwendungen der Kommune über die Anmeldung von mandari (Issue #1591).

Die Verwaltung registriert Anwendungen, die sich über mandari anmelden und im Namen von Personen auf Session
zugreifen, etwa eine eigene Rats-App. Regeln, Protokoll und Formular kommen aus der Plattform
(``apps.accounts.oidc.registration``); Session nennt nur den Bereich (``session-mandant:<Kennung>``) und prüft das
Recht „Einstellungen verwalten“ wie bei den Einreichungs-Zugängen. Ohne eingeschalteten Anbieter gibt es die Seite
nicht (404, nach der Prüfung des Rechts). Eine Anwendung der Kommune gilt nur bei ihr und erhält nur die Scopes, die Session für Kommunen anmeldet
(``apps.session.api.nichtoeffentlich.BINDUNG``); Nichtöffentliches gehört nicht dazu.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.views.generic import TemplateView

from apps.accounts.oidc import bindings
from apps.accounts.oidc.registration_views import OwnedApplicationsMixin
from apps.session.permissions import SessionViewMixin


class OidcApplicationsView(SessionViewMixin, OwnedApplicationsMixin, TemplateView):
    """Anwendungen der Kommune anzeigen, registrieren, sperren und löschen."""

    template_name = "session/settings/anwendungen.html"
    permission_required = "manage_settings"

    def oidc_owner(self) -> str:
        tenant: Any = self.session_tenant
        return bindings.compose(bindings.TENANT, str(tenant.pk))

    def oidc_page_url(self) -> str:
        tenant: Any = self.session_tenant
        return reverse("session:settings_applications", kwargs={"tenant_slug": tenant.slug})

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        overrides = {key: kwargs.pop(key) for key in ("form", "neu") if key in kwargs}
        parent: Any = super()
        context: dict[str, Any] = parent.get_context_data(**kwargs)
        context |= self.oidc_context(**overrides)
        return context

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        result = self.oidc_post(request)
        if isinstance(result, HttpResponse):
            return result
        return self.render_to_response(self.get_context_data(form=result["oidc_form"], neu=result["oidc_neu"]))
