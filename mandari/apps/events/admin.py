# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin-Seite der Ereignistechnik (Issue #510): Abonnements mit Rückstand, geparkte Ereignisse, Aufträge.

- **Abonnements:** Zustand, Cursor, Rückstand (wie ``mandari_events_lag_seconds``) und geparkte
  Ereignisse je Zustand. Pausieren und Fortsetzen (aktiv oder im Schattenbetrieb) über
  ``dispatch.set_state``.
- **Geparkte Ereignisse:** standardmäßig nur der Kopf jeder Kette je Objekt mit der Zahl seiner
  Folgeereignisse, statt einer langen Liste blockierter Ereignisse. Erneut versuchen
  (``dispatch.retry_parked``) und verwerfen (``dispatch.discard_parked``, mit Bestätigung). Beide halten
  die Reihenfolge je Objekt ein: Ein Folgeereignis lässt sich nicht vorziehen, nach dem Verwerfen rückt
  das nächste nach.
- **Aufträge:** nur lesend, mit Filtern nach Status und Warteschlange.

Nur für Administratoren (Superuser) sichtbar und bedienbar. Anlegen, Ändern und Löschen gibt es nicht:
Zeilen entstehen im Betrieb, und ein gelöschtes Abonnement finge am Ende des Journals neu an. Jeder
Eingriff steht im Sicherheitsprotokoll (Ereignis „Eingriff in den Betrieb“, ``record_operation``) – mit
Konto, Adresse, Aktion und Kennungen, nie mit Inhalten – in derselben Transaktion wie der Eingriff.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.db import transaction
from django.db.models import Count, IntegerField, OuterRef, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce, Now
from django.http import HttpRequest
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.http import urlencode
from django.utils.safestring import SafeString
from unfold.admin import ModelAdmin

from apps.accounts.security_audit import record_operation
from apps.common.admin_mixins import ImmutableAdminMixin, status_pill

from . import dispatch, metrics, registry
from .eingriffe import parked_identifiers
from .models import Event, ParkedEvent, ParkedState, Subscription, SubscriptionState, Task, TaskStatus

#: Farben der Zustände in den Listen
_FARBEN: dict[str, str] = {
    SubscriptionState.AKTIV: "#16a34a",
    SubscriptionState.SCHATTEN: "#2563eb",
    SubscriptionState.PAUSIERT: "#d97706",
    ParkedState.WIEDERHOLEN: "#d97706",
    ParkedState.BLOCKIERT: "#64748b",
    ParkedState.TOT: "#dc2626",
    TaskStatus.WARTEND: "#64748b",
    TaskStatus.LAEUFT: "#2563eb",
    TaskStatus.ERLEDIGT: "#16a34a",
    TaskStatus.FEHLGESCHLAGEN: "#dc2626",
    TaskStatus.TOT: "#dc2626",
}
#: Ab diesem Rückstand (Sekunden) gilt ein Abonnement als im Verzug (Alarmschwelle, docs/MONITORING.md)
RUECKSTAND_ALARM = 300


def nur_administratoren(request: HttpRequest) -> bool:
    """Sichtbar und bedienbar nur für aktive Superuser (auch für die Navigation, ``UNFOLD``)."""
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_active and user.is_superuser)


class _NurAdministratoren(ImmutableAdminMixin):
    """Nur Superuser sehen die Seite; anlegen, ändern und löschen kann niemand."""

    def has_module_permission(self, request: HttpRequest) -> bool:
        return nur_administratoren(request)

    def has_view_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return nur_administratoren(request)

    def has_eingriff_permission(self, request: HttpRequest) -> bool:
        """Recht für die Aktionen (``permissions=["eingriff"]``)."""
        return nur_administratoren(request)


def _zustand(state: str, label: object) -> SafeString:
    return status_pill(_FARBEN.get(state, "#64748b"), label)


