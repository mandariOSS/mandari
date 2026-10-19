# SPDX-License-Identifier: AGPL-3.0-or-later
"""
@-Erwähnungen in den Notizen eines TOPs (Issue #874).

Der Editor speichert eine Erwähnung als Knoten ``<span class="erwaehnung" data-type="mention" data-id="…">@Name</span>``
(``data-id`` = Kennung der Mitgliedschaft, Positivliste in ``apps/work/sanitize.py``). Nach jedem Speichern der
Notizen, ob live über den WebSocket oder über das Formular, bekommen neu erwähnte Personen eine Benachrichtigung in
mandari mit Link zum TOP. Als Auslöser gilt, wer den Stand mit der neuen Erwähnung zuerst gespeichert hat.

- **Nur wer den TOP sehen darf:** aktive Mitglieder der Organisation (keine Gäste) mit Leserecht für
  Fraktionssitzungen; bei nichtöffentlichen TOPs nur Vereidigte. Andere Kennungen im HTML bleiben stumm.
- **Einmal je TOP und Person:** Der Schlüssel ``fs-erwaehnung:<TOP>`` steht als ``event_key`` an der
  Benachrichtigung (eindeutig je Empfänger, wie bei den Ereignissen der Datendrehscheibe, #529). Erneutes Speichern,
  gleichzeitiges Speichern mehrerer Schreibender und erneutes Erwähnen legen nichts doppelt an.
- **Ohne Inhalt:** Die Nachricht nennt nur TOP und Sitzung, keine Notizen; keine E-Mail.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, transaction
from django.urls import reverse

from .visibility import is_item_internal, is_sworn_member

if TYPE_CHECKING:
    from apps.tenants.models import Membership

    from .models import FactionAgendaItem

logger = logging.getLogger(__name__)

_ERWAEHNUNG_RE = re.compile(r"<span\b[^>]*\bdata-type=\"mention\"[^>]*>", re.IGNORECASE)
_KENNUNG_RE = re.compile(r"\bdata-id=\"([0-9a-fA-F-]{36})\"")


def erwaehnte_kennungen(html: str | None) -> set[uuid.UUID]:
    """Kennungen der erwähnten Mitgliedschaften im (bereinigten) HTML der Notizen."""
    kennungen: set[uuid.UUID] = set()
    for tag in _ERWAEHNUNG_RE.findall(html or ""):
        treffer = _KENNUNG_RE.search(tag)
        if not treffer:
            continue
        try:
            kennungen.add(uuid.UUID(treffer.group(1)))
        except ValueError:
            continue
    return kennungen


def schluessel(item: FactionAgendaItem) -> str:
    """Grundschlüssel der Benachrichtigungen eines TOPs (``NotificationHub`` hängt die Mitgliedschaft an)."""
    return f"fs-erwaehnung:{item.pk}"


def empfaenger(item: FactionAgendaItem, kennungen: set[uuid.UUID], *, ausser: Membership | None) -> list[Membership]:
    """Erwähnte Mitgliedschaften, die den TOP sehen dürfen (ohne die schreibende Person selbst)."""
    from apps.tenants.models import Membership

    if not kennungen:
        return []
    qs = (
        Membership.objects.filter(
            pk__in=kennungen,
            organization_id=item.meeting.organization_id,
            is_active=True,
            is_guest=False,
            user__is_active=True,
        )
        .select_related("user")
        .prefetch_related("roles__permissions", "individual_permissions", "denied_permissions")
    )
    if ausser is not None:
        qs = qs.exclude(pk=ausser.pk)
    intern = is_item_internal(item)
    return [m for m in qs if m.has_permission("faction.view_public") and (not intern or is_sworn_member(m))]


def benachrichtigen(
    item: FactionAgendaItem, html: str | None, *, von: Membership | None, vorher: str | None = None
) -> list[Any]:
    """
    Neu erwähnte Personen benachrichtigen; liefert die angelegten Benachrichtigungen.

    Neu sind Erwähnungen, die im vorher gespeicherten Stand fehlten: Speichert jemand anders den Text mit einer
    bestehenden Erwähnung, gilt sie nicht als seine.
    """
    from apps.work.notifications.models import Notification, NotificationType
    from apps.work.notifications.services import NotificationHub

    personen = empfaenger(item, erwaehnte_kennungen(html) - erwaehnte_kennungen(vorher), ausser=von)
    if not personen:
        return []
    grund = schluessel(item)
    schon = set(
        Notification.objects.filter(event_key__in=[f"{grund}:{m.pk}" for m in personen]).values_list(
            "recipient_id", flat=True
        )
    )
    meeting = item.meeting
    link = reverse("work:faction_detail", kwargs={"org_slug": meeting.organization.slug, "meeting_id": meeting.pk})
    wer = von.user.get_display_name() if von is not None else "Jemand"
    intern = is_item_internal(item)
    angelegt = []
    for person in personen:
        if person.pk in schon:
            continue
        try:
            # Eigener Sicherungspunkt: Speichern zwei Schreibende gleichzeitig, verliert eine Seite am eindeutigen
            # Schlüssel, ohne die Transaktion des Speicherns zu stören
            with transaction.atomic():
                neu = NotificationHub.send(
                    recipient=person,
                    notification_type=NotificationType.FACTION_MENTION,
                    title="Erwähnung im Protokoll",
                    message=f"{wer} hat Sie im Protokoll zu TOP {item.number} „{item.title}“ ({meeting.title}) erwähnt.",
                    link=f"{link}?top={item.pk}",
                    actor=von,
                    metadata={"meeting_id": str(meeting.pk), "item_id": str(item.pk)},
                    send_email=False,
                    event_key=grund,
                    # Keine Weiterleitung an eine Vertretung bei nichtöffentlichen TOPs: Sie ist nicht geprüft
                    _forwarded=intern,
                )
        except IntegrityError:
            continue
        except Exception:
            logger.exception("Erwähnung im TOP %s: Benachrichtigung nicht angelegt", item.pk)
            continue
        if neu is not None:
            angelegt.append(neu)
    return angelegt
