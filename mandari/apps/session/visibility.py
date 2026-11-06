# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sichtbarkeit nichtöffentlicher Inhalte im Session RIS – eine Regel für alle Ansichten.

Listen, Auswahlfelder, Suche, Detailseiten, Aktionen und Arbeitsvorräte filtern über diese Bausteine
statt über eigene ``is_public``-Bedingungen:

- ``SessionMeeting.objects.visible_to(permissions)``: nichtöffentliche Sitzungen nur mit
  ``view_non_public_meetings``
- ``SessionAgendaItem.objects.visible_to(permissions)``: nichtöffentliche TOPs und alle TOPs
  nichtöffentlicher Sitzungen nur mit ``view_non_public_meetings``
- ``SessionPaper.objects.visible_to(permissions)``: nichtöffentliche Vorlagen nur mit
  ``view_non_public_papers``
- ``SessionFile.objects.visible_to(permissions)``: Gegenstück zu ``file_service.file_visible`` –
  Sichtrecht des Elternobjekts (Vorlage: ``view_papers``, Sitzung/TOP: ``view_meetings``, Anhang
  eines eingereichten Antrags: ``view_applications``), bei einer nichtöffentlichen Anlage oder einem
  nichtöffentlichen Elternobjekt zusätzlich das NÖ-Recht

Für Beziehungen liefern ``meeting_q``, ``agenda_item_q``, ``paper_q`` und ``file_q`` dieselbe
Bedingung mit Präfix, z. B. ``SessionCosignature.objects.filter(paper_q(permissions, "paper__"))``.

``permissions`` ist die Menge der Rechte einer Person (``SessionPermissionChecker.permissions``).
Die Basis-Sichtrechte für Sitzungen und Vorlagen prüfen die Views selbst; nur bei Anlagen, deren
Elternobjekte verschiedene Rechte verlangen, gehören sie zur Regel.

``Sicht`` (Issue #565) baut darauf auf: dieselbe Regel für die nichtöffentliche Ausgabe der Schnittstelle (Profil
Nichtöffentliches von kommParl), erweitert um Rechte mit Geltungsbereich, befristete Zuweisungen und das
Mitwirkungsverbot (``sicht_fuer_person``), und für Dienste, die an einen Mandanten gebunden sind
(``sicht_fuer_dienst``). Die Oberfläche stellt mit #773 auf dieselbe zentrale Prüfung um.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from django.db import models
from django.db.models import Q

if TYPE_CHECKING:
    # Nur für die Typparameter der QuerySets unten (als Zeichenkette, sonst zirkulärer Import)
    from .models import SessionAgendaItem, SessionFile, SessionMeeting, SessionPaper  # noqa: F401

NON_PUBLIC_MEETINGS = "view_non_public_meetings"
NON_PUBLIC_PAPERS = "view_non_public_papers"


def meeting_q(permissions: Collection[str], prefix: str = "") -> Q:
    """Sitzungen: nichtöffentliche nur mit dem NÖ-Sichtrecht für Sitzungen."""
    if NON_PUBLIC_MEETINGS in permissions:
        return Q()
    return Q(**{f"{prefix}is_public": True})


def agenda_item_q(permissions: Collection[str], prefix: str = "") -> Q:
    """TOPs: nichtöffentliche TOPs und TOPs nichtöffentlicher Sitzungen nur mit dem NÖ-Sichtrecht."""
    if NON_PUBLIC_MEETINGS in permissions:
        return Q()
    return Q(**{f"{prefix}is_public": True, f"{prefix}meeting__is_public": True})


def paper_q(permissions: Collection[str], prefix: str = "") -> Q:
    """Vorlagen: nichtöffentliche nur mit dem NÖ-Sichtrecht für Vorlagen."""
    if NON_PUBLIC_PAPERS in permissions:
        return Q()
    return Q(**{f"{prefix}is_public": True})


def optional(condition: Q, relation: str) -> Q:
    """
    Optionale Beziehung: leer oder sichtbar. Eine leere Bedingung (volles Recht) bleibt leer –
    ``Q() | Q(x__isnull=True)`` ergäbe in Django sonst nur ``x__isnull=True``.
    """
    if not condition:
        return Q()
    return condition | Q(**{f"{relation}__isnull": True})


