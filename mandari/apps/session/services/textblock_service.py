# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Textbausteine und Standard-Tagesordnungspunkte (Issue #85).

- apply_standard_items(): beim Anlegen einer Sitzung die für das Gremium
  hinterlegten Standard-TOPs automatisch in die Tagesordnung übernehmen.
- Textbausteine werden clientseitig in die Editor-Felder eingefügt;
  Platzhalter ({gremium}, {datum}, {sitzung}, {vorlage}) ersetzt das
  Formular-JavaScript mit den Werten des jeweiligen Kontexts.
"""

from django.db.models import Q

from ..models import SessionAgendaItem, SessionMeeting, SessionStandardAgendaItem

# Ende-TOPs bekommen bewusst sehr hohe Order-Werte, damit später manuell
# ergänzte TOPs (Order = (Anzahl+1)*100) davor einsortiert werden.
END_ORDER_BASE = 900000


def standard_items_for(meeting: SessionMeeting):
    """Aktive Standard-TOPs für das Gremium der Sitzung (inkl. „alle Gremien")."""
    return (
        SessionStandardAgendaItem.objects.filter(
            tenant=meeting.tenant, is_active=True
        )
        .filter(Q(organization__isnull=True) | Q(organization=meeting.organization))
        .order_by("placement", "order", "name")
    )


def apply_standard_items(meeting: SessionMeeting) -> int:
    """
    Standard-TOPs in eine (frisch angelegte) Sitzung übernehmen.

    Bereits vorhandene TOPs bleiben unberührt; die Nummern werden
    fortlaufend hinter den vorhandenen TOPs vergeben.
    """
    existing = meeting.agenda_items.count()
    number = existing
    created = 0

    start_items = []
    end_items = []
    for template in standard_items_for(meeting):
        (start_items if template.placement == "start" else end_items).append(template)

    for index, template in enumerate(start_items):
        number += 1
        SessionAgendaItem.objects.create(
            meeting=meeting,
            number=str(number),
            order=(existing + index + 1) * 100,
            name=template.name,
            is_public=template.is_public and meeting.is_public,
        )
        created += 1

    for index, template in enumerate(end_items):
        number += 1
        SessionAgendaItem.objects.create(
            meeting=meeting,
            number=str(number),
            order=END_ORDER_BASE + index * 100,
            name=template.name,
            is_public=template.is_public and meeting.is_public,
        )
        created += 1

    return created
