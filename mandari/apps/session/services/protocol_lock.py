# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sperre der Niederschrift nach der Genehmigung (Issue #318).

Mit der Genehmigung (bzw. der direkten Veröffentlichung, wenn der Mandant keinen
Genehmigungsschritt vorsieht) sind Ergebnis, Stimmen (Zähler und namentliche Stimmen),
Beschluss- und Protokolltexte aller TOPs der Sitzung sowie der allgemeine Teil der Niederschrift
schreibgeschützt. Die Sperre sitzt im Modell (``save``/``delete`` von TOP, Einzelstimme,
Beratungsstation, Niederschrift und Sitzung) und im Abstimmungs-Service (Sammelschreibzugriffe),
nicht in der Oberfläche: Views, Admin, Signale und Befehle laufen alle hier durch.

Geändert werden darf danach nur innerhalb von :func:`permit` – das nutzen die Berichtigung
(``protocol_correction_service``) und die fristgerechte Löschung nichtöffentlicher Inhalte
(``privacy_service``). Der Rückfall einer genehmigten Niederschrift in Entwurf oder Prüfung ist
auch dort ausgeschlossen.

:class:`ProtocolLockedError` ist ein ``PermissionDenied``: Trifft ein Schreibweg ohne eigene
Fehlerbehandlung auf die Sperre, antwortet Django mit 403 statt mit einem Serverfehler.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from django.core.exceptions import PermissionDenied

if TYPE_CHECKING:
    from django.db.models import Model

    from apps.session.models import (
        SessionAgendaItem,
        SessionConsultation,
        SessionMeeting,
        SessionProtocol,
        SessionVote,
    )

#: Status, ab denen die Niederschrift gesperrt ist
LOCKED_STATUSES = ("approved", "published")

#: Felder eines TOP, die mit der Genehmigung gesperrt sind (verschlüsselte zuletzt: Sie werden
#: nur entschlüsselt, wenn sonst nichts geändert ist)
AGENDA_ITEM_LOCKED_FIELDS = (
    "vote_result",
    "votes_yes",
    "votes_no",
    "votes_abstain",
    "voting_method",
    "resolution_text",
    "protocol_note",
    "is_withdrawn",
    "withdrawn_reason",
    "paper",
    "resolution_text_encrypted",
    "protocol_note_encrypted",
)
#: Gesperrte Felder der Niederschrift selbst
PROTOCOL_LOCKED_FIELDS = ("content", "chair_name", "recorder_name", "content_encrypted")
#: Felder einer Beratungsstation, die am gesperrten TOP hängen
CONSULTATION_LOCKED_FIELDS = ("result", "agenda_item")

MESSAGE = (
    "Die Niederschrift dieser Sitzung ist genehmigt. Ergebnis, Stimmen und Texte lassen sich nur noch "
    "über eine Berichtigung ändern."
)
MESSAGE_DELETE_ITEM = (
    "Die Niederschrift dieser Sitzung ist genehmigt. Tagesordnungspunkte lassen sich nicht mehr löschen."
)
MESSAGE_DELETE_MEETING = "Die Niederschrift dieser Sitzung ist genehmigt. Die Sitzung lässt sich nicht mehr löschen."
MESSAGE_STATUS = (
    "Eine genehmigte Niederschrift geht nicht zurück in Entwurf oder Prüfung. Korrekturen laufen über "
    "eine Berichtigung."
)


class ProtocolLockedError(PermissionDenied):
    """Schreibzugriff auf eine gesperrte Niederschrift; die Meldung ist für die Oberfläche formuliert."""

    def __init__(self, message: str = MESSAGE) -> None:
        super().__init__(message)
        #: fester, für Nutzer formulierter Text – nur diesen in Antworten ausgeben
        self.user_message = message

    def __str__(self) -> str:
        return self.user_message


_permitted: ContextVar[frozenset[Any]] = ContextVar("session_protocol_lock_permitted", default=frozenset())