def file_q(permissions: Collection[str], prefix: str = "") -> Q:
    """
    Anlagen nach der Regel von ``file_service.file_visible``.

    Das Elternobjekt bestimmt das Recht in derselben Reihenfolge wie ``file_service.file_parent``:
    Vorlage vor TOP vor Sitzung vor eingereichtem Antrag; Anlagen ohne Elternobjekt zählen wie
    Vorlagen-Anlagen. Anhänge eines Antrags (noch ohne Vorlage) sind intern und nur mit
    ``view_applications`` sichtbar – unabhängig von ``is_public`` (#584).
    """

    def cond(**lookups: Any) -> Q:
        return Q(**{f"{prefix}{key}": value for key, value in lookups.items()})

    parts: list[Q] = []
    if "view_papers" in permissions:
        at_paper = cond(paper__isnull=False)
        orphan = cond(paper__isnull=True, agenda_item__isnull=True, meeting__isnull=True, application__isnull=True)
        if NON_PUBLIC_PAPERS not in permissions:
            at_paper &= cond(is_public=True, paper__is_public=True)
            orphan &= cond(is_public=True)
        parts += [at_paper, orphan]
    if "view_meetings" in permissions:
        at_item = cond(paper__isnull=True, agenda_item__isnull=False)
        at_meeting = cond(paper__isnull=True, agenda_item__isnull=True, meeting__isnull=False)
        if NON_PUBLIC_MEETINGS not in permissions:
            at_item &= cond(is_public=True, agenda_item__is_public=True, agenda_item__meeting__is_public=True)
            at_meeting &= cond(is_public=True, meeting__is_public=True)
        parts += [at_item, at_meeting]
    if "view_applications" in permissions:
        parts.append(
            cond(paper__isnull=True, agenda_item__isnull=True, meeting__isnull=True, application__isnull=False)
        )
    if not parts:
        return cond(pk__in=[])
    result = parts[0]
    for part in parts[1:]:
        result |= part
    return result


def meeting_visible(permissions: Collection[str], meeting: Any) -> bool:
    """Einzelne Sitzung nach :func:`meeting_q` (für bereits geladene Objekte)."""
    return NON_PUBLIC_MEETINGS in permissions or bool(meeting.is_public)


def agenda_item_visible(permissions: Collection[str], item: Any) -> bool:
    """Einzelner TOP nach :func:`agenda_item_q` (für bereits geladene Objekte)."""
    return NON_PUBLIC_MEETINGS in permissions or bool(item.is_public and item.meeting.is_public)


def paper_visible(permissions: Collection[str], paper: Any) -> bool:
    """Einzelne Vorlage nach :func:`paper_q` (für bereits geladene Objekte)."""
    return NON_PUBLIC_PAPERS in permissions or bool(paper.is_public)


class MeetingQuerySet(models.QuerySet["SessionMeeting"]):
    def visible_to(self, permissions: Collection[str]) -> MeetingQuerySet:
        return self.filter(meeting_q(permissions))


class AgendaItemQuerySet(models.QuerySet["SessionAgendaItem"]):
    def visible_to(self, permissions: Collection[str]) -> AgendaItemQuerySet:
        return self.filter(agenda_item_q(permissions))


class PaperQuerySet(models.QuerySet["SessionPaper"]):
    def visible_to(self, permissions: Collection[str]) -> PaperQuerySet:
        return self.filter(paper_q(permissions))


class FileQuerySet(models.QuerySet["SessionFile"]):
    def visible_to(self, permissions: Collection[str]) -> FileQuerySet:
        return self.filter(file_q(permissions))


# =============================================================================
# Sicht einer Person oder eines Dienstes (Profil Nichtöffentliches von kommParl, Issue #565)
# =============================================================================

#: Vorlagen im Entwurf oder in der Prüfung sind Arbeitsstände der Verwaltung; die Sicht gibt sie nicht aus
ARBEITSSTAENDE = ("draft", "review")


