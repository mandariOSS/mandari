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

import hashlib
import html
import logging
import re
from contextlib import ExitStack

from django.db import transaction
from django.utils import timezone

from .models import StatusTransitionError

logger = logging.getLogger(__name__)

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
    """Darf dieses Dokument jetzt eingereicht werden? (Session-Weg und E-Mail-Weg, Issue #580)"""
    # Spät importiert: email_submission importiert dieses Modul
    from .email_submission import submission_block_reason

    if motion.session_application_id:
        return False, "Dieses Dokument wurde bereits eingereicht."
    # Nichtöffentliche Unterlagen verlassen Work nicht (Issue #873)
    if motion.is_sworn_in_only():
        return False, "Dokumente aus „Nichtöffentliche Vorgänge“ werden nicht eingereicht."
    email_reason = submission_block_reason(motion)
    if email_reason:
        return False, email_reason
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


def attachment_preview(motion) -> tuple[list, list[tuple]]:
    """Anhänge für die Einreichung: (werden übermittelt, [(Anhang, Grund)] nicht angenommen) (#584)."""
    from apps.session.services.application_service import attachment_accepted

    accepted, rejected = [], []
    for document in motion.documents.all():
        reason = attachment_accepted(document.filename, document.file_size)
        if reason is None:
            accepted.append(document)
        else:
            rejected.append((document, reason))
    return accepted, rejected


# =============================================================================
# Einreichen über den Befehl submission.submit (Issue #540)
# =============================================================================

#: Meldung, wenn die Ablage der Anhänge in Session scheitert (fester Text, keine Ausnahme-Details)
ATTACHMENTS_FAILED = (
    "Die Anhänge konnten nicht an die Verwaltung übergeben werden. Der Antrag wurde nicht eingereicht – "
    "bitte später erneut versuchen."
)
#: Meldungen zu Problemen des Befehls, die keinen Text der Verwaltung tragen
SUBMIT_FAILED = "Der Antrag konnte nicht eingereicht werden. Bitte später erneut versuchen."
KEY_REUSED = (
    "Für dieses Dokument liegt schon eine Einreichung mit anderen Angaben vor. Bitte die Seite neu laden und die "
    "Angaben prüfen."
)
#: Gründe der Verwaltung für nicht angenommene Anhänge (Codes aus der Quittung)
ATTACHMENT_REASONS = {
    "dateityp": "Dateityp wird von der Verwaltung nicht angenommen",
    "groesse": "zu groß",
    "virenpruefung": "Virenprüfung nicht bestanden oder nicht verfügbar",
}
#: Felder des Befehls → Bezeichnung im Formular
FIELD_LABELS = {
    "title": "Titel",
    "resolution_proposal": "Beschlussvorschlag",
    "justification": "Begründung",
    "financial_impact": "Finanzielle Auswirkungen",
    "urgency_reason": "Begründung der Dringlichkeit",
    "co_signers": "Mitunterzeichnende",
    "co_signers_text": "Mitunterzeichnende (höchstens 5000 Zeichen)",
    "target_organization": "Zielgremium",
    "deadline": "Gewünschter Beratungstermin",
    "application_type": "Antragsart",
    "submitter": "Name und E-Mail-Adresse der einreichenden Person",
    "attachments": "Anhänge",
}
_CHUNK = 1024 * 1024


def _client():
    """Client für Befehle an die Verwaltung; mandari Session läuft in derselben Installation."""
    from hub.commands import InProcessClient

    return InProcessClient()


def idempotency_key(motion) -> str:
    """
    Idempotenzschlüssel der Einreichung, abgeleitet aus dem eigenen Stand (kein eigenes Feld nötig).

    Dokument und Zeitpunkt seiner letzten Speicherung, so wie es zu Beginn der Anfrage geladen wurde: Scheitert
    nach der Quittung das Speichern in Work, bleibt das Dokument unverändert, die Wiederholung sendet denselben
    Schlüssel und erhält dieselbe Quittung statt eines zweiten Antrags. Ebenso ein zweiter Klick, der dasselbe
    Dokument geladen hat, während der erste noch Anlagen liest. Eine festgehaltene Einreichung speichert das
    Dokument; jede spätere Einreichung (etwa nachdem die Verwaltung den Antrag gelöscht oder eine Probe
    zurückgesetzt hat) trägt deshalb einen neuen Schlüssel.
    """
    stamp = int(motion.updated_at.timestamp() * 1_000_000) if motion.updated_at else 0
    return f"work.motion.{motion.pk}.{stamp}"


