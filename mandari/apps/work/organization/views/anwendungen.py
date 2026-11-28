# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstellungen › Mitglieder und Rechte › Anwendungen: eigene Anwendungen der Organisation (Issue #1591).

Die Organisation registriert Anwendungen, die sich über mandari anmelden und im Namen ihrer Mitglieder zugreifen,
etwa eine eigene App der Fraktion. Regeln, Protokoll und Formular kommen aus der Plattform
(``apps.accounts.oidc.registration``); Work nennt nur den Bereich (``work-organisation:<Kennung>``) und prüft das Recht
„Organisation bearbeiten“. Ohne eingeschalteten Anbieter gibt es die Seite nicht (404, nach der
Prüfung des Rechts).
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.generic import TemplateView

from apps.accounts.oidc.registration_views import OwnedApplicationsMixin
from apps.common.mixins import WorkViewMixin

from .. import anwendungen


class OrganizationApplicationsView(WorkViewMixin, OwnedApplicationsMixin, TemplateView):
    """Anwendungen der Organisation anzeigen, registrieren, sperren und löschen."""

    template_name = "work/organization/anwendungen.html"
    permission_required = "organization.edit"

    def oidc_owner(self) -> str:
        organization: Any = self.organization
        return anwendungen.owner(organization.pk)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        overrides = {key: kwargs.pop(key) for key in ("form", "neu") if key in kwargs}
        parent: Any = super()
        context: dict[str, Any] = parent.get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "anwendungen"
        context |= self.oidc_context(**overrides)
        return context

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        result = self.oidc_post(request)
        if isinstance(result, HttpResponse):
            return result
        return self.render_to_response(self.get_context_data(form=result["oidc_form"], neu=result["oidc_neu"]))
