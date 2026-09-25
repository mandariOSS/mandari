# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Berichtigung einer genehmigten Niederschrift (Issue #318).

Nach der Genehmigung sind Ergebnis, Stimmen und Texte gesperrt (``protocol_lock``). Korrekturen
laufen nur hierüber: mit dem Recht „Protokolle freigeben“ (``approve_protocols``, auch aus einer
Vertretung), mit Grund und geänderten Werten. Jede Berichtigung steht als sprechender Eintrag im
Audit-Log und in der Niederschrift als „Berichtigung vom … (Grund)“ – in der öffentlichen
Fassung nur, wenn sie den öffentlichen Teil betrifft.

Vier-Augen-Prinzip (Entscheidung): Die Berichtigung folgt der Einstellung „Vier-Augen-Prinzip:
Niederschrift“. Ist sie aus, wird eine Berichtigung sofort wirksam und protokolliert – wie heute
die Genehmigung selbst. Ist sie an, entsteht ein Antrag, den eine zweite Person mit demselben
Recht bestätigt; wer ihn gestellt hat (auch in Vertretung), bestätigt ihn nicht selbst. Eine
eigene Einstellung gibt es bewusst nicht: Wer die Genehmigung absichert, will auch deren
nachträgliche Änderung abgesichert wissen.

Datenschutz: Die Anzeige (``changes``) enthält nur unverschlüsselte Werte, lange Texte nur als
„geändert“. Alle alten und neuen Werte liegen verschlüsselt im Antrag; beim Wirksamwerden wird
geprüft, dass der Stand seit dem Antrag unverändert ist.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction
from django.utils import timezone

from apps.session import audit
from apps.session.models import (
    SessionAgendaItem,
    SessionPerson,
    SessionProtocol,
    SessionProtocolCorrection,
    SessionVote,
)
from apps.session.services import four_eyes_service, protocol_lock, voting_service

if TYPE_CHECKING:
    from django.db.models import Model

    from apps.session.models import SessionUser

_audit = cast(Any, audit)

Correction = SessionProtocolCorrection

#: Höchstlänge des Grundes
REASON_MAX = 2000
#: Obergrenze je Zähler (wie in der Abstimmungserfassung)
COUNT_MAX = 9999
#: Abstimmungsarten mit Einzelstimmen; bei den übrigen werden die Summen berichtigt
INDIVIDUAL_METHODS = voting_service.INDIVIDUAL_METHODS


