# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Niederschrift-Workflow für das Session RIS (Issue #31).

Zentrale Logik für:
- Anlegen des Protokolls je Sitzung (TOP-Struktur kommt aus der Tagesordnung,
  Teilnehmerverzeichnis aus der Anwesenheitserfassung)
- Statusübergänge Entwurf -> Prüfung -> genehmigt -> veröffentlicht
  (Genehmigungsvermerk in der Folgesitzung)
- Niederschrift-PDF in Ö-Fassung und interner NÖ-Fassung (gemeinsame
  PDF-Bausteine aus apps/common, amtlicher Briefkopf)
"""

from django.template.loader import render_to_string
from django.utils import timezone

from apps.common.pdf import html_to_pdf
from apps.session.models import SessionMeeting, SessionProtocol
from apps.session.services import agenda_service, attendance_service

# Erlaubte Statusübergänge (Workflow-Guard)
TRANSITIONS = {
    "submit": ("draft", "review"),
    "reject": ("review", "draft"),
    "approve": ("review", "approved"),
    "publish": ("approved", "published"),
}


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


def apply_transition(protocol: SessionProtocol, action: str) -> bool:
    """
    Statusübergang prüfen und Status setzen (ohne save()).

    Returns:
        True, wenn der Übergang zulässig war.
    """
    transition = TRANSITIONS.get(action)
    if transition is None or protocol.status != transition[0]:
        return False
    protocol.status = transition[1]
    return True


def participant_directory(meeting: SessionMeeting) -> dict:
    """Teilnehmerverzeichnis aus der Anwesenheitserfassung gruppieren."""
    attendances = list(meeting.attendances.select_related("person").order_by("person__family_name"))
    present = [a for a in attendances if a.status in attendance_service.PRESENT_STATUSES]
    excused = [a for a in attendances if a.status in ("excused", "declined")]
    absent = [a for a in attendances if a.status == "absent"]
    other = [a for a in attendances if a not in present and a not in excused and a not in absent]
    return {"present": present, "excused": excused, "absent": absent, "other": other, "all": attendances}


def build_protocol_pdf(protocol: SessionProtocol, *, internal: bool) -> bytes:
    """
    Niederschrift-PDF erzeugen.

    Args:
        protocol: das Protokoll
        internal: True = interne NÖ-Fassung (inkl. nichtöffentlicher Teile),
                  False = Ö-Fassung (NÖ-Inhalte erscheinen niemals)

    Returns:
        bytes: PDF-Inhalt
    """
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
        "generated_at": timezone.localtime(),
        "address_lines": [line for line in (tenant.address or "").splitlines() if line.strip()],
    }
    html = render_to_string("session/pdf/protocol.html", context)
    return html_to_pdf(html)
