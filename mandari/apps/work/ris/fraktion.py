# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Für die Fraktion“ auf den RIS-Seiten im neuen Erscheinungsbild (Issue #853).

Was die eigene Organisation zu einem Objekt des Ratsinformationssystems erarbeitet hat: Positionen samt Begründung
und Ergebnis (die Übergabe-Infos aus dem Beratungsverlauf), Notizen und Kommentare, Dokumente. Gelesen wird nur, was
das Mitglied auch an seinem Ort sieht:

- Positionen, Notizen und Kommentare mit ``meetings.prepare`` – dem Recht der Sitzungsvorbereitung, in der sie
  entstehen; Kommentare zusätzlich nach ihrer eigenen Sichtbarkeit (``PaperComment.is_visible_to``),
- Dokumente nach ``Motion.visible_to`` (Federführung, Entwürfe, Ordner für Vereidigte),
- Mitglieder der Organisation nur mit ``members.view``.

Immer nur Daten der eigenen Organisation; RIS-Daten nur über die Lese-Fassade ``hub.ris.selectors``. Alles liest
nur, keine Abfrage schreibt.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from django.db.models import Count, Q
from django.urls import reverse

from apps.common.permissions import PermissionChecker

#: Farbe des Punkts je Position (nur zusammen mit dem Text, Konzept Abschnitt 5)
POSITION_TON = {"for": "zustimmung", "against": "ablehnung", "abstain": "enthaltung"}


def darf(membership: Any, recht: str) -> bool:
    """Hat das Mitglied das Recht (Rollen, eigene und verweigerte Rechte wie überall in Work)?"""
    return membership is not None and bool(cast(Any, PermissionChecker)(membership).has_permission(recht))


@dataclass(frozen=True)
class Position:
    """Position der Organisation an einem Tagesordnungspunkt, wie sie die Vorbereitung festhält."""

    agenda_item_id: uuid.UUID
    code: str
    label: str
    endgueltig: bool
    ergebnis: str
    begruendung: str
    geaendert: datetime

    @property
    def ton(self) -> str:
        """„zustimmung“, „ablehnung“, „enthaltung“ oder „neutral“ für den Punkt vor dem Text."""
        return POSITION_TON.get(self.code, "neutral")


def positionen(organization: Any, agenda_item_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Position]:
    """
    Gesetzte Positionen der Organisation je Tagesordnungspunkt: nicht „Noch offen“ oder mit Ergebnis bzw. Begründung.
    Eine Abfrage; die Begründung wird entschlüsselt (sie ist für die ganze Organisation sichtbar).
    """
    from apps.work.meetings.models import AgendaItemPosition

    ids = list(agenda_item_ids)
    if not ids:
        return {}
    zeilen = (
        AgendaItemPosition.objects.filter(organization=organization, agenda_item_id__in=ids)
        .filter(~Q(position="open") | ~Q(outcome="") | Q(reasoning_encrypted__isnull=False))
        .select_related("organization")
        .order_by("updated_at")
    )
    ergebnis: dict[uuid.UUID, Position] = {}
    for zeile in zeilen:
        ergebnis[zeile.agenda_item_id] = Position(
            agenda_item_id=zeile.agenda_item_id,
            code=zeile.position,
            label=zeile.get_position_display(),
            endgueltig=zeile.is_final,
            ergebnis=zeile.get_outcome_display() if zeile.outcome else "",
            begruendung=str(cast(Any, zeile).get_reasoning_decrypted() or "").strip(),
            geaendert=zeile.updated_at,
        )
    return ergebnis


