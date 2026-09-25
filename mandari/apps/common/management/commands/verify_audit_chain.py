# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hash-Ketten der Protokolle prüfen (Issue #221).

Prüft je Bereich – Session-Mandant, Fraktion, plattformweites Sicherheitsprotokoll – ob jeder
Eintrag unverändert ist (Hash), ob keiner fehlt (lückenlose Nummern, Verkettung zum Vorgänger)
und ob das Ende zum Kettenkopf passt. Die Ausgabe nennt je Bereich den Kettenkopf (Nummer und
Hash); wer ihn regelmäßig außerhalb von mandari festhält, erkennt später auch eine vollständig
neu berechnete Kette.

    python manage.py verify_audit_chain                          # alle Bereiche
    python manage.py verify_audit_chain --tenant stadt-musterstadt
    python manage.py verify_audit_chain --chain security
    python manage.py verify_audit_chain --strict                 # Hinweise zählen als Befund

Exit-Code 1 bei einem Befund (Manipulation, Lücke, Einträge außerhalb der Kette). Eignet sich für
einen täglichen Cron-Lauf mit Benachrichtigung bei Fehlschlag.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.common import audit_chain
from apps.common.einmalig import EinmaligMixin

CHAIN_CHOICES = ("all", "session", "faction", "security")


class Command(EinmaligMixin, BaseCommand):
    sperre = "verify_audit_chain"
    sperre_ttl = 6 * 3600
    help = "Hash-Ketten der Protokolle prüfen (Exit-Code 1 bei Befund)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--tenant", help="Slug eines Session-Mandanten")
        parser.add_argument("--organization", help="Slug einer Organisation (Fraktions-Änderungshistorie)")
        parser.add_argument("--chain", choices=CHAIN_CHOICES, default="all", help="Nur diese Kette prüfen")
        parser.add_argument(
            "--strict", action="store_true", help="Hinweise (z. B. unverketteter Altbestand) als Befund werten"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        targets = targets_from_options(options)
        if not targets:
            self.stdout.write("Keine Protokolle gefunden.")
            return
        findings = 0
        notes = 0
        for spec, scope_id, label in targets:
            result = audit_chain.verify(spec, scope_id)
            status = "intakt" if result.ok else f"BEFUND ({result.error_count})"
            self.stdout.write(
                f"{label}: {status} – {result.checked} Einträge geprüft, "
                f"Kettenkopf Nr. {result.head_seq} {result.head_hash or '-'}"
            )
            for message in result.errors:
                self.stderr.write(f"  - {message}")
            for message in result.warnings:
                self.stdout.write(f"  Hinweis: {message}")
            findings += 0 if result.ok else 1
            notes += len(result.warnings)
        if findings or (options.get("strict") and notes):
            raise CommandError(
                f"Prüfung fehlgeschlagen: {findings} Bereich(e) mit Befund, {notes} Hinweis(e).", returncode=1
            )
        self.stdout.write(f"OK: {len(targets)} Bereich(e) geprüft.")


def targets_from_options(options: dict[str, Any]) -> list[tuple[audit_chain.ChainSpec, Any, str]]:
    """Zu prüfende bzw. zu verkettende Bereiche aus den Befehlsoptionen (auch für audit_chain_backfill)."""
    from apps.session.models import SessionTenant
    from apps.tenants.models import Organization

    if options.get("tenant"):
        tenant = SessionTenant.objects.filter(slug=options["tenant"]).first()
        if tenant is None:
            raise CommandError(f"Mandant '{options['tenant']}' nicht gefunden.")
        return [(audit_chain.SESSION, tenant.pk, f"Session {tenant.slug}")]
    if options.get("organization"):
        organization = Organization.objects.filter(slug=options["organization"]).first()
        if organization is None:
            raise CommandError(f"Organisation '{options['organization']}' nicht gefunden.")
        return [(audit_chain.FACTION, organization.pk, f"Fraktion {organization.slug}")]

    chain = options.get("chain") or "all"
    targets: list[tuple[audit_chain.ChainSpec, Any, str]] = []
    if chain in ("all", "session"):
        slugs = dict(SessionTenant.objects.values_list("pk", "slug"))
        for scope_id in audit_chain.scope_ids(audit_chain.SESSION):
            targets.append((audit_chain.SESSION, scope_id, f"Session {slugs.get(scope_id, scope_id)}"))
    if chain in ("all", "faction"):
        slugs = dict(Organization.objects.values_list("pk", "slug"))
        for scope_id in audit_chain.scope_ids(audit_chain.FACTION):
            targets.append((audit_chain.FACTION, scope_id, f"Fraktion {slugs.get(scope_id, scope_id)}"))
    if chain in ("all", "security"):
        targets.append((audit_chain.SECURITY, None, "Sicherheitsprotokoll"))
    return targets
