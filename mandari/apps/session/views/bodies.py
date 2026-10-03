# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Körperschaften im Mandanten (Issue #756): Verwaltung und Filter.

- **Einstellungen → Körperschaften:** anlegen, bearbeiten, Standard festlegen (Recht „Einstellungen verwalten“).
  Die Kachel erscheint erst, wenn der Mandant mehr als eine Körperschaft führt (auch inaktive); die zweite legt der
  Betrieb an (Admin), wenn die Verwaltung mehrere Körperschaften führt. Jede Änderung steht über die Signale im
  Prüfprotokoll.
- **Filter „Körperschaft“** für Listen, Kalender und Arbeitsvorrat (``BodyFilterMixin``): Standard ist die
  Gesamtansicht über alle Körperschaften; Mandanten mit einer Körperschaft sehen keinen Filter.
"""

from __future__ import annotations

import logging
from functools import cached_property
from typing import Any, cast

from django import forms
from django.contrib import messages
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, TemplateView, UpdateView

from ..models import SessionBody, SessionTenant
from ..permissions import SessionViewMixin
from ..services import body_service, tenant_provisioning

logger = logging.getLogger(__name__)

# =============================================================================
# Filter in Listen
# =============================================================================


class BodyFilterMixin:
    """Filter „Körperschaft“ (``?body=<kurzkennung>``); ohne Angabe alle Körperschaften."""

    request: HttpRequest
    session_tenant: Any

    @cached_property
    def body_choice(self) -> body_service.BodyChoice:
        return body_service.choice(self.session_tenant, self.request.GET.get("body", ""))

    def filter_body(self, queryset: QuerySet[Any], prefix: str = "") -> QuerySet[Any]:
        """Nur Objekte der gewählten Körperschaft; ohne Auswahl unverändert."""
        return queryset.filter(self.body_choice.q(prefix)) if self.body_choice.selected else queryset

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = super().get_context_data(**kwargs)  # type: ignore[misc]
        context["body_choice"] = self.body_choice
        return context


# =============================================================================
# Einstellungen → Körperschaften
# =============================================================================


class SessionBodyForm(forms.ModelForm):  # type: ignore[type-arg]
    """
    Körperschaft anlegen oder bearbeiten. Die Datenbankregeln (Kurzkennung je Mandant) prüft das Formular selbst,
    weil der Mandant kein Formularfeld ist – sonst endete eine doppelte Kennung im Serverfehler.
    """

    #: Vorlage je Körperschaftstyp (Issue #757): Gremien, Geschäftsordnung und Standard-TOPs – nur beim Anlegen
    template = forms.ChoiceField(label="Vorlage", required=False)

    class Meta:
        model = SessionBody
        fields = ["name", "short_name", "slug", "body_type", "ags", "rgs", "parent", "is_active"]

    def __init__(self, *args: Any, tenant: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        self.instance.tenant = tenant
        self.templates: dict[str, tenant_provisioning.GremienVorlage] = {}
        if not self.instance._state.adding:
            del self.fields["template"]
        else:
            self.templates = _templates()
            choices = [(key, vorlage.label) for key, vorlage in self.templates.items()]
            self.fields["template"].choices = [("", "Ohne Vorlage"), *choices]  # type: ignore[attr-defined]
        parents = SessionBody.objects.filter(tenant=tenant).order_by("-is_default", "name")
        if self.instance.pk:
            parents = parents.exclude(pk=self.instance.pk)
        self.fields["parent"].queryset = parents  # type: ignore[attr-defined]
        self.fields["slug"].required = False
        self.fields["slug"].help_text = (
            "Leer lassen: bleibt unverändert"
            if self.instance.pk
            else "Leer lassen: wird aus Kurzname bzw. Name gebildet"
        )

    def clean_slug(self) -> str:
        slug = str(self.cleaned_data.get("slug") or "")
        if not slug and self.instance.pk:
            # Beim Bearbeiten bleibt die bisherige Kennung: Filteradressen (?body=…) gelten weiter
            return str(self.instance.slug)
        taken = SessionBody.objects.filter(tenant=self.tenant, slug=slug).exclude(pk=self.instance.pk)
        if slug and taken.exists():
            raise forms.ValidationError("Diese Kurzkennung ist im Mandanten schon vergeben.")
        return slug

    def clean_parent(self) -> SessionBody | None:
        parent: SessionBody | None = self.cleaned_data.get("parent")
        # Keine Kreise: Die eigene Körperschaft darf nicht über ihr stehen (Kette nach oben, begrenzt)
        current, depth = parent, 0
        while current is not None and depth < 20:
            if self.instance.pk and current.pk == self.instance.pk:
                raise forms.ValidationError("Die Körperschaft stünde damit über sich selbst.")
            current, depth = current.parent, depth + 1
        return parent


def _templates() -> dict[str, tenant_provisioning.GremienVorlage]:
    """Vorlagen je Körperschaftstyp aus der Preset-Datei; eine ungültige Datei ergibt keine Auswahl."""
    try:
        return tenant_provisioning.load_presets().committee_templates
    except tenant_provisioning.ProvisioningError:
        logger.exception("Preset-Datei der Mandanten ist ungültig.")
        return {}


class BodyListView(SessionViewMixin, TemplateView):
    template_name = "session/settings/bodies.html"
    permission_required = "manage_settings"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = cast(Any, super()).get_context_data(**kwargs)
        tenant = cast(SessionTenant, self.session_tenant)
        context["bodies"] = list(body_service.bodies(tenant, include_inactive=True).select_related("parent"))
        return context


def _form_kwargs(view: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    kwargs["tenant"] = view.session_tenant
    return kwargs


def _bodies_url(view: Any) -> str:
    return reverse("session:settings_bodies", kwargs={"tenant_slug": view.session_tenant.slug})


class BodyCreateView(SessionViewMixin, CreateView):  # type: ignore[type-arg]
    model = SessionBody
    form_class = SessionBodyForm
    template_name = "session/settings/body_form.html"
    permission_required = "manage_settings"

    def get_form_kwargs(self) -> dict[str, Any]:
        return _form_kwargs(self, super().get_form_kwargs())

    def get_success_url(self) -> str:
        return _bodies_url(self)

    def form_valid(self, form: Any) -> HttpResponse:
        response = super().form_valid(form)
        messages.success(self.request, f"Körperschaft „{form.instance.name}“ wurde angelegt.")
        vorlage = form.templates.get(form.cleaned_data.get("template") or "")
        if vorlage is not None:
            for schritt in tenant_provisioning.apply_template(form.instance, vorlage):
                messages.info(self.request, schritt)
        return response


class BodyUpdateView(SessionViewMixin, UpdateView):  # type: ignore[type-arg]
    model = SessionBody
    form_class = SessionBodyForm
    template_name = "session/settings/body_form.html"
    permission_required = "manage_settings"
    pk_url_kwarg = "body_id"

    def get_queryset(self) -> QuerySet[SessionBody]:
        return SessionBody.objects.filter(tenant=self.session_tenant)

    def get_form_kwargs(self) -> dict[str, Any]:
        return _form_kwargs(self, super().get_form_kwargs())

    def get_success_url(self) -> str:
        return _bodies_url(self)

    def form_valid(self, form: Any) -> HttpResponse:
        messages.success(self.request, f"Körperschaft „{form.instance.name}“ wurde gespeichert.")
        return super().form_valid(form)


class BodyDefaultView(SessionViewMixin, View):
    """Standardkörperschaft festlegen (POST)."""

    http_method_names = ["post"]
    permission_required = "manage_settings"

    def post(self, request: HttpRequest, tenant_slug: str, body_id: Any) -> HttpResponse:
        body = get_object_or_404(SessionBody, pk=body_id, tenant=self.session_tenant)
        body_service.set_default(body)
        messages.success(request, f"„{body.name}“ ist jetzt die Standardkörperschaft.")
        return redirect("session:settings_bodies", tenant_slug=tenant_slug)