@contextmanager
def permit(meeting_id: Any) -> Iterator[None]:
    """Schreibzugriffe auf die gesperrte Sitzung erlauben (nur Berichtigung und Datenschutz-Löschung)."""
    token = _permitted.set(_permitted.get() | {meeting_id})
    try:
        yield
    finally:
        _permitted.reset(token)


def is_permitted(meeting_id: Any) -> bool:
    """Läuft gerade ein erlaubter Schreibvorgang für diese Sitzung?"""
    return meeting_id in _permitted.get()


def is_locked(meeting_id: Any) -> bool:
    """Ist die Niederschrift der Sitzung genehmigt oder veröffentlicht?"""
    if meeting_id is None:
        return False
    from apps.session.models import SessionProtocol

    return SessionProtocol.objects.filter(meeting_id=meeting_id, status__in=LOCKED_STATUSES).exists()


def locked_meeting_ids(meeting_ids: Iterable[Any]) -> set[Any]:
    """Gesperrte Sitzungen aus einer Menge – eine Abfrage für Listen und Admin."""
    from apps.session.models import SessionProtocol

    ids = [pk for pk in meeting_ids if pk is not None]
    if not ids:
        return set()
    return set(
        SessionProtocol.objects.filter(meeting_id__in=ids, status__in=LOCKED_STATUSES).values_list(
            "meeting_id", flat=True
        )
    )


def ensure_unlocked(meeting_id: Any, message: str = MESSAGE) -> None:
    """Für Services mit Sammelschreibzugriffen (``bulk_*``), an denen kein ``save`` hängt."""
    if not is_permitted(meeting_id) and is_locked(meeting_id):
        raise ProtocolLockedError(message)


# =============================================================================
# Vergleich
# =============================================================================


def _value(obj: Model, name: str) -> Any:
    """Feldwert für den Vergleich; verschlüsselte Felder im Klartext (Neuverschlüsseln ändert nichts)."""
    field = obj._meta.get_field(name)
    raw = getattr(obj, getattr(field, "attname", name))
    if not name.endswith("_encrypted"):
        return raw if raw is not None else ""
    if not raw:
        return ""
    getter = getattr(obj, f"get_{name.removesuffix('_encrypted')}_decrypted", None)
    if callable(getter):
        try:
            return getter()
        except Exception:  # noqa: BLE001 – nicht entschlüsselbar: im Zweifel als Änderung werten
            return bytes(raw)
    return bytes(raw)


def _changed(old: Model, new: Model, fields: Iterable[str]) -> list[str]:
    return [name for name in fields if _value(old, name) != _value(new, name)]


def _selected(candidates: tuple[str, ...], update_fields: Iterable[str] | None) -> tuple[str, ...]:
    """Gesperrte Felder, die dieses Speichern berührt (``update_fields`` kennt Namen und attnames)."""
    if update_fields is None:
        return candidates
    names = set(update_fields)
    return tuple(name for name in candidates if name in names or f"{name}_id" in names)


def _differs_from_default(obj: Model, name: str) -> bool:
    field = obj._meta.get_field(name)
    value = getattr(obj, getattr(field, "attname", name))
    default = field.get_default() if hasattr(field, "get_default") else None
    return (value or None) != (default or None)


# =============================================================================
# Wächter (aus den Modellen aufgerufen)
# =============================================================================


def guard_agenda_item(item: SessionAgendaItem, update_fields: Iterable[str] | None) -> None:
    """``SessionAgendaItem.save``: gesperrte Felder eines TOP einer genehmigten Niederschrift."""
    from apps.session.models import SessionAgendaItem

    fields = _selected(AGENDA_ITEM_LOCKED_FIELDS, update_fields)
    meeting_id = item.meeting_id
    if not fields or meeting_id is None or is_permitted(meeting_id):
        return
    if item._state.adding:
        # Neuer TOP mit Ergebnis oder Texten in einer genehmigten Niederschrift: keine Hintertür
        if any(_differs_from_default(item, name) for name in fields) and is_locked(meeting_id):
            raise ProtocolLockedError()
        return
    if not is_locked(meeting_id):
        return
    old = SessionAgendaItem.objects.select_related("meeting__tenant").filter(pk=item.pk).first()
    if old is not None and _changed(old, item, fields):
        raise ProtocolLockedError()