@dataclass(frozen=True)
class Sicht:
    """
    Was eine Person oder ein Dienst an Nichtöffentlichem eines Mandanten sieht: die Regel der Oberfläche
    (``meeting_q``, ``agenda_item_q``, ``paper_q``, ``file_q`` mit den Rechten aus ``SessionPermissionChecker``),
    erweitert um Rechte mit Geltungsbereich und befristete Zuweisungen aus dem Zugriffskontext (``apps.session.rechte``,
    nur bei eingeschaltetem Schalter des Mandanten) und um das Mitwirkungsverbot.

    - ``alles``: ein Dienst, der an diesen Mandanten gebunden ist, sieht alles Nichtöffentliche
    - ``permissions``: bisherige Rechtenamen der Person, wie die Oberfläche sie prüft, dazu die Häkchen aus befristeten
      mandantenweiten Zuweisungen
    - ``sitzung_gremien``: Gremien, in denen ``sitzung.sehen`` und ``sitzung.noe_sehen`` mit Geltungsbereich gelten
    - ``vorlage_koerperschaften``, ``vorlage_aemter``, ``vorlage_gremien``: Geltungsbereiche von ``vorlage.sehen`` und
      ``vorlage.noe_sehen`` (Körperschaft der Vorlage, federführendes Amt, Gremien der Beratungsfolge)
    - ``gesperrte_tops``: Tagesordnungspunkte, bei denen die Person als befangen vermerkt ist (Mitwirkungsverbot); sie
      und ihre Anlagen sieht sie nur, soweit sie öffentlich sind

    Arbeitsstände (Vorlagen im Entwurf oder in der Prüfung) zeigt die Sicht nie. Die Q-Ausdrücke filtern Listen, die
    Prüfungen auf einzelnen, schon geladenen Objekten dienen der Abbildung (``hub.ris.mapping.session.SessionSicht``).
    Beide folgen derselben Regel.
    """

    alles: bool = False
    permissions: frozenset[str] = frozenset()
    sitzung_gremien: frozenset[str] = frozenset()
    vorlage_koerperschaften: frozenset[str] = frozenset()
    vorlage_aemter: frozenset[str] = frozenset()
    vorlage_gremien: frozenset[str] = frozenset()
    gesperrte_tops: frozenset[str] = frozenset()

    # -- Rechte ------------------------------------------------------------------------------------------------

    @property
    def alle_sitzungen(self) -> bool:
        """Nichtöffentliches aller Sitzungen des Mandanten (wie in der Oberfläche: Sitzungen sehen und NÖ-Recht)."""
        return self.alles or {"view_meetings", NON_PUBLIC_MEETINGS} <= self.permissions

    @property
    def alle_vorlagen(self) -> bool:
        """Nichtöffentliche Vorlagen des Mandanten (wie in der Oberfläche: Vorlagen sehen und NÖ-Recht)."""
        return self.alles or {"view_papers", NON_PUBLIC_PAPERS} <= self.permissions

    # -- Listenfilter --------------------------------------------------------------------------------------------

    def sitzung_noe_q(self, prefix: str = "") -> Q:
        """Sitzungen, deren nichtöffentliche Inhalte die Sicht zeigt (gleich, ob die Sitzung öffentlich ist)."""
        if self.alle_sitzungen:
            return Q()
        if not self.sitzung_gremien:
            return Q(**{f"{prefix}pk__in": []})
        from .models import SessionMeeting

        gremien = sorted(self.sitzung_gremien)
        gemeinsam = SessionMeeting.joint_organizations.through.objects.filter(sessionorganization_id__in=gremien)
        return Q(**{f"{prefix}organization_id__in": gremien}) | Q(
            **{f"{prefix}pk__in": gemeinsam.values("sessionmeeting_id")}
        )

    def meeting_q(self, prefix: str = "") -> Q:
        """Sitzungen der Sicht: öffentliche, nichtöffentliche mit veröffentlichtem Termin und die der NÖ-Rechte."""
        if self.alle_sitzungen:
            return Q()
        return Q(**{f"{prefix}is_public": True}) | Q(**{f"{prefix}date_public": True}) | self.sitzung_noe_q(prefix)

    def top_noe_q(self, prefix: str = "") -> Q:
        """Tagesordnungspunkte, deren nichtöffentliche Inhalte die Sicht zeigt (ohne Mitwirkungsverbot)."""
        bedingung = _oder_alles(self.sitzung_noe_q(f"{prefix}meeting__"))
        if self.gesperrte_tops:
            bedingung &= ~Q(**{f"{prefix}pk__in": sorted(self.gesperrte_tops)})
        return bedingung

    def agenda_item_q(self, prefix: str = "") -> Q:
        """Tagesordnungspunkte der Sicht: öffentliche öffentlicher Sitzungen und die der NÖ-Rechte."""
        oeffentlich = Q(**{f"{prefix}is_public": True, f"{prefix}meeting__is_public": True})
        return oeffentlich | self.top_noe_q(prefix)

    def vorlage_noe_q(self, prefix: str = "") -> Q:
        """Vorlagen, deren nichtöffentliche Inhalte die Sicht zeigt (ohne Arbeitsstände)."""
        kein_arbeitsstand = ~Q(**{f"{prefix}status__in": ARBEITSSTAENDE})
        if self.alle_vorlagen:
            return kein_arbeitsstand
        teile: list[Q] = []
        if self.vorlage_koerperschaften:
            teile.append(Q(**{f"{prefix}body_id__in": sorted(self.vorlage_koerperschaften)}))
        if self.vorlage_aemter:
            teile.append(Q(**{f"{prefix}lead_department_id__in": sorted(self.vorlage_aemter)}))
        if self.vorlage_gremien:
            from .models import SessionConsultation

            beratungen = SessionConsultation.objects.filter(organization_id__in=sorted(self.vorlage_gremien))
            teile.append(Q(**{f"{prefix}pk__in": beratungen.values("paper_id")}))
        if not teile:
            return Q(**{f"{prefix}pk__in": []})
        bedingung = teile[0]
        for teil in teile[1:]:
            bedingung |= teil
        return bedingung & kein_arbeitsstand

    def paper_q(self, prefix: str = "") -> Q:
        """Vorlagen der Sicht: veröffentlichte und die der NÖ-Rechte, nie Arbeitsstände."""
        veroeffentlicht = Q(**{f"{prefix}is_public": True}) & ~Q(**{f"{prefix}status__in": ARBEITSSTAENDE})
        return veroeffentlicht | self.vorlage_noe_q(prefix)

    def file_q(self, prefix: str = "") -> Q:
        """
        Anlagen der Sicht, nach dem Bezugsobjekt in der Reihenfolge von ``file_service.file_parent`` (Vorlage vor TOP
        vor Sitzung): öffentliche Anlagen öffentlicher Bezugsobjekte und Anlagen, deren Bezugsobjekt die Sicht mit
        seinen nichtöffentlichen Inhalten zeigt. Anhänge von Anträgen und Anlagen ohne Bezugsobjekt nie; die
        öffentliche Fassung einer Niederschrift nur, solange sie veröffentlicht ist.
        """

        def cond(**lookups: Any) -> Q:
            return Q(**{f"{prefix}{key}": value for key, value in lookups.items()})

        an_vorlage = cond(paper__isnull=False) & (
            (cond(is_public=True, paper__is_public=True) & ~cond(paper__status__in=ARBEITSSTAENDE))
            | self.vorlage_noe_q(f"{prefix}paper__")
        )
        an_top = cond(paper__isnull=True, agenda_item__isnull=False) & (
            cond(is_public=True, agenda_item__is_public=True, agenda_item__meeting__is_public=True)
            | self.top_noe_q(f"{prefix}agenda_item__")
        )
        an_sitzung = cond(paper__isnull=True, agenda_item__isnull=True, meeting__isnull=False) & (
            cond(is_public=True, meeting__is_public=True) | _oder_alles(self.sitzung_noe_q(f"{prefix}meeting__"))
        )
        unveroeffentlichte_fassung = cond(public_protocol__isnull=False) & ~cond(public_protocol__status="published")
        return (an_vorlage | an_top | an_sitzung) & ~unveroeffentlichte_fassung

    # -- Prüfungen geladener Objekte (für die Abbildung) ---------------------------------------------------------

    def sitzung_noe(self, meeting: Any) -> bool:
        """Zeigt die Sicht die nichtöffentlichen Inhalte dieser Sitzung?"""
        if self.alle_sitzungen:
            return True
        if not self.sitzung_gremien:
            return False
        return any(str(org_id) in self.sitzung_gremien for org_id in meeting.participating_organization_ids)

    def top_noe(self, item: Any) -> bool:
        """Zeigt die Sicht die nichtöffentlichen Inhalte dieses Tagesordnungspunkts (kein Mitwirkungsverbot)?"""
        return str(item.pk) not in self.gesperrte_tops and self.sitzung_noe(item.meeting)

    def vorlage_noe(self, paper: Any) -> bool:
        """Zeigt die Sicht diese Vorlage mit ihren nichtöffentlichen Inhalten (nie Arbeitsstände)?"""
        if paper.status in ARBEITSSTAENDE:
            return False
        if self.alle_vorlagen:
            return True
        if str(paper.body_id) in self.vorlage_koerperschaften or str(paper.lead_department_id) in self.vorlage_aemter:
            return True
        if self.vorlage_gremien:
            return any(str(c.organization_id) in self.vorlage_gremien for c in paper.consultations.all())
        return False

    def anlage_noe(self, file_obj: Any) -> bool:
        """Zeigt die Sicht diese Anlage über die nichtöffentlichen Inhalte ihres Bezugsobjekts?"""
        if getattr(file_obj, "public_protocol_id", None) is not None:
            return False  # öffentliche Fassung einer Niederschrift: nur nach der öffentlichen Regel
        if file_obj.paper_id:
            return self.vorlage_noe(file_obj.paper)
        if file_obj.agenda_item_id:
            return self.top_noe(file_obj.agenda_item)
        if file_obj.meeting_id:
            return self.sitzung_noe(file_obj.meeting)
        return False


