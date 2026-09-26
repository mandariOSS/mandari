# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management-Command: Schlüsselwechsel.

Erfasst jedes Feld aus ``apps/common/crypto_registry.py``: Inhalte mit
Mandantenschlüssel, eingepackte Mandantenschlüssel, plattformweite Zugangsdaten mit
dem Hauptschlüssel und den zweiten Faktor. Ablauf und Sicherheitseigenschaften stehen
in ``apps/common/key_rotation.py``, der Betriebsablauf in ``docs/KRYPTOKONZEPT.md``
(Abschnitt „Schlüsselwechsel (Routine und Notfall)“).

    # Bestandsaufnahme, ändert nichts
    python manage.py rotate_encryption --dry-run

    # Hauptschlüssel gewechselt (alter in ENCRYPTION_MASTER_KEY_PREVIOUS): nur neu einpacken
    python manage.py rotate_encryption --master-only

    # Zusätzlich neue Mandantenschlüssel; nach einem Neustart der Anwendung abschließen
    python manage.py rotate_encryption
    python manage.py rotate_encryption --finalize

Ausgegeben werden nur Anzahlen und Datensatzkennungen, nie Werte oder Schlüssel.
"""

from __future__ import annotations

import os
from argparse import ArgumentParser
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError

from apps.common.crypto_registry import KIND_LABELS, TENANT_MODELS, KeyKind
from apps.common.key_rotation import MASTER_LEVEL, KeyRotation, RotationError, RotationResult, ScanResult, Status

COLUMNS = (Status.CURRENT, Status.PREVIOUS, Status.PLAINTEXT, Status.UNREADABLE, Status.UNASSIGNED)


class Command(BaseCommand):
    help = "Schlüsselwechsel für alle verschlüsselten Werte (Hauptschlüssel, Mandantenschlüssel, zweiter Faktor)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Nur zeigen, was betroffen wäre – Anzahlen je Feld, keine Werte, keine Änderung",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--master-only",
            action="store_true",
            help="Nur die Hauptschlüssel-Ebene umschreiben (Mandantenschlüssel, Plattform-Zugangsdaten, 2FA)",
        )
        mode.add_argument(
            "--finalize",
            action="store_true",
            help="Begonnenen Wechsel abschließen: Nachzügler neu verschlüsseln, vorherige Mandantenschlüssel löschen",
        )
        parser.add_argument(
            "--tenant-type",
            choices=["organization", "session", "both"],
            default="both",
            help="Welche Mandanten neue Schlüssel erhalten bzw. abgeschlossen werden (Standard: both)",
        )
        parser.add_argument(
            "--old-master-key",
            action="append",
            default=[],
            help=(
                "Zusätzlicher alter Hauptschlüssel (Base64), etwa nach einem Datenbankumzug. Auch über die "
                "Umgebungsvariable OLD_MASTER_KEY. Im laufenden Betrieb ENCRYPTION_MASTER_KEY_PREVIOUS verwenden."
            ),
        )
        parser.add_argument(
            "--ignore-unreadable",
            action="store_true",
            help="Unlesbare Werte (z. B. mit unbekanntem Schlüssel) unverändert lassen, statt abzubrechen",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        extra = [*options["old_master_key"]]
        if os.environ.get("OLD_MASTER_KEY"):
            extra.append(os.environ["OLD_MASTER_KEY"])
        tenant_type = options["tenant_type"]
        tenant_types = tuple(TENANT_MODELS.values()) if tenant_type == "both" else (tenant_type,)
        try:
            rotation = KeyRotation(
                extra_master_keys=extra,
                tenant_types=tenant_types,
                ignore_unreadable=options["ignore_unreadable"],
            )
        except (ImproperlyConfigured, ValueError) as exc:
            raise CommandError(str(exc)) from None

        master_only, finalize = options["master_only"], options["finalize"]
        self.stdout.write(f"Hauptschlüssel: aktueller und {len(rotation.previous_masters)} vorherige(r)")

        if options["dry_run"]:
            scan = rotation.scan()
            self._print_scan(scan)
            self._print_plan(scan, rotation, master_only=master_only, finalize=finalize)
            self.stdout.write(self.style.WARNING("DRY-RUN – es wurde nichts geändert."))
            return

        try:
            result = rotation.run(master_only=master_only, finalize=finalize)
        except RotationError as exc:
            raise CommandError(f"Abgebrochen, nichts Unvollständiges gespeichert: {exc}") from None
        self._print_result(result)

        if master_only or finalize:
            self._print_verification(rotation.scan(), rotation, finalize=finalize)
        else:
            self.stdout.write(
                "\nNächste Schritte:\n"
                "  1. Anwendung neu starten (alle Dienste mit ENCRYPTION_MASTER_KEY).\n"
                "  2. python manage.py rotate_encryption --finalize\n"
                "  3. Erst danach ENCRYPTION_MASTER_KEY_PREVIOUS leeren (falls gesetzt) und erneut neu starten."
            )

    # -- Ausgabe --------------------------------------------------------------------------------

    def _print_scan(self, scan: ScanResult) -> None:
        width = max(len(report.entry.label) for report in scan.fields)
        header = f"{'Feld':<{width}}  {'Schlüssel':<38}" + "".join(f"{status.value:>18}" for status in COLUMNS)
        self.stdout.write(header)
        for report in scan.fields:
            line = f"{report.entry.label:<{width}}  {KIND_LABELS[report.entry.kind]:<38}"
            line += "".join(f"{report.counts[status]:>18}" for status in COLUMNS)
            if report.unreadable_ids:
                line += f"   z. B. {', '.join(report.unreadable_ids)}"
            self.stdout.write(line)

    def _print_plan(self, scan: ScanResult, rotation: KeyRotation, *, master_only: bool, finalize: bool) -> None:
        master = scan.count(Status.PREVIOUS, Status.PLAINTEXT, kinds=MASTER_LEVEL)
        self.stdout.write("\nGeplant:")
        self.stdout.write(f"  Hauptschlüssel-Ebene: {master} Wert(e) auf den aktuellen Hauptschlüssel umschreiben")
        if not master_only:
            selected = {label for label, short in TENANT_MODELS.items() if short in rotation.tenant_types}
            fresh = [ref for ref, begun in scan.tenants.items() if ref[0] in selected and not begun]
            started = [ref for ref, begun in scan.tenants.items() if ref[0] in selected and begun]
            pending = sum(scan.tenant_count(ref, Status.PREVIOUS) for ref in started)
            if finalize:
                self.stdout.write(
                    f"  Abschluss: {len(started)} Mandant(en), {pending} Nachzügler neu verschlüsseln, "
                    "danach vorherige Mandantenschlüssel löschen"
                )
            else:
                values = sum(scan.tenant_count(ref, Status.CURRENT, Status.PREVIOUS) for ref in fresh)
                self.stdout.write(
                    f"  Neue Mandantenschlüssel: {len(fresh)}; begonnene Wechsel fortsetzen: {len(started)}"
                )
                self.stdout.write(f"  Mandanteninhalte neu verschlüsseln: {values + pending} Wert(e)")
        blocked = scan.count(Status.UNASSIGNED) + (0 if rotation.ignore_unreadable else scan.count(Status.UNREADABLE))
        if blocked:
            self.stdout.write(
                self.style.ERROR(f"  Achtung: {blocked} Wert(e) würden den Lauf abbrechen (siehe Tabelle).")
            )

    def _print_result(self, result: RotationResult) -> None:
        self.stdout.write(self.style.SUCCESS("Schlüsselwechsel ausgeführt:"))
        self.stdout.write(f"  Hauptschlüssel-Ebene umgeschrieben: {result.master_values_rewritten}")
        self.stdout.write(f"  Neue Mandantenschlüssel:            {result.tenants_switched}")
        self.stdout.write(f"  Fortgesetzte Wechsel:               {result.tenants_resumed}")
        self.stdout.write(f"  Mandanteninhalte neu verschlüsselt: {result.tenant_values_rewritten}")
        self.stdout.write(f"  Abgeschlossene Mandanten:           {result.tenants_finalized}")
        if result.skipped_unreadable:
            self.stdout.write(self.style.WARNING(f"  Unlesbar, unverändert gelassen:     {result.skipped_unreadable}"))

    def _print_verification(self, scan: ScanResult, rotation: KeyRotation, *, finalize: bool) -> None:
        on_old_master = scan.count(Status.PREVIOUS, Status.PLAINTEXT, kinds=MASTER_LEVEL)
        on_old_tenant = scan.count(Status.PREVIOUS, kinds=(KeyKind.TENANT,))
        started = sum(1 for begun in scan.tenants.values() if begun)
        self.stdout.write("\nPrüfung nach dem Lauf:")
        self.stdout.write(f"  Werte mit vorherigem Hauptschlüssel: {on_old_master}")
        self.stdout.write(f"  Werte mit vorherigem Mandantenschlüssel: {on_old_tenant}")
        self.stdout.write(f"  Mandanten mit nicht abgeschlossenem Wechsel: {started}")
        if on_old_master:
            self.stdout.write(self.style.ERROR("  ENCRYPTION_MASTER_KEY_PREVIOUS noch NICHT entfernen."))
        elif rotation.previous_masters:
            # Auch vorherige Mandantenschlüssel sind jetzt mit dem aktuellen Hauptschlüssel eingepackt
            self.stdout.write(
                self.style.SUCCESS("  ENCRYPTION_MASTER_KEY_PREVIOUS kann jetzt geleert werden; danach neu starten.")
            )
        if finalize and (on_old_tenant or started):
            self.stdout.write(self.style.WARNING("  Nicht alles abgeschlossen – bitte den Lauf wiederholen."))
