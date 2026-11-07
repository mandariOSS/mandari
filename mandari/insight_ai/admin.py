# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin: Kommunen im Tagesbatch, Batches und Verbrauch der Zusammenfassungen mit der Claude API, verworfene
Zusammenfassungen (mechanische Prüfung, Issue #1271).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.contrib import admin, messages
from django.http import HttpRequest
from unfold.admin import ModelAdmin

from .models import KiVerbrauch, TagesbatchKommune, VerworfeneZusammenfassung, ZusammenfassungsBatch


class _NurLesen(ModelAdmin):  # type: ignore[misc]
    """Einträge entstehen nur im Betrieb (Zeitpläne, Aufrufe); im Admin nur ansehen."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(TagesbatchKommune)
class TagesbatchKommuneAdmin(ModelAdmin):  # type: ignore[misc]
    """Kommunen im täglichen Batch (Issue #1220); auch per ``manage.py tagesbatch``."""

    list_display = ("body", "ab", "verortung_pruefen", "angelegt_am")
    raw_id_fields = ("body",)
    readonly_fields = ("angelegt_am",)


@admin.register(ZusammenfassungsBatch)
class ZusammenfassungsBatchAdmin(_NurLesen):
    list_display = (
        "batch_id",
        "status",
        "modell",
        "eingereicht_am",
        "abgeholt_am",
        "anzahl",
        "gespeichert",
        "verworfen",
        "fehlgeschlagen",
        "geschaetzt_usd",
        "kosten_usd",
    )
    list_filter = ("status", "modell")
    exclude = ("anfragen",)
    date_hierarchy = "eingereicht_am"


@admin.register(KiVerbrauch)
class KiVerbrauchAdmin(_NurLesen):
    list_display = ("zeitpunkt", "art", "modell", "vorgang_id", "eingabe_tokens", "ausgabe_tokens", "kosten_usd")
    list_filter = ("art", "modell")
    date_hierarchy = "zeitpunkt"

    def changelist_view(self, request: HttpRequest, extra_context: dict[str, Any] | None = None) -> Any:
        """Oben der Stand des Monatsbudgets (verbucht, für offene Batches reserviert, Grenze)."""
        from apps.common.models import AISettings

        from . import kosten

        budget = Decimal(AISettings.get_settings().summary_monthly_budget_usd or 0)
        verbraucht = kosten.monatsverbrauch().quantize(Decimal("0.01"))
        reserviert = kosten.reserviert().quantize(Decimal("0.01"))
        messages.info(
            request,
            f"Laufender Monat: {verbraucht} USD verbucht, {reserviert} USD für offene Batches reserviert, "
            f"Monatsbudget {budget} USD.",
        )
        return super().changelist_view(request, extra_context)


@admin.register(VerworfeneZusammenfassung)
class VerworfeneZusammenfassungAdmin(_NurLesen):
    """Von der mechanischen Prüfung verworfene Zusammenfassungen: welche Regel greift wie oft, je Fassung und Modell."""

    list_display = ("zeitpunkt", "regel", "teil", "prompt_fassung", "modell", "weg", "grund", "vorgang_id")
    list_filter = ("regel", "teil", "prompt_fassung", "modell", "weg")
    search_fields = ("grund", "entwurf")
    date_hierarchy = "zeitpunkt"
