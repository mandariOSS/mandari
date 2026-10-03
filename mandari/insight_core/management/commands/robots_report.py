# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Bericht, welche Quellen die robots.txt für uns sperrt.

Prüft je Quelle die Schnittstelle (Adresse der Quelle) und Datei-Downloads (Stichprobe der jüngsten
Download-Adressen, je Host eine) nach RFC 9309 und nennt die entscheidende Regel und eine eingetragene
Ausnahme samt Vermerk. Die robots.txt wird immer ausgewertet, auch wenn eine Ausnahme gilt: Quellen, die wir
per Ausnahme gegen die robots.txt laden, gehören auf die Liste der gesperrten Quellen (Grundlage für
Freigabe-Anfragen). Geprüft wird mit dem User-Agent, mit dem wir abrufen (Schnittstelle: User-Agent der
Quelle, Dateien: ``download_headers``, sonst jeweils unser Standard). Die robots.txt kommt aus dem gemeinsamen
Cache (24 Stunden), ``--refresh`` lädt sie neu.

    python manage.py robots_report                 # alle aktiven Quellen
    python manage.py robots_report --nur-gesperrte # robots.txt sperrt (auch mit Ausnahme), ist nicht
                                                   # erreichbar, oder die Ausnahme ist ungültig
    python manage.py robots_report --json          # maschinenlesbar
    python manage.py robots_report --requeue       # nach Freigabe: übersprungene Dateien neu einreihen

JSON je Quelle: ``gesperrt`` (wir rufen etwas nicht ab), ``robots_sperrt`` (die robots.txt sperrt
Schnittstelle oder Dateien, auch wenn eine Ausnahme den Abruf erlaubt), ``nicht_erreichbar`` (robots.txt
nicht erreichbar); je Prüfung ``erlaubt`` (mit Ausnahme), ``robots_erlaubt`` (robots.txt allein), ``regel``,
``zustand`` und ``ausnahme_aktiv``.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from django.core.management.base import BaseCommand, CommandParser
from mandari_oparl.crawler import PRODUCT_TOKEN
from mandari_oparl.robots import (
    KIND_API,
    KIND_FILES,
    STATE_UNREACHABLE,
    RobotsOverride,
    robots_override,
    robots_override_problem,
)

#: So viele jüngste Download-Adressen je Quelle werden nach Hosts gesichtet
SAMPLE_FILES = 50
#: Höchstens so viele Datei-Hosts je Quelle werden geprüft
MAX_FILE_HOSTS = 3


