# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Niederschrift über OParl und im Bürgerportal (Issue #318).

Mit dem Veröffentlichen entsteht aus dem öffentlichen Teil der Niederschrift eine Datei an der
Sitzung (``SessionFile``, gespeichert unter ``session/files/…``, nie direkt über ``/media/``
abrufbar). Die Session-OParl-API liefert sie als ``resultsProtocol`` des ``Meeting`` (OParl 1.1)
über die bestehende Datei-View aus; der Insight-Ingestor spiegelt sie ins Bürgerportal, wo sie
auf der Sitzungsseite erscheint.

Der nichtöffentliche Teil erscheint nie: PDF (bestehende Erzeugung ``protocol_service
.build_protocol_pdf`` in der Ö-Fassung) und Text entstehen ausschließlich aus unverschlüsselten
Feldern öffentlicher TOPs einer öffentlichen Sitzung, verschlüsselte Felder werden hier nie
entschlüsselt. Für nichtöffentliche Sitzungen gibt es keine öffentliche Fassung.

Jede Änderung erzeugt eine neue Datei mit neuer Kennung und löscht die alte: Die alte hinterlässt
einen Tombstone und verschwindet sofort aus dem Bürgerportal (``retract_from_portal``, samt
Suchindex und Dokumenten-Zwischenspeicher); der Spiegel lädt die neue Fassung frisch. Das gilt
für Rücknahme, Berichtigung und jede Änderung am öffentlichen Inhalt nach der Veröffentlichung
(etwa ein TOP, der nachträglich nichtöffentlich wird).

PDF-Erzeugung ist teuer: Sie läuft beim Veröffentlichen und bei Änderungen, nie beim Abruf.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.session import audit
from apps.session.models import SessionFile, SessionMeeting, SessionProtocol
from apps.session.services import agenda_service, protocol_service

if TYPE_CHECKING:
    from apps.session.models import SessionAgendaItem, SessionUser

logger = logging.getLogger(__name__)

_log_event = cast(Any, audit).log_event

#: Anzeigename der Datei (OParl ``name``, ``fileName`` mit Endung) – ohne Inhalte der Sitzung
PUBLIC_FILE_NAME = "Niederschrift (öffentlicher Teil)"
PUBLIC_MIME_TYPE = "application/pdf"


def is_publishable(protocol: SessionProtocol) -> bool:
    """Veröffentlichte Niederschrift einer öffentlichen Sitzung?"""
    return protocol.status == "published" and bool(protocol.meeting.is_public)


# =============================================================================
# Text der öffentlichen Fassung (OParl ``text``, Suchindex)
# =============================================================================


def _paper_visible(item: SessionAgendaItem) -> bool:
    from apps.session.oparl_publication import UNVEROEFFENTLICHT

    paper = item.paper
    return paper is not None and bool(paper.is_public) and paper.status not in UNVEROEFFENTLICHT


def _item_lines(item: SessionAgendaItem) -> list[str]:
    """Ein öffentlicher TOP: ausschließlich unverschlüsselte Felder."""
    head = f"TOP {item.number}: {item.name}"
    if item.is_supplementary:
        head += " (Nachtrag)"
    if item.is_withdrawn:
        head += " (abgesetzt" + (f": {item.withdrawn_reason}" if item.withdrawn_reason else "") + ")"
    lines = [head]
    if item.paper is not None and _paper_visible(item):
        lines.append(f"Vorlage: {item.paper.reference}")
    if item.protocol_note:
        lines.append(item.protocol_note)
    if item.resolution_text:
        lines.append(f"Beschluss: {item.resolution_text}")
    if item.vote_result != "pending":
        method = f" ({item.get_voting_method_display()})" if item.voting_method != "summary" else ""
        lines.append(
            f"Abstimmung: {item.get_vote_result_display()}{method} (Ja: {item.votes_yes}, "
            f"Nein: {item.votes_no}, Enthaltungen: {item.votes_abstain})"
        )
    votes = list(item.votes.select_related("person"))
    if item.voting_method == "roll_call":
        named = [
            f"{v.person.display_name} ({v.get_vote_display()})" for v in votes if v.vote in ("yes", "no", "abstain")
        ]
        if named:
            lines.append("Namentlich: " + ", ".join(named))
    excluded = [v.person.display_name for v in votes if v.vote == "excluded"]
    if excluded:
        lines.append(
            "Mitwirkungsverbot (§ 31 GO): " + ", ".join(excluded) + " – an Beratung und Abstimmung nicht beteiligt."
        )
    return [line for line in lines if line]


