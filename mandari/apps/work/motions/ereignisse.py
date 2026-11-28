# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignisse der Dokumente für die Datendrehscheibe (Issue #529, ``docs/adr/20260929-ereignistechnik-postgres.md``).

Die Ansichten in ``apps.work.motions.views`` melden hier, dass ein Dokument kommentiert, die Federführung
übertragen, eine Freigabe angefragt oder entschieden oder das Dokument für eine Person freigegeben wurde.
Das Ereignis entsteht in derselben Transaktion wie die Änderung; das Abonnement ``benachrichtigung`` legt
daraus die Benachrichtigungen an. Schalter und Schreiben: ``apps.work.notifications.ereignisse``.

Aggregat ist das Dokument (``Document``, wie ``work.document.status_changed``).
"""

from __future__ import annotations

from typing import Any, Final

from apps.events import CanonicalRef
from apps.work.notifications import ereignisse as benachrichtigung

KOMMENTIERT: Final = "work.document.commented"
ZUGEWIESEN: Final = "work.document.assigned"
FREIGABE_ANGEFRAGT: Final = "work.document.approval_requested"
FREIGABE_ENTSCHIEDEN: Final = "work.document.approval_decided"
FREIGEGEBEN: Final = "work.document.shared"


def melden(typ: str, motion: Any, actor: Any, **felder: Any) -> bool:
    """Ereignis ``typ`` zum Dokument schreiben; ``True``, wenn der direkte Aufruf entfällt (``aktiv``)."""
    return benachrichtigung.melden(
        typ,
        aggregate=CanonicalRef("Document", motion.id),
        organization_id=motion.organization_id,
        actor=actor,
        payload={"document": motion.id, "organization": motion.organization_id, **felder},
    )
