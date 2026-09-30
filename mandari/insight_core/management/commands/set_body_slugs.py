# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Slugs von Kommunen setzen – mit eindeutiger Zuordnung über die ID (Issue #373).

Mit Slug hat eine Kommune ihr eigenes Bürgerportal unter ``/insight/k/<slug>/`` und ihre Sitemap
``/sitemap-insight-<slug>.xml``. Der Befehl rät keine Zuordnung: Ohne Angaben listet er die gelisteten
Kommunen mit ID, Slug, Cache-Verzeichnis und einem Vorschlag, den man prüft und ausdrücklich übergibt:

    python manage.py set_body_slugs                                   # Übersicht mit Vorschlag
    python manage.py set_body_slugs <id>=muenster <id>=koeln --dry-run
    python manage.py set_body_slugs <id>=muenster <id>=koeln

Idempotent: Hat die Kommune den Slug schon, ändert sich nichts. Einen anderen, schon gesetzten Slug
ersetzt der Befehl nur mit ``--replace`` (bisherige Links auf den alten Slug werden ungültig). Alle
Angaben werden vorab geprüft; ist eine fehlerhaft, ändert der Befehl nichts.

Der Dokument-Cache bleibt, wo er ist: Vor dem neuen Slug wird das bisherige Cache-Verzeichnis der
Kommune festgeschrieben (``OParlBody.file_cache_dir``). Kein Dokument wird verschoben, neu geladen
oder gelöscht.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils.text import slugify

from insight_core.models import OParlBody, validate_body_slug
from insight_core.services import file_cache

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})


def anzeigename(body: OParlBody) -> str:
    """Wie ``OParlBody.get_display_name``: Anzeigename, sonst Kurz-, sonst Langname."""
    return str(body.display_name or body.short_name or body.name or "")


def vorschlag(body: OParlBody) -> str:
    """Slug-Vorschlag aus dem Anzeigenamen – nur zur Anzeige, wird nie selbst gesetzt."""
    return slugify(anzeigename(body).translate(_UMLAUTS))


@dataclass
class Zuordnung:
    body: OParlBody
    slug: str

    @property
    def unveraendert(self) -> bool:
        return self.body.slug == self.slug