def public_text(protocol: SessionProtocol) -> str:
    """
    Text der öffentlichen Fassung – dieselben Inhalte wie das Ö-PDF, ohne Briefkopf.

    Quelle sind nur unverschlüsselte Felder der Sitzung, der öffentlichen TOPs (ohne
    nichtöffentliche Unterpunkte), des allgemeinen Teils und der öffentlichen Berichtigungen.
    """
    meeting = protocol.meeting
    start = timezone.localtime(meeting.start)
    termin = f"{start:%d.%m.%Y}, {start:%H:%M} Uhr"
    if meeting.end:
        termin += f" bis {timezone.localtime(meeting.end):%H:%M} Uhr"
    lines = [
        "Niederschrift – öffentlicher Teil",
        str(meeting.tenant.name),
        f"Sitzung: {meeting.name}",
        f"Gremium: {meeting.organization.name}",
        f"Termin: {termin}",
    ]
    if meeting.location:
        lines.append(f"Ort: {meeting.location}" + (f", {meeting.room}" if meeting.room else ""))

    participants = protocol_service.participant_directory(meeting)
    if participants["present"]:
        lines += ["", "Anwesend:"]
        lines += [f"{a.person.display_name} ({a.get_role_display()})" for a in participants["present"]]
    if participants["excused"]:
        lines += ["", "Entschuldigt:"] + [a.person.display_name for a in participants["excused"]]
    if participants["absent"]:
        lines += ["", "Unentschuldigt abwesend:"] + [a.person.display_name for a in participants["absent"]]

    if protocol.content:
        lines += ["", "Allgemeines", protocol.content]

    agenda = agenda_service.grouped_agenda(meeting, include_non_public=False)
    lines += ["", "Verhandlung der Tagesordnung (öffentlicher Teil)"]
    for item in agenda["public"]:
        lines += ["", *_item_lines(item)]
        for sub in item.children_list:
            lines += ["", *_item_lines(sub)]

    notes = protocol_service.correction_notes(
        protocol, internal=False, visible_item_ids=protocol_service.public_item_ids(agenda)
    )
    if notes:
        lines += ["", "Berichtigungen"]
        for note in notes:
            changes = "; ".join(note["changes"])
            lines.append(
                f"Berichtigung vom {timezone.localtime(note['date']):%d.%m.%Y} ({note['reason']}) – "
                f"{note['subject']}" + (f": {changes}" if changes else "")
            )
    if protocol.approval_note:
        lines += ["", "Genehmigungsvermerk", protocol.approval_note]
    signatures = [name for name in (protocol.chair_name, protocol.recorder_name) if name]
    if signatures:
        lines += ["", "Unterschriften: " + ", ".join(signatures)]
    return "\n".join(lines).strip() + "\n"


# =============================================================================
# Veröffentlichen, Aktualisieren, Zurücknehmen
# =============================================================================


def _file_name(protocol: SessionProtocol) -> str:
    """Speichername ohne Inhalte der Sitzung, nur mit dem Sitzungsdatum."""
    return f"niederschrift-{timezone.localtime(protocol.meeting.start):%Y-%m-%d}.pdf"


def _touch_meeting(meeting_id: Any) -> None:
    """``modified`` der Sitzung anheben, damit inkrementelle OParl-Clients sie neu laden."""
    SessionMeeting.objects.filter(pk=meeting_id).update(updated_at=timezone.now())


def _current_file(protocol: SessionProtocol) -> SessionFile | None:
    if protocol.public_file_id is None:
        return None
    return SessionFile.objects.filter(pk=protocol.public_file_id).first()


