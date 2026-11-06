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
(2FA-Gerät, Sitzungen, Tokens, Sicherheitsbenachrichtigungen).
Die letzte Bedingung ist absichtlich generisch über alle Rückbeziehungen des
User-Modells: Jede neue Beziehung schützt ein Konto automatisch, bis sie hier
ausdrücklich als Konto-Artefakt freigegeben wird.

Aufruf über ``manage.py cleanup_orphaned_accounts`` (siehe dort), Ausgabe nur Zahlen.

Derselbe Weg löscht ein Konto, sobald seine letzte Mitgliedschaft nach dem Auslauf „Nur Lesen, Löschung nach
30 Tagen“ gelöscht ist (Issue #1237, :func:`loesche_konto_nach_austritt`): ohne Fristen, aber nur, wenn nichts
mehr daran hängt. Dort binden Verweise, die beim Löschen nur geleert werden (Inhalte bleiben als „Ehemaliges
Mitglied“), das Konto nicht, ebenso wenig Tokens, der Kalender-Feed und versandte Einladungen. Die Insight-Merkliste
bindet: Das Konto bleibt, nur die Mitgliedschaft ist gelöscht. Mit dem Konto verschwindet auch die Datei des
Profilbilds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import models, transaction
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
        "sessions",
        "email_verification_tokens",
        "security_notifications",
        "webauthn_credentials",
    }
)


#: Zusätzlich beim Löschen nach dem Austritt (Issue #1237): Tokens und Zugänge des OpenID-Anbieters, der
#: Kalender-Feed und von der Person versandte Einladungen (offene verfallen schon beim Austritt, angenommene sind
#: Verlauf). Verweise mit SET_NULL binden dort ohnehin nicht, außer ``IMMER_BINDEND``. Die Insight-Merkliste
#: (``bookmarks``) bindet bewusst: Sie gehört zur Nutzung außerhalb der Organisation.
AUSTRITT_ENTFAELLT: frozenset[str] = KONTO_ARTEFAKTE | {
    "accounts_oauthaccesstoken",
    "accounts_oauthrefreshtoken",
    "accounts_oauthidtoken",
    "accounts_oauthgrant",
    "accounts_oauthdevicegrant",
    "calendar_feed_token",
    "created_invitations",
}
#: Bindet immer, obwohl der Verweis nur geleert würde: Eine Organisation verlöre ihre Eigentümerschaft.
IMMER_BINDEND: frozenset[str] = frozenset({"owned_organizations"})


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


def _fremde_verknuepfungen(
    user: User, *, entfaellt: frozenset[str] = KONTO_ARTEFAKTE, verweise_leeren: bool = False, erste: bool = False
) -> list[str]:
    """
    Was am Konto hängt und kein bloßes Konto-Artefakt ist (Namen der Rückbeziehungen).

    ``verweise_leeren``: Verweise mit SET_NULL zählen nicht (außer ``IMMER_BINDEND``), weil ihr Inhalt bleibt.
    ``erste``: nach dem ersten Fund aufhören (nur die Frage, ob überhaupt etwas hängt).
    """
    gefunden: list[str] = []
    if user.groups.exists():
        gefunden.append("groups")
    if user.user_permissions.exists():
        gefunden.append("user_permissions")
    if gefunden and erste:
        return gefunden
    for rel in User._meta.related_objects:
        name = rel.get_accessor_name()
        if name is None or name in entfaellt:
            continue
        if verweise_leeren and rel.on_delete is models.SET_NULL and name not in IMMER_BINDEND:
            continue
        if rel.related_model._default_manager.filter(**{rel.field.name: user}).exists():
            gefunden.append(name)
            if erste:
                break
    return gefunden


def _hat_fremde_verknuepfungen(user: User) -> bool:
    """True, wenn am Konto etwas hängt, das kein bloßes Konto-Artefakt ist."""
    return bool(_fremde_verknuepfungen(user, erste=True))


def _hat_gueltige_einladung(user: User, jetzt: datetime) -> bool:
    offen = Q(accepted_at__isnull=True, expires_at__gt=jetzt, email__iexact=user.email)
    return UserInvitation.objects.filter(offen).exists() or SessionInvitation.objects.filter(offen).exists()


def ist_frei(user: User, jetzt: datetime | None = None) -> bool:
    """Konto wird nirgends gebraucht (unabhängig von Fristen)."""
    jetzt = jetzt or timezone.now()
    if user.is_staff or user.is_superuser:
        return False
    return not _hat_fremde_verknuepfungen(user) and not _hat_gueltige_einladung(user, jetzt)


def bindungen_nach_austritt(user: User, jetzt: datetime | None = None) -> list[str]:
    """
    Was ein Konto nach dem Löschen seiner Mitgliedschaft noch hält (Issue #1237); leer = es darf weg.

    Mitarbeiter- und Superuser-Konten nie, ebenso wenig Konten mit weiterer Mitgliedschaft (auch deaktiviert oder
    im Auslauf), Session-Mandant, Gruppe, eigener Organisation, gültiger Einladung oder sonst einem Eintrag, der
    beim Löschen des Kontos mit verschwände.
    """
    if user.is_staff or user.is_superuser:
        return ["mitarbeiterkonto"]
    gruende = _fremde_verknuepfungen(user, entfaellt=AUSTRITT_ENTFAELLT, verweise_leeren=True)
    if _hat_gueltige_einladung(user, jetzt or timezone.now()):
        gruende.append("einladungen")
    return gruende


def loesche_konto_nach_austritt(user: User) -> list[str]:
    """
    Konto löschen, wenn nach dem Löschen der letzten Mitgliedschaft nichts mehr daran hängt (Issue #1237).

    Liefert die Bindungen, die das verhindern; leer heißt gelöscht (oder schon weg). Geprüft wird unter Sperre
    der Kontozeile, wie beim Löschen verwaister Konten. Die Datei des Profilbilds verschwindet nach erfolgreicher
    Transaktion mit.
    """
    with transaction.atomic():
        aktuell = User.objects.select_for_update().filter(pk=user.pk).first()
        if aktuell is None:
            return []
        gruende = bindungen_nach_austritt(aktuell)
        if gruende:
            logger.info("Konto nach Austritt nicht gelöscht, Bindungen: %s", ", ".join(gruende))
            return gruende
        _profilbild_nach_commit_entfernen(aktuell)
        aktuell.delete()
    return []


def _profilbild_nach_commit_entfernen(user: User) -> None:
    """Datei des Profilbilds erst nach erfolgreicher Transaktion löschen; fehlt sie schon, ist das kein Fehler."""
    if not user.avatar or not user.avatar.name:
        return
    storage, name = user.avatar.storage, user.avatar.name

    def entfernen() -> None:
        try:
            if storage.exists(name):
                storage.delete(name)
        except OSError:
            logger.warning("Profilbild eines gelöschten Kontos konnte nicht entfernt werden")

    transaction.on_commit(entfernen)


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
