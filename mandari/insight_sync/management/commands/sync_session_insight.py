# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lokaler Sync einer Session-OParl-Quelle in die Insight-Modelle (Issue #36).

Synchroner, leichtgewichtiger Gegenpart zum Ingestor-Daemon — spiegelt die
öffentliche OParl-API eines Session-Mandanten direkt in die
insight_core-Modelle, aus denen das Bürgerportal gespeist wird:

    # Nach Slug des Session-Mandanten (Quelle muss registriert sein,
    # siehe manage.py session_insight_source bzw. Veröffentlichungs-Schalter)
    python manage.py sync_session_insight --tenant musterstadt

    # Nach Quell-URL, Voll-Sync
    python manage.py sync_session_insight --source-url http://localhost:8000/session/musterstadt/api/oparl/ --full

    # Alle registrierten Session-Quellen inkrementell
    python manage.py sync_session_insight --all

Nur für Session-Quellen (``sync_config["session_tenant"]``), also die eigene Schnittstelle eines Session-Mandanten:
Der Spiegel ruft mit eigener Kennung ab, ohne robots.txt und ohne Takt je Server. Ratsinformationssysteme gleicht der
Ingestor ab (``python -m src.main sync --source <url>``), nach den Abrufregeln (docs/ABRUFREGELN.md, Issue #1027).
"""

from django.core.management.base import BaseCommand, CommandError

from insight_core.models import OParlSource
from insight_sync.session_mirror import SessionMirror


def _is_session_source(source: OParlSource) -> bool:
    return isinstance(source.sync_config, dict) and bool(source.sync_config.get("session_tenant"))


class Command(BaseCommand):
    help = "Spiegelt Session-OParl-Quellen synchron in die Insight-Modelle (Issue #36)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Slug des Session-Mandanten (sync_config.session_tenant)")
        parser.add_argument("--source-url", help="System-URL der OParl-Quelle")
        parser.add_argument("--all", action="store_true", help="Alle registrierten Session-Quellen")
        parser.add_argument("--full", action="store_true", help="Voll-Sync statt inkrementell")

    def _resolve_sources(self, options):
        if options["all"]:
            sources = [source for source in OParlSource.objects.filter(is_active=True) if _is_session_source(source)]
            if not sources:
                raise CommandError("Keine registrierten Session-Quellen gefunden (session_insight_source).")
            return sources
        if options.get("source_url"):
            source = OParlSource.objects.filter(url=options["source_url"]).first()
            if source is None:
                raise CommandError(f"Quelle {options['source_url']} ist nicht registriert.")
            if not _is_session_source(source):
                # Fremde Systeme nur über den Ingestor: robots.txt, Kennung und Takt je Server (Issue #1027)
                raise CommandError(
                    f"Quelle {options['source_url']} ist keine Session-Quelle (sync_config.session_tenant). "
                    "Ratsinformationssysteme gleicht der Ingestor ab: python -m src.main sync --source <url>"
                )
            return [source]
        if options.get("tenant"):
            for source in OParlSource.objects.filter(is_active=True):
                if (
                    isinstance(source.sync_config, dict)
                    and source.sync_config.get("session_tenant") == options["tenant"]
                ):
                    return [source]
            raise CommandError(
                f"Für Mandant '{options['tenant']}' ist keine aktive Quelle registriert "
                "(python manage.py session_insight_source --tenant <slug>)."
            )
        raise CommandError("Bitte --tenant, --source-url oder --all angeben.")

    def handle(self, *args, **options):
        for source in self._resolve_sources(options):
            self.stdout.write(f"Sync {source.name} ({source.url}) …")
            mirror = SessionMirror(source)
            stats = mirror.sync(full=options["full"])
            summary = ", ".join(f"{key}={value}" for key, value in stats.items() if value)
            self.stdout.write(self.style.SUCCESS(f"  OK: {summary or 'keine Änderungen'}"))
