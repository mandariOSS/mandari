# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zugangstoken nur als Hash speichern.

Einladungs-, Bestätigungs- und Feed-Links tragen ein Zufallstoken. In der Datenbank steht davon nur
der SHA-256-Hash; das Token selbst bekommt die berechtigte Person genau einmal zu sehen, in der Mail
oder direkt nach dem Erzeugen in der Oberfläche. Gerät die Datenbank oder eine Sicherung in falsche
Hände, lässt sich mit den Hashes kein Link bauen.

Warum SHA-256 ohne Salz und ohne langsame Schlüsselableitung: Die Tokens sind Zufallswerte mit
mindestens 122 Bit Entropie (UUID4; neue Tokens 256 Bit). Salz und Streckung schützen Geheimnisse
mit wenig Entropie, etwa Passwörter, vor Wörterbüchern und vorberechneten Tabellen. Bei 2^122
Möglichkeiten ist schon das Durchprobieren aussichtslos, ein Salz brächte nichts. Ohne Salz bleibt
der Hash deterministisch: Das Token aus dem Link genügt, um den Datensatz über einen Index zu finden.

Welche Felder gehasht sind und warum die übrigen Tokens im Klartext bleiben, steht in
``HASHED_TOKEN_FIELDS`` und ``PLAINTEXT_TOKEN_FIELDS``; ``apps/common/tests/test_token_registry.py``
prüft, dass jedes Token-Feld aller Modelle in genau einer der beiden Listen steht.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any, ClassVar, Self, cast

from django.db.models import QuerySet

#: Zufallsbytes neuer Tokens (256 Bit; ``token_urlsafe`` liefert daraus 43 Zeichen)
TOKEN_BYTES = 32
#: Länge des gespeicherten Hashes (SHA-256, hexadezimal)
HASH_LENGTH = 64
#: Längere Eingaben sind nie ein gültiges Token und werden gar nicht erst gehasht
MAX_TOKEN_LENGTH = 256


def new_token() -> str:
    """Neues URL-taugliches Zufallstoken."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(raw: str) -> str:
    """SHA-256 über das Token, hexadezimal (64 Zeichen)."""
    return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()


def unusable_token_hash() -> str:
    """
    Hash eines sofort verworfenen Tokens: Feld-Default für Datensätze, die ohne ``issue_token`` entstehen.

    Niemand kennt das zugehörige Token, der Datensatz ist also nie per Link erreichbar. Klartext kann so
    auch versehentlich nicht in die Datenbank gelangen.
    """
    return hash_token(new_token())


def token_matches(raw: object, stored_hash: str) -> bool:
    """Passt das Token zum gespeicherten Hash? Vergleich in konstanter Zeit."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_TOKEN_LENGTH or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(raw), stored_hash)


def find_by_token(queryset: QuerySet[Any], raw: object, field: str = "token") -> Any | None:
    """
    Datensatz zum Token aus einem Link oder ``None``.

    Nachgeschlagen wird über den Hash; der Abgleich danach läuft in konstanter Zeit. Laufzeitunterschiede
    beim Datenbankvergleich verraten höchstens etwas über den Hash, aus dem sich kein Token gewinnen lässt.
    """
    if not isinstance(raw, str) or not raw or len(raw) > MAX_TOKEN_LENGTH:
        return None
    digest = hash_token(raw)
    candidate = queryset.filter(**{field: digest}).first()
    if candidate is None or not hmac.compare_digest(str(getattr(candidate, field)), digest):
        return None
    return candidate