class CorrectionError(ValueError):
    """Berichtigung nicht möglich; die Meldung ist für die Oberfläche formuliert."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        #: fester, für Nutzer formulierter Text – nur diesen in Antworten ausgeben
        self.user_message = user_message


@dataclass(frozen=True)
class FieldSpec:
    """Ein berichtigungsfähiges Feld."""

    name: str
    label: str
    kind: str  # "choice" | "count" | "text"
    encrypted: bool = False
    #: lange Texte erscheinen in der Anzeige nur als „geändert“
    long: bool = False


ITEM_FIELDS = (
    FieldSpec("vote_result", "Ergebnis", "choice"),
    FieldSpec("votes_yes", "Ja-Stimmen", "count"),
    FieldSpec("votes_no", "Nein-Stimmen", "count"),
    FieldSpec("votes_abstain", "Enthaltungen", "count"),
    FieldSpec("resolution_text", "Beschlusstext", "text", long=True),
    FieldSpec("protocol_note", "Protokolltext", "text", long=True),
    FieldSpec("resolution_text_encrypted", "Nichtöffentlicher Beschlusstext", "text", encrypted=True, long=True),
    FieldSpec("protocol_note_encrypted", "Nichtöffentlicher Protokolltext", "text", encrypted=True, long=True),
)
GENERAL_FIELDS = (
    FieldSpec("content", "Allgemeiner Teil", "text", long=True),
    FieldSpec("chair_name", "Vorsitz (Unterschrift)", "text"),
    FieldSpec("recorder_name", "Protokollführung (Unterschrift)", "text"),
)
GENERAL_NP_FIELDS = (
    FieldSpec("content_encrypted", "Allgemeiner Teil (nichtöffentlich)", "text", encrypted=True, long=True),
)
COUNT_FIELDS = ("votes_yes", "votes_no", "votes_abstain")
VOTE_PREFIX = "vote_"
#: Farbe des Status in der Anzeige
STATUS_TONES = {
    Correction.STATUS_PENDING: "amber",
    Correction.STATUS_APPLIED: "green",
    Correction.STATUS_REJECTED: "gray",
}


@dataclass
class Outcome:
    """Ergebnis eines Antrags: wirksam oder zur Bestätigung, mit Hinweisen."""

    correction: SessionProtocolCorrection
    applied: bool
    warnings: list[str] = field(default_factory=list)


# =============================================================================
# Gegenstand, Felder, aktuelle Werte
# =============================================================================


def _result_labels() -> dict[str, str]:
    return {str(k): str(v) for k, v in SessionAgendaItem._meta.get_field("vote_result").choices or []}


def _vote_labels() -> dict[str, str]:
    return {str(k): str(v) for k, v in SessionVote.VOTE_CHOICES}


def item_is_public(item: SessionAgendaItem) -> bool:
    """Öffentlicher TOP einer öffentlichen Sitzung (auch der übergeordnete TOP öffentlich)."""
    parent = item.parent
    return bool(item.is_public and item.meeting.is_public and (parent is None or parent.is_public))


def field_specs(target: str, item: SessionAgendaItem | None, *, can_view_np: bool) -> tuple[FieldSpec, ...]:
    """Berichtigungsfähige Felder je Gegenstand (verschlüsselte nur mit NÖ-Recht)."""
    if target == Correction.TARGET_ITEM and item is not None:
        return tuple(
            spec
            for spec in ITEM_FIELDS
            if (can_view_np or not spec.encrypted)
            and not (spec.name in COUNT_FIELDS and item.voting_method in INDIVIDUAL_METHODS)
        )
    if target == Correction.TARGET_GENERAL:
        return GENERAL_FIELDS
    if target == Correction.TARGET_GENERAL_NP and can_view_np:
        return GENERAL_NP_FIELDS
    return ()


def _read(obj: Model, spec: FieldSpec) -> Any:
    """Aktueller Wert; verschlüsselte Felder im Klartext (nur interne Verwendung)."""
    if spec.encrypted:
        getter = getattr(obj, f"get_{spec.name.removesuffix('_encrypted')}_decrypted")
        return str(getter() or "")
    value = getattr(obj, spec.name)
    if spec.kind == "count":
        return int(value or 0)
    return str(value or "")


def _write(obj: Model, spec: FieldSpec, value: Any) -> None:
    if spec.encrypted:
        getattr(obj, f"set_{spec.name.removesuffix('_encrypted')}_encrypted")(value)
    else:
        setattr(obj, spec.name, value)


def current_values(obj: Model, specs: tuple[FieldSpec, ...]) -> dict[str, Any]:
    """Werte des Gegenstands für das Formular."""
    return {spec.name: _read(obj, spec) for spec in specs}


def current_votes(item: SessionAgendaItem) -> dict[str, str]:
    """Einzelstimmen je Person (ID als Text)."""
    return {str(person_id): str(vote) for person_id, vote in item.votes.values_list("person_id", "vote")}


def _parse(spec: FieldSpec, raw: Any) -> Any:
    text = str(raw if raw is not None else "").replace("\r\n", "\n")
    if spec.kind == "count":
        text = text.strip()
        if not text.isdigit() or int(text) > COUNT_MAX:
            raise CorrectionError(f"{spec.label}: bitte eine Zahl zwischen 0 und {COUNT_MAX} angeben.")
        return int(text)
    if spec.kind == "choice":
        if text not in _result_labels():
            raise CorrectionError(f"{spec.label}: ungültiger Wert.")
        return text
    if spec.name in ("chair_name", "recorder_name"):
        return text.strip()[:255]
    return text


def _display(spec: FieldSpec, value: Any) -> Any:
    if spec.kind == "choice":
        return _result_labels().get(str(value), str(value))
    return value


# =============================================================================
# Antrag
# =============================================================================


def _check_target(protocol: SessionProtocol, target: str, item: SessionAgendaItem | None, *, can_view_np: bool) -> None:
    if not protocol.is_locked:
        raise CorrectionError(
            "Berichtigungen gibt es erst nach der Genehmigung. Bis dahin bearbeiten Sie die Niederschrift direkt."
        )
    if target not in dict(Correction.TARGET_CHOICES):
        raise CorrectionError("Unbekannter Gegenstand der Berichtigung.")
    if target == Correction.TARGET_ITEM:
        if item is None or item.meeting_id != protocol.meeting_id:
            raise CorrectionError("Der Tagesordnungspunkt gehört nicht zu dieser Sitzung.")
        if not item_is_public(item) and not can_view_np:
            raise CorrectionError("Für nichtöffentliche Tagesordnungspunkte fehlt die Berechtigung.")
    elif target == Correction.TARGET_GENERAL_NP and not can_view_np:
        raise CorrectionError("Für den nichtöffentlichen Teil fehlt die Berechtigung.")
    if not protocol.meeting.is_public and not can_view_np:
        raise CorrectionError("Für nichtöffentliche Sitzungen fehlt die Berechtigung.")


def _vote_changes(
    item: SessionAgendaItem, data: Mapping[str, Any], assessed: voting_service.Eligibility
) -> tuple[dict[str, str], dict[str, str]]:
    """Geänderte Einzelstimmen (alt, neu) je Person-ID; nur stimmberechtigte Anwesende erhalten Stimmen."""
    if item.voting_method not in INDIVIDUAL_METHODS:
        return {}, {}
    valid = {value for value, _ in SessionVote.VOTE_CHOICES}
    existing = current_votes(item)
    allowed = {str(pk) for pk in assessed.voting_person_ids}
    old: dict[str, str] = {}
    new: dict[str, str] = {}
    rejected: list[str] = []
    for key, raw in data.items():
        if not key.startswith(VOTE_PREFIX):
            continue
        try:
            person_id = str(uuid.UUID(key.removeprefix(VOTE_PREFIX)))
        except ValueError:
            raise CorrectionError("Ungültige Stimme.") from None
        value = str(raw or "")
        if value and value not in valid:
            raise CorrectionError("Ungültige Stimme.")
        if value == existing.get(person_id, ""):
            continue
        if value and person_id not in allowed:
            rejected.append(person_id)
            continue
        old[person_id] = existing.get(person_id, "")
        new[person_id] = value
    if rejected:
        names = SessionPerson.objects.filter(pk__in=rejected, tenant_id=item.meeting.tenant_id).values_list(
            "family_name", flat=True
        )
        raise CorrectionError(
            "Stimmen nur von stimmberechtigten Anwesenden: " + (", ".join(sorted(names)) or "unbekannte Person") + "."
        )
    return old, new


def _person_names(item: SessionAgendaItem, person_ids: list[str]) -> dict[str, str]:
    persons = SessionPerson.objects.filter(pk__in=person_ids, tenant_id=item.meeting.tenant_id)
    return {str(p.pk): p.display_name for p in persons}


def propose(
    protocol: SessionProtocol,
    *,
    target: str,
    item: SessionAgendaItem | None,
    data: Mapping[str, Any],
    reason: str,
    user: SessionUser,
    can_view_np: bool,
    request: Any = None,
) -> Outcome:
    """
    Berichtigung beantragen; ohne Vier-Augen-Prinzip sofort wirksam.

    Raises:
        CorrectionError: kein Grund, keine Änderung, unzulässiger Wert oder Gegenstand.
        four_eyes_service.ApprovalError: Recht fehlt (auch aus einer Vertretung).
    """
    item = item if target == Correction.TARGET_ITEM else None
    _check_target(protocol, target, item, can_view_np=can_view_np)
    reason = (reason or "").strip()
    if not reason:
        raise CorrectionError("Bitte geben Sie den Grund der Berichtigung an.")
    if len(reason) > REASON_MAX:
        raise CorrectionError(f"Der Grund ist zu lang (höchstens {REASON_MAX} Zeichen).")

    # Recht und Vertretung (Vier-Augen erst bei der Bestätigung)
    on_behalf_of = four_eyes_service.authorize(four_eyes_service.PROCESS_PROTOCOL, protocol, user, four_eyes=False)

    source: Model = item if item is not None else protocol
    specs = field_specs(target, item, can_view_np=can_view_np)
    before = current_values(source, specs)
    old: dict[str, Any] = {}
    new: dict[str, Any] = {}
    for spec in specs:
        if spec.name not in data:
            continue
        value = _parse(spec, data.get(spec.name))
        if value != before[spec.name]:
            old[spec.name] = before[spec.name]
            new[spec.name] = value

    warnings: list[str] = []
    votes_old: dict[str, str] = {}
    votes_new: dict[str, str] = {}
    if item is not None:
        assessed = voting_service.eligibility(item.meeting)
        votes_old, votes_new = _vote_changes(item, data, assessed)
        if any(name in new for name in COUNT_FIELDS):
            counts = {name: new.get(name, before.get(name, 0)) for name in COUNT_FIELDS}
            check = voting_service.check_counts(
                item, counts["votes_yes"], counts["votes_no"], counts["votes_abstain"], assessed=assessed
            )
            if check.hard:
                raise CorrectionError(check.message.replace(" Die Stimmenzahlen wurden nicht übernommen.", ""))
            if check.exceeded:
                warnings.append(check.message)
    if not new and not votes_new:
        raise CorrectionError("Die Berichtigung ändert keinen Wert.")

    specs_by_name = {spec.name: spec for spec in specs}
    public_scope = (
        item_is_public(item) if item is not None else target == Correction.TARGET_GENERAL and protocol.meeting.is_public
    )
    plain_changed = any(not specs_by_name[name].encrypted for name in new) or bool(votes_new)
    is_public = bool(public_scope and plain_changed)
    changes = _display_changes(item, specs_by_name, old, new, votes_old, votes_new, is_public=is_public)

    correction = Correction(
        protocol=protocol,
        target=target,
        agenda_item=item,
        is_public=is_public,
        changes=changes,
        requested_by=user,
        requested_on_behalf_of=on_behalf_of,
        requested_at=timezone.now(),
    )
    if is_public:
        correction.reason = reason
    else:
        cast(Any, correction).set_reason_encrypted(reason)
    payload = {"alt": old, "neu": new, "stimmen": {"alt": votes_old, "neu": votes_new}}
    cast(Any, correction).set_payload_encrypted(json.dumps(payload, ensure_ascii=False))

    four_eyes = four_eyes_service.required(protocol.meeting.tenant, four_eyes_service.PROCESS_CORRECTION)
    with transaction.atomic():
        correction.status = Correction.STATUS_PENDING
        correction.save()
        if four_eyes:
            _log(correction, "protocol_correction_requested", user=user, on_behalf_of=on_behalf_of, request=request)
        else:
            _apply(correction)
            correction.decided_by = user
            correction.decided_on_behalf_of = on_behalf_of
            correction.decided_at = correction.applied_at
            correction.save(update_fields=["decided_by", "decided_on_behalf_of", "decided_at"])
            _log(correction, "protocol_correction", user=user, on_behalf_of=on_behalf_of, request=request)
    return Outcome(correction=correction, applied=not four_eyes, warnings=warnings)


def _display_changes(
    item: SessionAgendaItem | None,
    specs: dict[str, FieldSpec],
    old: dict[str, Any],
    new: dict[str, Any],
    votes_old: dict[str, str],
    votes_new: dict[str, str],
    *,
    is_public: bool,
) -> list[dict[str, Any]]:
    """Anzeige der Änderungen: unverschlüsselte Werte; lange Texte und NÖ-Felder nur als „geändert“."""
    entries: list[dict[str, Any]] = []
    for name, value in new.items():
        spec = specs[name]
        if spec.encrypted:
            # Verschlüsselte Felder erscheinen nie in einer öffentlichen Berichtigung
            if not is_public:
                entries.append({"feld": spec.label, "geaendert": True})
            continue
        if spec.long:
            entries.append({"feld": spec.label, "geaendert": True})
        else:
            entries.append({"feld": spec.label, "alt": _display(spec, old[name]), "neu": _display(spec, value)})
    if item is not None and votes_new:
        # Namen nur, wo die Stimmen ohnehin namentlich veröffentlicht werden (namentliche Abstimmung)
        if is_public and item.voting_method != "roll_call":
            entries.append({"feld": "Einzelstimmen", "geaendert": True})
        else:
            labels = _vote_labels()
            names = _person_names(item, list(votes_new))
            for person_id, value in votes_new.items():
                entries.append(
                    {
                        "feld": f"Stimme {names.get(person_id, 'unbekannt')}",
                        "alt": labels.get(votes_old.get(person_id, ""), "–"),
                        "neu": labels.get(value, "–"),
                    }
                )
    return entries


# =============================================================================
# Wirksam werden, bestätigen, ablehnen
# =============================================================================


def _payload(correction: SessionProtocolCorrection) -> dict[str, Any]:
    raw = cast(Any, correction).get_payload_decrypted() or "{}"
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _apply(correction: SessionProtocolCorrection) -> None:
    """Werte übernehmen – nur, wenn der Stand seit dem Antrag unverändert ist."""
    payload = _payload(correction)
    old: dict[str, Any] = payload.get("alt") or {}
    new: dict[str, Any] = payload.get("neu") or {}
    votes: dict[str, Any] = payload.get("stimmen") or {}
    protocol = correction.protocol
    stale = CorrectionError(
        "Der Stand hat sich seit dem Antrag geändert. Bitte lehnen Sie die Berichtigung ab und stellen Sie sie neu."
    )
    with transaction.atomic(), protocol_lock.permit(protocol.meeting_id):
        source: Model
        if correction.target == Correction.TARGET_ITEM:
            if correction.agenda_item_id is None:
                raise CorrectionError("Der Tagesordnungspunkt existiert nicht mehr.")
            item = (
                SessionAgendaItem.objects.select_for_update()
                .select_related("meeting__tenant")
                .get(pk=correction.agenda_item_id)
            )
            source = item
            specs = {spec.name: spec for spec in ITEM_FIELDS}
        else:
            item = None
            source = SessionProtocol.objects.select_for_update().select_related("meeting__tenant").get(pk=protocol.pk)
            specs = {spec.name: spec for spec in (*GENERAL_FIELDS, *GENERAL_NP_FIELDS)}
        for name, value in old.items():
            if name not in specs or _read(source, specs[name]) != value:
                raise stale
        vote_old: dict[str, str] = votes.get("alt") or {}
        vote_new: dict[str, str] = votes.get("neu") or {}
        if item is not None and vote_new:
            existing = current_votes(item)
            if any(existing.get(pid, "") != value for pid, value in vote_old.items()):
                raise stale
        for name, value in new.items():
            _write(source, specs[name], value)
        if new:
            source.save()
        if item is not None and vote_new:
            persons = {
                str(p.pk): p
                for p in SessionPerson.objects.filter(pk__in=list(vote_new), tenant_id=item.meeting.tenant_id)
            }
            try:
                voting_service.capture_votes(
                    item,
                    {persons[pid]: value for pid, value in vote_new.items() if pid in persons},
                    recorded_by=correction.requested_by,
                )
            except voting_service.VotingRightsError as exc:
                raise CorrectionError(exc.user_message) from exc
        correction.status = Correction.STATUS_APPLIED
        correction.applied_at = timezone.now()
        correction.save(update_fields=["status", "applied_at"])


def _log(
    correction: SessionProtocolCorrection,
    action: str,
    *,
    user: SessionUser,
    on_behalf_of: SessionUser | None,
    request: Any,
    note: str = "",
) -> None:
    """Sprechender Audit-Eintrag an der Niederschrift – Grund nur bei öffentlichen Berichtigungen im Klartext."""
    changes: dict[str, Any] = {
        "berichtigung": str(correction.pk),
        "gegenstand": correction.subject_label,
        "grund": correction.reason if correction.is_public else "(nichtöffentlich, verschlüsselt gespeichert)",
        "aenderungen": correction.changes,
        "oeffentlicher_teil": correction.is_public,
    }
    if correction.requested_by_id and correction.decided_by_id and action != "protocol_correction_requested":
        changes["beantragt_von"] = str(correction.requested_by_id)
    if note:
        changes["vermerk"] = note[:300]
    _audit.log_event(
        action,
        correction.protocol,
        tenant=correction.protocol.meeting.tenant,
        user=user,
        request=request,
        on_behalf_of=on_behalf_of,
        changes=changes,
    )


def confirm(correction: SessionProtocolCorrection, *, user: SessionUser, request: Any = None) -> None:
    """
    Beantragte Berichtigung bestätigen und wirksam werden lassen (Vier-Augen-Prinzip).

    Raises:
        CorrectionError: nicht mehr offen oder Stand geändert.
        four_eyes_service.ApprovalError: eigene Berichtigung oder Recht fehlt.
    """
    if correction.status != Correction.STATUS_PENDING:
        raise CorrectionError("Diese Berichtigung ist nicht mehr offen.")
    on_behalf_of = four_eyes_service.authorize(four_eyes_service.PROCESS_CORRECTION, correction, user)
    with transaction.atomic():
        _apply(correction)
        correction.decided_by = user
        correction.decided_on_behalf_of = on_behalf_of
        correction.decided_at = correction.applied_at
        correction.save(update_fields=["decided_by", "decided_on_behalf_of", "decided_at"])
        _log(correction, "protocol_correction", user=user, on_behalf_of=on_behalf_of, request=request)


def reject(correction: SessionProtocolCorrection, *, user: SessionUser, note: str = "", request: Any = None) -> None:
    """Beantragte Berichtigung ablehnen – auch durch die antragstellende Person (Rückzug)."""
    if correction.status != Correction.STATUS_PENDING:
        raise CorrectionError("Diese Berichtigung ist nicht mehr offen.")
    on_behalf_of = four_eyes_service.authorize(four_eyes_service.PROCESS_CORRECTION, correction, user, four_eyes=False)
    correction.status = Correction.STATUS_REJECTED
    correction.decided_by = user
    correction.decided_on_behalf_of = on_behalf_of
    correction.decided_at = timezone.now()
    correction.decision_note = (note or "").strip()[:500]
    correction.save(update_fields=["status", "decided_by", "decided_on_behalf_of", "decided_at", "decision_note"])
    _log(
        correction,
        "protocol_correction_rejected",
        user=user,
        on_behalf_of=on_behalf_of,
        request=request,
        note=correction.decision_note,
    )


# =============================================================================
# Anzeige
# =============================================================================


def internal_rows(
    protocol: SessionProtocol, *, can_view_np: bool, actor: SessionUser | None = None
) -> list[dict[str, Any]]:
    """
    Berichtigungen für die interne Ansicht der Niederschrift.

    Ohne NÖ-Recht erscheinen nur Berichtigungen des öffentlichen Teils; mit NÖ-Recht wird der
    Grund nichtöffentlicher Berichtigungen entschlüsselt (Lesezugriff protokolliert die View).
    """
    rows: list[dict[str, Any]] = []
    corrections = protocol.corrections.select_related("agenda_item", "requested_by__user", "decided_by__user").order_by(
        "requested_at"
    )
    for correction in corrections:
        if not correction.is_public and not can_view_np:
            continue
        decision = None
        if actor is not None and correction.status == Correction.STATUS_PENDING:
            decision = four_eyes_service.evaluate(four_eyes_service.PROCESS_CORRECTION, correction, actor)
        rows.append(
            {
                "correction": correction,
                "reason": correction.reason if correction.is_public else cast(Any, correction).get_reason_decrypted(),
                "changes": _change_lines(correction),
                "decision": decision,
                "tone": STATUS_TONES.get(correction.status, "gray"),
            }
        )
    return rows


def _change_lines(correction: SessionProtocolCorrection) -> list[str]:
    from apps.session.services import protocol_service

    return protocol_service.change_lines(correction)
