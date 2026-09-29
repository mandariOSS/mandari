# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Was mit den Daten eines Mitglieds geschieht, wenn seine Mitgliedschaft gelöscht wird (Issue #420).

Grundsatz: Inhalte der Organisation bleiben erhalten, rein persönliche Daten entfallen.

- **Namen in Protokollen** (Anwesenheit, Redner:innen, Zuständige, Teilnahmebestätigungen) sichert
  :func:`preserve_names` vor dem Löschen; sie bleiben unabhängig von der Genehmigung sichtbar
  (Issue #591, Zweck: Nachweis der Beschlussfassung und der Teilnahme).
- **Organisationsinhalte** verweisen mit ``on_delete=SET_NULL`` auf die Mitgliedschaft. Der Verweis
  wird geleert, die Oberfläche zeigt „Ehemaliges Mitglied“ (``apps.common.formatting.member_name``):
  Dokumente mit Anhängen, Versionen, Kommentaren und Entscheidungen über Freigaben; Aufgaben mit
  Kommentaren, Anhängen, Verlauf und Freigaben anderer; Fraktionssitzungen, Protokolleinträge,
  Abstimmungsergebnisse, ausgestellte Teilnahmenachweise; Sitzungsvorbereitung, TOP-Diskussion,
  Anlagen, Datei-Anmerkungen; Support-Tickets und ihre Nachrichten.
- **Persönliche Daten** hängen mit ``CASCADE`` an der Mitgliedschaft: private TOP-Notizen,
  Benachrichtigungen und -einstellungen, Abwesenheiten, eigene Änderungsanträge, DSGVO-Exporte,
  Freigaben einer Aufgabe an die Person.
- **Gemischte Modelle** (je Eintrag persönlich oder geteilt) haben ``SET_NULL``; den persönlichen
  Teil löscht :func:`purge_personal_data` vor dem Löschen der Mitgliedschaft (``pre_delete``):
  nicht geteilte Redebeiträge und private Vorgangs-Kommentare, Aufgaben, die nur die Person sah,
  offene Freigabe-Anfragen an die Person, Teilnahmen an noch nicht begonnenen Fraktionssitzungen.

Der Receiver greift bei jedem Löschweg der Mitgliedschaft (Mitglied entfernen, Registrierung
ablehnen, Konto löschen, Admin). Beim Löschen der ganzen Organisation entfällt ohnehin alles; dann
spart er sich die Arbeit.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db.models import QuerySet

if TYPE_CHECKING:
    from apps.tenants.models import Membership

logger = logging.getLogger(__name__)


def purge_personal_data(membership: Membership) -> dict[str, int]:
    """Persönliche Einträge gemischter Modelle löschen; liefert die Anzahl je Bereich."""
    from apps.work.faction.services import discard_open_invitations
    from apps.work.meetings.services import delete_private_member_notes
    from apps.work.motions.services import withdraw_pending_approvals
    from apps.work.tasks.services import delete_personal_tasks

    return {
        "fraktionssitzungen": discard_open_invitations(membership),
        "sitzungsvorbereitung": delete_private_member_notes(membership),
        "freigaben": withdraw_pending_approvals(membership),
        "aufgaben": delete_personal_tasks(membership),
    }


def _organization_deleted_with(origin: Any) -> bool:
    """Wird die Mitgliedschaft mit ihrer ganzen Organisation gelöscht?"""
    from apps.tenants.models import Organization

    if isinstance(origin, Organization):
        return True
    return isinstance(origin, QuerySet) and issubclass(origin.model, Organization)


def preserve_names(membership: Membership) -> int:
    """Namen in Anwesenheit und Protokollen sichern, bevor ``SET_NULL`` die Verweise leert (Issue #591)."""
    from apps.work.faction.services import preserve_member_names

    return preserve_member_names(membership)


def membership_pre_delete(sender: Any, instance: Membership, origin: Any = None, **kwargs: Any) -> None:
    """pre_delete(Membership): Namen sichern, persönliche Einträge löschen, Organisationsinhalte bleiben."""
    if _organization_deleted_with(origin):
        return
    preserve_names(instance)
    counts = purge_personal_data(instance)
    if any(counts.values()):
        logger.info("Mitgliedschaft %s entfernt, persönliche Einträge gelöscht: %s", instance.pk, counts)


def register() -> None:
    """Receiver registrieren (WorkConfig.ready)."""
    from django.db.models.signals import pre_delete

    from apps.tenants.models import Membership

    pre_delete.connect(membership_pre_delete, sender=Membership, dispatch_uid="work_membership_personal_data")