class HashedTokenMixin:
    """
    Mixin für Modelle mit einem Token, von dem nur der Hash gespeichert wird (vor ``models.Model`` erben).

    ``issue_token()`` erzeugt ein neues Token, setzt den Hash und hält das Token in ``plain_token``
    bereit – nur im Speicher, für die Mail oder die Anzeige direkt danach. Aus der Datenbank geladene
    Datensätze haben kein ``plain_token``.
    """

    token_field: ClassVar[str] = "token"

    #: Klartext direkt nach ``issue_token()``; nie gespeichert
    plain_token: str | None = None

    def issue_token(self) -> str:
        """Neues Token setzen (Hash im Feld, Klartext in ``plain_token``); speichert nicht."""
        raw = new_token()
        setattr(self, self.token_field, hash_token(raw))
        self.plain_token = raw
        return raw

    def token_for_link(self) -> str:
        """
        Token für einen Link, der jetzt verschickt wird.

        Direkt nach ``issue_token()`` das eben erzeugte Token. Sonst – etwa beim erneuten Senden einer
        Einladung – ein neues Token, gespeichert; ältere Links auf diesen Datensatz werden damit ungültig.
        """
        if self.plain_token:
            return self.plain_token
        raw = self.issue_token()
        cast(Any, self).save(update_fields=[self.token_field])
        return raw

    def token_matches(self, raw: object) -> bool:
        return token_matches(raw, str(getattr(self, self.token_field) or ""))

    @classmethod
    def find_by_token(cls, raw: object, queryset: QuerySet[Any] | None = None) -> Self | None:
        """Datensatz zum Token oder ``None`` (optional innerhalb eines vorgefilterten QuerySets)."""
        base = queryset if queryset is not None else cast(Any, cls)._default_manager.all()
        return cast("Self | None", find_by_token(base, raw, cls.token_field))


#: Felder, in denen nur der SHA-256-Hash eines Tokens steht
HASHED_TOKEN_FIELDS: frozenset[str] = frozenset(
    {
        "accounts.EmailVerificationToken.token",
        "accounts.TrustedDevice.device_token",
        "insight_core.PublicQuestion.verification_token",
        "session.SessionAPIToken.token",
        "session.SessionInvitation.token",
        "tenants.UserInvitation.token",
        "work.CalendarFeedToken.token",
        "work.AdministrationConnection.token_hash",
        "minutes.GpuNode.token_hash",
    }
)

#: Token-Felder, die bewusst im Klartext bleiben, mit Begründung
PLAINTEXT_TOKEN_FIELDS: dict[str, str] = {
    "accounts.PasswordResetToken.token": (
        "Ungenutzt: Das Zurücksetzen läuft über die zustandslosen Tokens von Django "
        "(default_token_generator); dieses Modell legt keine Zeilen an."
    ),
    "insight_core.PublicQuestion.answer_token": (
        "Die Erinnerungsmail an das Ratsmitglied verschickt denselben Antwortlink erneut. Eine Antwort "
        "erscheint erst nach Freigabe durch die Moderation."
    ),
    "insight_core.InsightSubscriber.token": (
        "Jeder Digest enthält den Verwaltungs- und Abmeldelink, der Server braucht das Token also bei "
        "jedem Versand. Es erlaubt nur, dieses Abo zu verwalten."
    ),
    "insight_core.DecisionSubscription.token": (
        "Jede Benachrichtigung enthält den Abmeldelink, der Server braucht das Token also bei jedem Versand. "
        "Es erlaubt nur, dieses Abo zu beenden."
    ),
    "work.FactionPublicApiAccess.token": (
        "Öffentlich gedacht: Das Token steht im Einbindungs-Snippet auf der Website der Fraktion (CORS) "
        "und liefert nur öffentliche Termine und Tagesordnungen."
    ),
    "work.FactionAttendanceCertificate.token": (
        "Prüfcode, der auf dem Teilnahmenachweis steht und an Dritte weitergegeben wird; er bestätigt nur "
        "die Zahl der Teilnahmen und muss beim erneuten Herunterladen wieder aufgedruckt werden."
    ),
    "session.SessionAPIToken.token_prefix": (
        "Nur die ersten 8 von 64 Zeichen, damit Verwaltung und Fraktion ein Token wiedererkennen; "
        "224 Bit bleiben geheim."
    ),
    "work.AdministrationConnection.token_prefix": (
        "Nur die ersten 8 von 64 Zeichen, damit Verwaltung und Fraktion ein Token wiedererkennen; "
        "224 Bit bleiben geheim."
    ),
    "session.SessionInvitationRecipient.response_nonce": (
        "Kein Token: fließt in den mit SECRET_KEY signierten Rückmeldelink ein. Ohne den Schlüssel, der "
        "nicht in der Datenbank liegt, lässt sich daraus kein Link bauen; Erinnerungen und Serienbriefe "
        "erzeugen den Link neu."
    ),
}
