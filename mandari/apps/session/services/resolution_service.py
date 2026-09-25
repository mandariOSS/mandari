# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlussregister und Beschlussauszüge (Issue #32).

Zentrale Logik für:
- Beschlussnummern-Vergabe je Mandant/Jahr (B/<Jahr>/<lfd>, serialisiert
  über Zeilen-Lock auf dem Tenant — analog zur Eingangsnummern-Vergabe)
- Beschlussauszug-PDF je TOP bzw. als Sammel-Ausfertigung einer Sitzung
  (amtlicher Briefkopf, Beschlusstext, Abstimmungsergebnis,
  Auszugs-/Ausfertigungsvermerk; NÖ-Beschlusstexte nur intern)
- Stand des Auszugs (Issue #318): Nach der Genehmigung ist der TOP gesperrt, der Auszug gibt
  den genehmigten Stand samt Berichtigungen wieder; vorher ist er als vorläufig gekennzeichnet
"""

from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from apps.common.pdf import html_to_pdf
from apps.session.models import SessionAgendaItem, SessionMeeting, SessionProtocol, SessionTenant

# Abstimmungsergebnisse, die als gefasster Beschluss ins Register aufgenommen werden
DECIDED_RESULTS = ("approved", "rejected", "deferred", "noted")


def decided_items(tenant, *, include_non_public: bool):
    """Alle gefassten Beschlüsse (TOPs mit Ergebnis) eines Mandanten."""
    qs = (
        SessionAgendaItem.objects.filter(meeting__tenant=tenant, vote_result__in=DECIDED_RESULTS)
        .exclude(is_withdrawn=True)
        .select_related("meeting__organization", "paper")
        .order_by("-meeting__start", "order")
    )
    if not include_non_public:
        qs = qs.filter(is_public=True, meeting__is_public=True)
    return qs


def assign_resolution_number(item: SessionAgendaItem) -> bool:
    """
    Beschlussnummer vergeben (idempotent).

    Sicherheit: Die Vergabe läuft in einer Transaktion mit Zeilen-Lock auf
    dem Tenant, damit parallele Ausfertigungen keine doppelten Nummern
    erzeugen (Muster: SessionApplication._next_reference).

    Returns:
        True, wenn eine neue Nummer vergeben wurde.
    """
    if item.resolution_number:
        return False

    tenant_id = item.meeting.tenant_id
    year = timezone.localtime(item.meeting.start).year
    prefix = f"B/{year}/"

    with transaction.atomic():
        SessionTenant.objects.select_for_update().get(pk=tenant_id)
        max_num = 0
        refs = SessionAgendaItem.objects.filter(
            meeting__tenant_id=tenant_id,
            resolution_number__startswith=prefix,
        ).values_list("resolution_number", flat=True)
        for ref in refs:
            try:
                num = int(ref.rsplit("/", 1)[-1])
            except (TypeError, ValueError):
                continue
            max_num = max(max_num, num)
        item.resolution_number = f"{prefix}{max_num + 1:04d}"
        item.save()  # Audit: update-Eintrag über Signal
    return True


def ensure_numbers_for_meeting(meeting: SessionMeeting) -> int:
    """Beschlussnummern für alle gefassten Beschlüsse einer Sitzung vergeben."""
    assigned = 0
    items = (
        meeting.agenda_items.filter(vote_result__in=DECIDED_RESULTS)
        .exclude(is_withdrawn=True)
        .order_by("order", "number")
    )
    for item in items:
        if assign_resolution_number(item):
            assigned += 1
    return assigned


def build_extract_pdf(items: list, *, internal: bool) -> bytes:
    """
    Beschlussauszug-PDF erzeugen (ein oder mehrere TOPs, je TOP eine Seite).

    Args:
        items: Liste von SessionAgendaItems (bereits Ö/NÖ-gefiltert!)
        internal: True = interne Ausfertigung inkl. NÖ-Beschlusstexten

    Returns:
        bytes: PDF-Inhalt
    """
    if not items:
        raise ValueError("Keine Beschlüsse für die Ausfertigung übergeben.")

    from apps.session.oparl_publication import UNVEROEFFENTLICHT
    from apps.session.services import protocol_service

    tenant = items[0].meeting.tenant
    protocols = {
        protocol.meeting_id: protocol
        for protocol in SessionProtocol.objects.filter(meeting_id__in={item.meeting_id for item in items})
    }
    notes_by_meeting: dict = {}
    for item in items:
        # Stand: genehmigte (gesperrte) Niederschrift oder vorläufig (Issue #318)
        protocol = protocols.get(item.meeting_id)
        if protocol is not None and protocol.is_locked:
            stamp = protocol.approved_at or protocol.published_at
            item.protocol_state = "Stand der genehmigten Niederschrift" + (
                f" vom {timezone.localtime(stamp).strftime('%d.%m.%Y')}" if stamp else ""
            )
            if item.meeting_id not in notes_by_meeting:
                notes_by_meeting[item.meeting_id] = protocol_service.correction_notes(
                    protocol, internal=internal, visible_item_ids={i.pk for i in items if i.is_public}
                )
            item.correction_notes = [n for n in notes_by_meeting[item.meeting_id] if n["item_id"] == item.pk]
        else:
            item.protocol_state = "Vorläufiger Stand – die Niederschrift ist noch nicht genehmigt."
            item.correction_notes = []
        paper = item.paper
        item.paper_visible = paper is not None and (
            internal or (paper.is_public and paper.status not in UNVEROEFFENTLICHT)
        )
        item.resolution_np = item.get_resolution_text_decrypted() if internal else ""
        # Namentliche Abstimmung + Befangenheit (Issue #41)
        votes = list(item.votes.select_related("person"))
        item.roll_call_votes = (
            [v for v in votes if v.vote in ("yes", "no", "abstain")] if item.voting_method == "roll_call" else []
        )
        item.excluded_persons = [v.person for v in votes if v.vote == "excluded"]

    context = {
        "tenant": tenant,
        "items": items,
        "internal": internal,
        "variant_label": "Interne Ausfertigung (inkl. nichtöffentlicher Teile)"
        if internal
        else "Öffentliche Ausfertigung",
        "generated_at": timezone.localtime(),
        "address_lines": [line for line in (tenant.address or "").splitlines() if line.strip()],
    }
    html = render_to_string("session/pdf/resolution_extract.html", context)
    return html_to_pdf(html)