def _oder_alles(bedingung: Q) -> Q:
    """Eine leere Bedingung heißt „alle“; in einer Oder-Verknüpfung ginge sie verloren, deshalb ausdrücklich."""
    return bedingung if bedingung else Q(pk__isnull=False)


def sicht_fuer_dienst() -> Sicht:
    """Sicht eines Dienstes, der an den Mandanten gebunden ist: alles Nichtöffentliche außer Arbeitsständen."""
    return Sicht(alles=True)


def sicht_fuer_person(session_user: Any, *, tag: date | None = None) -> Sicht:
    """
    Sicht einer Person im Mandanten ihres Kontos (``SessionUser``), bei jedem Abruf neu berechnet.

    - Rechte wie in der Oberfläche (``SessionPermissionChecker``), dazu die Häkchen aus befristeten mandantenweiten
      Zuweisungen und die Geltungsbereiche aus dem Zugriffskontext (nur bei eingeschaltetem Schalter des Mandanten;
      abgelaufene Zuweisungen wirken nicht)
    - Mitwirkungsverbot: :func:`mitwirkungsverbote`
    """
    from .permissions import SessionPermissionChecker
    from .rechte import aufloesung, katalog
    from .rechte.bereiche import AMT, GREMIUM, KOERPERSCHAFT, bereich

    permissions = set(SessionPermissionChecker(session_user).permissions)
    kontext = aufloesung.zugriffskontext(session_user, tag=tag)
    permissions |= katalog.haekchen_aus_rechten(kontext.mandantenweit)

    def bereiche(recht: str, grundrecht: str, art: str) -> frozenset[str]:
        return frozenset(
            kennung for kennung in kontext.kennungen(recht, art) if kontext.gilt_in(grundrecht, [bereich(art, kennung)])
        )

    return Sicht(
        permissions=frozenset(permissions),
        sitzung_gremien=bereiche("sitzung.noe_sehen", "sitzung.sehen", GREMIUM),
        vorlage_koerperschaften=bereiche("vorlage.noe_sehen", "vorlage.sehen", KOERPERSCHAFT),
        vorlage_aemter=bereiche("vorlage.noe_sehen", "vorlage.sehen", AMT),
        vorlage_gremien=bereiche("vorlage.noe_sehen", "vorlage.sehen", GREMIUM),
        gesperrte_tops=mitwirkungsverbote(session_user),
    )