class Command(BaseCommand):
    help = "Slugs von Kommunen setzen (Zuordnung über die ID, idempotent, mit --dry-run)"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "zuordnungen", nargs="*", metavar="ID=SLUG", help="Kommune (UUID) und Slug, z. B. 3f2…=muenster"
        )
        parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts ändern")
        parser.add_argument(
            "--replace", action="store_true", help="Einen schon gesetzten, anderen Slug ersetzen (alte Links brechen)"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not options["zuordnungen"]:
            self._uebersicht()
            return
        zuordnungen = self._pruefen(options["zuordnungen"], ersetzen=options["replace"])
        dry_run = options["dry_run"]
        gesetzt = 0
        with transaction.atomic():
            for z in zuordnungen:
                gesetzt += self._setzen(z, dry_run=dry_run)
        unveraendert = len(zuordnungen) - gesetzt
        if dry_run:
            self.stdout.write(self.style.WARNING(f"Probelauf: {gesetzt} würden gesetzt, {unveraendert} unverändert."))
        else:
            self.stdout.write(self.style.SUCCESS(f"{gesetzt} gesetzt, {unveraendert} unverändert."))

    # ------------------------------------------------------------------
    # Übersicht
    # ------------------------------------------------------------------

    def _uebersicht(self) -> None:
        bodies = list(OParlBody.objects.listed().order_by("name"))
        if not bodies:
            self.stdout.write("Keine gelisteten Kommunen.")
            return
        vorschlaege: list[str] = []
        for body in bodies:
            cache_dir = body.file_cache_dir or f"{file_cache.body_dir_name(body)} (noch nicht festgeschrieben)"
            self.stdout.write(
                f"{body.id}  {anzeigename(body)[:40]:<40}  Slug: {body.slug or '–':<25}  Cache-Verzeichnis: {cache_dir}"
            )
            if not body.slug and vorschlag(body):
                vorschlaege.append(f"{body.id}={vorschlag(body)}")
        if vorschlaege:
            self.stdout.write("")
            self.stdout.write("Vorschlag für Kommunen ohne Slug – vor dem Ausführen prüfen, dann erst mit --dry-run:")
            self.stdout.write("  python manage.py set_body_slugs " + " ".join(vorschlaege) + " --dry-run")

    # ------------------------------------------------------------------
    # Prüfen und Setzen
    # ------------------------------------------------------------------

    def _pruefen(self, angaben: list[str], *, ersetzen: bool) -> list[Zuordnung]:
        """Alle Angaben vorab prüfen; bei einem Fehler wird nichts geändert."""
        fehler: list[str] = []
        zuordnungen: list[Zuordnung] = []
        je_body: dict[Any, str] = {}
        je_slug: dict[str, Any] = {}
        for angabe in angaben:
            kennung, trenner, slug = angabe.partition("=")
            slug = slug.strip()
            if not trenner or not slug:
                fehler.append(f"„{angabe}“: bitte als ID=SLUG angeben.")
                continue
            try:
                body_id = uuid.UUID(kennung.strip())
            except ValueError:
                fehler.append(f"„{angabe}“: „{kennung}“ ist keine Kommunen-ID (UUID).")
                continue
            body = OParlBody.objects.filter(pk=body_id, deleted=False).select_related("source").first()
            if body is None:
                fehler.append(f"„{angabe}“: keine Kommune mit dieser ID.")
                continue
            try:
                validate_body_slug(slug)
            except ValidationError as exc:
                fehler.append(f"„{angabe}“: {' '.join(exc.messages)}")
                continue
            if je_body.get(body.pk, slug) != slug or je_slug.get(slug, body.pk) != body.pk:
                fehler.append(f"„{angabe}“: widerspricht einer anderen Angabe in diesem Aufruf.")
                continue
            je_body[body.pk], je_slug[slug] = slug, body.pk
            fehler += self._konflikte(body, slug, ersetzen=ersetzen)
            zuordnungen.append(Zuordnung(body, slug))
        if fehler:
            raise CommandError("Nichts geändert:\n  " + "\n  ".join(fehler))
        return list({z.body.pk: z for z in zuordnungen}.values())

    def _konflikte(self, body: OParlBody, slug: str, *, ersetzen: bool) -> list[str]:
        from apps.session.models import SessionTenant
        from insight_core.portal import tenant_body

        name = f"„{anzeigename(body)}“"
        fehler: list[str] = []
        andere = OParlBody.objects.filter(slug=slug).exclude(pk=body.pk).first()
        if andere is not None:
            fehler.append(f"{name}: Slug „{slug}“ hat schon „{anzeigename(andere)}“ ({andere.pk}).")
        mandant = SessionTenant.objects.filter(slug=slug).first()
        if mandant is not None:
            kommune = tenant_body(mandant)
            if kommune is None or kommune.pk != body.pk:
                fehler.append(f"{name}: „{slug}“ ist der Slug eines Session-Mandanten und führt zu dessen Portal.")
        if body.slug and body.slug != slug and not ersetzen:
            fehler.append(f"{name} hat schon den Slug „{body.slug}“ – nur mit --replace ändern (alte Links brechen).")
        return fehler

    def _setzen(self, z: Zuordnung, *, dry_run: bool) -> int:
        body = z.body
        name = f"{anzeigename(body)[:40]:<40} ({body.pk})"
        if z.unveraendert:
            self.stdout.write(f"{name}  unverändert: {z.slug}")
            return 0
        verb = "würde setzen" if dry_run else "gesetzt"
        cache_dir = body.file_cache_dir or file_cache.body_dir_name(body)
        hinweis = "" if body.is_listed else "  (nicht gelistet: Portal und Sitemap erst nach dem Listen)"
        self.stdout.write(
            f"{name}  {verb}: {body.slug or '–'} → {z.slug}  (Cache-Verzeichnis bleibt: {cache_dir}){hinweis}"
        )
        if dry_run:
            return 1
        # Erst das bisherige Cache-Verzeichnis festschreiben, dann den Slug ändern
        file_cache.pin_body_dir(body)
        alt = body.slug
        body.slug = z.slug
        body.save(update_fields=["slug", "updated_at"])
        transaction.on_commit(lambda: cache.delete_many([f"insight_portal_slug:{s}" for s in (alt, z.slug) if s]))
        return 1