class Command(BaseCommand):
    help = "Bericht: Welche Quellen sperrt die robots.txt (Schnittstelle, Dateien), welche Ausnahmen gelten?"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--alle", action="store_true", help="Auch inaktive Quellen prüfen")
        parser.add_argument(
            "--nur-gesperrte",
            action="store_true",
            help="Nur Quellen, deren robots.txt sperrt (auch mit Ausnahme) oder nicht erreichbar ist",
        )
        parser.add_argument("--json", action="store_true", help="Ausgabe als JSON")
        parser.add_argument("--refresh", action="store_true", help="robots.txt neu laden statt aus dem Cache")
        parser.add_argument(
            "--requeue",
            action="store_true",
            help="Wegen robots.txt übersprungene Dateien neu einreihen, wo Dateien jetzt erlaubt sind",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from insight_core.models import OParlSource

        sources = OParlSource.objects.order_by("name")
        if not options["alle"]:
            sources = sources.filter(is_active=True)

        rows = [self._check(source, refresh=options["refresh"], requeue=options["requeue"]) for source in sources]
        if options["nur_gesperrte"]:
            rows = [
                row
                for row in rows
                if row["robots_sperrt"] or row["gesperrt"] or row["nicht_erreichbar"] or row["ausnahme_problem"]
            ]

        if options["json"]:
            self.stdout.write(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        for row in rows:
            self._print(row)
        sperrt = sum(1 for row in rows if row["robots_sperrt"])
        per_ausnahme = sum(1 for row in rows if row["robots_sperrt"] and not row["gesperrt"])
        self.stdout.write(
            f"\n{len(rows)} Quelle(n), davon {sperrt} mit Sperre in der robots.txt, {per_ausnahme} davon per "
            f"Ausnahme geladen (Produkt-Token {PRODUCT_TOKEN})."
        )

    def _check(self, source: Any, *, refresh: bool, requeue: bool) -> dict[str, Any]:
        from insight_core.models import OParlFile
        from insight_core.services import robots

        config = source.sync_config if isinstance(source.sync_config, dict) else {}
        override = robots_override(config)
        api_agent = (source.user_agent or "").strip() or robots.USER_AGENT
        api = self._decide(source.url, KIND_API, override, agent=api_agent, refresh=refresh)

        hosts: dict[str, str] = {}
        urls = (
            OParlFile.objects.filter(body__source=source, deleted=False)
            .exclude(download_url__isnull=True)
            .exclude(download_url="")
            .order_by("-file_date", "-created_at")
            .values_list("download_url", flat=True)[:SAMPLE_FILES]
        )
        for url in urls:
            if not isinstance(url, str):
                continue
            host = urlsplit(url).netloc.lower()
            if host and host not in hosts and len(hosts) < MAX_FILE_HOSTS:
                hosts[host] = url
        file_agent = robots.user_agent_for(source)
        files = [
            self._decide(url, KIND_FILES, override, agent=file_agent, refresh=refresh) | {"beispiel": url}
            for url in hosts.values()
        ]
        checks = [api, *files]

        row: dict[str, Any] = {
            "quelle": source.name,
            "id": str(source.pk),
            "aktiv": source.is_active,
            "schnittstelle": api,
            "dateien": files,
            "ausnahme": {"bereich": override.scope, "vermerk": override.note} if override else None,
            "ausnahme_problem": robots_override_problem(config),
            # Wir rufen etwas nicht ab: Sperre ohne Ausnahme oder robots.txt nicht erreichbar
            "gesperrt": any(not check["erlaubt"] for check in checks),
            # Die robots.txt sperrt Schnittstelle oder Dateien, auch wenn eine Ausnahme den Abruf erlaubt
            "robots_sperrt": any(
                not check["robots_erlaubt"] and check["zustand"] != STATE_UNREACHABLE for check in checks
            ),
            "nicht_erreichbar": any(check["zustand"] == STATE_UNREACHABLE for check in checks),
        }
        files_allowed = all(f["erlaubt"] for f in files)
        if requeue and files_allowed:
            row["neu_eingereiht"] = robots.requeue_blocked_files(source)
        return row

    @staticmethod
    def _decide(url: str, kind: str, override: RobotsOverride | None, *, agent: str, refresh: bool) -> dict[str, Any]:
        """robots.txt immer auswerten; eine Ausnahme der Quelle getrennt davon vermerken."""
        from insight_core.services import robots

        decision = robots.decide(url, agent=agent, refresh=refresh)
        ausnahme_aktiv = override is not None and override.covers(kind)
        return {
            "erlaubt": decision.allowed or ausnahme_aktiv,
            "robots_erlaubt": decision.allowed,
            "regel": decision.rule,
            "zustand": decision.state,
            "robots_status": robots.load(url, agent=agent).status_code,
            "ausnahme_aktiv": ausnahme_aktiv,
        }

    def _print(self, row: dict[str, Any]) -> None:
        def text(result: dict[str, Any]) -> str:
            if result["zustand"] == STATE_UNREACHABLE:
                befund = f"robots.txt nicht erreichbar (HTTP {result['robots_status'] or '–'})"
            elif result["robots_erlaubt"]:
                return "erlaubt"
            else:
                befund = f"gesperrt ({result['regel']})" if result["regel"] else "gesperrt"
            return f"{befund}, Ausnahme aktiv" if result["ausnahme_aktiv"] else befund

        if row["gesperrt"]:
            marker = self.style.ERROR("[GESPERRT]")
        elif row["robots_sperrt"]:
            marker = self.style.WARNING("[AUSNAHME]")
        else:
            marker = self.style.SUCCESS("[frei]    ")
        self.stdout.write(f"{marker} {row['quelle']} ({row['id']}){'' if row['aktiv'] else ' – inaktiv'}")
        self.stdout.write(f"    Schnittstelle: {text(row['schnittstelle'])}")
        if not row["dateien"]:
            self.stdout.write("    Dateien: keine Download-Adressen bekannt")
        for result in row["dateien"]:
            self.stdout.write(f"    Dateien ({urlsplit(result['beispiel']).netloc}): {text(result)}")
        if row["ausnahme"]:
            self.stdout.write(f"    Ausnahme aktiv ({row['ausnahme']['bereich']}): {row['ausnahme']['vermerk']}")
        if row["ausnahme_problem"]:
            self.stdout.write(self.style.WARNING(f"    Ausnahme unwirksam: {row['ausnahme_problem']}"))
        if row.get("neu_eingereiht"):
            counts = row["neu_eingereiht"]
            self.stdout.write(
                f"    Neu eingereiht: {counts['extraction']} Textextraktion, {counts['file_cache']} Dokument-Cache"
            )
