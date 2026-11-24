# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abgelaufene Demo-Organisationen sperren, vor der Löschung erinnern und nach der Frist löschen (Issue #1406, #1575).

Stündlich als Zeitplan im Worker (``apps/common/schedules.py``). Sperrt zuerst (``is_active`` aus, alle Daten
bleiben). Ist eine Löschfrist eingestellt (``DEMO_LOESCHFRIST_TAGE``), erinnert er danach die Administratoren 7 Tage
und 24 Stunden vor der Löschung per Mail und löscht gesperrte Demos nach der Frist endgültig (höchstens
``demo.LOESCHUNGEN_JE_LAUF`` je Lauf). ``--dry-run`` nennt, was der Lauf täte, auch Demos, die er im selben Lauf erst
sperren und dann löschen würde. Scheitert eine Löschung oder eine Erinnerung, endet der Befehl mit einem Fehler,
nachdem er alles Übrige erledigt hat.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from apps.common.einmalig import EinmaligMixin
from apps.tenants import demo
from apps.tenants.models import Organization


class Command(EinmaligMixin, BaseCommand):
    sperre = "demo_ablauf"
    sperre_ttl = 600
    help = "Abgelaufene Demo-Organisationen sperren, vor der Löschung erinnern und nach der Frist löschen."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts ändern")

    def handle(self, *args: Any, **options: Any) -> None:
        jetzt = timezone.now()
        if options["dry_run"]:
            self._probelauf(jetzt)
            return
        gesperrt = demo.abgelaufene_sperren(jetzt)
        for org in gesperrt:
            self.stdout.write(f"gesperrt: {org.slug} (Ende {org.demo_endet_am:%d.%m.%Y %H:%M})")
        self.stdout.write(f"{len(gesperrt)} Demo(s) gesperrt")
        fehler: list[str] = []
        for erinnerung in demo.erinnerungen_senden(jetzt):
            self.stdout.write(
                f"erinnert: {erinnerung.slug} ({erinnerung.stunden} Stunden vor der Löschung, "
                f"{erinnerung.empfaenger} Empfänger)"
            )
            if erinnerung.wiederholen:
                fehler.append(f"Erinnerung {erinnerung.slug} nicht versendet (nächster Lauf versucht es erneut)")
            elif erinnerung.fehlgeschlagen:
                fehler.append(f"Erinnerung {erinnerung.slug}: {erinnerung.fehlgeschlagen} Empfänger nicht erreicht")
        lauf = demo.faellige_loeschen(jetzt)
        for geloescht in lauf.geloescht:
            self.stdout.write(
                f"gelöscht: {geloescht.slug} ({geloescht.eintraege} Einträge, {geloescht.dateien} Dateien, "
                f"Konten gelöscht {geloescht.konten_geloescht}, behalten {geloescht.konten_behalten})"
            )
        self.stdout.write(f"{len(lauf.geloescht)} Demo(s) gelöscht")
        fehler += [f"Löschung {eintrag}" for eintrag in lauf.fehlgeschlagen]
        if fehler:
            raise CommandError("; ".join(fehler))

    def _probelauf(self, jetzt: Any) -> None:
        """Was der Lauf täte. Demos, die er erst sperrt, zählen danach als gesperrt (ohne verschickte Erinnerung)."""
        sperren = list(Organization.objects.filter(demo.abgelaufen_q(jetzt), Q(is_active=True)).order_by("slug"))
        for org in sperren:
            self.stdout.write(f"würde sperren: {org.slug} (Ende {org.demo_endet_am:%d.%m.%Y %H:%M})")
        self.stdout.write(f"{len(sperren)} Demo(s) fällig (Probelauf)")
        abgelaufen = list(Organization.objects.filter(demo.abgelaufen_q(jetzt)).order_by("demo_endet_am", "slug"))
        loeschen = []
        for org in abgelaufen:
            if org.is_active:
                org.is_active = False
                org.demo_erinnert_stunden = None
            loeschung = demo.loeschung_am(org)
            if loeschung is not None and loeschung <= jetzt:
                loeschen.append(org)
                continue
            stufe = demo.faellige_stufe(org, jetzt)
            if stufe is not None:
                self.stdout.write(f"würde erinnern: {org.slug} ({stufe} Stunden vor der Löschung)")
        for org in loeschen[: demo.LOESCHUNGEN_JE_LAUF]:
            self.stdout.write(f"würde löschen: {org.slug} (Ende {org.demo_endet_am:%d.%m.%Y %H:%M})")
        self.stdout.write(f"{len(loeschen)} Demo(s) zum Löschen fällig (Probelauf)")
