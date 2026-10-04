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
from apps.session.visibility import paper_visible

# Abstimmungsergebnisse, die als gefasster Beschluss ins Register aufgenommen werden
DECIDED_RESULTS = ("approved", "rejected", "deferred", "noted")

# Sortierung der Beschlusslisten: neueste Sitzung zuerst, TOPs einer Sitzung in Tagesordnungsreihenfolge.
# Sitzung, TOP-Nummer und Primärschlüssel als Nachrang: Gleichzeitige Sitzungen und TOPs mit gleicher
# Reihenfolge (order = 0, wenn keine gepflegt ist) lägen sonst in beliebiger Folge vor, auch über
# Seitengrenzen hinweg (Issue #653).
DECIDED_ITEMS_ORDERING = ("-meeting__start", "meeting_id", "order", "number", "id")


def decided_items(tenant, *, include_non_public: bool):
    """Alle gefassten Beschlüsse (TOPs mit Ergebnis) eines Mandanten."""
    qs = (
        SessionAgendaItem.objects.filter(meeting__tenant=tenant, vote_result__in=DECIDED_RESULTS)
        .exclude(is_withdrawn=True)
        .select_related("meeting__organization", "paper")
        .order_by(*DECIDED_ITEMS_ORDERING)
    )
    if not include_non_public:
        qs = qs.filter(is_public=True, meeting__is_public=True)
    return qs


def assign_resolution_number(item: SessionAgendaItem) -> bool:
    """
    Beschlussnummer vergeben (idempotent).

    Die Vergabe läuft in einer Transaktion mit Zeilen-Lock auf dem Tenant, damit parallele
    Ausfertigungen keine doppelten Nummern erzeugen (Muster: SessionApplication._next_reference).
    Ob der TOP schon eine Nummer hat, wird erst unter dieser Sperre an der Datenbank geprüft:
    Eine doppelte Auslösung (zwei Anfragen mit demselben, noch nummernlosen Stand) vergibt sonst
    eine zweite Nummer und überschreibt die erste. Gespeichert wird nur die Nummer.

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
        current = SessionAgendaItem.objects.select_for_update().get(pk=item.pk)
        if current.resolution_number:
            # Inzwischen von einer parallelen Ausfertigung vergeben: übernehmen, nicht neu vergeben
            item.resolution_number = current.resolution_number
            return False
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
        current.resolution_number = f"{prefix}{max_num + 1:04d}"
        # Nur die Nummer speichern (frischer Stand aus der Datenbank): Ein veralteter Stand des
        # Aufrufers überschreibt keine zwischenzeitlichen Änderungen. Audit: update-Eintrag über Signal.
        current.save(update_fields=["resolution_number", "updated_at"])
    item.resolution_number = current.resolution_number
    return True


def ensure_numbers_for_meeting(meeting: SessionMeeting) -> int:
    """Beschlussnummern für alle gefassten Beschlüsse einer Sitzung vergeben."""
    from apps.session import hub_events

    assigned = 0
    # Drehscheibe (Issue #535): je neu nummeriertem Beschluss ris.resolution.adopted (resolutionNumber)
    with hub_events.track(meeting.tenant) as tracked:
        tracked.agenda(meeting)
        items = (
            meeting.agenda_items.filter(vote_result__in=DECIDED_RESULTS)
            .exclude(is_withdrawn=True)
            .order_by("order", "number")
        )
        for item in items:
            if assign_resolution_number(item):
                assigned += 1
    return assigned


def touch_published_implementations(tenant) -> int:
    """
    Freigabe des Umsetzungsstands am Mandanten geändert (Schalter der Verwaltung, Veröffentlichung im Bürgerportal
    beendet bzw. wieder aufgenommen, Issue #525): Die OParl-Schnittstelle gibt ``mandari:implementation`` der
    freigegebenen Beschlüsse anders aus, ohne dass sich die TOPs selbst ändern. Ihr ``updated_at`` rückt vor, damit
    Abgleiche mit ``modified_since`` (Ingestor, Spiegel, Dritte) sie sofort neu lesen, nicht erst beim Vollabgleich.

    Betroffen sind die Beschlüsse, die am TOP die Regel der Beschlussseiten erfüllen (``decision_tracking.visibility``):
    angenommen, öffentlicher TOP einer öffentlichen Sitzung, am Beschluss freigegeben, nicht abgesetzt. Ein
    ``UPDATE`` ohne Signale: Nur der Zeitpunkt ändert sich, kein fachlicher Inhalt (kein Audit, keine Sperre).

    Returns:
        Zahl der betroffenen Beschlüsse.
    """
    return (
        SessionAgendaItem.objects.filter(
            meeting__tenant=tenant,
            vote_result="approved",
            is_public=True,
            meeting__is_public=True,
            implementation_public=True,
        )
        .exclude(is_withdrawn=True)
        .update(updated_at=timezone.now())
    )


def build_extract_pdf(items: list, *, internal: bool, permissions=None) -> bytes:
    """
    Beschlussauszug-PDF erzeugen (ein oder mehrere TOPs, je TOP eine Seite).

    Args:
        items: Liste von SessionAgendaItems (bereits Ö/NÖ-gefiltert!)
        internal: True = interne Ausfertigung inkl. NÖ-Beschlusstexten
        permissions: Rechte der abrufenden Person; eine nichtöffentliche Vorlage nennt auch die
            interne Ausfertigung nur mit dem NÖ-Recht für Vorlagen (das Sitzungsrecht genügt nicht)

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
            (internal and (permissions is None or paper_visible(permissions, paper)))
            or (paper.is_public and paper.status not in UNVEROEFFENTLICHT)
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
