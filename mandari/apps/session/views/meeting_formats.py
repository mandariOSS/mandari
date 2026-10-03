# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstellungen „Sitzungsformate“ (Issue #138): Landesprofil und Nachweis der örtlichen Rechtsgrundlage.

Der Mandant wählt sein Land (Kommunalverfassungsrecht für hybride und digitale Sitzungen) und weist die
Regelung in Hauptsatzung bzw. Geschäftsordnung mit Datum und Fundstelle nach. Ohne Landesprofil sind nur
Präsenzsitzungen möglich. Jede Änderung wird im Audit-Log festgehalten.

Seit Issue #757 zeigt die Seite das Sitzungsrecht des Landes in der Fassung zu einem Stichtag (``?stichtag=``,
Standard heute) und führt zum **Ortsrecht** je Körperschaft (Hauptsatzung und Geschäftsordnung,
``BodyLocalRulesView``).
"""

from __future__ import annotations

from datetime import date
from functools import cached_property
from typing import Any, cast

from django import forms
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionBody, SessionStateProfile, SessionTenant
from ..permissions import SessionViewMixin
from ..services import body_service, meeting_format_service, state_law_service
from ..services.state_law_service import LocalRules

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

    def _stichtag(self) -> date | None:
        """Stichtag der Rechtsübersicht aus ``?stichtag=JJJJ-MM-TT``; ungültig oder leer: heute."""
        raw = self.request.GET.get("stichtag", "")
        try:
            return date.fromisoformat(raw) if raw else None
        except ValueError:
            return None

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = cast(Any, super()).get_context_data(**kwargs)
        context.setdefault("form", MeetingFormatSettingsForm(instance=self.tenant))
        context["profile"] = self.tenant.state_profile
        if self.tenant.state_profile is not None:
            # Sitzungsrecht in der Fassung zum Stichtag (Issue #757)
            context["law"] = state_law_service.effective(self.tenant.state_profile, self._stichtag())
            context["law_versions"] = state_law_service.versions(self.tenant.state_profile)
            # Hinweis am Nachweis der Hauptsatzungsregel (z. B. Zweidrittelmehrheit, § 64 Abs. 3 Satz 4 NKomVG)
            # nach dem heute geltenden Recht, unabhängig vom Stichtag der Rechtsübersicht
            context["basis_hint"] = state_law_service.effective(self.tenant.state_profile).entries.get(
                "remote_basis_hint"
            )
        # Je Körperschaft: Ortsrecht und Warnung vor dem Ablauf eines Notlagenbeschlusses
        context["local_rules"] = []
        for body in body_service.bodies(self.tenant, include_inactive=False):
            rules = LocalRules.of(body)
            context["local_rules"].append((body, rules, state_law_service.emergency_warning(rules)))
        # Ausgenommene Ausschussarten (z. B. NRW): Welche Gremien sind eingeordnet, welche nicht?
        context["committee_kinds"] = meeting_format_service.committee_kind_overview(self.tenant)
        context["can_manage_organizations"] = cast(Any, self).has_permission("manage_organizations")
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


# =============================================================================
# Ortsrecht je Körperschaft (Issue #757)
# =============================================================================

_DAYS: dict[str, Any] = {"min_value": 0, "max_value": state_law_service.MAX_DAYS, "required": False}


class LocalRulesForm(forms.Form):
    """
    Hauptsatzung und Geschäftsordnung einer Körperschaft. Nachweise nur vollständig (Datum und Fundstelle);
    der Notlagenbeschluss mit Datum und Ablauf, höchstens so lange, wie das Landesrecht erlaubt.
    """

    remote_per_invitation = forms.BooleanField(label="Zuschaltung je Ladung zulassen", required=False)
    remote_public_only = forms.BooleanField(label="Zuschaltung nur in öffentlichen Sitzungen", required=False)
    recording_date = forms.DateField(label="Bild- und Tonaufnahmen: Hauptsatzung vom", required=False)
    recording_reference = forms.CharField(label="Fundstelle", required=False, max_length=255)
    video_public_date = forms.DateField(label="Öffentlichkeit per Video: Hauptsatzung vom", required=False)
    video_public_reference = forms.CharField(label="Fundstelle", required=False, max_length=255)
    emergency_date = forms.DateField(label="Notlagenbeschluss vom", required=False)
    emergency_until = forms.DateField(label="gilt bis", required=False)
    emergency_reference = forms.CharField(label="Fundstelle", required=False, max_length=255)
    rules_date = forms.DateField(label="Geschäftsordnung vom", required=False)
    rules_reference = forms.CharField(label="Fundstelle", required=False, max_length=255)
    invitation_days = forms.IntegerField(label="Ladungsfrist (Tage)", **_DAYS)
    invitation_day_kind = forms.ChoiceField(
        label="Zählweise", choices=state_law_service.DAY_KIND_CHOICES, required=False
    )
    deadline_start = forms.ChoiceField(
        label="Fristbeginn", choices=state_law_service.DEADLINE_START_CHOICES, required=False
    )
    urgent_days = forms.IntegerField(label="Ladungsfrist im Eilfall (Tage)", **_DAYS)
    urgent_notice = forms.CharField(label="Pflichthinweis in der Ladung im Eilfall", required=False, max_length=500)
    motion_days = forms.IntegerField(label="Antragsfrist (Tage vor der Sitzung)", **_DAYS)
    question_days = forms.IntegerField(label="Frist für Anfragen (Tage vor der Sitzung)", **_DAYS)
    minutes_signers = forms.CharField(label="Unterzeichnende der Niederschrift", required=False, max_length=500)
    voting_methods = forms.CharField(label="Abstimmungsarten", required=False, max_length=500)
    committees_public = forms.ChoiceField(
        label="Ausschüsse tagen", choices=state_law_service.COMMITTEE_PUBLICITY_CHOICES, required=False
    )
    residents_questions_minutes = forms.IntegerField(label="Einwohnerfragestunde (Minuten)", **_DAYS)

    #: Nachweise in der Hauptsatzung: Datum und Fundstelle nur gemeinsam
    BASIS_PAIRS = (("recording_date", "recording_reference"), ("video_public_date", "video_public_reference"))

    def __init__(self, *args: Any, law: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.law = law

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        for name in LocalRules.TEXT_FIELDS:
            cleaned[name] = str(cleaned.get(name) or "").strip()
        for day_field, reference_field in self.BASIS_PAIRS:
            if bool(cleaned.get(day_field)) != bool(cleaned.get(reference_field)) and day_field not in self.errors:
                self.add_error(day_field, "Für den Nachweis bitte Datum und Fundstelle angeben.")
        if cleaned.get("rules_reference") and not cleaned.get("rules_date") and "rules_date" not in self.errors:
            self.add_error("rules_date", "Bitte das Datum der Geschäftsordnung angeben.")
        start, until = cleaned.get("emergency_date"), cleaned.get("emergency_until")
        if (start or until or cleaned.get("emergency_reference")) and not (start and until):
            self.add_error("emergency_until", "Für den Notlagenbeschluss bitte Datum und Ablauf angeben.")
        elif start and until:
            if until < start:
                self.add_error("emergency_until", "Der Ablauf liegt vor dem Beschluss.")
            elif self.law is not None:
                months = self.law.value("emergency_max_months")
                if months and until > state_law_service.add_months(start, int(months)):
                    norm = self.law.norm("emergency_resolution")
                    self.add_error(
                        "emergency_until",
                        f"Ein Notlagenbeschluss gilt höchstens {months} Monate{f' ({norm})' if norm else ''}.",
                    )
        return cleaned

    def rules(self) -> LocalRules:
        return LocalRules.from_json(
            {name: value for name, value in self.cleaned_data.items() if value not in (None, "")}
        )


def _initial(rules: LocalRules) -> dict[str, Any]:
    """Vorbelegung; Datumsangaben als ISO-Text, damit das Datumsfeld des Browsers sie anzeigt."""
    initial = {name: getattr(rules, name) for name in LocalRulesForm.base_fields}
    return {name: value.isoformat() if isinstance(value, date) else value for name, value in initial.items()}


class BodyLocalRulesView(SessionViewMixin, TemplateView):
    """Ortsrecht einer Körperschaft pflegen (Recht „Einstellungen verwalten“); Änderungen im Prüfprotokoll."""

    template_name = "session/settings/local_rules.html"
    permission_required = "manage_settings"

    @cached_property
    def body(self) -> SessionBody:
        return get_object_or_404(SessionBody, pk=self.kwargs["body_id"], tenant=self.session_tenant)

    @cached_property
    def law(self) -> Any:
        profile = cast(SessionTenant, self.session_tenant).state_profile
        return state_law_service.effective(profile) if profile is not None else None

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = cast(Any, super()).get_context_data(**kwargs)
        rules = LocalRules.of(self.body)
        context.setdefault("form", LocalRulesForm(initial=_initial(rules), law=self.law))
        context["body"] = self.body
        context["law"] = self.law
        context["emergency_warning"] = state_law_service.emergency_warning(rules)
        return context

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = LocalRulesForm(request.POST, law=self.law)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        body = self.body
        before = LocalRules.of(body).to_json()
        after = form.rules().to_json()
        if before != after:
            # Ohne Speichersignal: Der Eintrag unten nennt die geänderten Regeln einzeln; das Signal des
            # Prüfprotokolls schriebe sonst einen zweiten Eintrag für dieselbe Änderung
            body.local_rules = after
            body.updated_at = timezone.now()
            SessionBody.objects.filter(pk=body.pk).update(local_rules=after, updated_at=body.updated_at)
            changes = {
                name: {"alt": before.get(name, ""), "neu": after.get(name, "")}
                for name in sorted(set(before) | set(after))
                if before.get(name) != after.get(name)
            }
            _log_event(
                "update",
                body,
                tenant=self.session_tenant,
                user=self.session_user,
                request=request,
                changes={"ortsrecht": changes},
            )
        messages.success(request, f"Ortsrecht für „{body.name}“ gespeichert.")
        return redirect("session:settings_local_rules", tenant_slug=body.tenant.slug, body_id=body.pk)
