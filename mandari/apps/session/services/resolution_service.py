# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlussregister und Beschlussauszüge (Issue #32).

Zentrale Logik für:
- Beschlussnummern-Vergabe je Mandant/Jahr (B/<Jahr>/<lfd>, serialisiert
  über Zeilen-Lock auf dem Tenant — analog zur Eingangsnummern-Vergabe)
- Beschlussauszug-PDF je TOP bzw. als Sammel-Ausfertigung einer Sitzung
  (amtlicher Briefkopf, Beschlusstext, Abstimmungsergebnis,
  Auszugs-/Ausfertigungsvermerk; NÖ-Beschlusstexte nur intern)
"""

from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from apps.common.pdf import html_to_pdf
from apps.session.models import SessionAgendaItem, SessionMeeting, SessionTenant

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

    tenant = items[0].meeting.tenant
    for item in items:
        item.resolution_np = item.get_resolution_text_decrypted() if internal else ""

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
