# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bezug der eigenen Organisation für die Gewichtung der Recherche (Issue #853).

Die Suche in Work stellt Treffer nach vorn, mit denen die Fraktion arbeitet. Der Bezug ändert nur die Reihenfolge,
nie die Treffermenge (``insight_core.services.search_ranking.RelationBoost``), und er stützt sich ausschließlich auf
Daten der eigenen Organisation, die das Mitglied auch sehen darf:

* **Eigener Antrag** – Dokumente der Organisation, die mit einem Vorgang verknüpft sind (eingereichter Antrag oder
  Änderungsantrag zu einer Vorlage), nur soweit das Mitglied das Dokument sehen darf (``Motion.visible_to``).
* **Von der Fraktion bearbeitet** – Vorgänge mit einer Position der Fraktion (nicht „noch offen“), mit Notizen oder
  Kommentaren der Organisation (private nur der eigenen Person) oder als verknüpfte Vorlage eines öffentlichen TOPs
  einer Fraktionssitzung. Nichtöffentliche TOPs zählen nicht: Ihr Bezug darf über die Reihenfolge nichts verraten.
* **Vorbereitete Sitzung** – Sitzungen, zu denen die Organisation eine Vorbereitung angelegt hat.
* **Bald auf der Tagesordnung** (Aktualität) – Vorgänge, die in den nächsten vier Wochen in einer Sitzung der eigenen
  Kommunen beraten werden; was gerade ansteht, zählt für eine Fraktion mehr als das Datum allein.
* **Gremien** – „Meine Gremien“ des Mitglieds (gefolgt, sonst zugewiesen); ohne eigene die zugewiesenen Gremien aller
  aktiven Mitglieder der Organisation. Die Vertretung selbst (Rat, Kreistag …) zählt nicht: Sie berät fast jeden
  Vorgang, ihr Bezug würde nichts unterscheiden.

Inhalte (Notizen, Positionen, Anträge) verlassen dabei nie die Datenbank: Gezählt wird nur, *dass* es sie gibt.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, cast

from django.db.models import Q
from django.utils import timezone

from hub.ris import selectors as ris
from insight_core.services.search_ranking import RelationBoost

#: Faktoren auf die Relevanz je Index (vor der Rangfusion); so bleibt die Textrelevanz maßgeblich
FAKTOR_EIGENER_ANTRAG: Final = 1.5
FAKTOR_BEARBEITET: Final = 1.3
FAKTOR_SITZUNG_VORBEREITET: Final = 1.2
FAKTOR_ANSTEHEND: Final = 1.3
FAKTOR_GREMIUM: Final = 1.15
#: Zeitraum „bald auf der Tagesordnung“
ANSTEHEND_TAGE: Final = 28
#: Einordnung (OParl ``classification``) der Vertretungen, die nicht als eigenes Gremium zählen
VERTRETUNGEN: Final = (
    "rat",
    "stadtrat",
    "gemeinderat",
    "kreistag",
    "bezirkstag",
    "stadtverordnetenversammlung",
    "gemeindevertretung",
    "bürgerschaft",
    "regionalrat",
    "landschaftsversammlung",
    "verbandsversammlung",
)
#: Obergrenze je Quelle (neueste zuerst), damit die Abfrage an den Suchdienst klein bleibt
MAX_KENNUNGEN: Final = 2000

GREMIEN_MEINE: Final = "meine"
GREMIEN_FRAKTION: Final = "fraktion"


@dataclass(frozen=True)
class Fraktionsbezug:
    """Kennungen und Gremien mit eigenem Bezug; ``boost()`` für den Suchdienst, ``text()`` für die Trefferliste."""

    eigene_antraege: frozenset[str] = frozenset()
    bearbeitet: frozenset[str] = frozenset()
    sitzungen: frozenset[str] = frozenset()
    anstehend: frozenset[str] = frozenset()
    gremien: tuple[str, ...] = ()
    gremien_quelle: str = ""

    def boost(self) -> RelationBoost:
        return RelationBoost(
            papers=(
                (self.eigene_antraege, FAKTOR_EIGENER_ANTRAG),
                (self.bearbeitet, FAKTOR_BEARBEITET),
                (self.anstehend, FAKTOR_ANSTEHEND),
            ),
            meetings=((self.sitzungen, FAKTOR_SITZUNG_VORBEREITET),),
            committees=self.gremien,
            committee_factor=FAKTOR_GREMIUM,
        )

    def text(self, kind: str, pk: str, gremien: Iterable[str] = ()) -> str:
        """Kurzer Satz, warum ein Treffer nach vorn rückt („Eigener Antrag · In Ihren Gremien“), sonst leer."""
        teile: list[str] = []
        if kind == "vorgang" and pk in self.eigene_antraege:
            teile.append("Eigener Antrag")
        elif kind == "vorgang" and pk in self.bearbeitet:
            teile.append("Von der Fraktion bearbeitet")
        elif kind == "sitzung" and pk in self.sitzungen:
            teile.append("Von der Fraktion vorbereitet")
        if self.gremien and set(gremien) & set(self.gremien):
            teile.append("In Ihren Gremien" if self.gremien_quelle == GREMIEN_MEINE else "In Gremien der Fraktion")
        return " · ".join(teile)

    def faktor(self, kind: str, pk: str) -> float:
        """Faktor eines Treffers ohne Gremien (für die Reihenfolge der Datenbanksuche ohne Suchdienst)."""
        if kind == "vorgang":
            faktor = FAKTOR_EIGENER_ANTRAG if pk in self.eigene_antraege else 1.0
            faktor *= FAKTOR_BEARBEITET if pk in self.bearbeitet else 1.0
            return faktor * (FAKTOR_ANSTEHEND if pk in self.anstehend else 1.0)
        if kind == "sitzung" and pk in self.sitzungen:
            return FAKTOR_SITZUNG_VORBEREITET
        return 1.0


