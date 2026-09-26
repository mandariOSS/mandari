# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Niederschrift-Workflow für das Session RIS (Issue #31).

Zentrale Logik für:
- Anlegen des Protokolls je Sitzung (TOP-Struktur kommt aus der Tagesordnung,
  Teilnehmerverzeichnis aus der Anwesenheitserfassung)
- Statusübergänge Entwurf -> Prüfung -> genehmigt -> veröffentlicht
  (Genehmigungsvermerk in der Folgesitzung, Verknüpfung mit deren TOP „Genehmigung der
  Niederschrift“) bzw. – je Mandant einstellbar – Prüfung -> veröffentlicht ohne
  Genehmigungsschritt (Issue #318)
- Veröffentlichen und Zurücknehmen der öffentlichen Fassung (OParl, Bürgerportal, Issue #318)
- Niederschrift-PDF in Ö-Fassung und interner NÖ-Fassung (gemeinsame
  PDF-Bausteine aus apps/common, amtlicher Briefkopf), mit Berichtigungen
"""

import logging
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone

from apps.common.pdf import html_to_pdf
from apps.session import audit
from apps.session.models import (
    SessionAgendaItem,
    SessionMeeting,
    SessionProtocol,
    SessionProtocolCorrection,
)
from apps.session.services import agenda_service, attendance_service, four_eyes_service

logger = logging.getLogger(__name__)

# Erlaubte Statusübergänge (Workflow-Guard): Aktion -> (mögliche Ausgangsstatus, Zielstatus)
TRANSITIONS = {
    "submit": (("draft",), "review"),
    "reject": (("review",), "draft"),
    "approve": (("review",), "approved"),
    "publish": (("approved",), "published"),
    # Rücknahme der Veröffentlichung (Issue #318): die Niederschrift bleibt genehmigt und gesperrt
    "unpublish": (("published",), "approved"),
}

#: Folgesitzungen, deren TOPs zur Genehmigung angeboten werden
APPROVAL_MEETINGS_LIMIT = 10
#: Stichworte eines TOP „Genehmigung der Niederschrift“
APPROVAL_ITEM_KEYWORDS = ("niederschrift", "protokoll")


class WorkflowError(Exception):
    """Workflow-Schritt nicht möglich; die Meldung ist für die Oberfläche formuliert."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        #: fester, für Nutzer formulierter Text – nur diesen in Antworten ausgeben
        self.user_message = user_message


def get_or_create_protocol(meeting: SessionMeeting, created_by=None) -> tuple[SessionProtocol, bool]:
    """Protokoll zu einer Sitzung anlegen (falls noch nicht vorhanden)."""
    protocol = getattr(meeting, "protocol", None)
    if protocol is not None:
        return protocol, False
    protocol = SessionProtocol.objects.create(
        meeting=meeting,
        created_by=created_by,
        content=_initial_content(meeting),
    )
    return protocol, True


def _initial_content(meeting: SessionMeeting) -> str:
    """Vorbefüllter Rahmen für den allgemeinen Teil der Niederschrift."""
    start_local = timezone.localtime(meeting.start)
    lines = [
        f"Niederschrift über die Sitzung „{meeting.name}“",
        f"des Gremiums {meeting.organization.name}",
        f"am {start_local.strftime('%d.%m.%Y')}, Beginn {start_local.strftime('%H:%M')} Uhr.",
        "",
        "Die Beschlussfähigkeit wurde festgestellt.",
    ]
    return "\n".join(lines)


def transition_for(protocol: SessionProtocol, action: str) -> str | None:
    """
    Zielstatus einer Aktion oder None, wenn sie aus dem aktuellen Status unzulässig ist.

    Mandanten ohne Genehmigungsschritt (Issue #318) veröffentlichen direkt aus der Prüfung;
    eine Genehmigung gibt es dort nicht.
    """
    direct = protocol.meeting.tenant.protocol_direct_publication
    if action == "approve" and direct:
        return None
    transition = TRANSITIONS.get(action)
    if transition is None:
        return None
    sources: tuple[str, ...] = transition[0]
    if action == "publish" and direct:
        sources = (*sources, "review")
    return transition[1] if protocol.status in sources else None


def apply_transition(protocol: SessionProtocol, action: str) -> bool:
    """
    Statusübergang prüfen und Status setzen (ohne save()).

    Returns:
        True, wenn der Übergang zulässig war.
    """
    target = transition_for(protocol, action)
    if target is None:
        return False
    protocol.status = target
    return True


def approval_candidates(meeting: SessionMeeting, *, include_non_public: bool):
    """
    TOPs „Genehmigung der Niederschrift“ der Folgesitzungen desselben Gremiums (Issue #318).

    Angeboten werden TOPs, deren Betreff „Niederschrift“ oder „Protokoll“ enthält, aus den nächsten
    Sitzungen des Gremiums; nichtöffentliche nur mit NÖ-Recht (der NÖ-Teil einer Niederschrift
    wird im NÖ-Teil der Folgesitzung genehmigt).
    """
    meeting_ids = list(
        SessionMeeting.objects.filter(
            tenant_id=meeting.tenant_id, organization_id=meeting.organization_id, start__gt=meeting.start
        )
        .order_by("start")
        .values_list("pk", flat=True)[:APPROVAL_MEETINGS_LIMIT]
    )
    keywords = Q()
    for word in APPROVAL_ITEM_KEYWORDS:
        keywords |= Q(name__icontains=word)
    qs = (
        SessionAgendaItem.objects.filter(meeting_id__in=meeting_ids, is_withdrawn=False)
        .filter(keywords)
        .select_related("meeting")
        .order_by("meeting__start", "order", "number")
    )
    if not include_non_public:
        qs = qs.filter(is_public=True, meeting__is_public=True)
    return qs


def _select_approval_item(meeting: SessionMeeting, raw: str, *, include_non_public: bool) -> SessionAgendaItem | None:
    if not raw:
        return None
    try:
        return approval_candidates(meeting, include_non_public=include_non_public).filter(pk=raw).first()
    except (ValueError, ValidationError):
        return None


def perform_action(
    protocol: SessionProtocol,
    action: str,
    *,
    user: Any,
    data: Any,
    request: Any = None,
    include_non_public: bool = False,
) -> str:
    """
    Workflow-Schritt ausführen: submit, reject, approve, publish, unpublish.

    Prüft Statusübergang, Recht, Vier-Augen-Prinzip und Vertretung (Issue #222). Veröffentlichen
    erzeugt die öffentliche Fassung (OParl, Bürgerportal); scheitert das, bleibt der Status
    unverändert. Zurücknehmen löscht sie sofort (Tombstone, Rücknahme aus dem Bürgerportal).

    Returns:
        Erfolgsmeldung für die Oberfläche.

    Raises:
        WorkflowError: Schritt nicht zulässig.
    """
    from apps.session.services import protocol_publication

    meeting = protocol.meeting
    old_status = protocol.status
    target = transition_for(protocol, action)
    if target is None:
        raise WorkflowError(f"Aktion nicht möglich: Statusübergang aus „{protocol.get_status_display()}“ unzulässig.")

    # Vier-Augen-Prinzip und Vertretung (Issue #222): Genehmigen bzw. direktes Veröffentlichen
    # ist die Freigabe; Zurückweisen, späteres Veröffentlichen und Zurücknehmen nicht
    vertreten = None
    if action != "submit":
        release = action == "approve" or (action == "publish" and old_status == "review")
        try:
            vertreten = four_eyes_service.authorize(
                four_eyes_service.PROCESS_PROTOCOL, protocol, user, four_eyes=release
            )
        except four_eyes_service.ApprovalError as exc:
            raise WorkflowError(exc.user_message) from exc
    vermerk = f" (in Vertretung für {vertreten.user.email})" if vertreten else ""
    protocol.status = target
    now = timezone.now()

    if action == "submit":
        protocol.review_requested_by = user
        protocol.review_requested_at = now
        protocol.save()
        return "Protokoll wurde zur Prüfung gegeben."

    if action == "reject":
        comment = str(data.get("comment", "")).strip()
        with audit.in_vertretung(vertreten):
            protocol.save()
        # Audit: Zurückweisung mit Kommentar nachvollziehbar machen
        audit.log_event(
            "update",
            protocol,
            user=user,
            request=request,
            on_behalf_of=vertreten,
            changes={
                "status": {"alt": old_status, "neu": protocol.status},
                "zurueckweisungs_kommentar": comment[:300],
            },
        )
        return f"Protokoll wurde mit Anmerkungen zurück in den Entwurf gegeben{vermerk}."

    if action == "approve":
        protocol.approved_by = user
        protocol.approved_on_behalf_of = vertreten
        protocol.approved_at = now
        item = _select_approval_item(meeting, str(data.get("approval_item", "")), include_non_public=include_non_public)
        if item is not None:
            protocol.approval_agenda_item = item
            protocol.approval_meeting = item.meeting
        else:
            approval_meeting_id = str(data.get("approval_meeting", ""))
            if approval_meeting_id:
                candidates = SessionMeeting.objects.filter(
                    pk=approval_meeting_id, tenant_id=meeting.tenant_id, organization_id=meeting.organization_id
                )
                # Nichtöffentliche Folgesitzungen nur mit NÖ-Sichtrecht (wie die Auswahl der TOPs)
                if not include_non_public:
                    candidates = candidates.filter(is_public=True)
                try:
                    protocol.approval_meeting = candidates.first()
                except (ValueError, ValidationError):
                    protocol.approval_meeting = None
        note = str(data.get("approval_note", "")).strip()[:500]
        if note:
            protocol.approval_note = note
        elif protocol.approval_meeting:
            start_local = timezone.localtime(protocol.approval_meeting.start)
            # Der Vermerk steht in der öffentlichen Fassung: TOP-Nummer nur eines öffentlichen TOP
            public_item = item is not None and item.is_public and item.meeting.is_public
            top = f" unter TOP {item.number}" if item is not None and public_item else ""
            protocol.approval_note = f"Genehmigt in der Sitzung am {start_local.strftime('%d.%m.%Y')}{top}."
        with audit.in_vertretung(vertreten):
            protocol.save()  # Audit: approve-Aktion über Signal
        return f"Niederschrift wurde genehmigt{vermerk}. Ergebnis, Stimmen und Texte sind jetzt schreibgeschützt."

    if action == "publish":
        if old_status == "review":
            # Ohne Genehmigungsschritt (Issue #318): Wer veröffentlicht, gibt frei
            protocol.approved_by = user
            protocol.approved_on_behalf_of = vertreten
            protocol.approved_at = now
        protocol.published_at = now
        try:
            with transaction.atomic():
                with audit.in_vertretung(vertreten):
                    protocol.save()  # Audit: publish-Aktion über Signal
                public_file = protocol_publication.publish(protocol, user=user)
        except Exception as exc:
            logger.exception("Öffentliche Fassung der Niederschrift %s konnte nicht erzeugt werden", protocol.pk)
            protocol.status = old_status
            raise WorkflowError(
                "Die öffentliche Fassung konnte nicht erzeugt werden; die Niederschrift ist nicht veröffentlicht. "
                "Bitte versuchen Sie es erneut."
            ) from exc
        if public_file is None:
            return (
                f"Niederschrift wurde veröffentlicht{vermerk}. Die Sitzung ist nichtöffentlich – "
                "es entsteht keine öffentliche Fassung."
            )
        return f"Öffentliche Fassung der Niederschrift wurde veröffentlicht{vermerk} (OParl und Bürgerportal)."

    # unpublish: erst die Datei zurücknehmen (Tombstone, solange noch veröffentlicht), dann der Status
    with transaction.atomic():
        protocol_publication.withdraw(protocol)
        with audit.in_vertretung(vertreten):
            protocol.save()  # Audit: unpublish-Aktion über Signal
    return f"Veröffentlichung wurde zurückgenommen{vermerk}. Die öffentliche Fassung ist aus OParl und Bürgerportal entfernt."


def participant_directory(meeting: SessionMeeting) -> dict:
    """Teilnehmerverzeichnis aus der Anwesenheitserfassung gruppieren."""
    attendances = list(meeting.attendances.select_related("person").order_by("person__family_name"))
    present = [a for a in attendances if a.status in attendance_service.PRESENT_STATUSES]
    excused = [a for a in attendances if a.status in ("excused", "declined")]
    absent = [a for a in attendances if a.status == "absent"]
    other = [a for a in attendances if a not in present and a not in excused and a not in absent]
    return {"present": present, "excused": excused, "absent": absent, "other": other, "all": attendances}


def public_item_ids(agenda: dict) -> set:
    """IDs aller TOPs (inkl. Unterpunkte) einer Ö-gruppierten Tagesordnung."""
    ids = set()
    for item in agenda["public"]:
        ids.add(item.pk)
        ids.update(sub.pk for sub in item.children_list)
    return ids


def change_lines(correction: SessionProtocolCorrection) -> list[str]:
    """Anzeigezeilen der unverschlüsselten Änderungen einer Berichtigung."""
    lines = []
    for entry in correction.changes or []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("feld", ""))
        if entry.get("geaendert"):
            lines.append(f"{label} geändert")
        else:
            lines.append(f"{label}: {entry.get('alt', '–')} → {entry.get('neu', '–')}")
    return lines


def correction_notes(protocol: SessionProtocol, *, internal: bool, visible_item_ids: set | None = None) -> list[dict]:
    """
    Wirksame Berichtigungen für die Niederschrift: „Berichtigung vom … (Grund)“ (Issue #318).

    Öffentliche Fassung (``internal=False``): nur Berichtigungen des öffentlichen Teils, deren TOP
    noch öffentlich ist; ausschließlich unverschlüsselte Felder, nichts wird entschlüsselt. Interne
    Fassung: alle, der Grund nichtöffentlicher Berichtigungen entschlüsselt.
    """
    notes = []
    corrections = protocol.corrections.filter(status=SessionProtocolCorrection.STATUS_APPLIED).select_related(
        "agenda_item"
    )
    for correction in corrections.order_by("applied_at", "requested_at"):
        if not internal:
            if not correction.is_public:
                continue
            if correction.target == SessionProtocolCorrection.TARGET_ITEM and (
                correction.agenda_item_id is None
                or visible_item_ids is None
                or correction.agenda_item_id not in visible_item_ids
            ):
                continue
            reason = correction.reason
        else:
            reason = correction.reason if correction.is_public else correction.get_reason_decrypted()
        notes.append(
            {
                "item_id": correction.agenda_item_id,
                "date": correction.applied_at or correction.requested_at,
                "subject": correction.subject_label,
                "reason": reason,
                "changes": change_lines(correction),
            }
        )
    return notes


def build_protocol_pdf(protocol: SessionProtocol, *, internal: bool) -> bytes:
    """
    Niederschrift-PDF erzeugen.

    Args:
        protocol: das Protokoll
        internal: True = interne NÖ-Fassung (inkl. nichtöffentlicher Teile),
                  False = Ö-Fassung (NÖ-Inhalte erscheinen niemals; nichts wird entschlüsselt)

    Returns:
        bytes: PDF-Inhalt
    """
    from apps.session.oparl_publication import UNVEROEFFENTLICHT

    meeting = protocol.meeting
    tenant = meeting.tenant
    agenda = agenda_service.grouped_agenda(meeting, include_non_public=internal)

    # NÖ-Texte nur für die interne Fassung entschlüsseln
    def add_votes(item):
        # Namentliche Abstimmung + Befangenheit (Issue #41)
        votes = list(item.votes.select_related("person"))
        item.roll_call_votes = (
            [v for v in votes if v.vote in ("yes", "no", "abstain")] if item.voting_method == "roll_call" else []
        )
        item.excluded_persons = [v.person for v in votes if v.vote == "excluded"]
        # Vorlagennummer in der Ö-Fassung nur, wenn die Vorlage selbst veröffentlicht ist (Issue #318)
        paper = item.paper
        item.paper_visible = paper is not None and (
            internal or (paper.is_public and paper.status not in UNVEROEFFENTLICHT)
        )

    def decorate(items):
        for item in items:
            item.protocol_note_np = item.get_protocol_note_decrypted() if internal else ""
            item.resolution_np = item.get_resolution_text_decrypted() if internal else ""
            add_votes(item)
            for sub in item.children_list:
                sub.protocol_note_np = sub.get_protocol_note_decrypted() if internal else ""
                sub.resolution_np = sub.get_resolution_text_decrypted() if internal else ""
                add_votes(sub)
        return items

    context = {
        "tenant": tenant,
        "meeting": meeting,
        "protocol": protocol,
        "internal": internal,
        "variant_label": "Nichtöffentliche Fassung (intern)" if internal else "Öffentliche Fassung",
        "agenda_public": decorate(agenda["public"]),
        "agenda_non_public": decorate(agenda["non_public"]) if internal else [],
        "participants": participant_directory(meeting),
        "content_np": (protocol.get_content_decrypted() or "") if internal else "",
        "corrections": correction_notes(protocol, internal=internal, visible_item_ids=public_item_ids(agenda)),
        "generated_at": timezone.localtime(),
        "address_lines": [line for line in (tenant.address or "").splitlines() if line.strip()],
    }
    html = render_to_string("session/pdf/protocol.html", context)
    return html_to_pdf(html)