def _dauer(sekunden: float) -> str:
    if sekunden < 120:
        return f"{sekunden:.0f} s"
    if sekunden < 7200:
        return f"{sekunden / 60:.0f} min"
    if sekunden < 2 * 86400:
        return f"{sekunden / 3600:.1f} h"
    return f"{sekunden / 86400:.1f} Tage"


# =============================================================================
# Abonnements
# =============================================================================


def _geparkt(state: str) -> Coalesce:
    """Zahl geparkter Ereignisse eines Zustands je Abonnement (eine Unterabfrage, keine Abfrage je Zeile)."""
    anzahl = (
        ParkedEvent.objects.filter(subscription=OuterRef("name"), state=state)
        .order_by()
        .values("subscription")
        .annotate(anzahl=Count("id"))
        .values("anzahl")[:1]
    )
    return Coalesce(Subquery(anzahl, output_field=IntegerField()), Value(0))


@admin.register(Subscription)
class SubscriptionAdmin(_NurAdministratoren, ModelAdmin):  # type: ignore[misc]
    """Abonnements der Zustellung: Zustand, Rückstand, geparkte Ereignisse; pausieren und fortsetzen."""

    list_display = ("name", "zustand", "warteschlange", "cursor_seq", "rueckstand", "geparkt", "updated_at")
    ordering = ("name",)
    search_fields = ("name",)
    list_filter = ("state",)
    actions = ("pausieren", "fortsetzen", "fortsetzen_im_schatten")
    fields = ("name", "state", "cursor_seq", "updated_at")
    readonly_fields = fields

    def get_queryset(self, request: HttpRequest) -> QuerySet[Subscription]:
        basis: QuerySet[Subscription] = super().get_queryset(request)
        return basis.annotate(
            jetzt=Now(),
            n_wiederholen=_geparkt(ParkedState.WIEDERHOLEN),
            n_blockiert=_geparkt(ParkedState.BLOCKIERT),
            n_tot=_geparkt(ParkedState.TOT),
        )

    @admin.display(description="Zustand", ordering="state")
    def zustand(self, obj: Subscription) -> SafeString:
        return _zustand(obj.state, obj.get_state_display())

    @admin.display(description="Warteschlange")
    def warteschlange(self, obj: Subscription) -> str:
        spec = _registriert().get(obj.name)
        return spec.queue if spec else "nicht registriert"

    @admin.display(description="Rückstand")
    def rueckstand(self, obj: Subscription) -> str | SafeString:
        spec = _registriert().get(obj.name)
        if spec is None:
            # Ohne Handler im Code bekommt die Zeile nichts mehr zugestellt
            return "–"
        # Datenbankzeit aus der Abfrage (``get_queryset``), wie bei der Metrik
        sekunden = metrics.lag_seconds(spec, obj.cursor_seq, getattr(obj, "jetzt", None) or timezone.now())
        if not sekunden:
            return "aktuell"
        text = _dauer(sekunden)
        if sekunden > RUECKSTAND_ALARM and obj.state != SubscriptionState.PAUSIERT:
            return _zustand(ParkedState.TOT, text)
        return text

    @admin.display(description="Geparkt (wiederholen / blockiert / tot)")
    def geparkt(self, obj: Subscription) -> str | SafeString:
        werte = (getattr(obj, "n_wiederholen", 0), getattr(obj, "n_blockiert", 0), getattr(obj, "n_tot", 0))
        if not any(werte):
            return "–"
        adresse = f"{reverse('admin:events_parkedevent_changelist')}?{urlencode({'subscription__exact': obj.name})}"
        return format_html('<a href="{}">{} / {} / {}</a>', adresse, *werte)

    def _zustand_setzen(
        self, request: HttpRequest, queryset: QuerySet[Subscription], neu: str, nur_aus: set[str]
    ) -> None:
        geaendert, uebersprungen = [], []
        for name, vorher in queryset.values_list("name", "state"):
            if vorher not in nur_aus:
                uebersprungen.append(name)
                continue
            with transaction.atomic():
                alt = dispatch.set_state(name, neu)
                if alt is None or alt == neu:
                    uebersprungen.append(name)
                    continue
                record_operation(request, "abonnement_zustand", abonnement=name, vorher=alt, nachher=neu)
            geaendert.append(name)
        label = SubscriptionState(neu).label
        if geaendert:
            messages.success(request, f"{len(geaendert)} Abonnement(s) jetzt {label}: {', '.join(geaendert)}.")
        if uebersprungen:
            messages.warning(request, f"Unverändert (Zustand passt nicht): {', '.join(uebersprungen)}.")

    @admin.action(description="Pausieren (nichts mehr zustellen, Cursor bleibt stehen)", permissions=["eingriff"])
    def pausieren(self, request: HttpRequest, queryset: QuerySet[Subscription]) -> None:
        self._zustand_setzen(
            request, queryset, SubscriptionState.PAUSIERT, {SubscriptionState.AKTIV, SubscriptionState.SCHATTEN}
        )

    @admin.action(description="Fortsetzen (aktiv)", permissions=["eingriff"])
    def fortsetzen(self, request: HttpRequest, queryset: QuerySet[Subscription]) -> None:
        self._zustand_setzen(request, queryset, SubscriptionState.AKTIV, {SubscriptionState.PAUSIERT})

    @admin.action(description="Fortsetzen im Schattenbetrieb", permissions=["eingriff"])
    def fortsetzen_im_schatten(self, request: HttpRequest, queryset: QuerySet[Subscription]) -> None:
        self._zustand_setzen(request, queryset, SubscriptionState.SCHATTEN, {SubscriptionState.PAUSIERT})


