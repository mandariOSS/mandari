# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session als Quelle des RIS-Projektors (``hub.projections.ris_session``, Issue #536).

Der Projektor bildet Session-Objekte genau so ab, wie die OParl-Schnittstelle des Mandanten sie ausliefert – mit
denselben Querysets für das Öffentliche (``apps.session.oparl_publication``, ``visible_*``), denselben
Vorbereitungen und derselben Abbildung (``apps.session.api.oparl.LIST_SPECS``, ``hub.ris.mapping.session``). Eine
zweite Serialisierung gibt es nicht. Vor der Freischaltung der Schnittstelle (Issue #319) und bei deaktiviertem
Mandanten liefert sie nichts aus; dann liefert auch die Quelle nichts.

Die Drehscheibe importiert Session nicht: Dieses Modul reicht die Sicht als ``ris_session.Quelle`` hinein, und
``apps.session.subscribers`` registriert damit das Abonnement.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterator
from typing import Any, Final, cast

from django.db.models import QuerySet

from apps.events import Delivery
from apps.events.models import Event
from apps.session import oparl_publication
from apps.session.api import oparl as schnittstelle
from apps.session.models import (
    SessionAgendaItem,
    SessionConsultation,
    SessionFile,
    SessionMeeting,
    SessionPaper,
    SessionTenant,
)
from hub.projections import ris_session
from hub.ris.canonical import Objekt
from hub.ris.retraction import REASON_NOT_PUBLIC, REASON_WITHDRAWN

#: Die Schnittstelle des Fachmoduls ist nicht typisiert (``scripts/mypy_allowlist.txt``)
_API: Any = schnittstelle
_PUBLIKATION: Any = oparl_publication

#: Art -> Liste der Schnittstelle (Segment in ``LIST_SPECS``)
SEGMENTE: Final[dict[str, str]] = {
    "organization": "organizations",
    "person": "people",
    "membership": "memberships",
    "meeting": "meetings",
    "agendaitem": "agendaitems",
    "paper": "papers",
    "consultation": "consultations",
    "file": "files",
    "legislativeterm": "legislativeterms",
}
#: Vollbau in der Reihenfolge des Spiegels (``SessionMirror.sync``: nach der Körperschaft Gremien, Personen,
#: Sitzungen, Vorlagen; Tagesordnungspunkte, Anlagen, Beratungen und Mitgliedschaften kommen eingebettet)
VOLLBAU: Final = ("organization", "person", "meeting", "paper")
#: Objekte je Abfrage im Vollbau
STAPEL: Final = 200


class SessionQuelle:
    """Objekte eines Session-Mandanten in der Abbildung seiner OParl-Schnittstelle."""

    def __init__(self, tenant: SessionTenant) -> None:
        self.tenant = tenant
        self.mapping = _API._mapping(tenant)
        # Wie ``apps.session.api.oparl._get_tenant``: sonst antwortet jeder Endpunkt mit 404
        self.offen = bool(tenant.is_active and tenant.oparl_public_since is not None)

    @property
    def mandant(self) -> uuid.UUID:
        return self.tenant.pk

    @property
    def uebernommen(self) -> bool:
        """Wie ``insight_service.sync_publication_state``: Quelle aktiv nur bei Veröffentlichung im Bürgerportal."""
        return self.offen and bool(self.tenant.insight_publish)

    def kennung(self, adresse: str) -> uuid.UUID:
        return cast(uuid.UUID, self.mapping.uris.canonical_id(adresse))

    def adresse(self, art: str, pk: Any) -> str:
        return str(self.mapping.uris.obj(art, pk))

    def kommune(self) -> str:
        return str(self.mapping.uris.body())

    def _alle(self, art: str) -> QuerySet[Any]:
        """Alle Objekte der Art im Mandanten, öffentlich oder nicht (zum Auflösen von Kennungen)."""
        tenant = self.tenant
        if art == "meeting":
            return SessionMeeting.objects.filter(tenant=tenant)
        if art == "agendaitem":
            return SessionAgendaItem.objects.filter(meeting__tenant=tenant)
        if art == "paper":
            return SessionPaper.objects.filter(tenant=tenant)
        if art == "consultation":
            return SessionConsultation.objects.filter(paper__tenant=tenant)
        if art == "file":
            return SessionFile.objects.filter(tenant=tenant)
        raise ValueError("Unbekannte Art eines Session-Objekts.")

    def index(self, arten: Collection[str]) -> dict[uuid.UUID, tuple[str, Any]]:
        ergebnis: dict[uuid.UUID, tuple[str, Any]] = {}
        for art in sorted(arten):
            # Der Ort einer Sitzung trägt deren Schlüssel (``…/location/<Sitzung>/``)
            ziel = "meeting" if art == "location" else art
            for pk in self._alle(ziel).values_list("pk", flat=True).iterator(chunk_size=2000):
                ergebnis[self.kennung(self.adresse(art, pk))] = (ziel, pk)
        return ergebnis

    def _sichtbar(self, art: str) -> QuerySet[Any]:
        """Was die Liste der Schnittstelle ausliefert (ohne Vorbereitung)."""
        return cast(QuerySet[Any], _API.LIST_SPECS[SEGMENTE[art]][0](self.tenant))

    def _liste(self, art: str, pks: Collection[Any]) -> QuerySet[Any]:
        """Die ausgelieferten Objekte unter ``pks``, vorbereitet wie in der Schnittstelle (ohne Abfragen je Objekt)."""
        vorbereiten = _API.LIST_SPECS[SEGMENTE[art]][1]
        abfrage = self._sichtbar(art).filter(pk__in=list(pks))
        if vorbereiten is not None:
            abfrage = vorbereiten(abfrage, self.tenant)
        return abfrage.order_by("pk")

    def _abbilden(self, art: str, obj: Any) -> Objekt:
        abbilden = _API.LIST_SPECS[SEGMENTE[art]][2]
        return cast(Objekt, abbilden(self.mapping, obj))

    def objekte(self, art: str, pks: Collection[Any]) -> dict[Any, Objekt]:
        if not self.offen or not pks:
            return {}
        return {obj.pk: self._abbilden(art, obj) for obj in self._liste(art, pks)}

    def grund(self, art: str, pk: Any) -> str:
        """
        Grund einer Rücknahme wie in ``oparl_publication._withdrawal_reason``: Geht eine öffentliche Vorlage zurück in
        Entwurf oder Prüfung, nimmt die Verwaltung sie (mit Beratungen und Anlagen) zurück; sonst ist das Objekt oder
        das, woran es hängt, nichtöffentlich geworden.
        """
        if art == "paper":
            vorlage = SessionPaper.objects.filter(pk=pk).first()
        elif art == "consultation":
            vorlage = SessionPaper.objects.filter(consultations__pk=pk).first()
        elif art == "file":
            vorlage = SessionPaper.objects.filter(files__pk=pk).first()
        else:
            vorlage = None
        if vorlage is not None and vorlage.is_public and vorlage.status in _PUBLIKATION.UNVEROEFFENTLICHT:
            return REASON_WITHDRAWN
        return REASON_NOT_PUBLIC

    def vollstaendig(self) -> Iterator[tuple[str, Objekt]]:
        if not self.offen:
            return
        yield "body", cast(Objekt, self.mapping.body())
        for art in VOLLBAU:
            pks = list(self._sichtbar(art).order_by("pk").values_list("pk", flat=True))
            for start in range(0, len(pks), STAPEL):
                for obj in self._liste(art, pks[start : start + STAPEL]):
                    yield art, self._abbilden(art, obj)


def quelle_fuer(mandant: uuid.UUID) -> SessionQuelle | None:
    """Quelle eines Session-Mandanten; ``None``, wenn es ihn nicht (mehr) gibt."""
    tenant = SessionTenant.objects.filter(pk=mandant).first()
    return None if tenant is None else SessionQuelle(tenant)


def projektor(events: list[Event], delivery: Delivery) -> None:
    """Handler des Abonnements ``ris.session_projektor`` (``hub.projections.ris_session.verarbeiten``)."""
    ris_session.verarbeiten(events, delivery, quelle_fuer)
