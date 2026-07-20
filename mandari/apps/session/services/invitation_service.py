# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ladungs-/Einladungsversand für das Session RIS (Issue #29).

Zentrale Logik für:
- Empfängerkreis aus der aktuellen Gremienbesetzung (inkl. Vertreter und
  beratender Mitglieder; Gäste erhalten nur den öffentlichen Teil)
- Einladungs-PDF mit Tagesordnung (Ö-/NÖ-Teil je Empfängerberechtigung,
  amtlicher Briefkopf über die gemeinsamen PDF-Bausteine in apps/common)
- ICS-Kalenderanhang
- Nachladung/Nachtrags-Tagesordnung als eigener Versandtyp
- lückenlose Versand-Protokollierung (Dispatch + Empfänger + Audit-Log)
"""

import logging

from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone

from apps.common.ical import build_ics_event
from apps.common.pdf import html_to_pdf
from apps.session import audit
from apps.session.models import (
    SessionInvitationDispatch,
    SessionInvitationRecipient,
    SessionMeeting,
)
from apps.session.services import agenda_service

logger = logging.getLogger(__name__)


def get_recipients(meeting: SessionMeeting) -> list[dict]:
    """
    Empfängerkreis aus der aktuellen Gremienbesetzung ermitteln.

    Regeln:
    - aktive Mitgliedschaften zum Sitzungsdatum (start_date/end_date)
    - nur aktive Personen mit E-Mail-Adresse
    - Gäste erhalten nur den öffentlichen Teil der Tagesordnung,
      alle übrigen Funktionen (Mitglied, Vorsitz, Vertreter, beratende
      Mitglieder, sachkundige Bürger) die vollständige Tagesordnung

    Returns:
        Liste von dicts: person, membership, name, email, role,
        include_non_public, missing_email (Personen ohne E-Mail werden
        mit missing_email=True aufgeführt, damit die Verwaltung sie sieht)
    """
    meeting_date = timezone.localtime(meeting.start).date()
    memberships = (
        meeting.organization.memberships.select_related("person", "substitute_for")
        .filter(person__is_active=True)
        .filter(Q(start_date__isnull=True) | Q(start_date__lte=meeting_date))
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=meeting_date))
        .order_by("person__family_name", "person__given_name")
    )

    recipients = []
    seen_person_ids = set()
    for membership in memberships:
        person = membership.person
        if person.pk in seen_person_ids:
            continue
        seen_person_ids.add(person.pk)
        recipients.append(
            {
                "person": person,
                "membership": membership,
                "name": person.display_name,
                "email": person.email,
                "role": membership.get_role_display(),
                "include_non_public": membership.role != "guest",
                "missing_email": not person.email,
            }
        )
    return recipients


def build_agenda_pdf(
    meeting: SessionMeeting,
    *,
    include_non_public: bool,
    supplementary_only: bool = False,
) -> bytes:
    """
    Einladungs-PDF mit Tagesordnung erzeugen (amtlicher Briefkopf des Mandanten).

    Args:
        meeting: die Sitzung
        include_non_public: NÖ-Teil aufnehmen (nur für berechtigte Empfänger)
        supplementary_only: nur Nachtrags-TOPs (Nachtrags-Tagesordnung)

    Returns:
        bytes: PDF-Inhalt
    """
    agenda = agenda_service.grouped_agenda(meeting, include_non_public=include_non_public)

    def _filter(items):
        if not supplementary_only:
            return items
        result = []
        for item in items:
            children = [c for c in item.children_list if c.is_supplementary]
            if item.is_supplementary or children:
                item.children_list = children if not item.is_supplementary else item.children_list
                result.append(item)
        return result

    tenant = meeting.tenant
    context = {
        "tenant": tenant,
        "meeting": meeting,
        "agenda_public": _filter(agenda["public"]),
        "agenda_non_public": _filter(agenda["non_public"]),
        "include_non_public": include_non_public,
        "supplementary_only": supplementary_only,
        "title": "Nachtrags-Tagesordnung" if supplementary_only else "Einladung",
        "generated_at": timezone.localtime(),
        "address_lines": [line for line in (tenant.address or "").splitlines() if line.strip()],
    }
    html = render_to_string("session/pdf/invitation.html", context)
    return html_to_pdf(html)


def build_meeting_ics(meeting: SessionMeeting) -> bytes:
    """ICS-Kalenderanhang für eine Sitzung erzeugen."""
    location_parts = [meeting.location, meeting.room, meeting.street_address]
    location_parts.append(f"{meeting.postal_code} {meeting.locality}".strip())
    location = ", ".join(part for part in location_parts if part)

    return build_ics_event(
        uid=f"session-meeting-{meeting.pk}@mandari",
        summary=meeting.name,
        start=meeting.start,
        end=meeting.end,
        description=f"Sitzung des Gremiums {meeting.organization.name}",
        location=location,
        organizer_name=meeting.tenant.name,
        organizer_email=meeting.tenant.contact_email,
    )


def send_invitations(
    meeting: SessionMeeting,
    *,
    sent_by,
    dispatch_type: str = "invitation",
    subject: str = "",
    message: str = "",
    request=None,
) -> SessionInvitationDispatch:
    """
    Ladung/Nachladung an die Gremienbesetzung versenden und protokollieren.

    - E-Mail mit PDF-Tagesordnung (Ö/NÖ je Empfängerberechtigung) + ICS
    - Dispatch + Empfänger inkl. Zustellstatus werden gespeichert
    - Audit-Eintrag "invitation_sent" mit Versandzusammenfassung
    - Erstladung setzt meeting_state="invitation_sent" und invitation_sent_at

    Returns:
        der angelegte SessionInvitationDispatch
    """
    from apps.common.email import send_email

    if dispatch_type not in ("invitation", "supplementary"):
        raise ValueError(f"Unbekannte Versandart: {dispatch_type}")

    supplementary = dispatch_type == "supplementary"
    subject = subject.strip() or _default_subject(meeting, supplementary)
    message = message.strip() or (meeting.invitation_text or "").strip()

    # PDF-Varianten nur einmal erzeugen (Ö-only und vollständig)
    pdf_full = build_agenda_pdf(meeting, include_non_public=True, supplementary_only=supplementary)
    pdf_public = build_agenda_pdf(meeting, include_non_public=False, supplementary_only=supplementary)
    ics_bytes = build_meeting_ics(meeting)

    pdf_name = "nachtrags-tagesordnung.pdf" if supplementary else "einladung-tagesordnung.pdf"

    dispatch = SessionInvitationDispatch.objects.create(
        meeting=meeting,
        dispatch_type=dispatch_type,
        subject=subject,
        message=message,
        sent_by=sent_by,
    )

    sent_count = 0
    failed_count = 0
    for recipient in get_recipients(meeting):
        if recipient["missing_email"]:
            continue
        include_np = recipient["include_non_public"]
        body = _build_email_body(meeting, message, supplementary)
        try:
            send_email(
                subject=subject,
                body=body,
                to=[recipient["email"]],
                attachments=[
                    (pdf_name, pdf_full if include_np else pdf_public, "application/pdf"),
                    ("sitzung.ics", ics_bytes, "text/calendar"),
                ],
                fail_silently=False,
            )
            status, error = "sent", ""
            sent_count += 1
        except Exception as exc:  # noqa: BLE001 — Zustellstatus je Empfänger dokumentieren
            logger.exception("Ladung an %s konnte nicht versendet werden.", recipient["email"])
            status, error = "failed", str(exc)[:1000]
            failed_count += 1

        SessionInvitationRecipient.objects.create(
            dispatch=dispatch,
            person=recipient["person"],
            name=recipient["name"],
            email=recipient["email"],
            membership_role=recipient["role"],
            includes_non_public=include_np,
            status=status,
            error=error,
        )

    # Erstladung: Sitzungsstatus fortschreiben
    if not supplementary and meeting.invitation_sent_at is None:
        meeting.invitation_sent_at = timezone.now()
        if meeting.meeting_state in ("draft", "scheduled"):
            meeting.meeting_state = "invitation_sent"
        meeting.save()  # Audit: invitation_sent-Aktion über Signal

    # Audit: Versand mit Zusammenfassung protokollieren (wer, wann, an wen)
    audit.log_event(
        "invitation_sent",
        meeting,
        user=sent_by,
        request=request,
        changes={
            "versandart": dispatch.get_dispatch_type_display(),
            "empfaenger_versandt": sent_count,
            "empfaenger_fehlgeschlagen": failed_count,
            "betreff": subject[:300],
        },
    )

    return dispatch


def _default_subject(meeting: SessionMeeting, supplementary: bool) -> str:
    date_str = timezone.localtime(meeting.start).strftime("%d.%m.%Y")
    if supplementary:
        return f"Nachtrag zur Einladung: {meeting.name} am {date_str}"
    return f"Einladung: {meeting.name} am {date_str}"


def _build_email_body(meeting: SessionMeeting, message: str, supplementary: bool) -> str:
    start_local = timezone.localtime(meeting.start)
    lines = ["Guten Tag,", ""]
    if supplementary:
        lines.append(
            "zur bereits versandten Einladung erhalten Sie anbei die Nachtrags-Tagesordnung "
            f"für die Sitzung „{meeting.name}“."
        )
    else:
        lines.append(f"hiermit laden wir Sie zur Sitzung „{meeting.name}“ ein.")
    lines.extend(
        [
            "",
            f"Gremium: {meeting.organization.name}",
            f"Termin:  {start_local.strftime('%d.%m.%Y, %H:%M Uhr')}",
        ]
    )
    if meeting.location:
        location = meeting.location + (f", {meeting.room}" if meeting.room else "")
        lines.append(f"Ort:     {location}")
    if message:
        lines.extend(["", message])
    lines.extend(
        [
            "",
            "Die Tagesordnung finden Sie im Anhang (PDF); der Termin liegt als Kalenderdatei (ICS) bei.",
            "",
            "Mit freundlichen Grüßen",
            meeting.tenant.name,
        ]
    )
    return "\n".join(lines)
