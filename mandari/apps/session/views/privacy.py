# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DSGVO-Paket — Views (Issue #43).

- PrivacySettingsView: Aufbewahrungsfristen je Datenart + Text der
  öffentlichen Datenschutz-Hinweisseite (manage_settings).
- PrivacyPurgeRunView: Anonymisierungs-/Löschlauf aus der UI starten
  (nachweisbar auditiert; Dry-Run möglich).
- PersonDataExportView: Betroffenenauskunft — Export aller gespeicherten
  Daten zu einer Person als JSON (Art. 15 DSGVO); Bankdaten nur mit
  manage_allowances entschlüsselt. Jeder Export wird auditiert.
- PrivacyNoticeView: öffentliche Hinweisseite "Datenschutz im RIS" je
  Mandant (ohne Login).
"""

import logging

from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionPerson, SessionTenant
from ..permissions import SessionPermissionChecker, SessionViewMixin
from ..services import privacy_service

logger = logging.getLogger(__name__)


class PrivacySettingsView(SessionViewMixin, TemplateView):
    """Datenschutz-Einstellungen: Fristen je Datenart + Hinweisseiten-Text."""

    template_name = "session/settings/privacy.html"
    permission_required = "manage_settings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["privacy"] = privacy_service.get_privacy_settings(self.session_tenant)
        context["persons_for_export"] = SessionPerson.objects.filter(tenant=self.session_tenant).order_by(
            "family_name", "given_name"
        )
        return context

    def post(self, request, *args, **kwargs):
        tenant = self.session_tenant
        settings = tenant.settings or {}
        privacy = settings.setdefault("privacy", {})

        old = privacy_service.get_privacy_settings(tenant)
        for key in ("persons_years", "audit_years", "np_content_years"):
            try:
                privacy[key] = max(0, min(int(request.POST.get(key, 0) or 0), 100))
            except (TypeError, ValueError):
                privacy[key] = 0
        privacy["notice"] = (request.POST.get("notice") or "").strip()[:20000]

        tenant.settings = settings
        tenant.save(update_fields=["settings", "updated_at"])

        new = privacy_service.get_privacy_settings(tenant)
        audit.log_event(
            "update",
            tenant,
            tenant=tenant,
            user=self.session_user,
            request=request,
            changes={
                "dsgvo_einstellungen": {
                    "personen_jahre": {"alt": old["persons_years"], "neu": new["persons_years"]},
                    "audit_jahre": {"alt": old["audit_years"], "neu": new["audit_years"]},
                    "noe_jahre": {"alt": old["np_content_years"], "neu": new["np_content_years"]},
                    "hinweisseite_geaendert": old["notice"] != new["notice"],
                }
            },
        )
        messages.success(request, "Datenschutz-Einstellungen gespeichert.")
        return redirect("session:privacy_settings", tenant_slug=tenant.slug)


class PrivacyPurgeRunView(SessionViewMixin, View):
    """Anonymisierungs-/Löschlauf für den Mandanten starten (auditiert)."""

    permission_required = "manage_settings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        dry_run = request.POST.get("dry_run") == "1"
        stats = privacy_service.run_privacy_purge(
            self.session_tenant, dry_run=dry_run, user=self.session_user, request=request
        )
        prefix = "Probelauf: " if dry_run else "Löschlauf abgeschlossen: "
        messages.success(
            request,
            f"{prefix}{stats['persons_anonymized']} Person(en) anonymisiert, "
            f"{stats['np_meetings_cleared']} Sitzung(en) NÖ-Inhalte geleert, "
            f"{stats['audit_deleted']} Audit-Eintrag/-Einträge gelöscht.",
        )
        if stats["skipped"]:
            messages.info(request, "Übersprungen: " + ", ".join(stats["skipped"]))
        return redirect("session:privacy_settings", tenant_slug=tenant_slug)


class PersonDataExportView(SessionViewMixin, View):
    """Betroffenenauskunft als JSON-Download (Art. 15 DSGVO, auditiert)."""

    permission_required = "manage_settings"

    def get(self, request, tenant_slug, person_id):
        person = get_object_or_404(SessionPerson, pk=person_id, tenant=self.session_tenant)

        include_bank = SessionPermissionChecker(self.session_user).has_permission("manage_allowances")
        data = privacy_service.subject_access_export(self.session_tenant, person, include_bank=include_bank)

        audit.log_event(
            "download",
            person,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"dsgvo_auskunft": {"bankdaten_entschluesselt": include_bank}},
        )

        response = JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})
        response["Content-Disposition"] = (
            f'attachment; filename="auskunft-{person.family_name.lower()}-{person.given_name.lower()}.json"'
        )
        return response


class PrivacyNoticeView(TemplateView):
    """Öffentliche Hinweisseite "Datenschutz im RIS" je Mandant (ohne Login)."""

    template_name = "pages/public/session_privacy.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tenant = SessionTenant.objects.filter(slug=kwargs.get("tenant_slug"), is_active=True).first()
        if tenant is None:
            raise Http404("Mandant nicht gefunden")
        context["tenant"] = tenant
        context["notice"] = privacy_service.get_privacy_settings(tenant)["notice"]
        return context
