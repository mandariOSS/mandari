# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstellungen „Sitzungsformate“ (Issue #138): Landesprofil und Nachweis der örtlichen Rechtsgrundlage.

Der Mandant wählt sein Land (Kommunalverfassungsrecht für hybride und digitale Sitzungen) und weist die
Regelung in Hauptsatzung bzw. Geschäftsordnung mit Datum und Fundstelle nach. Ohne Landesprofil sind nur
Präsenzsitzungen möglich. Jede Änderung wird im Audit-Log festgehalten.
"""

from __future__ import annotations

from typing import Any, cast

from django import forms
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionStateProfile, SessionTenant
from ..permissions import SessionViewMixin

_log_event = cast(Any, audit).log_event

AUDITED_FIELDS = (
    "state_profile",
    "hybrid_basis_kind",
    "hybrid_basis_date",
    "hybrid_basis_reference",
    "digital_public_registration_days",
)
BASIS_FIELDS = ("hybrid_basis_kind", "hybrid_basis_date", "hybrid_basis_reference")
MAX_REGISTRATION_DAYS = 30


class MeetingFormatSettingsForm(forms.ModelForm):  # type: ignore[type-arg]
    """Landesprofil und Nachweis; Art, Datum und Fundstelle nur gemeinsam."""

    class Meta:
        model = SessionTenant
        fields = list(AUDITED_FIELDS)
        widgets = {"hybrid_basis_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        profile_field = cast(Any, self.fields["state_profile"])
        profile_field.queryset = SessionStateProfile.objects.order_by("name")
        profile_field.empty_label = "Kein Landesprofil (nur Präsenzsitzungen)"
        kind_field = cast(Any, self.fields["hybrid_basis_kind"])
        kind_field.choices = [("", "Kein Nachweis"), *[choice for choice in kind_field.choices if choice[0]]]

    def clean_hybrid_basis_reference(self) -> str:
        return str(self.cleaned_data.get("hybrid_basis_reference") or "").strip()

    def clean_digital_public_registration_days(self) -> int | None:
        days: int | None = self.cleaned_data.get("digital_public_registration_days")
        if days is not None and days > MAX_REGISTRATION_DAYS:
            raise forms.ValidationError(f"Höchstens {MAX_REGISTRATION_DAYS} Tage.")
        return days

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        if any(cleaned.get(name) for name in BASIS_FIELDS):
            for name in BASIS_FIELDS:
                if not cleaned.get(name):
                    self.add_error(name, "Für den Nachweis bitte Art, Datum und Fundstelle angeben.")
        return cleaned


def _snapshot(tenant: SessionTenant) -> dict[str, str]:
    return {name: "" if getattr(tenant, name) is None else str(getattr(tenant, name)) for name in AUDITED_FIELDS}


class MeetingFormatSettingsView(SessionViewMixin, TemplateView):
    """Landesprofil wählen, Rechtsgrundlage nachweisen, Rechtslage des Landes mit Quellen anzeigen."""

    template_name = "session/settings/meeting_formats.html"
    permission_required = "manage_settings"

    @property
    def tenant(self) -> SessionTenant:
        return cast(SessionTenant, self.session_tenant)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = cast(Any, super()).get_context_data(**kwargs)
        context.setdefault("form", MeetingFormatSettingsForm(instance=self.tenant))
        context["profile"] = self.tenant.state_profile
        return context

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        tenant = self.tenant
        before = _snapshot(tenant)
        form = MeetingFormatSettingsForm(request.POST, instance=tenant)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        form.save()
        after = _snapshot(tenant)
        changes = {
            name: {"alt": before[name], "neu": after[name]} for name in AUDITED_FIELDS if before[name] != after[name]
        }
        if changes:
            _log_event(
                "update",
                tenant,
                tenant=tenant,
                user=self.session_user,
                request=request,
                changes={"sitzungsformate": changes},
            )
        messages.success(request, "Einstellungen zu Sitzungsformaten gespeichert.")
        return redirect("session:settings_meeting_formats", tenant_slug=tenant.slug)
