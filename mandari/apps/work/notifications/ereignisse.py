# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignisse für das Abonnement ``benachrichtigung`` schreiben (Issue #529, ``docs/adr/20260929-ereignistechnik-postgres.md``).

Die Fachfunktionen des Work-Moduls (Aufgaben in ``apps.work.tasks.ereignisse``, Dokumente in
``apps.work.motions.ereignisse``) melden hier, was eine Benachrichtigung auslöst. Das Ereignis entsteht in
derselben Transaktion wie die Änderung; das Abonnement ``benachrichtigung`` legt daraus die
Benachrichtigungen an. Die Nutzlast nennt nur Kennungen.

Schalter ``WORK_NOTIFICATION_SUBSCRIPTION``:

- ``aus`` (Standard): kein Ereignis; die Fachfunktion benachrichtigt wie bisher selbst.
- ``schatten``: Ereignis und bisheriger Weg; das Abonnement vergleicht nur. Scheitert das Ereignis,
  bleibt die Änderung bestehen (Sicherungspunkt, Protokolleintrag).
- ``aktiv``: nur das Ereignis; Änderung und Ereignis sind atomar, die Benachrichtigung entsteht im Worker.

``melden`` liefert ``True``, wenn das Abonnement die Benachrichtigung übernimmt; dann entfällt der
direkte Aufruf. Mit ``schatten`` und ``aktiv`` muss eine Transaktion offen sein (``publish``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

from django.conf import settings
from django.db import transaction

from apps.events import CanonicalRef, publish, tenant_ref, user_ref

logger = logging.getLogger(__name__)

VERSION: Final = 1


def modus() -> str:
    return str(getattr(settings, "WORK_NOTIFICATION_SUBSCRIPTION", "aus"))


def _text(wert: Any) -> Any:
    """Kennungen als Text, Wahrheitswerte unverändert (JSON-Schema ``format: uuid`` bzw. ``boolean``)."""
    return wert if isinstance(wert, bool) else str(wert)


def _schreiben(typ: str, aggregate: CanonicalRef, organization_id: Any, actor: Any, payload: Mapping[str, Any]) -> None:
    publish(
        typ,
        version=VERSION,
        aggregate=aggregate,
        tenant=tenant_ref("org", organization_id),
        visibility="intern",
        payload=dict(payload),
        actor_ref=user_ref(actor.user_id) if actor is not None else None,
    )


def melden(typ: str, *, aggregate: CanonicalRef, organization_id: Any, actor: Any, payload: Mapping[str, Any]) -> bool:
    """Ereignis ``typ`` schreiben; ``True``, wenn der direkte Aufruf entfällt (``aktiv``).

    ``actor`` ist die auslösende Mitgliedschaft (``None``: ohne Auslöser); die Nutzlast enthält nur
    Kennungen und Wahrheitswerte, ``None`` lässt ein Feld weg.
    """
    aktuell = modus()
    if aktuell == "aus":
        return False
    werte = {name: _text(wert) for name, wert in payload.items() if wert is not None}
    if aktuell == "aktiv":
        _schreiben(typ, aggregate, organization_id, actor, werte)
        return True
    try:
        with transaction.atomic():
            _schreiben(typ, aggregate, organization_id, actor, werte)
    except Exception:  # noqa: BLE001 – im Schatten darf das Ereignis die Änderung nicht verhindern
        logger.exception("Schatten: Ereignis %s zu %s %s nicht geschrieben", typ, aggregate.type, aggregate.id)
    return False
