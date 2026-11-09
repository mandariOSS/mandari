# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fehlende Teilnahmen an kommenden Fraktionssitzungen nachtragen (Issue #1288).

    python manage.py fraktion_teilnahme_nachtragen                     # Probelauf: zeigt nur, was fehlt
    python manage.py fraktion_teilnahme_nachtragen --org <slug>        # Probelauf für eine Organisation
    python manage.py fraktion_teilnahme_nachtragen --org <slug> --ausfuehren

Für den Bestand vor Issue #1288: Mitglieder, die nach dem Anlegen einer Sitzung beigetreten sind oder eine Rolle, ein
Gremium oder die Auswahl einer Art erhalten haben, fehlen in deren Teilnahmen. Eingetragen wird wie beim Beitritt
(:func:`apps.work.faction.teilnahme.nachtragen`): nur aktive Mitglieder ohne Gastzugang, nur kommende Sitzungen
(Entwurf, geplant, eingeladen) der Arten, zu deren Teilnehmenden sie gehören; bestehende Teilnahmen bleiben.

Achtung: Zu Sitzungen, zu denen schon eingeladen ist, schickt der Einladungslauf den Nachgetragenen danach ihre
Einladung per E-Mail (spätestens nach 15 Minuten). Der Probelauf nennt diese Zahl gesondert. Ausgegeben werden nur
Zahlen je Organisation, keine Namen.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.tenants.models import Membership, Organization
from apps.work.faction import teilnahme


class Command(BaseCommand):
    help = "Trägt fehlende Teilnahmen an kommenden Fraktionssitzungen nach (ohne --ausfuehren nur Probelauf)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--org", action="append", default=[], metavar="SLUG", help="Organisation (Kurzname), mehrfach möglich"
        )
        parser.add_argument("--ausfuehren", action="store_true", help="wirklich eintragen (sonst nur Probelauf)")

    def handle(self, *args: Any, **options: Any) -> None:
        slugs: list[str] = options["org"]
        ausfuehren: bool = options["ausfuehren"]
        organisationen = Organization.objects.order_by("slug")
        if slugs:
            organisationen = organisationen.filter(slug__in=slugs)
            fehlend = set(slugs) - set(organisationen.values_list("slug", flat=True))
            if fehlend:
                raise CommandError(f"Unbekannte Organisation: {', '.join(sorted(fehlend))}")

        summe_teilnahmen = summe_eingeladen = 0
        for organisation in organisationen:
            mitglieder = teilnahmen = eingeladen = 0
            for membership in Membership.objects.filter(organization=organisation, is_active=True, is_guest=False):
                sitzungen = list(teilnahme.kommende_sitzungen_ohne(membership))
                if not sitzungen:
                    continue
                mitglieder += 1
                eingeladen += sum(1 for s in sitzungen if s.invitation_sent_at)
                teilnahmen += teilnahme.nachtragen(membership) if ausfuehren else len(sitzungen)
            if teilnahmen:
                self.stdout.write(
                    f"{organisation.slug}: {teilnahmen} Teilnahme(n) für {mitglieder} Mitglied(er)"
                    f"{' nachgetragen' if ausfuehren else ' fehlen'}, davon {eingeladen} zu Sitzungen mit "
                    "versandter Einladung (Einladung folgt mit dem Einladungslauf)"
                )
            summe_teilnahmen += teilnahmen
            summe_eingeladen += eingeladen

        art = "Nachgetragen" if ausfuehren else "Probelauf, fehlen"
        self.stdout.write(f"{art}: {summe_teilnahmen} Teilnahme(n), davon {summe_eingeladen} mit Einladung per E-Mail.")
        if not ausfuehren and summe_teilnahmen:
            self.stdout.write("Zum Eintragen erneut mit --ausfuehren aufrufen.")
