# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ladungsnachweis und Serienbrief (Issue #225).

- **Ladungsnachweis (PDF)** für die Akte: je Person jeder Versand (Art, Zustellweg,
  Zeitpunkt, Status), die Empfangsbestätigung und die Rückmeldung mit Zeitstempel und Herkunft.
  Gründe für Absagen stehen bewusst NICHT im Nachweis – sie bleiben dem Sitzungsdienst in der
  Übersicht vorbehalten.
- **Serienbrief** für Personen mit Zustellweg „Brief“: ein PDF mit einem Anschreiben je Person
  (Anschrift, Sitzungsdaten, Tagesordnung Ö/NÖ je Berechtigung, Rückmeldelink mit QR-Code)
  und ein CSV für den Serienbrief in der Textverarbeitung.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any, cast

from django.template.loader import render_to_string
from django.utils import timezone

from apps.common import csv_safety
from apps.common.pdf import html_to_pdf
from apps.session.models import SessionInvitationDispatch, SessionInvitationRecipient, SessionMeeting
from apps.session.services import agenda_service, invitation_token
from apps.session.services.invitation_response_service import MeetingOverview, meeting_location

logger = logging.getLogger(__name__)

LETTER_STATUSES = ("letter_pending", "letter_sent")


def _address_lines(tenant: Any) -> list[str]:
    return [line for line in (tenant.address or "").splitlines() if line.strip()]


def build_proof_pdf(meeting: SessionMeeting, overview: MeetingOverview, *, generated_by: str) -> bytes:
    """Ladungsnachweis als PDF (ohne Absagegründe)."""
    tenant = meeting.tenant
    html = render_to_string(
        "session/pdf/invitation_proof.html",
        {
            "tenant": tenant,
            "meeting": meeting,
            "overview": overview,
            "location": meeting_location(meeting),
            "generated_at": timezone.localtime(),
            "generated_by": generated_by,
            "address_lines": _address_lines(tenant),
        },
    )
    return html_to_pdf(html)


@dataclass(frozen=True)
class LetterBatch:
    """Briefempfänger eines Versands (für die Übersicht: Serienbrief und Briefversand vermerken)."""

    dispatch: SessionInvitationDispatch
    total: int
    pending: int


def letter_batches(meeting: SessionMeeting) -> list[LetterBatch]:
    """Versände der Sitzung mit Briefempfängern, jüngster zuerst."""
    batches: dict[Any, LetterBatch] = {}
    rows = (
        SessionInvitationRecipient.objects.filter(
            dispatch__meeting=meeting, channel="letter", status__in=LETTER_STATUSES
        )
        .select_related("dispatch")
        .order_by("-dispatch__sent_at")
    )
    for row in rows:
        batch = batches.get(row.dispatch_id)
        pending = 1 if row.status == "letter_pending" else 0
        if batch is None:
            batches[row.dispatch_id] = LetterBatch(row.dispatch, 1, pending)
        else:
            batches[row.dispatch_id] = LetterBatch(batch.dispatch, batch.total + 1, batch.pending + pending)
    return list(batches.values())


def letter_recipients(dispatch: SessionInvitationDispatch) -> list[SessionInvitationRecipient]:
    """Empfänger eines Versands mit Zustellweg Brief (Reihenfolge nach Name)."""
    return list(
        dispatch.recipients.filter(channel="letter", status__in=LETTER_STATUSES)
        .select_related("person", "substitute_for")
        .order_by("name")
    )


def _qr_data_uri(url: str) -> str:
    """QR-Code des Rückmeldelinks als PNG-Data-URI (reines Python via segno)."""
    try:
        import segno

        return str(segno.make(url, error="m").png_data_uri(scale=3))
    except Exception:  # noqa: BLE001 — ohne QR-Code bleibt die Adresse als Text im Brief
        logger.warning("QR-Code für den Serienbrief konnte nicht erzeugt werden.")
        return ""


def _letter_entries(dispatch: SessionInvitationDispatch) -> list[dict[str, Any]]:
    entries = []
    for recipient in letter_recipients(dispatch):
        person = recipient.person
        address = cast(Any, person).get_address_decrypted() if person is not None else ""
        url = invitation_token.response_url(recipient)
        entries.append(
            {
                "recipient": recipient,
                "person": person,
                "address_lines": [line for line in (address or "").splitlines() if line.strip()],
                "response_url": url,
                "qr_data_uri": _qr_data_uri(url),
                "salutation": salutation(person, recipient.name),
            }
        )
    return entries


