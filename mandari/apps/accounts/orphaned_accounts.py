# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verwaiste Konten finden und löschen (Issue #238, DSGVO Art. 5 Abs. 1 lit. e).

Ein Konto gilt als verwaist, wenn es in eine dieser Gruppen fällt …

- **unbestätigt**: E-Mail-Adresse nie bestätigt und älter als ``UNBESTAETIGT_TAGE``
  (Bestätigungslink 48 h plus Kulanz),
- **abgelehnt**: Registrierungsanfrage vor mehr als ``ABGELEHNT_TAGE`` abgelehnt
  (Stempel ``User.registration_rejected_at``),
- **Altbestand**: bestätigt, aber ohne Ablehnungsstempel (Konten von vor der Einführung
  des Stempels), seit ``ALTBESTAND_TAGE`` ohne Anmeldung und älter als diese Frist,

… und zugleich nirgends mehr gebraucht wird: kein Staff/Superuser, keine Gruppe, keine
Mitgliedschaft in Organisation oder Session-Mandant, keine gültige Einladung auf die
Adresse und kein weiteres verknüpftes Objekt außer den reinen Konto-Artefakten
(2FA-Gerät, vertraute Geräte, Sitzungen, Tokens, Sicherheitsbenachrichtigungen).
Die letzte Bedingung ist absichtlich generisch über alle Rückbeziehungen des
User-Modells: Jede neue Beziehung schützt ein Konto automatisch, bis sie hier
ausdrücklich als Konto-Artefakt freigegeben wird.

Aufruf über ``manage.py cleanup_orphaned_accounts`` (siehe dort), Ausgabe nur Zahlen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.accounts.models import User
from apps.session.models import SessionInvitation
from apps.tenants.models import UserInvitation

logger = logging.getLogger(__name__)

UNBESTAETIGT_TAGE = 9  # 48 h Bestätigungslink + 7 Tage Kulanz
ABGELEHNT_TAGE = 30
ALTBESTAND_TAGE = 30

# Rückbeziehungen, die zum Konto selbst gehören und mit ihm verschwinden dürfen.
KONTO_ARTEFAKTE: frozenset[str] = frozenset(
    {
        "totp_device",
        "trusted_devices",
        "sessions",
        "password_reset_tokens",
        "email_verification_tokens",
        "security_notifications",
        "webauthn_credentials",
    }
)


@dataclass
class Fund:
    unbestaetigt: list[User]
    abgelehnt: list[User]
    altbestand: list[User]

    @property
    def alle(self) -> list[User]:
        return self.unbestaetigt + self.abgelehnt + self.altbestand

    @property
    def gesamt(self) -> int:
        return len(self.alle)


def _hat_fremde_verknuepfungen(user: User) -> bool:
    """True, wenn am Konto etwas hängt, das kein bloßes Konto-Artefakt ist."""
    if user.groups.exists() or user.user_permissions.exists():
        return True
    for rel in User._meta.related_objects:
        if rel.get_accessor_name() in KONTO_ARTEFAKTE:
            continue
        if rel.related_model._default_manager.filter(**{rel.field.name: user}).exists():
            return True
    return False


def _hat_gueltige_einladung(user: User, jetzt: datetime) -> bool:
    offen = Q(accepted_at__isnull=True, expires_at__gt=jetzt, email__iexact=user.email)
    return UserInvitation.objects.filter(offen).exists() or SessionInvitation.objects.filter(offen).exists()


def ist_frei(user: User, jetzt: datetime | None = None) -> bool:
    """Konto wird nirgends gebraucht (unabhängig von Fristen)."""
    jetzt = jetzt or timezone.now()
    if user.is_staff or user.is_superuser:
        return False
    return not _hat_fremde_verknuepfungen(user) and not _hat_gueltige_einladung(user, jetzt)


def _kandidaten(jetzt: datetime) -> tuple[QuerySet[User], QuerySet[User], QuerySet[User]]:
    basis = User.objects.filter(is_staff=False, is_superuser=False).order_by("date_joined")
    unbestaetigt = basis.filter(email_verified=False, date_joined__lt=jetzt - timedelta(days=UNBESTAETIGT_TAGE))
    abgelehnt = basis.filter(registration_rejected_at__lt=jetzt - timedelta(days=ABGELEHNT_TAGE))
    altbestand_grenze = jetzt - timedelta(days=ALTBESTAND_TAGE)
    altbestand = basis.filter(
        email_verified=True,
        registration_rejected_at__isnull=True,
        date_joined__lt=altbestand_grenze,
    ).filter(Q(last_login__isnull=True) | Q(last_login__lt=altbestand_grenze))
    return unbestaetigt, abgelehnt, altbestand


def verwaiste_konten(jetzt: datetime | None = None) -> Fund:
    """Alle Konten, die die Fristen gerissen haben und nirgends gebraucht werden."""
    jetzt = jetzt or timezone.now()
    unbestaetigt, abgelehnt, altbestand = _kandidaten(jetzt)
    gesehen: set[object] = set()

    def _frei(qs: QuerySet[User]) -> list[User]:
        treffer: list[User] = []
        for user in qs:
            if user.pk in gesehen:
                continue
            if ist_frei(user, jetzt):
                gesehen.add(user.pk)
                treffer.append(user)
        return treffer

    return Fund(unbestaetigt=_frei(unbestaetigt), abgelehnt=_frei(abgelehnt), altbestand=_frei(altbestand))


def loesche_verwaiste_konten(*, dry_run: bool = False, jetzt: datetime | None = None) -> tuple[Fund, int]:
    """Findet verwaiste Konten und löscht sie (außer bei ``dry_run``). Liefert Fund und Anzahl gelöschter."""
    fund = verwaiste_konten(jetzt)
    if dry_run:
        return fund, 0
    geloescht = 0
    for user in fund.alle:
        with transaction.atomic():
            # Zwischen Suche und Löschung kann sich etwas geändert haben (Login, Einladung).
            aktuell = User.objects.select_for_update().filter(pk=user.pk).first()
            if aktuell is None or not ist_frei(aktuell, jetzt):
                continue
            aktuell.delete()
            geloescht += 1
    logger.info(
        "Verwaiste Konten gelöscht: %d (unbestätigt %d, abgelehnt %d, Altbestand %d)",
        geloescht,
        len(fund.unbestaetigt),
        len(fund.abgelehnt),
        len(fund.altbestand),
    )
    return fund, geloescht