def mitwirkungsverbote(session_user: Any) -> frozenset[str]:
    """
    Tagesordnungspunkte, bei denen eine Person des Kontos als befangen vermerkt ist (festgestelltes Mitwirkungsverbot,
    ``SessionVote`` mit ``excluded``).

    Session verknüpft Konten und Personen noch nicht fest (#761). Als Person des Kontos gilt deshalb jede Person des
    Mandanten mit derselben E-Mail-Adresse (ohne Groß- und Kleinschreibung) und die über ``SessionUser.oparl_person``
    verknüpfte. Die Sperre schränkt nur ein: Mehr Treffer sperren mehr, sie geben nie Zugriff.
    """
    from .models import SessionPerson, SessionVote

    email = str(getattr(session_user.user, "email", "") or "").strip()
    personen = Q(email__iexact=email) if email else Q(pk__in=[])
    if session_user.oparl_person_id is not None:
        personen |= Q(oparl_person_id=session_user.oparl_person_id)
    kandidaten = SessionPerson.objects.filter(personen, tenant_id=session_user.tenant_id).values("pk")
    tops = SessionVote.objects.filter(vote="excluded", person_id__in=kandidaten).values_list(
        "agenda_item_id", flat=True
    )
    return frozenset(str(pk) for pk in tops)
