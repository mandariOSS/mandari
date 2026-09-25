# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Antrag digital bei der Verwaltung einreichen (Work → Session, Issue #40).

Ablauf:
1. Die Verwaltung stellt im Session-RIS einen Einreichungs-Token aus
   (``SessionAPIToken`` mit ``can_submit_applications``).
2. Die Fraktion hinterlegt den Token in den Organisationseinstellungen
   (``AdministrationConnection``); dabei wird er geprüft und nur als Hash gespeichert.
3. Aus dem Dokument-Editor heraus wird der Antrag mit Vorschau eingereicht —
   Beschlussvorschlag und Begründung werden aus dem Dokument vorbelegt. Der Status
   wechselt über die definierten Übergänge auf „Eingereicht“, die einreichende Person
   erhält eine Eingangsbestätigung per E-Mail (Issue #316).
4. Was danach in Session passiert (Eingang, Vorlagennummer, Beratungsfolge, Beschluss),
   meldet ``administration_feedback`` zurück (Issue #316).
"""

from __future__ import annotations

import html
import re

from django.db import transaction
from django.utils import timezone

from .models import StatusTransitionError

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

# Status, aus denen nicht (mehr) eingereicht wird
NOT_SUBMITTABLE_STATUSES = {"completed", "adopted", "rejected", "withdrawn", "archived", "deleted"}


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
    # Ein Token gehört genau einer Organisation: Die Verwaltung stellt je Fraktion einen eigenen aus
    if (
        AdministrationConnection.objects.filter(token_hash=hashed, is_active=True)
        .exclude(organization=organization)
        .exists()
    ):
        raise SubmissionError(
            "Dieser Token ist bereits mit einer anderen Organisation verbunden. Bitte bei der Verwaltung "
            "einen eigenen Token anfordern."
        )

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
    if motion.status in NOT_SUBMITTABLE_STATUSES:
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
    token = connection.get_token()
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
            submitted_via_token=token,
        )
    except ValueError as exc:
        raise SubmissionError(str(exc)) from exc

    from .administration_feedback import SUBMISSION_VIA, record_submission, send_receipt

    motion.session_application = application
    motion.administration_status = "submitted"
    motion.save(update_fields=["session_application", "administration_status", "updated_at"])
    # Status über die definierten Übergänge (Issue #316): Wer mit Freigaberecht einreicht, gibt damit frei
    if motion.status == "submitted":
        motion.submitted_at = timezone.now()
        motion.save(update_fields=["submitted_at", "updated_at"])
    else:
        try:
            motion.advance_to("submitted", via=SUBMISSION_VIA)
        except StatusTransitionError as exc:
            raise SubmissionError(f"Im Status „{motion.get_status_display()}“ kann nicht eingereicht werden.") from exc
    record_submission(motion, application)

    if token is not None:
        token.record_usage()
    connection.last_used_at = timezone.now()
    connection.save(update_fields=["last_used_at"])

    motion_id, membership_id = motion.pk, membership.pk
    transaction.on_commit(lambda: send_receipt(motion_id, membership_id), robust=True)
    return application
