# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Protokolleinträge lesen nur mit dem Recht am Protokoll; die vorige Sitzung nennt der Genehmigungs-TOP nur mit Sicht.

- Die Niederschrift als PDF und der Protokollversand verlangen ``protocols.view_public`` bzw. Vereidigung mit
  ``protocols.view_full``. Dieselbe Regel gilt für die Einträge auf den Seiten der Sitzung: TOP-Panel, bisherige
  Sitzungsseite, Sitzungsansicht und Protokollseite nach der Sitzung. Alle Standardrollen mit Zugang zu den Sitzungen
  haben die Rechte; die Prüfung wirkt, wenn eine Organisation sie einer Rolle oder Person entzieht. Wer das Protokoll
  führt, liest, was er schreibt.
- Sitzungsarten „nur Teilnehmende“ (Issue #1239): Wer einzeln zu einer Sitzung eingeladen ist, sieht am
  Genehmigungs-TOP weder Titel noch Datum noch Niederschrift der vorigen Sitzung, die er nicht sehen darf.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.permissions import DEFAULT_ROLES
from apps.common.tests.factories import RoleFactory
from apps.work.faction import protokoll, sitzung
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionMeeting,
    FactionMeetingType,
    FactionProtocolEntry,
)

OEFFENTLICH = "Eintrag zum Radweg am Kanal"
INTERN = "Eintrag zum Grundstückskauf"
OHNE_TOP = "Eintrag ohne Tagesordnungspunkt"
VORIGE = "Vorstand Kassenlage im Oktober"


def _mitglied(org: Any, make_member: Any, name: str, rechte: list[str], *, vereidigt: bool = False) -> Any:
    m = make_member(org, rechte, email=f"{name}@example.org")
    if vereidigt:
        m.is_sworn_in = True
        m.save(update_fields=["is_sworn_in"])
    return m


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    rechte = ["faction.view_public", "faction.view_non_public", "faction.manage", "protocols.view_full"]
    return _mitglied(org, make_member, "vorsitz", rechte, vereidigt=True)


@pytest.fixture
def welt(org: Any, vorsitz: Any) -> dict[str, Any]:
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() - timedelta(days=1),
        status="completed",
        created_by=vorsitz,
    )
    offen = FactionAgendaItem.objects.create(meeting=meeting, number="2", title="Verkehr", order=1)
    geheim = FactionAgendaItem.objects.create(
        meeting=meeting, number="NÖ 1", title="Liegenschaften", visibility="internal", order=2
    )
    for nr, (top, text) in enumerate(((offen, OEFFENTLICH), (geheim, INTERN), (None, OHNE_TOP)), start=1):
        eintrag: Any = FactionProtocolEntry(
            meeting=meeting, agenda_item=top, entry_type="note", created_by=vorsitz, order=nr
        )
        eintrag.set_content_encrypted(text)
        eintrag.save()
    return {"meeting": meeting, "offen": offen, "geheim": geheim}


def _panel(client_for: Any, member: Any, top: FactionAgendaItem) -> str:
    url = reverse(
        "work:faction_item_panel",
        kwargs={"org_slug": top.meeting.organization.slug, "meeting_id": top.meeting_id, "item_id": top.pk},
    )
    antwort = client_for(member.user).get(url)
    assert antwort.status_code == 200
    return cast(str, antwort.content.decode())


def _seite(client_for: Any, member: Any, meeting: FactionMeeting) -> str:
    url = reverse("work:faction_detail", kwargs={"org_slug": meeting.organization.slug, "meeting_id": meeting.pk})
    antwort = client_for(member.user).get(url, {"ansicht": "bisher"})
    assert antwort.status_code == 200
    return cast(str, antwort.content.decode())


def _texte(dok: protokoll.Dokument) -> dict[str, int]:
    return {t.item.number: len(t.eintraege) for t in dok.tops} | {"ohne": len(dok.allgemein)}


def test_standardrollen_mit_sitzungen_lesen_auch_das_protokoll() -> None:
    """Die Prüfung nimmt keiner Standardrolle etwas: Wer die Sitzungen sieht, hat auch das Recht am Protokoll."""
    for key, rolle in DEFAULT_ROLES.items():
        rechte = set(cast(list[str], rolle["permissions"]))
        if "faction.view_public" in rechte:
            assert "protocols.view_public" in rechte, key
        if "faction.view_non_public" in rechte:
            assert "protocols.view_full" in rechte, key


@pytest.mark.django_db
def test_eintraege_oeffentlicher_tops_nur_mit_recht_am_protokoll(
    org: Any, make_member: Any, welt: dict[str, Any], client_for: Any
) -> None:
    ohne = _mitglied(org, make_member, "ohne", ["faction.view_public"])
    mit = _mitglied(org, make_member, "mit", ["faction.view_public", "protocols.view_public"])
    meeting, top = welt["meeting"], welt["offen"]

    assert OEFFENTLICH not in _panel(client_for, ohne, top)
    assert OEFFENTLICH not in _seite(client_for, ohne, meeting)
    assert sitzung.protokolleintraege(top, ohne) == []
    assert _texte(protokoll.dokument(meeting, ohne)) == {"2": 0, "ohne": 0}

    assert OEFFENTLICH in _panel(client_for, mit, top)
    assert OEFFENTLICH in _seite(client_for, mit, meeting)
    assert len(sitzung.protokolleintraege(top, mit)) == 1
    assert _texte(protokoll.dokument(meeting, mit)) == {"2": 1, "ohne": 0}