def withdraw(protocol: SessionProtocol) -> bool:
    """
    Öffentliche Fassung zurücknehmen: Datei löschen (Tombstone, sofortige Rücknahme aus dem
    Bürgerportal über die Signale in ``oparl_publication``).

    Returns:
        True, wenn es eine öffentliche Fassung gab.
    """
    current = _current_file(protocol)
    SessionProtocol.objects.filter(pk=protocol.pk).update(public_file=None)
    protocol.public_file = None
    if current is None:
        return False
    current.delete()
    _touch_meeting(protocol.meeting_id)
    return True


def publish(
    protocol: SessionProtocol,
    *,
    user: SessionUser | None = None,
    force: bool = False,
    text: str | None = None,
) -> SessionFile | None:
    """
    Öffentliche Fassung erzeugen oder aktualisieren (idempotent).

    Nicht veröffentlichte Niederschriften und nichtöffentliche Sitzungen haben keine öffentliche
    Fassung – eine vorhandene wird zurückgenommen. Ist der Text unverändert, bleibt die Datei.

    Returns:
        Die aktuelle öffentliche Datei oder None.
    """
    if not is_publishable(protocol):
        withdraw(protocol)
        return None
    text = public_text(protocol) if text is None else text
    current = _current_file(protocol)
    if (
        not force
        and current is not None
        and current.is_public
        and current.meeting_id == protocol.meeting_id
        and current.text_content == text
    ):
        return current

    pdf = protocol_service.build_protocol_pdf(protocol, internal=False)
    with transaction.atomic():
        new = SessionFile(
            tenant_id=protocol.meeting.tenant_id,
            meeting_id=protocol.meeting_id,
            name=PUBLIC_FILE_NAME,
            mime_type=PUBLIC_MIME_TYPE,
            size=len(pdf),
            text_content=text,
            is_public=True,
            created_by=user,
        )
        new.file.save(_file_name(protocol), ContentFile(pdf), save=False)
        new.save()
        SessionProtocol.objects.filter(pk=protocol.pk).update(public_file=new)
        protocol.public_file = new
        if current is not None:
            current.delete()
        _touch_meeting(protocol.meeting_id)
        _log_event(
            "publish",
            protocol,
            user=user,
            changes={
                "oeffentliche_fassung": "neu erzeugt" if current is not None else "erzeugt",
                "datei": str(new.pk),
            },
        )
    return new


def refresh_meeting(meeting_id: Any) -> None:
    """
    Öffentliche Fassung einer Sitzung auf den aktuellen Stand bringen (nach dem Commit).

    Weicht der öffentliche Inhalt von der veröffentlichten Fassung ab, wird die alte Fassung zuerst
    zurückgenommen und erst dann die neue erzeugt: Die alte könnte gerade nichtöffentlich
    gewordene Inhalte enthalten. Scheitert die neue Fassung, bleibt die Sitzung ohne öffentliche
    Niederschrift statt mit einer veralteten.
    """
    protocol = (
        SessionProtocol.objects.select_related("meeting__tenant", "meeting__organization")
        .filter(meeting_id=meeting_id)
        .first()
    )
    if protocol is None or (protocol.public_file_id is None and not is_publishable(protocol)):
        return
    text = public_text(protocol) if is_publishable(protocol) else None
    current = _current_file(protocol)
    unchanged = current is not None and text is not None and current.is_public and current.text_content == text
    if unchanged:
        return
    if current is not None:
        withdraw(protocol)
    if text is None:
        return
    try:
        publish(protocol, text=text)
    except Exception:
        logger.exception("Öffentliche Fassung der Niederschrift %s konnte nicht erneuert werden", protocol.pk)


def schedule_refresh(meeting_id: Any) -> None:
    """Nach dem Commit prüfen, ob die öffentliche Fassung neu entstehen muss."""
    transaction.on_commit(lambda: refresh_meeting(meeting_id), robust=True)


def has_public_protocol(meeting_id: Any) -> bool:
    """Gibt es für die Sitzung eine veröffentlichte Niederschrift oder eine öffentliche Fassung?"""
    return (
        SessionProtocol.objects.filter(meeting_id=meeting_id)
        .filter(Q(status="published") | Q(public_file__isnull=False))
        .exists()
    )
