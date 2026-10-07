# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lesezugriffe auf die Live-Übertragungen (Issue #915).

- ``live_stand(meeting_id)``: alles für die öffentliche Live-Seite in Insight (Sitzung, Status, Player, jetzt
  laufender TOP, wer am Wort ist, Verlauf). ``None``, wenn es für kein Gremium der Sitzung eine Quelle gibt.
- ``wortmeldung(speech_id)``: Angaben einer Wortmeldung für Abonnenten von ``ris.broadcast.speaker_changed``
  (z. B. die Fraktionszuordnung, Issue #916): Person, Kommune, gelesene Fraktion bzw. Funktion, Zuordnung und Zahl
  gleicher Lesungen.

Personenseiten werden nur bei eindeutiger Zuordnung verlinkt; sonst steht nur der gelesene Name da.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from django.conf import settings

from insight_core.models import OParlConsultation, OParlMeeting

from .anbieter import anbieter
from .models import Broadcast, BroadcastSection, BroadcastSource, BroadcastSpeech, BroadcastStatus, SpeechAssignment


@dataclass(frozen=True)
class WortmeldungStand:
    name: str
    fraktion: str
    funktion: str
    person_id: uuid.UUID | None
    begonnen_am: datetime


@dataclass(frozen=True)
class AbschnittStand:
    nummer: str
    titel: str
    agenda_item_id: uuid.UUID | None
    paper_id: uuid.UUID | None
    begonnen_am: datetime
    beendet_am: datetime | None
    wortmeldungen: list[WortmeldungStand] = field(default_factory=list)


@dataclass(frozen=True)
class LiveStand:
    meeting: OParlMeeting
    gremium: str
    status: str
    begonnen_am: datetime | None
    beendet_am: datetime | None
    einbettung_url: str | None
    seiten_url: str
    anbieter_name: str
    jetzt_abschnitt: AbschnittStand | None
    am_wort: WortmeldungStand | None
    verlauf: list[AbschnittStand]
    #: Wortmeldungen vor dem ersten erkannten TOP
    ohne_abschnitt: list[WortmeldungStand]

    @property
    def laeuft(self) -> bool:
        return self.status == BroadcastStatus.LIVE


def quelle_fuer_sitzung(meeting: OParlMeeting) -> BroadcastSource | None:
    """Quelle eines Gremiums der Sitzung in derselben Kommune (aktive zuerst)."""
    return (
        BroadcastSource.objects.filter(body_id=meeting.body_id, organization__in=meeting.organizations.all())
        .select_related("organization")
        .order_by("-active", "created_at")
        .first()
    )


def _wortmeldung(speech: BroadcastSpeech) -> WortmeldungStand:
    eindeutig = speech.assignment == SpeechAssignment.EINDEUTIG and speech.person_id is not None
    return WortmeldungStand(
        name=speech.name_read,
        fraktion=speech.faction_read,
        funktion=speech.function_read,
        person_id=speech.person_id if eindeutig else None,
        begonnen_am=speech.started_at,
    )


def _vorlagen(abschnitte: list[BroadcastSection]) -> dict[uuid.UUID, uuid.UUID]:
    """Vorlage je Tagesordnungspunkt (erste nicht zurückgenommene Beratung)."""
    externe = {a.agenda_item.external_id: a.agenda_item.pk for a in abschnitte if a.agenda_item is not None}
    if not externe:
        return {}
    ergebnis: dict[uuid.UUID, uuid.UUID] = {}
    for extern, paper_id in (
        OParlConsultation.objects.filter(
            agenda_item_external_id__in=list(externe), deleted=False, paper__isnull=False, paper__deleted=False
        )
        .order_by("agenda_item_external_id", "created_at")
        .values_list("agenda_item_external_id", "paper_id")
    ):
        if extern and paper_id:
            ergebnis.setdefault(externe[extern], paper_id)
    return ergebnis


def _abschnitt(
    abschnitt: BroadcastSection, vorlagen: dict[uuid.UUID, uuid.UUID], sprecher: list[BroadcastSpeech]
) -> AbschnittStand:
    punkt = abschnitt.agenda_item
    titel = (punkt.name if punkt is not None and punkt.name else "") or abschnitt.title_read
    return AbschnittStand(
        nummer=abschnitt.number,
        titel=titel,
        agenda_item_id=punkt.pk if punkt is not None else None,
        paper_id=vorlagen.get(punkt.pk) if punkt is not None else None,
        begonnen_am=abschnitt.started_at,
        beendet_am=abschnitt.ended_at,
        wortmeldungen=[_wortmeldung(s) for s in sprecher],
    )


def live_stand(meeting_id: uuid.UUID) -> LiveStand | None:
    """Stand der Live-Seite einer Sitzung (siehe Moduldokumentation)."""
    meeting = (
        OParlMeeting.objects.filter(pk=meeting_id, deleted=False)
        .select_related("body")
        .prefetch_related("organizations")
        .first()
    )
    if meeting is None:
        return None
    quelle = quelle_fuer_sitzung(meeting)
    if quelle is None:
        return None
    broadcast = (
        Broadcast.objects.filter(source=quelle, meeting=meeting)
        .select_related("current_section", "current_speech")
        .first()
    )
    if broadcast is None and not (quelle.active and getattr(settings, "LIVE_UEBERTRAGUNG_AKTIV", False)):
        return None
    try:
        adapter = anbieter(quelle.provider)
    except KeyError:
        return None

    verlauf: list[AbschnittStand] = []
    ohne_abschnitt: list[WortmeldungStand] = []
    jetzt_abschnitt: AbschnittStand | None = None
    am_wort: WortmeldungStand | None = None
    if broadcast is not None:
        abschnitte = list(broadcast.sections.select_related("agenda_item").order_by("started_at"))
        sprecher = list(broadcast.speeches.order_by("started_at"))
        vorlagen = _vorlagen(abschnitte)
        je_abschnitt: dict[uuid.UUID | None, list[BroadcastSpeech]] = {}
        for s in sprecher:
            je_abschnitt.setdefault(s.section_id, []).append(s)
        verlauf = [_abschnitt(a, vorlagen, je_abschnitt.get(a.pk, [])) for a in abschnitte]
        ohne_abschnitt = [_wortmeldung(s) for s in je_abschnitt.get(None, [])]
        if broadcast.status == BroadcastStatus.LIVE:
            if broadcast.current_section_id is not None:
                jetzt_abschnitt = next(
                    (v for a, v in zip(abschnitte, verlauf, strict=True) if a.pk == broadcast.current_section_id),
                    None,
                )
            if broadcast.current_speech is not None:
                am_wort = _wortmeldung(broadcast.current_speech)

    return LiveStand(
        meeting=meeting,
        gremium=quelle.organization.name or "",
        status=broadcast.status if broadcast is not None else BroadcastStatus.GEPLANT,
        begonnen_am=broadcast.started_at if broadcast is not None else None,
        beendet_am=broadcast.ended_at if broadcast is not None else None,
        einbettung_url=adapter.einbettung_url(quelle.identifier),
        seiten_url=quelle.page_url,
        anbieter_name=adapter.name,
        jetzt_abschnitt=jetzt_abschnitt,
        am_wort=am_wort,
        verlauf=list(reversed(verlauf)),
        ohne_abschnitt=ohne_abschnitt,
    )


@dataclass(frozen=True)
class WortmeldungDaten:
    """Angaben einer Wortmeldung für Abonnenten (keine Bilddaten, keine Redezeit)."""

    speech_id: uuid.UUID
    broadcast_id: uuid.UUID
    meeting_id: uuid.UUID
    body_id: uuid.UUID
    organization_id: uuid.UUID
    person_id: uuid.UUID | None
    fraktion_gelesen: str
    funktion_gelesen: str
    zuordnung: str
    lesungen: int
    begonnen_am: datetime

    @property
    def eindeutig(self) -> bool:
        """Person eindeutig zugeordnet und mehrfach gleich gelesen (Bedingung für eine Fraktionszuordnung)."""
        return self.zuordnung == SpeechAssignment.EINDEUTIG and self.person_id is not None and self.lesungen >= 2


def wortmeldung(speech_id: uuid.UUID | str) -> WortmeldungDaten | None:
    """Angaben einer Wortmeldung (für Abonnenten von ``ris.broadcast.speaker_changed``)."""
    speech = BroadcastSpeech.objects.filter(pk=speech_id).select_related("broadcast__source").first()
    if speech is None:
        return None
    quelle = speech.broadcast.source
    return WortmeldungDaten(
        speech_id=speech.pk,
        broadcast_id=speech.broadcast_id,
        meeting_id=speech.broadcast.meeting_id,
        body_id=quelle.body_id,
        organization_id=quelle.organization_id,
        person_id=speech.person_id,
        fraktion_gelesen=speech.faction_read,
        funktion_gelesen=speech.function_read,
        zuordnung=speech.assignment,
        lesungen=speech.readings,
        begonnen_am=speech.started_at,
    )