def _kennungen(werte: Iterable[Any]) -> set[str]:
    return {str(wert) for wert in werte if wert}


def fraktionsbezug(organization: Any, membership: Any, bodies: Any = None) -> Fraktionsbezug:
    """Bezug der Organisation aus Sicht dieses Mitglieds; Gäste und fehlende Mitgliedschaft ergeben keinen Bezug.

    ``bodies`` (Kommunen der Organisation) braucht nur „bald auf der Tagesordnung“; ohne sie entfällt dieser Teil.
    """
    if organization is None or membership is None or getattr(membership, "is_guest", False):
        return Fraktionsbezug()
    gremien, quelle = _gremien(organization, membership)
    anstehend: set[str] = set()
    if bodies is not None:
        bis = timezone.now() + timedelta(days=ANSTEHEND_TAGE)
        anstehend = _kennungen(ris.paper_ids_on_upcoming_agendas(bodies, until=bis, limit=MAX_KENNUNGEN))
    return Fraktionsbezug(
        eigene_antraege=frozenset(_eigene_antraege(membership)),
        bearbeitet=frozenset(_bearbeitet(organization, membership)),
        sitzungen=frozenset(_vorbereitete_sitzungen(organization)),
        anstehend=frozenset(anstehend),
        gremien=gremien,
        gremien_quelle=quelle,
    )


def _eigene_antraege(membership: Any) -> set[str]:
    from apps.work.motions.models import Motion

    zeilen = (
        cast(Any, Motion)
        .visible_to(membership)
        .filter(Q(related_paper__isnull=False) | Q(parent_paper__isnull=False))
        .order_by("-updated_at")
        .values_list("related_paper_id", "parent_paper_id")[:MAX_KENNUNGEN]
    )
    return _kennungen(kennung for zeile in zeilen for kennung in zeile)


def _bearbeitet(organization: Any, membership: Any) -> set[str]:
    from apps.work.faction.models import FactionAgendaItem
    from apps.work.meetings.models import AgendaItemNote, AgendaItemPosition, AgendaPrivateNote, PaperComment

    tops: set[Any] = set()
    tops.update(
        AgendaItemPosition.objects.filter(organization=organization)
        .exclude(position__in=("open", ""))
        .order_by("-updated_at")
        .values_list("agenda_item_id", flat=True)[:MAX_KENNUNGEN]
    )
    tops.update(
        AgendaItemNote.objects.filter(organization=organization)
        .order_by("-created_at")
        .values_list("agenda_item_id", flat=True)[:MAX_KENNUNGEN]
    )
    tops.update(
        AgendaPrivateNote.objects.filter(organization=organization, author=membership)
        .order_by("-created_at")
        .values_list("agenda_item_id", flat=True)[:MAX_KENNUNGEN]
    )
    oeffentlich = Q(visibility="public") & (Q(parent__isnull=True) | Q(parent__visibility="public"))
    fraktion = FactionAgendaItem.objects.filter(oeffentlich, meeting__organization=organization)
    tops.update(
        fraktion.exclude(related_agenda_item__isnull=True)
        .order_by("-meeting__start")
        .values_list("related_agenda_item_id", flat=True)[:MAX_KENNUNGEN]
    )
    vorgaenge = _kennungen(ris.paper_ids_of_agenda_items(tops))
    vorgaenge |= _kennungen(
        PaperComment.objects.filter(organization=organization)
        .filter(~Q(visibility="private") | Q(author=membership))
        .order_by("-created_at")
        .values_list("paper_id", flat=True)[:MAX_KENNUNGEN]
    )
    vorgaenge |= _kennungen(
        FactionAgendaItem.related_papers.through.objects.filter(
            factionagendaitem__in=fraktion.values("pk")
        ).values_list("oparlpaper_id", flat=True)[:MAX_KENNUNGEN]
    )
    return vorgaenge


def _vorbereitete_sitzungen(organization: Any) -> set[str]:
    from apps.work.meetings.models import MeetingPreparation

    return _kennungen(
        MeetingPreparation.objects.filter(organization=organization)
        .order_by("-created_at")
        .values_list("meeting_id", flat=True)[:MAX_KENNUNGEN]
    )


def _gremien(organization: Any, membership: Any) -> tuple[tuple[str, ...], str]:
    from apps.tenants.models import Membership
    from apps.work.organization.selectors import my_committees

    meine = ris.organization_names(
        [c.pk for c in my_committees(membership).committees], exclude_classifications=VERTRETUNGEN
    )
    if meine:
        return tuple(meine), GREMIEN_MEINE
    kennungen = Membership.oparl_committees.through.objects.filter(
        membership__organization=organization, membership__is_active=True, membership__is_guest=False
    ).values_list("oparlorganization_id", flat=True)
    namen = ris.organization_names(set(kennungen), exclude_classifications=VERTRETUNGEN)
    return (tuple(namen), GREMIEN_FRAKTION) if namen else ((), "")
