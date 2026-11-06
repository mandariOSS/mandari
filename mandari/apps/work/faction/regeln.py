# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Regeln je Sitzungsart (Issue #1240, Epic #1226).

Einladung, Erinnerungen, Protokoll, Erster TOP und Beschlussfähigkeit gelten je Sitzungsart. Die wirksamen
Einstellungen entstehen feldweise in drei Stufen:

1. Standardwerte (:func:`standardwerte`),
2. Einstellungen der Organisation (``organization.settings["faction"]``),
3. Übersteuerung der Art (``FactionMeetingType.settings``; leer oder ohne das Feld heißt „wie die Organisation“).

Ein Leser fragt mit einer Sitzung, einer Sitzungsart oder einer Organisation. Mit einer Organisation gelten nur die
Stufen 1 und 2: So zeigt die Einstellungsseite der Organisation, was sie selbst speichert. Sitzungen und Arten
bekommen zusätzlich die Übersteuerung ihrer Art. Solange es nur die Standardart ohne Übersteuerung gibt, ist das
dasselbe wie bisher.

Dazu kommen die beiden Schalter der Art: :func:`nichtoeffentlich_erlaubt` (``non_public_enabled``) und
:func:`abstimmungen_erlaubt` (``voting_enabled``). Ohne Art (etwa eine Sitzung, die noch nicht gespeichert ist) sind
beide an, wie bisher.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apps.tenants.models import Organization

    from .models import FactionMeetingType

#: Felder, die eine Sitzungsart übersteuern kann. ``publish_protocols`` ist ein Feld der Organisation und gilt für alle
#: Arten; ``protocol_dispatch_since`` setzt Work selbst beim Einschalten des Protokollversands.
FELDER_JE_ART = frozenset(
    {
        "auto_create_approval_item",
        "link_previous_meeting",
        "protocol_revision_safe",
        "auto_lock_protocol_on_complete",
        "require_protocol_approval",
        "invitation_mode",
        "invitation_dispatch",
        "invitation_lead_hours",
        "agenda_reminder_enabled",
        "agenda_reminder_hours",
        "meeting_reminder_enabled",
        "meeting_reminder_hours",
        "protocol_dispatch",
        "protocol_dispatch_delay_hours",
        "protocol_dispatch_since",
        "quorum_rule",
        "first_agenda_title_with_previous",
        "first_agenda_title_no_previous",
        "first_agenda_description",
    }
)


def standardwerte() -> dict[str, Any]:
    """Standardwerte aller Einstellungen der Sitzungen (Stufe 1)."""
    from apps.common.quorum import DEFAULT_QUORUM_RULE

    from .invitations import INVITATION_DEFAULTS
    from .protocol_dispatch import PROTOCOL_DISPATCH_DEFAULTS

    return {
        "auto_create_approval_item": True,
        "link_previous_meeting": True,
        "protocol_revision_safe": True,
        "auto_lock_protocol_on_complete": True,
        "require_protocol_approval": True,
        "first_agenda_title_with_previous": "Tagesordnung festlegen und letztes Protokoll genehmigen",
        "first_agenda_title_no_previous": "Tagesordnung festlegen",
        "first_agenda_description": "",
        # Einladungslogik (Issue #62), TOP-Erinnerung (Issue #871), Erinnerung an die Sitzung (Issue #1071)
        **INVITATION_DEFAULTS,
        # Protokollversand (Issue #871)
        **PROTOCOL_DISPATCH_DEFAULTS,
        # Beschlussfähigkeit (Issue #69): Erweiterungspunkt, aktuell nur die Mehrheitsregel
        "quorum_rule": DEFAULT_QUORUM_RULE,
    }


def quelle(ziel: Any) -> tuple[Organization, FactionMeetingType | None]:
    """Organisation und Sitzungsart zu einer Sitzung, einer Art oder einer Organisation (dann ohne Art)."""
    from .models import FactionMeeting, FactionMeetingType

    if isinstance(ziel, FactionMeeting):
        art = ziel.meeting_type if ziel.meeting_type_id is not None else None
        return ziel.organization, art
    if isinstance(ziel, FactionMeetingType):
        return ziel.organization, ziel
    return ziel, None


