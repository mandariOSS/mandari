# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Antrag digital bei der Verwaltung einreichen (Work → Session, Issue #40).

Ablauf:
1. Die Verwaltung stellt im Session-RIS einen Einreichungs-Token aus
   (``SessionAPIToken`` mit ``can_submit_applications``).
2. Die Fraktion hinterlegt den Token in den Organisationseinstellungen
   (``AdministrationConnection``); dabei wird er geprüft und nur als Hash gespeichert.
3. Aus dem Dokument-Editor heraus wird der Antrag mit Vorschau eingereicht —
   Beschlussvorschlag und Begründung werden aus dem Dokument vorbelegt.
4. Statuswechsel der Verwaltung (eingegangen, in Vorlage umgewandelt, Beratung
   terminiert, abgelehnt) laufen per Signal zurück in den Work-Status und
   benachrichtigen Autor:in und Federführung.
"""

from __future__ import annotations

import html
import re

from django.db import transaction
from django.utils import timezone

# Reihenfolge = Priorität beim Erkennen der Abschnitte im Dokument
SECTION_PATTERNS = [
    ("resolution_proposal", re.compile(r"^(beschluss(vorschlag|text|entwurf|empfehlung)?|antragstext|antrag)\b", re.I)),
    ("resolution_proposal", re.compile(r"(möge|moege|wird gebeten|beschließ|beschliess)", re.I)),
    ("justification", re.compile(r"^(begründung|begruendung|sachverhalt|erläuterung|erlaeuterung)\b", re.I)),
    ("financial_impact", re.compile(r"^(finanzielle auswirkungen|finanzierung|kosten|haushalt)", re.I)),
]
_BLOCK_RE = re.compile(r"<(h[1-6]|p|li|blockquote|td|th|div)\b[^>]*>(.*?)</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")

APPLICATION_TYPE_HINTS = [
    ("inquiry", ("anfrage", "frage")),
    ("urgent", ("dringlich",)),
    ("amendment", ("änderung", "aenderung")),
    ("resolution", ("resolution",)),
    ("motion", ("antrag",)),
]

# Session-Antragsstatus → Work-Dokumentstatus
STATUS_MAP = {
    "submitted": "submitted",
    "received": "at_admin",
    "in_review": "at_admin",
    "accepted": "at_admin",
    "converted": "at_admin",
    "rejected": "rejected",
    "withdrawn": "approved",
}
# Work-Status, die von der Verwaltung nicht mehr überschrieben werden
FROZEN_WORK_STATUSES = {"completed", "archived", "deleted"}


class SubmissionError(ValueError):
    """Fachlicher Fehler beim Einreichen (Text ist für Nutzer:innen gedacht)."""


# =============================================================================
# Inhalt aufbereiten
# =============================================================================


def _text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = _TAG_RE.sub("", fragment)
    fragment = html.unescape(fragment)
    return re.sub(r"[ \t\xa0]+", " ", fragment).strip()


def extract_sections(content_html: str) -> dict:
    """
    Beschlussvorschlag, Begründung und finanzielle Auswirkungen aus dem
    Dokument-HTML herauslösen. Überschriften (h1–h6) oder kurze, fett gesetzte
    Absätze wie „Beschlussvorschlag:" oder „Begründung" trennen die Abschnitte.
    Ohne erkennbare Struktur landet der gesamte Text im Beschlussvorschlag.
    """
    sections = {"resolution_proposal": [], "justification": [], "financial_impact": [], "intro": []}
    current = "intro"
    found_heading = False
    blocks = list(_BLOCK_RE.finditer(content_html or ""))
    if not blocks:
        plain = _text(content_html or "")
        return {
            "resolution_proposal": plain,
            "justification": "",
            "financial_impact": "",
            "structured": False,
        }

    for match in blocks:
        tag = match.group(1).lower()
        inner = match.group(2)
        text = _text(inner)
        if not text:
            continue
        if tag == "div" and ("<p" in inner.lower() or "<h" in inner.lower()):
            continue  # Container, Inhalt kommt über die Kind-Elemente
        is_heading = tag.startswith("h") or (
            len(text) <= 60
            and re.fullmatch(r"\s*<(strong|b)>.*</(strong|b)>\s*:?\s*", inner.strip(), re.S | re.I) is not None
        )
        if is_heading or (len(text) <= 60 and text.endswith(":")):
            label = text.rstrip(":").strip()
            target = None
            for key, pattern in SECTION_PATTERNS:
                if pattern.search(label):
                    target = key
                    break
            if target:
                current = target
                found_heading = True
                continue
        sections[current].append(text)

    if not found_heading:
        return {
            "resolution_proposal": "\n\n".join(sections["intro"]),
            "justification": "",
            "financial_impact": "",
            "structured": False,
        }
    resolution = sections["resolution_proposal"]
    if not resolution:
        resolution = sections["intro"]
    return {
        "resolution_proposal": "\n\n".join(resolution),
        "justification": "\n\n".join(sections["justification"]),
        "financial_impact": "\n\n".join(sections["financial_impact"]),
        "structured": True,
    }


def guess_application_type(motion) -> str:
    """Antragsart aus dem Dokumenttyp ableiten (Standard: Antrag)."""
    doc_type = getattr(motion, "document_type", None)
    label = f"{getattr(doc_type, 'name', '')} {getattr(doc_type, 'slug', '')} {motion.title}".lower()
    for value, hints in APPLICATION_TYPE_HINTS:
        if any(hint in label for hint in hints):
            return value
    return "motion"


# =============================================================================
# Verbindung
# =============================================================================


def get_connection(organization):
    from .models import AdministrationConnection

    return (
        AdministrationConnection.objects.filter(organization=organization, is_active=True)
        .select_related("tenant")
        .first()
    )


def connection_state(connection) -> tuple[bool, str]:
    """(nutzbar?, Grund) — prüft Verbindung, Token und Verwaltung."""
    if connection is None:
        return False, "Es ist noch keine Verwaltung verbunden."
    if not connection.tenant.is_active:
        return False, f"Die Verwaltung „{connection.tenant.name}“ ist derzeit deaktiviert."
    token = connection.get_token()
    if token is None:
        return False, "Der Einreichungs-Token wurde von der Verwaltung gelöscht."
    if not token.is_valid():
        return False, "Der Einreichungs-Token ist abgelaufen oder wurde zurückgezogen."
    if not token.can_submit_applications:
        return False, "Der Einreichungs-Token erlaubt keine Antragseinreichung."
    return True, ""


def connect_with_token(organization, raw_token: str, membership):
    """Token prüfen und Verbindung anlegen bzw. auf die neue Verwaltung umstellen."""
    from apps.session.models import SessionAPIToken

    from .models import AdministrationConnection

    raw_token = (raw_token or "").strip()
    if len(raw_token) != 64:
        raise SubmissionError("Der Token muss aus 64 Zeichen bestehen. Bitte den kompletten Token einfügen.")
    hashed = SessionAPIToken.hash_token(raw_token)
    token = SessionAPIToken.objects.select_related("tenant").filter(token=hashed).first()
    if token is None:
        raise SubmissionError("Dieser Token ist nicht bekannt. Bitte bei der Verwaltung einen neuen Token anfordern.")
    if not token.is_valid():
        raise SubmissionError("Dieser Token ist abgelaufen oder wurde zurückgezogen.")
    if not token.can_submit_applications:
        raise SubmissionError("Dieser Token erlaubt keine Antragseinreichung.")
    if not token.tenant.is_active:
        raise SubmissionError("Die zugehörige Verwaltung ist deaktiviert.")

    connection, _ = AdministrationConnection.objects.update_or_create(
        organization=organization,
        defaults={
            "tenant": token.tenant,
            "token_hash": hashed,
            "token_prefix": token.token_prefix,
            "connected_by": membership,
            "connected_at": timezone.now(),
            "is_active": True,
        },
    )
    return connection


# =============================================================================
# Einreichen
# =============================================================================


def can_submit(motion, membership) -> tuple[bool, str]:
    """Darf dieses Dokument jetzt eingereicht werden?"""
    if motion.session_application_id:
        return False, "Dieses Dokument wurde bereits eingereicht."
    if not motion.is_submittable:
        return False, "Dieser Dokumenttyp ist nicht zum Einreichen vorgesehen."
    if motion.status in FROZEN_WORK_STATUSES or motion.status == "rejected":
        return False, f"Im Status „{motion.get_status_display()}“ kann nicht eingereicht werden."
    if motion.status not in ("approved", "submitted") and not membership.has_permission("motions.approve"):
        return False, "Das Dokument muss zuerst freigegeben werden (Status „Freigegeben“)."
    return True, ""


def build_prefill(motion) -> dict:
    sections = extract_sections(motion.get_content_decrypted() or "")
    return {
        "title": motion.title,
        "application_type": guess_application_type(motion),
        "resolution_proposal": sections["resolution_proposal"],
        "justification": sections["justification"],
        "financial_impact": sections["financial_impact"],
        "structured": sections["structured"],
    }


@transaction.atomic
def submit_motion(motion, membership, data: dict):
    """
    Antrag an die verbundene Verwaltung übergeben und das Dokument auf
    „Eingereicht“ setzen. ``data`` enthält die geprüften Formularwerte.
    """
    from apps.session.services.application_service import ApplicationService

    connection = get_connection(motion.organization)
    usable, reason = connection_state(connection)
    if not usable:
        raise SubmissionError(reason)
    allowed, reason = can_submit(motion, membership)
    if not allowed:
        raise SubmissionError(reason)

    user = membership.user
    submitter_name = user.get_full_name() or user.email
    try:
        application = ApplicationService.submit_application(
            tenant=connection.tenant,
            title=data["title"],
            justification=data["justification"],
            resolution_proposal=data["resolution_proposal"],
            submitter_name=submitter_name,
            submitter_email=user.email,
            application_type=data.get("application_type") or "motion",
            submitting_organization=motion.organization,
            target_organization_id=data.get("target_organization_id") or None,
            co_signers=data.get("co_signers", ""),
            financial_impact=data.get("financial_impact", ""),
            is_urgent=bool(data.get("is_urgent")),
            urgency_reason=data.get("urgency_reason", ""),
            deadline=data.get("deadline"),
        )
    except ValueError as exc:
        raise SubmissionError(str(exc)) from exc

    motion.session_application = application
    motion.status = "submitted"
    motion.submitted_at = timezone.now()
    motion.save(update_fields=["session_application", "status", "submitted_at", "updated_at"])

    token = connection.get_token()
    if token is not None:
        token.record_usage()
    connection.last_used_at = timezone.now()
    connection.save(update_fields=["last_used_at"])
    return application


# =============================================================================
# Rückmeldung der Verwaltung
# =============================================================================


def consultation_timeline(application) -> list[dict]:
    """Beratungsfolge der aus dem Antrag entstandenen Vorlage(n)."""
    if application is None:
        return []
    entries = []
    papers = application.created_papers.all().prefetch_related("consultations__organization", "consultations__meeting")
    for paper in papers:
        for consultation in paper.consultations.all():
            entries.append(
                {
                    "paper": paper,
                    "organization": consultation.organization.name if consultation.organization_id else "",
                    "meeting": consultation.meeting,
                    "start": consultation.meeting.start if consultation.meeting_id else None,
                    "role": consultation.get_role_display() if hasattr(consultation, "get_role_display") else "",
                    "authoritative": consultation.authoritative,
                    "result": consultation.get_result_display() if getattr(consultation, "result", "") else "",
                    "order": consultation.order,
                }
            )
    entries.sort(key=lambda e: (e["start"] is None, e["start"] or timezone.now(), e["order"]))
    return entries


def _notify(motion, title: str, message: str):
    from apps.work.notifications.services import NotificationHub

    recipients = []
    for member in (motion.author, getattr(motion, "responsible", None)):
        if member is not None and member not in recipients:
            recipients.append(member)
    for recipient in recipients:
        NotificationHub.notify_motion_ris_status(motion, recipient, title, message)


def sync_motion_from_application(application, old_status: str | None) -> None:
    """Statuswechsel der Verwaltung ins Work-Dokument übernehmen."""
    motion = getattr(application, "work_motion", None)
    if motion is None or motion.status in FROZEN_WORK_STATUSES:
        return
    new_status = STATUS_MAP.get(application.status)
    if new_status and new_status != motion.status and motion.status != "on_agenda":
        motion.status = new_status
        motion.save(update_fields=["status", "updated_at"])
    elif new_status == "rejected" and motion.status != "rejected":
        motion.status = "rejected"
        motion.save(update_fields=["status", "updated_at"])

    label = application.get_status_display()
    reference = application.reference or "ohne Eingangsnummer"
    note = f" Hinweis der Verwaltung: {application.processing_notes.strip()}" if application.processing_notes else ""
    _notify(
        motion,
        f"Verwaltung: {label}",
        f'Ihr Antrag "{motion.title}" ({reference}) hat bei {application.tenant.name} jetzt den Status „{label}“.{note}',
    )


def sync_motion_from_consultation(consultation) -> None:
    """Beratung terminiert → Dokument „Auf Tagesordnung“ + Benachrichtigung."""
    paper = consultation.paper
    application = getattr(paper, "source_application", None)
    if application is None or not consultation.meeting_id:
        return
    motion = getattr(application, "work_motion", None)
    if motion is None or motion.status in FROZEN_WORK_STATUSES:
        return
    meeting = consultation.meeting
    if motion.status != "on_agenda":
        motion.status = "on_agenda"
        motion.save(update_fields=["status", "updated_at"])
    when = timezone.localtime(meeting.start).strftime("%d.%m.%Y %H:%M") if meeting.start else "Termin offen"
    org_name = consultation.organization.name if consultation.organization_id else "Gremium"
    _notify(
        motion,
        "Beratung terminiert",
        f'Ihr Antrag "{motion.title}" ({application.reference}) wird am {when} im Gremium „{org_name}“ beraten.',
    )
