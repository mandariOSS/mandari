# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OpenID-Connect-Anbieter vor dem Einschalten prüfen (Issue #1215).

    python manage.py oidc_pruefen

Prüft Schalter, Signaturschlüssel (lesbar, RSA, mindestens 2048 Bit, auch die bisherigen beim Schlüsselwechsel),
Aussteller (``SITE_URL``: https, ohne Pfad, Host in ``ALLOWED_HOSTS``) und ob Discovery und JWKS über den
Test-Client antworten, mit passender Schlüssel-ID. Läuft auch bei ausgeschaltetem Anbieter und zeigt dann, was zum
Einschalten fehlt. Rückgabewert 1 bei mindestens einem Fehler. Einzelheiten: :mod:`apps.accounts.oidc.readiness`,
Anleitung: ``docs/schnittstelle/anmeldung.md#teil-b-betrieb``.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.oidc import provider_enabled, readiness


class Command(BaseCommand):
    help = (
        "Prüft den OpenID-Connect-Anbieter: Schalter, Signaturschlüssel, Aussteller, Discovery und JWKS. "
        "Läuft auch ausgeschaltet und nennt dann, was zum Einschalten fehlt."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        styles = {
            readiness.OK: self.style.SUCCESS,
            readiness.HINWEIS: self.style.WARNING,
            readiness.FEHLER: self.style.ERROR,
        }
        errors = 0
        for finding in readiness.check():
            if finding.level == readiness.FEHLER:
                errors += 1
            self.stdout.write(styles[finding.level](f"{finding.level:<8} {finding.area}: {finding.text}"))
        self.stdout.write("")
        if errors:
            stand = "arbeitet nicht wie erwartet" if provider_enabled() else "lässt sich so nicht einschalten"
            raise CommandError(f"{errors} Fehler: Der Anbieter {stand}.")
        if provider_enabled():
            self.stdout.write(self.style.SUCCESS("Der Anbieter ist eingeschaltet und bereit."))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "Bereit zum Einschalten: OIDC_PROVIDER_ENABLED=true setzen und die Anwendung neu starten, "
                    "danach diese Prüfung wiederholen."
                )
            )
