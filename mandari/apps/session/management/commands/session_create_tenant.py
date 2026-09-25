# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neuen Session-Mandanten mit einem Befehl arbeitsfähig anlegen (Issue #317).

Standardrollen, Nummernkreis-Preset, aktuelle Wahlperiode, optional eine Gremienvorlage, erster
Administrator (vorhandenes Konto oder Einladung per E-Mail) und Schlüssel für verschlüsselte
Felder. Idempotent: Ein erneuter Lauf ergänzt Fehlendes. Der Befehl gibt nie Passwörter oder
Einladungslinks aus. Anleitung: docs/SESSION_MANDANT_ANLEGEN.md.

    # Profile, Gremienvorlagen und Nummernkreis-Presets anzeigen
    python manage.py session_create_tenant --list-presets

    # Hamburger Bezirk nach Profil, zuerst als Prüflauf
    python manage.py session_create_tenant --profile hamburg_bezirk \\
        --name "Bezirksversammlung Musterbezirk" --slug musterbezirk \\
        --admin-email sitzungsdienst@example.org --dry-run

    # NRW-Stadt mit eigener Wahlperiode und ohne Gremienvorlage
    python manage.py session_create_tenant --profile nrw_stadt --name "Stadt Musterstadt" \\
        --slug musterstadt --ags 05999000 --admin-email rat@example.org \\
        --term-name "Wahlperiode 2025–2030" --term-start 2025-11-01 --term-end 2030-10-31 --no-committees
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.session.services import numbering_service, tenant_provisioning
from apps.session.services.tenant_provisioning import ProvisioningError

ACTOR = "Befehl session_create_tenant"


def _datum(value: str | None, option: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CommandError(f"{option}: Datum bitte im Format JJJJ-MM-TT angeben.") from None


class Command(BaseCommand):
    help = "Legt einen Session-Mandanten arbeitsfähig an: Rollen, Nummernkreis, Wahlperiode, Gremien, Administrator."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--name", help="Name des Mandanten, z. B. „Bezirksversammlung Musterbezirk“")
        parser.add_argument("--slug", help="URL-Kürzel (Kleinbuchstaben, Ziffern, Bindestriche)")
        parser.add_argument("--admin-email", help="E-Mail des ersten Administrators (Konto oder Einladung)")
        parser.add_argument(
            "--profile", default="", help="Profil aus der Preset-Datei, z. B. nrw_stadt, hamburg_bezirk"
        )
        parser.add_argument("--short-name", default="", help="Kurzname (höchstens 50 Zeichen)")
        parser.add_argument("--kind", default=None, help="Körperschaftstyp, z. B. stadt, bezirk, gemeinde")
        parser.add_argument("--ags", default="", help="Amtlicher Gemeindeschlüssel (2, 3, 5 oder 8 Ziffern)")
        parser.add_argument("--numbering", default=None, help="Nummernkreis-Preset, z. B. hamburg_bezirk")
        parser.add_argument("--term-name", default=None, help="Name der aktuellen Wahlperiode (eigene statt Profil)")
        parser.add_argument("--term-number", type=int, default=None, help="Nummer der Wahlperiode (für {wp})")
        parser.add_argument("--term-start", default=None, help="Beginn der Wahlperiode (JJJJ-MM-TT)")
        parser.add_argument("--term-end", default=None, help="Ende der Wahlperiode (JJJJ-MM-TT)")
        gremien = parser.add_mutually_exclusive_group()
        gremien.add_argument("--committees", default=None, help="Gremienvorlage aus der Preset-Datei")
        gremien.add_argument("--no-committees", action="store_true", help="Keine Gremien anlegen")
        parser.add_argument("--preset-file", default=None, help="Eigene Preset-Datei (JSON, Aufbau wie mitgeliefert)")
        parser.add_argument("--dry-run", action="store_true", help="Prüflauf: alles ausführen und zurückrollen")
        parser.add_argument("--list-presets", action="store_true", help="Profile und Vorlagen anzeigen")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            katalog = tenant_provisioning.load_presets(Path(options["preset_file"]) if options["preset_file"] else None)
        except ProvisioningError as fehler:
            raise CommandError(" ".join(fehler.messages)) from None
        if options["list_presets"]:
            self._list(katalog)
            return

        fehlend = [f"--{name.replace('_', '-')}" for name in ("name", "slug", "admin_email") if not options[name]]
        if fehlend:
            raise CommandError("Bitte angeben: " + ", ".join(fehlend))
        try:
            spec = tenant_provisioning.build_spec(
                name=options["name"],
                slug=options["slug"],
                admin_email=options["admin_email"],
                catalog=katalog,
                profile=options["profile"],
                short_name=options["short_name"],
                body_type=options["kind"],
                ags=options["ags"],
                numbering=options["numbering"],
                term_name=options["term_name"],
                term_number=options["term_number"],
                term_start=_datum(options["term_start"], "--term-start"),
                term_end=_datum(options["term_end"], "--term-end"),
                committees="" if options["no_committees"] else options["committees"],
            )
            ergebnis = tenant_provisioning.provision_tenant(
                spec, catalog=katalog, dry_run=options["dry_run"], actor=ACTOR
            )
        except ProvisioningError as fehler:
            raise CommandError("\n".join(fehler.messages)) from None

        if ergebnis.dry_run:
            self.stdout.write(self.style.WARNING("Prüflauf – nichts wurde gespeichert, keine E-Mail versendet."))
        for schritt in ergebnis.steps:
            self.stdout.write(f"  {schritt}")
        for warnung in ergebnis.warnings:
            self.stdout.write(self.style.WARNING(f"  Hinweis: {warnung}"))
        if not ergebnis.dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Mandant bereit: /session/{ergebnis.slug}/ – Bürgerportal nach der Veröffentlichung "
                    f"unter /insight/k/{ergebnis.slug}/."
                )
            )

    def _list(self, katalog: tenant_provisioning.PresetKatalog) -> None:
        from apps.session.models import SessionTenant

        self.stdout.write("Profile:")
        for profil in katalog.profiles.values():
            self.stdout.write(
                f"  {profil.key:<18} {profil.label} – Nummernkreis {profil.numbering}, "
                f"{profil.term_name}, Gremien: {profil.committees or 'keine'}"
            )
        self.stdout.write("Gremienvorlagen:")
        for vorlage in katalog.committee_templates.values():
            self.stdout.write(f"  {vorlage.key:<18} {', '.join(g.name for g in vorlage.gremien)}")
        self.stdout.write("Nummernkreis-Presets:")
        for key, preset in numbering_service.PRESETS.items():
            self.stdout.write(f"  {key:<24} {preset.label}")
        self.stdout.write("Körperschaftstypen:")
        for key, label in SessionTenant.BODY_TYPE_CHOICES:
            self.stdout.write(f"  {key:<18} {label}")