def _already_linked(motion) -> bool:
    """Ist das Dokument inzwischen verknüpft (etwa durch einen zweiten Klick, der früher fertig war)?"""
    from .models import Motion

    return Motion.objects.filter(pk=motion.pk, session_application__isnull=False).exists()


def _command_body(motion, membership, data: dict, attachments: list[dict]) -> dict:
    """Inhalt des Befehls aus den geprüften Formularwerten; leere optionale Angaben bleiben weg."""
    user = membership.user
    body: dict = {
        "application_type": data.get("application_type") or "motion",
        "title": data.get("title") or "",
        "resolution_proposal": data.get("resolution_proposal") or "",
        "justification": data.get("justification") or "",
        "submitter": {"name": user.get_full_name() or user.email, "email": user.email},
        "submitting_organization": str(motion.organization_id),
        "is_urgent": bool(data.get("is_urgent")),
        # Die Verwaltung erzeugt aus den Angaben die Antragsfassung als PDF (#584)
        "application_pdf": True,
    }
    for name in ("financial_impact", "urgency_reason"):
        if data.get(name):
            body[name] = data[name]
    # Wie bisher als Text (eine Person je Zeile), unverändert gespeichert
    if (data.get("co_signers") or "").strip():
        body["co_signers_text"] = data["co_signers"]
    if data.get("deadline"):
        body["deadline"] = data["deadline"].isoformat()
    if data.get("target_organization_id"):
        body["target_organization"] = str(data["target_organization_id"])
    if attachments:
        body["attachments"] = attachments
    return body


def _sha256(handle) -> tuple[int, str]:
    """Größe und SHA-256 einer Datei; setzt sie danach an den Anfang zurück."""
    digest = hashlib.sha256()
    size = 0
    handle.seek(0)
    for chunk in iter(lambda: handle.read(_CHUNK), b""):
        digest.update(chunk)
        size += len(chunk)
    handle.seek(0)
    return size, digest.hexdigest()


def _open_attachments(motion, stack: ExitStack) -> tuple[list[dict], dict, list, list[str]]:
    """
    Anhänge für den Befehl: (Angaben im Inhalt, Dateien je SHA-256, mitgehende Dokumente, nicht Übernommenes).

    Was die Verwaltung nach Dateityp oder Größe nicht annimmt, steht vorher in der Vorschau und geht nicht mit (#584).
    """
    accepted, rejected = attachment_preview(motion)
    skipped = [f"{document.filename} ({reason})" for document, reason in rejected]
    items: list[dict] = []
    files: dict = {}
    documents: list = []
    for document in accepted:
        try:
            handle = stack.enter_context(document.file.open("rb"))
        except (FileNotFoundError, ValueError, OSError):
            skipped.append(f"{document.filename} (Datei nicht gefunden)")
            continue
        size, digest = _sha256(handle)
        items.append({"name": document.filename or "Anhang", "size": size, "sha256": digest})
        files.setdefault(digest, handle)
        documents.append(document)
    return items, files, documents, skipped


def problem_message(problem) -> str:
    """Meldung für Nutzer:innen zu einem Problem des Befehls (feste Texte, Felder mit ihrer Bezeichnung)."""
    if problem.kind == "validierung" and problem.errors:
        labels: list[str] = []
        for item in problem.errors:
            name = str(item.get("pointer", "")).strip("/").split("/")[0]
            label = FIELD_LABELS.get(name, "Angaben")
            if label not in labels:
                labels.append(label)
        return "Bitte prüfen: " + "; ".join(labels) + "."
    if problem.kind == "idempotenzschluessel-wiederverwendet":
        return KEY_REUSED
    if problem.kind == "anlagen-nicht-abgelegt":
        return ATTACHMENTS_FAILED
    if problem.kind in {"keine-berechtigung", "mandant-unbekannt"}:
        # Feste Texte des Handlers in mandari Session, ohne Inhalte
        return str(problem.detail)
    return SUBMIT_FAILED


