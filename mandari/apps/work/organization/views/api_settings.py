# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigener Einstellungs-Reiter „API" für die öffentliche Fraktions-API v1.

Bündelt alle API-Optionen (bisher ein Abschnitt in den
Fraktionssitzungs-Einstellungen) und erweitert sie:
Zeitfenster (Vergangenheit/Zukunft), Inhaltsumfang (Ort/Tagesordnung),
CORS-Origins, Cache-Dauer, Nutzungsstatistik und ein fertiges
Einbindungs-Snippet für die Fraktions-Webseite.
"""

from django.conf import settings as django_settings
from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.work.faction.models import FactionPublicApiAccess

from .. import selectors, services


class OrganizationApiSettingsView(WorkViewMixin, TemplateView):
    """API-Reiter in den Organisationseinstellungen."""

    template_name = "work/organization/api_settings.html"
    permission_required = "faction.manage"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "api"
        context["can_manage_faction"] = selectors.permission_checker(self.membership).has_permission("faction.manage")

        access = FactionPublicApiAccess.for_organization(self.organization)
        site_url = getattr(django_settings, "SITE_URL", "").rstrip("/")
        api_base_url = f"{site_url}/api/public/v1/fraktionen/{access.token}/"
        context.update(
            {
                "api_access": access,
                "api_base_url": api_base_url,
                "api_meetings_url": f"{api_base_url}sitzungen/",
                "api_openapi_url": f"{site_url}/api/public/v1/openapi.json",
                "docs_url": "https://mandari.de/docs/fraktions-api/",
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        section = request.POST.get("section", "")
        handler = {
            "api_save": self._api_save,
            "api_regenerate": self._api_regenerate,
        }.get(section)
        if handler is None:
            messages.error(request, "Ungültige Aktion.")
        else:
            handler(request)
        return redirect("work:organization_api_settings", org_slug=self.organization.slug)

    def _api_save(self, request):
        """Alle API-Optionen speichern (auditiert)."""
        data = services.ApiSettingsInput(
            is_enabled=request.POST.get("api_enabled") == "on",
            show_location=request.POST.get("api_show_location") == "on",
            show_agenda=request.POST.get("api_show_agenda") == "on",
            past_days=request.POST.get("api_past_days"),
            future_days=request.POST.get("api_future_days"),
            cache_seconds=request.POST.get("api_cache_seconds"),
            allowed_origins_raw=request.POST.get("api_allowed_origins", ""),
        )
        enabled = services.save_api_settings(self.organization, self.membership, data)
        messages.success(request, "API-Einstellungen gespeichert." if enabled else "Öffentliche API deaktiviert.")

    def _api_regenerate(self, request):
        """API-Token erneuern — bisherige URLs werden sofort ungültig (auditiert)."""
        services.regenerate_api_token(self.organization, self.membership)
        messages.success(
            request,
            "API-Token erneuert. Bisherige API-URLs sind ab sofort ungültig — "
            "bitte die Einbindung auf der Webseite aktualisieren.",
        )