def _registriert() -> dict[str, registry.Subscriber]:
    return {spec.name: spec for spec in registry.load_subscribers()}


# =============================================================================
# Geparkte Ereignisse
# =============================================================================


class KetteFilter(admin.SimpleListFilter):
    """Standard: nur der Kopf jeder Kette (wiederholen oder tot); blockierte Folgeereignisse auf Wunsch."""

    title = "Ansicht"
    parameter_name = "kette"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("alle", "Alle Ereignisse, auch blockierte")]

    def choices(self, changelist: Any) -> Any:
        yield {
            "selected": self.value() is None,
            "query_string": changelist.get_query_string(remove=[self.parameter_name]),
            "display": "Kettenköpfe mit Folgeereignissen",
        }
        yield from list(super().choices(changelist))[1:]

    def queryset(self, request: HttpRequest, queryset: QuerySet[ParkedEvent]) -> QuerySet[ParkedEvent]:
        # Wer nach dem Zustand filtert, will genau diese Ereignisse sehen, auch blockierte
        if self.value() == "alle" or "state__exact" in request.GET:
            return queryset
        return queryset.exclude(state=ParkedState.BLOCKIERT)


@admin.register(ParkedEvent)
class ParkedEventAdmin(_NurAdministratoren, ModelAdmin):  # type: ignore[misc]
    """Geparkte Ereignisse je Abonnement und Objekt; erneut versuchen oder verwerfen."""

    list_display = (
        "subscription",
        "event_seq",
        "ereignistyp",
        "zustand",
        "folgeereignisse",
        "attempts",
        "next_attempt_at",
        "error_code",
        "aggregate_id",
    )
    list_filter = (KetteFilter, "state", "subscription")
    ordering = ("subscription", "event_seq")
    actions = ("erneut_versuchen", "verwerfen")
    fields = ("subscription", "event_seq", "aggregate_id", "state", "attempts", "next_attempt_at", "error_code")
    readonly_fields = fields

    def get_queryset(self, request: HttpRequest) -> QuerySet[ParkedEvent]:
        folgende = (
            ParkedEvent.objects.filter(
                subscription=OuterRef("subscription"),
                aggregate_id=OuterRef("aggregate_id"),
                event_seq__gt=OuterRef("event_seq"),
            )
            .order_by()
            .values("subscription")
            .annotate(anzahl=Count("id"))
            .values("anzahl")[:1]
        )
        basis: QuerySet[ParkedEvent] = super().get_queryset(request)
        return basis.annotate(
            typ=Subquery(Event.objects.filter(seq=OuterRef("event_seq")).values("type")[:1]),
            n_folgende=Coalesce(Subquery(folgende, output_field=IntegerField()), Value(0)),
        )

    @admin.display(description="Ereignistyp")
    def ereignistyp(self, obj: ParkedEvent) -> str:
        return getattr(obj, "typ", None) or "–"

    @admin.display(description="Zustand", ordering="state")
    def zustand(self, obj: ParkedEvent) -> SafeString:
        return _zustand(obj.state, obj.get_state_display())

    @admin.display(description="Folgeereignisse", ordering="n_folgende")
    def folgeereignisse(self, obj: ParkedEvent) -> int:
        return int(getattr(obj, "n_folgende", 0))

    @admin.action(description="Erneut versuchen (erstes Ereignis des Objekts, alle Versuche)", permissions=["eingriff"])
    def erneut_versuchen(self, request: HttpRequest, queryset: QuerySet[ParkedEvent]) -> None:
        erledigt = uebersprungen = 0
        for geparkt in queryset.order_by("subscription", "event_seq"):
            with transaction.atomic():
                if not dispatch.retry_parked(geparkt.pk):
                    uebersprungen += 1
                    continue
                record_operation(request, "geparkt_wiederholen", **parked_identifiers(geparkt))
            erledigt += 1
        if erledigt:
            messages.success(request, f"{erledigt} Ereignis(se) werden beim nächsten Lauf erneut zugestellt.")
        if uebersprungen:
            messages.warning(
                request,
                f"{uebersprungen} übersprungen: nicht das erste geparkte Ereignis seines Objekts (die Reihenfolge "
                "je Objekt bleibt erhalten) oder inzwischen zugestellt bzw. verworfen.",
            )

    @admin.action(description="Verwerfen (wird nie zugestellt)", permissions=["eingriff"])
    def verwerfen(self, request: HttpRequest, queryset: QuerySet[ParkedEvent]) -> TemplateResponse | None:
        if request.POST.get("post") != "ja":
            context = {
                **self.admin_site.each_context(request),
                "title": "Geparkte Ereignisse verwerfen",
                "opts": self.model._meta,
                "queryset": queryset.order_by("subscription", "event_seq"),
                "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            }
            return TemplateResponse(request, "admin/events/parkedevent/verwerfen_bestaetigen.html", context)
        verworfen = 0
        for geparkt in queryset.order_by("subscription", "event_seq"):
            with transaction.atomic():
                if not dispatch.discard_parked(geparkt.pk):
                    continue
                record_operation(request, "geparkt_verworfen", **parked_identifiers(geparkt))
            verworfen += 1
        messages.success(
            request, f"{verworfen} Ereignis(se) verworfen; das nächste Ereignis desselben Objekts rückt jeweils nach."
        )
        return None


# =============================================================================
# Aufträge
# =============================================================================


@admin.register(Task)
class TaskAdmin(_NurAdministratoren, ModelAdmin):  # type: ignore[misc]
    """Aufträge des Tasks-Backends (``events_task``), nur lesend."""

    list_display = (
        "task_path",
        "queue",
        "zustand",
        "versuche",
        "run_after",
        "created_at",
        "finished_at",
        "result_code",
    )
    list_filter = ("status", "queue")
    search_fields = ("task_path", "result_code")
    ordering = ("-created_at",)
    fields = (
        "id",
        "task_path",
        "queue",
        "status",
        "priority",
        "attempts",
        "max_attempts",
        "run_after",
        "locked_until",
        "created_at",
        "finished_at",
        "result_code",
        "idempotency_key",
        "args",
    )
    readonly_fields = fields

    @admin.display(description="Status", ordering="status")
    def zustand(self, obj: Task) -> SafeString:
        return _zustand(obj.status, obj.get_status_display())

    @admin.display(description="Versuche")
    def versuche(self, obj: Task) -> str:
        return f"{obj.attempts} / {obj.max_attempts}"