def einstellungen_der_organisation(organization: Organization) -> dict[str, Any]:
    """Eigene Einstellungen der Organisation (Stufe 2), als Kopie."""
    return dict((organization.settings or {}).get("faction", {}) or {})


def uebersteuerung(art: FactionMeetingType | None) -> dict[str, Any]:
    """Übersteuerte Felder der Art (Stufe 3). Unbekannte Felder und ``None`` zählen nicht („wie die Organisation“)."""
    if art is None or not isinstance(art.settings, dict):
        return {}
    return {key: value for key, value in art.settings.items() if key in FELDER_JE_ART and value is not None}


def einstellungen(ziel: Any) -> dict[str, Any]:
    """
    Gespeicherte Einstellungen für eine Sitzung, Art oder Organisation, ohne Standardwerte: Organisation, darüber die
    Art. Für Leser, die fehlende oder ungültige Werte selbst ersetzen (Einladung, Protokollversand, Erster TOP).
    """
    organization, art = quelle(ziel)
    ergebnis = einstellungen_der_organisation(organization)
    ergebnis.update(uebersteuerung(art))
    return ergebnis


def wirksame_einstellungen(ziel: Any) -> dict[str, Any]:
    """Wirksame Einstellungen einer Sitzung, Art oder Organisation: Standardwerte, darüber Organisation und Art."""
    ergebnis = standardwerte()
    ergebnis.update(einstellungen(ziel))
    return ergebnis


def _art(ziel: Any) -> FactionMeetingType | None:
    return quelle(ziel)[1]


def tagesordnung_frei(ziel: Any) -> bool:
    """Bekommen neue Sitzungen der Art keine Standard-Tagesordnung (``agenda_mode`` „free“)?"""
    art = _art(ziel)
    return art is not None and art.agenda_mode == "free"


def nichtoeffentlich_erlaubt(ziel: Any) -> bool:
    """Bietet die Art einen nichtöffentlichen Teil an? Bestehende NÖ-TOPs bleiben auch ohne ihn geschützt."""
    art = _art(ziel)
    return art is None or bool(art.non_public_enabled)


def abstimmungen_erlaubt(ziel: Any) -> bool:
    """Gibt es in Sitzungen der Art Abstimmungen und Beschlussfähigkeit? Erfasste Abstimmungen bleiben lesbar."""
    art = _art(ziel)
    return art is None or bool(art.voting_enabled)


#: Meldungen, wenn eine Art das Gewünschte nicht anbietet (Oberfläche und Server)
NOE_AUS = "Diese Sitzungsart hat keinen nichtöffentlichen Teil."
ABSTIMMUNGEN_AUS = "In dieser Sitzungsart gibt es keine Abstimmungen."


class NichtOeffentlichAusError(ValueError):
    """Ein nichtöffentlicher TOP in einer Art ohne nichtöffentlichen Teil."""

    def __init__(self) -> None:
        super().__init__(NOE_AUS)


class AbstimmungenAusError(ValueError):
    """Eine Abstimmung in einer Art ohne Abstimmungen."""

    def __init__(self) -> None:
        super().__init__(ABSTIMMUNGEN_AUS)


def pruefe_nichtoeffentlich(ziel: Any, visibility: str) -> None:
    """Server-Prüfung für neue oder in den NÖ-Teil verschobene TOPs: ``NichtOeffentlichAusError``, wenn die Art keinen hat."""
    if visibility == "internal" and not nichtoeffentlich_erlaubt(ziel):
        raise NichtOeffentlichAusError()


def pruefe_abstimmung(ziel: Any) -> None:
    """Server-Prüfung vor dem Erfassen oder Ändern einer Abstimmung: ``AbstimmungenAusError``, wenn die Art keine hat."""
    if not abstimmungen_erlaubt(ziel):
        raise AbstimmungenAusError()