def notizen_je_punkt(organization: Any, agenda_item_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Zahl der Notizen der Organisation je Tagesordnungspunkt (Diskussion in der Vorbereitung). Eine Abfrage."""
    from apps.work.meetings.models import AgendaItemNote

    ids = list(agenda_item_ids)
    if not ids:
        return {}
    zeilen = (
        AgendaItemNote.objects.filter(
            organization=organization, agenda_item_id__in=ids, migrated_to_paper_comment__isnull=True
        )
        .values("agenda_item_id")
        .annotate(anzahl=Count("id"))
    )
    return {zeile["agenda_item_id"]: zeile["anzahl"] for zeile in zeilen}


def kommentare_zur_vorlage(paper: Any, membership: Any) -> int:
    """Kommentare zur Vorlage, die das Mitglied sehen darf (eigene Organisation bzw. beratende Gremien)."""
    from apps.work.meetings.models import PaperComment

    return len(cast(Any, PaperComment).get_visible_comments_for_paper(paper, membership))


@dataclass(frozen=True)
class Dokument:
    """Dokument der Organisation mit Bezug zu einem RIS-Objekt."""

    titel: str
    status: str
    bezug: str
    url: str
    paper_id: uuid.UUID | None


def _dokument(motion: Any, slug: str, bezug: str, paper_id: uuid.UUID | None) -> Dokument:
    return Dokument(
        titel=str(motion.title or "Dokument"),
        status=str(motion.get_status_display()),
        bezug=bezug,
        url=reverse("work:document_editor", kwargs={"org_slug": slug, "motion_id": motion.pk}),
        paper_id=paper_id,
    )


def dokumente_zu_vorlagen(
    organization: Any, membership: Any, paper_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[Dokument]]:
    """
    Dokumente der Organisation, die das Mitglied sehen darf, je Vorlage: als diese Vorlage eingereicht
    (``related_paper``) oder als Änderungsantrag dazu (``parent_paper``). Eine Abfrage für alle Vorlagen.
    """
    from apps.work.motions.models import Motion

    ids = list(paper_ids)
    if not ids or membership is None:
        return {}
    sichtbar = Motion.visible_to(membership)  # type: ignore[no-untyped-call]
    treffer = (
        sichtbar.filter(organization=organization)
        .filter(Q(related_paper_id__in=ids) | Q(parent_paper_id__in=ids))
        .order_by("-updated_at")
    )
    ergebnis: dict[uuid.UUID, list[Dokument]] = {}
    for motion in treffer:
        if motion.related_paper_id in ids:
            ergebnis.setdefault(motion.related_paper_id, []).append(
                _dokument(motion, organization.slug, "Eingereicht als diese Vorlage", motion.related_paper_id)
            )
        if motion.parent_paper_id in ids:
            ergebnis.setdefault(motion.parent_paper_id, []).append(
                _dokument(motion, organization.slug, "Änderungsantrag zu dieser Vorlage", motion.parent_paper_id)
            )
    return ergebnis


def dokumente_zur_sitzung(organization: Any, membership: Any, meeting: Any) -> list[Dokument]:
    """Dokumente der Organisation, die sich auf die Sitzung beziehen (``related_meeting``) und sichtbar sind."""
    from apps.work.motions.models import Motion

    if membership is None:
        return []
    sichtbar = Motion.visible_to(membership)  # type: ignore[no-untyped-call]
    treffer = sichtbar.filter(organization=organization, related_meeting=meeting).order_by("-updated_at")[:10]
    return [_dokument(motion, organization.slug, "Bezieht sich auf diese Sitzung", None) for motion in treffer]


def eingereichte_dokumente(organization: Any, membership: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    """Zuletzt geänderte Dokumente mit Vorlage im RIS (eingereicht), die das Mitglied sehen darf."""
    from apps.work.motions.models import Motion

    if membership is None:
        return []
    sichtbar = Motion.visible_to(membership)  # type: ignore[no-untyped-call]
    treffer = (
        sichtbar.filter(organization=organization, related_paper__isnull=False)
        .select_related("related_paper")
        .order_by("-updated_at")[:limit]
    )
    slug = organization.slug
    return [
        {
            "titel": str(motion.title or "Dokument"),
            "url": reverse("work:document_editor", kwargs={"org_slug": slug, "motion_id": motion.pk}),
            "vorlage": motion.related_paper.reference or motion.related_paper.name or "Vorlage",
            "vorlage_url": reverse(
                "work:ris_paper_detail", kwargs={"org_slug": slug, "paper_id": motion.related_paper_id}
            ),
        }
        for motion in treffer
    ]


def mitglieder_je_person(organization: Any, person_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    """
    Aktive Mitglieder der Organisation, deren Konto mit einer Person des RIS verknüpft ist (``oparl_person``):
    Person → Name im Konto. Eine Abfrage.
    """
    from apps.tenants.models import Membership

    ids = list(person_ids)
    if not ids:
        return {}
    treffer = Membership.objects.filter(
        organization=organization, is_active=True, oparl_person_id__in=ids
    ).select_related("user")
    return {m.oparl_person_id: str(m.user.get_display_name()) for m in treffer if m.oparl_person_id}