def salutation(person: Any, fallback_name: str) -> str:
    """Briefanrede aus Anrede, Titel und Nachname („Sehr geehrte Frau Dr. Muster,“)."""
    if person is None:
        return f"Guten Tag {fallback_name},"
    form = (person.form_of_address or "").strip()
    name = " ".join(part for part in (person.title, person.family_name) if part)
    if form.lower() == "frau":
        return f"Sehr geehrte Frau {name},"
    if form.lower() == "herr":
        return f"Sehr geehrter Herr {name},"
    return f"Guten Tag {person.display_name},"


def build_serial_letter_pdf(dispatch: SessionInvitationDispatch) -> bytes:
    """Serienbrief: ein Anschreiben je Person mit Zustellweg Brief."""
    meeting = dispatch.meeting
    tenant = meeting.tenant
    supplementary = dispatch.dispatch_type == "supplementary"
    agendas: dict[bool, dict[str, Any]] = {}
    entries = _letter_entries(dispatch)
    for entry in entries:
        include_np = bool(entry["recipient"].includes_non_public)
        if include_np not in agendas:
            agendas[include_np] = _agenda(meeting, include_non_public=include_np, supplementary_only=supplementary)
        entry["agenda"] = agendas[include_np]
    html = render_to_string(
        "session/pdf/serial_letters.html",
        {
            "tenant": tenant,
            "meeting": meeting,
            "dispatch": dispatch,
            "entries": entries,
            "supplementary": supplementary,
            "location": meeting_location(meeting),
            "generated_at": timezone.localtime(),
            "address_lines": _address_lines(tenant),
        },
    )
    return html_to_pdf(html)


def _agenda(meeting: SessionMeeting, *, include_non_public: bool, supplementary_only: bool) -> dict[str, Any]:
    agenda = agenda_service.grouped_agenda(meeting, include_non_public=include_non_public)
    if not supplementary_only:
        return dict(agenda)

    def _only_supplementary(items: list[Any]) -> list[Any]:
        return [item for item in items if item.is_supplementary or any(c.is_supplementary for c in item.children_list)]

    return {"public": _only_supplementary(agenda["public"]), "non_public": _only_supplementary(agenda["non_public"])}


# Schutz gegen Formel-Injektion, gemeinsam mit dem Protokollexport (apps/common/csv_safety.py)
_csv_cell = csv_safety.csv_safe_cell


def build_serial_letter_csv(dispatch: SessionInvitationDispatch) -> str:
    """Serienbrief-Daten (Semikolon, CRLF) für die Textverarbeitung."""
    meeting = dispatch.meeting
    start_local = timezone.localtime(meeting.start)
    buffer = io.StringIO()
    writer = csv_safety.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "Anrede",
            "Titel",
            "Vorname",
            "Nachname",
            "Adresszeile1",
            "Adresszeile2",
            "Adresszeile3",
            "Adresszeile4",
            "Funktion",
            "Gremium",
            "Sitzung",
            "Datum",
            "Uhrzeit",
            "Ort",
            "Versandart",
            "Tagesordnung",
            "Vertretung fuer",
            "Rueckmeldelink",
        ]
    )
    for entry in _letter_entries(dispatch):
        recipient: SessionInvitationRecipient = entry["recipient"]
        person = entry["person"]
        lines = (entry["address_lines"] + ["", "", "", ""])[:4]
        writer.writerow(
            [
                _csv_cell(person.form_of_address if person else ""),
                _csv_cell(person.title if person else ""),
                _csv_cell(person.given_name if person else recipient.name),
                _csv_cell(person.family_name if person else ""),
                *(_csv_cell(line) for line in lines),
                _csv_cell(recipient.membership_role),
                _csv_cell(meeting.organization.name),
                _csv_cell(meeting.name),
                start_local.strftime("%d.%m.%Y"),
                start_local.strftime("%H:%M"),
                _csv_cell(meeting_location(meeting)),
                _csv_cell(dispatch.get_dispatch_type_display()),
                "vollständig" if recipient.includes_non_public else "nur öffentlicher Teil",
                _csv_cell(recipient.substitute_for.display_name if recipient.substitute_for else ""),
                entry["response_url"],
            ]
        )
    return buffer.getvalue()