def guard_agenda_item_delete(item: SessionAgendaItem) -> None:
    """``SessionAgendaItem.delete``: kein Löschen von TOPs einer genehmigten Niederschrift."""
    if item.meeting_id is not None and not is_permitted(item.meeting_id) and is_locked(item.meeting_id):
        raise ProtocolLockedError(MESSAGE_DELETE_ITEM)


def guard_meeting_delete(meeting: SessionMeeting) -> None:
    """``SessionMeeting.delete``: Sitzungen mit genehmigter Niederschrift bleiben erhalten."""
    if not is_permitted(meeting.pk) and is_locked(meeting.pk):
        raise ProtocolLockedError(MESSAGE_DELETE_MEETING)


def guard_vote(vote: SessionVote) -> None:
    """``SessionVote.save``/``delete``: namentliche Stimmen sind mit der Genehmigung gesperrt."""
    from apps.session.models import SessionProtocol

    meeting_id = (
        SessionProtocol.objects.filter(meeting__agenda_items=vote.agenda_item_id, status__in=LOCKED_STATUSES)
        .values_list("meeting_id", flat=True)
        .first()
    )
    if meeting_id is not None and not is_permitted(meeting_id):
        raise ProtocolLockedError()


def guard_protocol(protocol: SessionProtocol, update_fields: Iterable[str] | None) -> None:
    """``SessionProtocol.save``: Inhalt gesperrt, kein Rückfall in Entwurf oder Prüfung."""
    from apps.session.models import SessionProtocol

    if protocol._state.adding or protocol.pk is None:
        return
    fields = _selected((*PROTOCOL_LOCKED_FIELDS, "status"), update_fields)
    if not fields:
        return
    old = SessionProtocol.objects.select_related("meeting__tenant").filter(pk=protocol.pk).first()
    if old is None or old.status not in LOCKED_STATUSES:
        return
    if "status" in fields and protocol.status not in LOCKED_STATUSES:
        raise ProtocolLockedError(MESSAGE_STATUS)
    content = tuple(name for name in fields if name != "status")
    if content and not is_permitted(protocol.meeting_id) and _changed(old, protocol, content):
        raise ProtocolLockedError()


def guard_protocol_delete(protocol: SessionProtocol) -> None:
    """``SessionProtocol.delete``: eine genehmigte Niederschrift bleibt erhalten."""
    from apps.session.models import SessionProtocol

    if protocol.pk is None or is_permitted(protocol.meeting_id):
        return
    status = SessionProtocol.objects.filter(pk=protocol.pk).values_list("status", flat=True).first()
    if status in LOCKED_STATUSES:
        raise ProtocolLockedError("Eine genehmigte Niederschrift lässt sich nicht löschen.")


def guard_consultation(consultation: SessionConsultation, update_fields: Iterable[str] | None) -> None:
    """
    ``SessionConsultation.save``: Das Ergebnis einer Station mit gesperrtem TOP folgt nur dem TOP.

    Die Rückschreibung ``signals.sync_consultation_result`` übernimmt das Ergebnis des TOP und
    bleibt damit erlaubt (etwa nach einer Berichtigung); eine abweichende Hand-Änderung oder das
    Lösen der Station vom TOP nicht.
    """
    from apps.session.models import SessionAgendaItem, SessionConsultation

    if consultation._state.adding or not _selected(CONSULTATION_LOCKED_FIELDS, update_fields):
        return
    old = SessionConsultation.objects.filter(pk=consultation.pk).values("result", "agenda_item_id").first()
    if old is None or old["agenda_item_id"] is None:
        return
    item = SessionAgendaItem.objects.filter(pk=old["agenda_item_id"]).values("meeting_id", "vote_result").first()
    if item is None or is_permitted(item["meeting_id"]) or not is_locked(item["meeting_id"]):
        return
    if consultation.agenda_item_id != old["agenda_item_id"]:
        raise ProtocolLockedError()
    if consultation.result != old["result"] and consultation.result != item["vote_result"]:
        raise ProtocolLockedError()
