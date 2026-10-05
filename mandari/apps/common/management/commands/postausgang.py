# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Postausgang des Mail-Dienstes prüfen (Issue #528).

    python manage.py postausgang               # Zeilen je Zustand und verwaiste Zeilen
    python manage.py postausgang --einreihen   # verwaiste Zeilen neu einreihen
    python manage.py postausgang --verwerfen   # verwaiste Zeilen aufgeben (Inhalt löschen)

Verwaist ist eine wartende Zeile ohne wartenden oder laufenden Versandauftrag, etwa wenn ein älterer
Worker den Auftrag nicht kannte und ihn bis „tot“ wiederholt hat. Ausgegeben werden nur Zahlen und
Mailarten, nie Empfänger oder Inhalt.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Count

from apps.common.mail import outbox
from apps.common.models import MailOutbox


class Command(BaseCommand):
    help = "Postausgang des Mail-Dienstes: Zustand anzeigen, verwaiste Zeilen neu einreihen oder verwerfen."

    def add_arguments(self, parser: CommandParser) -> None:
        aktion = parser.add_mutually_exclusive_group()
        aktion.add_argument("--einreihen", action="store_true", help="Verwaiste Zeilen neu einreihen.")
        aktion.add_argument("--verwerfen", action="store_true", help="Verwaiste Zeilen aufgeben, Inhalt löschen.")
        parser.add_argument(
            "--aelter-als",
            type=int,
            default=15,
            metavar="MINUTEN",
            help="Nur Zeilen, die seit mindestens so vielen Minuten warten (Standard 15).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["aelter_als"] < 0:
            raise CommandError("--aelter-als darf nicht negativ sein.")
        for zeile in MailOutbox.objects.values("status").annotate(anzahl=Count("pk")).order_by("status"):
            self.stdout.write(f"{zeile['status']}: {zeile['anzahl']}")
        waisen = outbox.verwaist(timedelta(minutes=options["aelter_als"]))
        arten = Counter(row.kind for row in waisen)
        self.stdout.write(f"verwaist: {len(waisen)}" + "".join(f"\n  {art}: {n}" for art, n in sorted(arten.items())))
        if options["einreihen"]:
            self.stdout.write(self.style.SUCCESS(f"neu eingereiht: {outbox.neu_einreihen(waisen)}"))
        elif options["verwerfen"]:
            self.stdout.write(self.style.WARNING(f"verworfen: {outbox.verwerfen(waisen)}"))
