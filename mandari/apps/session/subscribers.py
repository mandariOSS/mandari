# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnements mit Session als Quelle (``apps.events.registry.load_subscribers`` importiert dieses Modul).

``ris.session_projektor`` (``hub.projections.ris_session``, Quelle ``apps.session.ris_projektion``) wird nur
registriert, wenn ``RIS_SESSION_PROJECTOR`` nicht ``aus`` ist. Ein neu angelegtes Abonnement startet im
Schattenbetrieb am Ende des Journals; den Stand davor schreibt ``manage.py ris_projektor_schatten aufbauen``. Es
schreibt nur die Schatten-Quelle, auch wenn es in der Datenbank auf ``aktiv`` steht (Bestand erst mit #537).
"""

from __future__ import annotations

from apps.events import subscriber
from apps.session import ris_projektion
from hub.projections import ris_session


def register() -> bool:
    """Registriert ``ris.session_projektor`` je nach Schalter; ``True``, wenn registriert."""
    if ris_session.modus() == ris_session.AUS:
        return False
    subscriber(
        ris_session.NAME,
        types=ris_session.TYPES,
        batch=ris_session.BATCH,
        queue=ris_session.QUEUE,
        transactional=True,
        shadow=True,
    )(ris_projektion.projektor)
    return True


register()