def submit_motion(motion, membership, data: dict, *, skipped: list[str] | None = None):
    """
    Antrag an die verbundene Verwaltung übergeben und das Dokument auf „Eingereicht“ setzen. ``data`` enthält
    die geprüften Formularwerte.

    Der Antrag geht als Befehl ``submission.submit`` an mandari Session (Issue #540); geprüft wird dort. Die
    Quittung ist festgeschrieben, bevor Work seinen Stand speichert (Muster aus #539): Schlüssel aus dem
    Dokument ableiten, Befehl senden, Quittung speichern. Scheitert der letzte Schritt, liefert die Wiederholung
    mit demselben Schlüssel dieselbe Quittung. Der Aufruf steht deshalb nie in einer offenen Transaktion.

    Wird ``skipped`` übergeben, stehen danach die Anhänge darin, die nicht übernommen wurden (Name mit Grund) –
    etwa nach der Virenprüfung oder weil die Datei fehlt.
    """
    from hub.commands import Command, CommandError

    connection = get_connection(motion.organization)
    usable, reason = connection_state(connection)
    if not usable:
        raise SubmissionError(reason)
    allowed, reason = can_submit(motion, membership)
    if not allowed:
        raise SubmissionError(reason)

    # Vor dem Lesen der Anlagen: Der Schlüssel gehört zum Stand, den diese Anfrage geprüft hat.
    key = idempotency_key(motion)
    with ExitStack() as stack:
        items, files, documents, not_transferred = _open_attachments(motion, stack)
        # Große Anlagen brauchen Zeit; ist das Dokument inzwischen eingereicht, nicht noch einmal senden.
        if _already_linked(motion):
            raise SubmissionError("Dieses Dokument wurde bereits eingereicht.")
        command = Command(
            name="submission.submit",
            body=_command_body(motion, membership, data, items),
            idempotency_key=key,
            tenant_ref=f"session:{connection.tenant_id}",
            actor_ref=f"user:{membership.user_id}",
            attachments=files,
        )
        try:
            receipt = _client().send(command)
        except CommandError as exc:
            raise SubmissionError(problem_message(exc.problem)) from None

    for entry in receipt.data.get("attachments_rejected") or ():
        index = entry.get("index")
        name = documents[index].filename if isinstance(index, int) and 0 <= index < len(documents) else "Anhang"
        not_transferred.append(f"{name} ({ATTACHMENT_REASONS.get(entry.get('reason'), 'nicht angenommen')})")
    if not_transferred:
        logger.info("Einreichung %s: %s Anhänge nicht übernommen", receipt.aggregate_id, len(not_transferred))
        if skipped is not None:
            skipped.extend(not_transferred)

    _record_receipt(motion, membership, connection, receipt)
    motion.refresh_from_db()
    return motion.session_application


@transaction.atomic
def _record_receipt(motion, membership, connection, receipt) -> None:
    """Quittung in Work festhalten: Verknüpfung, Status über die Übergänge, erstes Rückmeldeereignis."""
    from .administration_feedback import SUBMISSION_VIA, record_submission, send_receipt
    from .models import Motion

    locked = Motion.objects.select_for_update().get(pk=motion.pk)
    if locked.session_application_id == receipt.aggregate_id:
        return  # Eine gleichzeitige Wiederholung hat die Quittung schon festgehalten
    if locked.session_application_id:
        raise SubmissionError("Dieses Dokument wurde bereits eingereicht.")

    locked.session_application_id = receipt.aggregate_id
    locked.administration_status = "submitted"
    locked.save(update_fields=["session_application", "administration_status", "updated_at"])
    # Status über die definierten Übergänge (Issue #316): Wer mit Freigaberecht einreicht, gibt damit frei
    if locked.status == "submitted":
        locked.submitted_at = timezone.now()
        locked.save(update_fields=["submitted_at", "updated_at"])
    else:
        try:
            locked.advance_to("submitted", via=SUBMISSION_VIA)
        except StatusTransitionError as exc:
            # Rollt nur den Stand in Work zurück; die Verwaltung hat den Antrag (Quittung), eine Wiederholung mit
            # demselben Schlüssel verknüpft ihn ohne zweiten Eingang.
            raise SubmissionError(f"Im Status „{locked.get_status_display()}“ kann nicht eingereicht werden.") from exc
    record_submission(locked, locked.session_application)

    connection.last_used_at = timezone.now()
    connection.save(update_fields=["last_used_at"])

    motion_id, membership_id = locked.pk, membership.pk
    transaction.on_commit(lambda: send_receipt(motion_id, membership_id), robust=True)