@pytest.mark.django_db
def test_eintraege_nichtoeffentlicher_tops_nur_mit_vollem_recht(
    org: Any, make_member: Any, vorsitz: Any, welt: dict[str, Any], client_for: Any
) -> None:
    """Vereidigt und mit Sicht auf den nichtöffentlichen Teil, aber ohne ``protocols.view_full``."""
    rechte = ["faction.view_public", "faction.view_non_public", "protocols.view_public"]
    teil = _mitglied(org, make_member, "teil", rechte, vereidigt=True)
    meeting, top = welt["meeting"], welt["geheim"]

    assert INTERN not in _panel(client_for, teil, top)
    seite = _seite(client_for, teil, meeting)
    assert INTERN not in seite and OEFFENTLICH in seite
    assert sitzung.protokolleintraege(top, teil) == []
    assert _texte(protokoll.dokument(meeting, teil)) == {"2": 1, "NÖ 1": 0, "ohne": 0}

    assert INTERN in _panel(client_for, vorsitz, top)
    seite = _seite(client_for, vorsitz, meeting)
    assert INTERN in seite and OEFFENTLICH in seite
    assert _texte(protokoll.dokument(meeting, vorsitz)) == {"2": 1, "NÖ 1": 1, "ohne": 1}


@pytest.mark.django_db
def test_wer_das_protokoll_fuehrt_liest_es(org: Any, make_member: Any, welt: dict[str, Any], client_for: Any) -> None:
    schrift = _mitglied(org, make_member, "schrift", ["faction.view_public", "protocols.create"])
    leitung = _mitglied(org, make_member, "leitung", ["faction.view_public"])
    meeting = welt["meeting"]
    FactionMeeting.objects.filter(pk=meeting.pk).update(minute_taker=leitung)
    meeting.refresh_from_db()

    assert OEFFENTLICH in _panel(client_for, schrift, welt["offen"])
    assert OEFFENTLICH in _seite(client_for, schrift, meeting)
    assert sitzung.darf_eintraege_lesen(leitung, meeting, intern=False)
    assert len(sitzung.protokolleintraege(FactionAgendaItem.objects.get(pk=welt["offen"].pk), leitung)) == 1


@pytest.mark.django_db
def test_seitenleiste_zaehlt_nur_lesbare_eintraege(
    org: Any, make_member: Any, vorsitz: Any, welt: dict[str, Any], client_for: Any
) -> None:
    """Die Zahl der Einträge in der Seitenleiste folgt denselben Rechten wie die Einträge selbst."""
    ohne = _mitglied(org, make_member, "ohne", ["faction.view_public"])
    mit = _mitglied(org, make_member, "mit", ["faction.view_public", "protocols.view_public"])
    meeting = welt["meeting"]

    def zahl(member: Any) -> int:
        url = reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})
        antwort = client_for(member.user).get(url, {"ansicht": "bisher"})
        assert antwort.status_code == 200
        return cast(int, antwort.context["protocol_entry_count"])

    assert (zahl(ohne), zahl(mit), zahl(vorsitz)) == (0, 1, 3)


# ---- Vorige Sitzung am Genehmigungs-TOP ----------------------------------------------------------------------


@pytest.fixture
def vorstand(org: Any, vorsitz: Any) -> dict[str, Any]:
    """Art „nur Teilnehmende“: vorige und laufende Sitzung; ``eingeladen`` ist nur zur laufenden Sitzung geladen."""
    rolle = cast(Any, RoleFactory)(organization=org, name="Kernteam", permissions=[])
    art = FactionMeetingType.objects.create(
        organization=org,
        name="Vorstand",
        position=1,
        participants_mode="role",
        participants_role=rolle,
        visibility="participants",
    )
    vorige = FactionMeeting.objects.create(
        organization=org,
        meeting_type=art,
        title=VORIGE,
        start=timezone.now() - timedelta(days=14),
        status="completed",
        created_by=vorsitz,
    )
    laufend = FactionMeeting.objects.create(
        organization=org, meeting_type=art, title="Vorstand", start=timezone.now(), status="ongoing", created_by=vorsitz
    )
    top = FactionAgendaItem.objects.create(
        meeting=laufend, number="1", title="Protokoll genehmigen", is_approval_item=True, order=0
    )
    FactionAgendaItem.objects.filter(pk=top.pk).update(approves_meeting=vorige)
    return {"rolle": rolle, "vorige": vorige, "laufend": laufend, "top": FactionAgendaItem.objects.get(pk=top.pk)}


@pytest.mark.django_db
def test_genehmigungs_top_nennt_die_vorige_sitzung_nur_mit_sicht(
    org: Any, make_member: Any, vorstand: dict[str, Any], client_for: Any
) -> None:
    rechte = ["faction.view_public", "protocols.view_public"]
    kern = _mitglied(org, make_member, "kern", rechte)
    kern.roles.add(vorstand["rolle"])
    eingeladen = _mitglied(org, make_member, "eingeladen", rechte)
    FactionAttendance.objects.create(meeting=vorstand["laufend"], membership=eingeladen, status="invited")
    top, laufend = vorstand["top"], vorstand["laufend"]

    seite = _seite(client_for, eingeladen, laufend)
    assert VORIGE not in seite and str(vorstand["vorige"].pk) not in seite
    assert [u.art for u in sitzung.unterlagen(top, eingeladen)] == []
    assert VORIGE not in sitzung.herkunft(top, eingeladen)
    assert "Protokoll der vorigen Sitzung" in sitzung.herkunft(top, eingeladen)
    assert sitzung.vorige_mit_protokoll(top, eingeladen) is None

    assert VORIGE in sitzung.herkunft(top, kern)
    assert [u.art for u in sitzung.unterlagen(top, kern)] == ["protokoll"]
    assert VORIGE in _seite(client_for, kern, laufend)
