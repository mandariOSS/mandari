# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zweiten Faktor eines Kontos zurücksetzen – für Geräteverlust ohne Backup-Codes,
ausschließlich nach Identitätsprüfung.

    python manage.py reset_two_factor person@example.org --reason "Ticket 123, Rückruf verifiziert"

Entfernt Authenticator-App samt Backup-Codes, Sicherheitsschlüssel und
vertrauenswürdige Geräte. Besteht eine 2FA-Pflicht, folgt beim nächsten Login
die erneute Einrichtung. Der Vorgang wird mit Anlass protokolliert.
"""

from __future__ import annotations

import logging
from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import TrustedDevice, TwoFactorDevice, User, WebAuthnCredential

logger = logging.getLogger(__name__)


def _deleted(queryset: Any) -> int:
    """Nur die Anzahl gelöschter Zeilen — ``delete()`` liefert ``(Gesamtzahl, Details je Modell)``.

    Bewusst als eigene Funktion mit ``int``: Ins Protokoll gehört die Anzahl, nie ein
    Datensatz. Die Modelle tragen TOTP-Geheimnisse und Schlüsselkennungen.
    """
    total, _per_model = queryset.delete()
    return int(total)


class Command(BaseCommand):
    help = "Setzt Authenticator-App, Backup-Codes und Sicherheitsschlüssel eines Kontos zurück."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("email", help="E-Mail-Adresse des Kontos")
        parser.add_argument(
            "--reason", required=True, help="Anlass und Nachweis der Identitätsprüfung (wird protokolliert)"
        )
        parser.add_argument("--yes", action="store_true", help="Ohne Rückfrage ausführen")

    def handle(self, *args: Any, **options: Any) -> None:
        user = User.objects.filter(email__iexact=options["email"]).first()
        if user is None:
            raise CommandError("Konto nicht gefunden.")
        reason = str(options["reason"]).strip()
        if not reason:
            raise CommandError("Bitte den Anlass angeben (--reason).")
        if not options["yes"]:
            answer = input(f"Zweiten Faktor für {user.email} zurücksetzen? [ja/NEIN] ")
            if answer.strip().lower() != "ja":
                raise CommandError("Abgebrochen.")

        totp = _deleted(TwoFactorDevice.objects.filter(user=user))
        keys = _deleted(WebAuthnCredential.objects.filter(user=user))
        trusted = _deleted(TrustedDevice.objects.filter(user=user))
        logger.warning(
            "Zweiter Faktor zurückgesetzt",
            extra={"user_id": str(user.pk), "reason": reason, "totp": totp, "security_keys": keys, "trusted": trusted},
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Zurückgesetzt für {user.email}: Authenticator-App {totp}, Sicherheitsschlüssel {keys}, "
                f"vertrauenswürdige Geräte {trusted}."
            )
        )
