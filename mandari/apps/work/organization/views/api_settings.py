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
from apps.work.faction.audit import log_event
from apps.work.faction.models import FactionPublicApiAccess


class OrganizationApiSettingsView(WorkViewMixin, TemplateView):
    """API-Reiter in den Organisationseinstellungen."""

    template_name = "work/organization/api_settings.html"
    permission_required = "faction.manage"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "api"

        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        context["can_manage_faction"] = checker.has_permission("faction.manage")

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
        access = FactionPublicApiAccess.for_organization(self.organization)
        access.is_enabled = request.POST.get("api_enabled") == "on"
        access.show_location = request.POST.get("api_show_location") == "on"
        access.show_agenda = request.POST.get("api_show_agenda") == "on"

        def _int(name, current, lo, hi):
            try:
                return max(lo, min(int(request.POST.get(name, current)), hi))
            except (TypeError, ValueError):
                return current

        access.past_days = _int("api_past_days", access.past_days, 0, 3650)
        access.future_days = _int("api_future_days", access.future_days, 1, 3650)
        access.cache_seconds = _int("api_cache_seconds", access.cache_seconds, 0, 86400)

        # CORS-Origins: nur http(s)-Ursprünge übernehmen
        raw_origins = request.POST.get("api_allowed_origins", "")
        origins = []
        for candidate in raw_origins.replace("\n", ",").split(","):
            candidate = candidate.strip().rstrip("/")
            if candidate.startswith(("https://", "http://")) and " " not in candidate:
                origins.append(candidate)
        access.allowed_origins = ", ".join(dict.fromkeys(origins))

        access.save(
            update_fields=[
                "is_enabled",
                "past_days",
                "future_days",
                "show_location",
                "show_agenda",
                "cache_seconds",
                "allowed_origins",
                "updated_at",
            ]
        )
        log_event(
            "api_settings_changed",
            access,
            organization=self.organization,
            membership=self.membership,
            is_internal=False,
            changes={
                "is_enabled": access.is_enabled,
                "past_days": access.past_days,
                "future_days": access.future_days,
                "show_location": access.show_location,
                "show_agenda": access.show_agenda,
                "cache_seconds": access.cache_seconds,
                "allowed_origins": access.allowed_origins,
            },
        )
        messages.success(
            request,
            "API-Einstellungen gespeichert." if access.is_enabled else "Öffentliche API deaktiviert.",
        )

    def _api_regenerate(self, request):
        """API-Token erneuern — bisherige URLs werden sofort ungültig (auditiert)."""
        access = FactionPublicApiAccess.for_organization(self.organization)
        access.regenerate()
        log_event(
            "api_settings_changed",
            access,
            organization=self.organization,
            membership=self.membership,
            is_internal=False,
            changes={"token": "erneuert"},
        )
        messages.success(
            request,
            "API-Token erneuert. Bisherige API-URLs sind ab sofort ungültig — "
            "bitte die Einbindung auf der Webseite aktualisieren.",
        )
