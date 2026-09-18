# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstellungen → Nummernkreise (Issue #150).

Die Verwaltung legt fest, wie Vorlagen- bzw. Drucksachennummern aussehen: Muster, Zählerbereich
(Jahr, Wahlperiode, fortlaufend), Zeitpunkt der Vergabe, Vorlagenarten je Kreis und das Muster
der Unternummern. Presets übernehmen verbreitete Praxis (Hamburger Bezirke, NRW-Kommunen). Der
Startwert ist nur aufwärts verstellbar – so lässt sich beim Umstieg aus einem Altsystem nahtlos
weiterzählen, ohne dass je eine Nummer doppelt entsteht.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionNumberRange, SessionPaper, SessionTenant
from ..permissions import SessionViewMixin
from ..services import numbering_service
from ..services.numbering_service import NumberingError

_log_event = cast(Any, audit).log_event


def _paper_types() -> dict[str, str]:
    return dict(cast(Any, SessionPaper._meta.get_field("paper_type")).choices)


class NumberingSettingsView(SessionViewMixin, TemplateView):
    template_name = "session/settings/numbering.html"
    permission_required = "manage_settings"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = cast(Any, super()).get_context_data(**kwargs)
        ranges: list[Any] = list(
            SessionNumberRange.objects.filter(tenant=self.session_tenant).order_by("-is_active", "order")
        )
        typen = _paper_types()
        for rng in ranges:
            rng.vorschau = numbering_service.preview(rng)
            rng.typ_namen = [typen.get(t, t) for t in rng.paper_types or []]
        context["ranges"] = ranges
        context["paper_types"] = list(typen.items())
        context["reset_choices"] = SessionNumberRange.RESET_CHOICES
        context["assign_choices"] = SessionNumberRange.ASSIGN_CHOICES
        context["presets"] = numbering_service.PRESETS
        return context


class NumberingSaveView(SessionViewMixin, View):
    """POST-Aktionen: Bezeichnung, Kreis speichern, Preset übernehmen, nächste Nummer setzen."""

    http_method_names = ["post"]
    permission_required = "manage_settings"

    @property
    def tenant(self) -> SessionTenant:
        return cast(SessionTenant, self.session_tenant)

    def post(self, request: HttpRequest, tenant_slug: str) -> HttpResponse:
        aktion = request.POST.get("action", "")
        handler: Callable[[HttpRequest], None] | None = {
            "label": self._label,
            "range": self._range,
            "preset": self._preset,
            "next": self._next,
        }.get(aktion)
        if handler is None:
            messages.error(request, "Unbekannte Aktion.")
        else:
            handler(request)
        return redirect("session:settings_numbering", tenant_slug=tenant_slug)

    def _label(self, request: HttpRequest) -> None:
        label = (request.POST.get("reference_label") or "").strip()[:40]
        if not label:
            messages.error(request, "Bitte eine Bezeichnung angeben, z. B. „Drucksache“.")
            return
        SessionTenant.objects.filter(pk=self.tenant.pk).update(reference_label=label)
        self.tenant.reference_label = label
        messages.success(request, f"Bezeichnung gespeichert: {label}.")

    def _range(self, request: HttpRequest) -> None:
        rng_id = request.POST.get("range_id")
        rng = (
            get_object_or_404(SessionNumberRange, pk=rng_id, tenant=self.session_tenant)
            if rng_id
            else SessionNumberRange(tenant=self.session_tenant)
        )
        pattern = (request.POST.get("pattern") or "").strip()
        reset = request.POST.get("reset", "yearly")
        sub_pattern = (request.POST.get("sub_pattern") or "{parent}.{sub}").strip()
        fehler = numbering_service.validate_pattern(pattern, reset) + numbering_service.validate_sub_pattern(
            sub_pattern
        )
        if reset not in dict(SessionNumberRange.RESET_CHOICES):
            fehler.append("Ungültiger Zählerbereich.")
        if fehler:
            messages.error(request, " ".join(fehler))
            return
        # Laufender Kreis: Muster und Zählerbereich bestimmen bereits vergebene Nummern mit
        if rng.pk and rng.counters.filter(value__gt=0).exists() and (rng.pattern != pattern or rng.reset != reset):
            messages.error(
                request,
                "Aus diesem Kreis wurden schon Nummern vergeben – Muster und Zählerbereich bleiben. "
                "Für ein neues Schema bitte einen neuen Kreis anlegen und diesen deaktivieren.",
            )
            return
        typen = set(_paper_types())
        rng.name = (request.POST.get("name") or "Vorlagen").strip()[:100]
        rng.prefix = (request.POST.get("prefix") or "").strip()[:20]
        rng.pattern, rng.reset, rng.sub_pattern = pattern, reset, sub_pattern
        rng.assign_on = "release" if request.POST.get("assign_on") == "release" else "create"
        rng.paper_types = [t for t in request.POST.getlist("paper_types") if t in typen]
        rng.is_active = request.POST.get("is_active") == "on"
        rng.save()
        _log_event(
            "update",
            rng,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"muster": pattern, "zuruecksetzen": reset},
        )
        messages.success(
            request, f"Nummernkreis „{rng.name}“ gespeichert – nächste Nummer {numbering_service.preview(rng)}."
        )

    def _preset(self, request: HttpRequest) -> None:
        key = request.POST.get("preset", "")
        if key not in numbering_service.PRESETS:
            messages.error(request, "Unbekanntes Preset.")
            return
        numbering_service.apply_preset(self.tenant, key)
        _log_event(
            "update", self.session_tenant, user=self.session_user, request=request, changes={"nummernkreis_preset": key}
        )
        messages.success(request, f"Preset „{numbering_service.PRESETS[key].label}“ übernommen.")

    def _next(self, request: HttpRequest) -> None:
        rng = get_object_or_404(SessionNumberRange, pk=request.POST.get("range_id"), tenant=self.session_tenant)
        try:
            naechste = int(request.POST.get("next_number") or 0)
            numbering_service.set_next_number(rng, naechste)
        except (ValueError, NumberingError) as exc:
            messages.error(request, str(exc) if isinstance(exc, NumberingError) else "Bitte eine ganze Zahl angeben.")
            return
        _log_event(
            "update",
            rng,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"naechste_nummer": naechste},
        )
        messages.success(request, f"Nächste Nummer im Kreis „{rng.name}“: {numbering_service.preview(rng)}.")
