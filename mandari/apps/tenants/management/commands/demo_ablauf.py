# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abgelaufene Demo-Organisationen sperren (Issue #1406).

Stündlich als Zeitplan im Worker (``apps/common/schedules.py``). Sperrt nur (``is_active`` aus); alle Daten
bleiben erhalten. ``--dry-run`` nennt die fälligen Organisationen, ohne etwas zu ändern.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.common.einmalig import EinmaligMixin
from apps.tenants import demo
from apps.tenants.models import Organization


class Command(EinmaligMixin, BaseCommand):
    sperre = "demo_ablauf"
    sperre_ttl = 600
    help = "Abgelaufene Demo-Organisationen sperren (Daten bleiben erhalten)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts ändern")

    def handle(self, *args: Any, **options: Any) -> None:
        jetzt = timezone.now()
        if options["dry_run"]:
            faellig = Organization.objects.filter(demo.abgelaufen_q(jetzt), Q(is_active=True)).order_by("slug")
            for org in faellig:
                self.stdout.write(f"würde sperren: {org.slug} (Ende {org.demo_endet_am:%d.%m.%Y %H:%M})")
            self.stdout.write(f"{faellig.count()} Demo(s) fällig (Probelauf)")
            return
        gesperrt = demo.abgelaufene_sperren(jetzt)
        for org in gesperrt:
            self.stdout.write(f"gesperrt: {org.slug} (Ende {org.demo_endet_am:%d.%m.%Y %H:%M})")
        self.stdout.write(f"{len(gesperrt)} Demo(s) gesperrt")
