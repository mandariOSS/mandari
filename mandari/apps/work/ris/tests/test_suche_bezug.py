# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bezug der Fraktion für die Gewichtung der Recherche (Issue #853): nur Daten der eigenen Organisation, die das Mitglied
sehen darf; private Inhalte anderer und nichtöffentliche TOPs zählen nicht, die Vertretung (Rat) nicht als Gremium.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.tenants.models import Organization
from apps.work.faction.models import FactionAgendaItem, FactionMeeting
from apps.work.meetings.models import (
    AgendaItemNote,
    AgendaItemPosition,
    AgendaPrivateNote,
    MeetingPreparation,
    PaperComment,
)
from apps.work.motions.models import Motion
from apps.work.ris import selectors
from apps.work.ris.bezug import FAKTOR_EIGENER_ANTRAG, Fraktionsbezug, fraktionsbezug
from hub.ris import selectors as ris
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlSource,
)

RIS = "https://ris.beispiel.example/oparl"


@pytest.fixture
def body(org: Any) -> OParlBody:
    quelle = OParlSource.objects.create(name="RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=quelle, name="Beispielstadt")
    org.body = body
    org.save(update_fields=["body"])
    return body


def _vorlage(body: OParlBody, key: str) -> OParlPaper:
    return OParlPaper.objects.create(external_id=f"{RIS}/paper/{key}", body=body, name=f"Vorlage {key}")


def _top(body: OParlBody, key: str, vorlage: OParlPaper, *, start: Any = None) -> OParlAgendaItem:
    sitzung = OParlMeeting.objects.create(external_id=f"{RIS}/meeting/{key}", body=body, name=key, start=start)
    top = OParlAgendaItem.objects.create(external_id=f"{RIS}/agenda/{key}", meeting=sitzung, number="1", order=1)
    OParlConsultation.objects.create(
        external_id=f"{RIS}/consultation/{key}",
        body=body,
        paper=vorlage,
        agenda_item_external_id=top.external_id,
        meeting_external_id=sitzung.external_id,
    )
    return top


@pytest.mark.django_db
def test_bezug_aus_eigenen_daten_ohne_fremdes_und_privates(org: Any, body: OParlBody, make_member: Any) -> None:
    ich = make_member(org, ["motions.view", "ris.view"], email="ich@example.org")
    andere = make_member(org, ["motions.view"], email="andere@example.org")
    fremd = Organization.objects.create(name="Andere Fraktion", slug="andere-fraktion")

    antrag, privat_fremd, entwurf_ich = _vorlage(body, "antrag"), _vorlage(body, "privat"), _vorlage(body, "entwurf")
    Motion.objects.create(
        organization=org, author=ich, title="Eigener", visibility="organization", related_paper=antrag
    )
    Motion.objects.create(
        organization=org, author=andere, title="Privat", visibility="private", related_paper=privat_fremd
    )
    Motion.objects.create(
        organization=org, author=ich, title="Änderung", visibility="private", parent_paper=entwurf_ich
    )
    Motion.objects.create(organization=fremd, title="Fremd", visibility="organization", related_paper=privat_fremd)

    position, offen, notiz, meine_notiz, deren_notiz = (
        _vorlage(body, k) for k in ("pos", "offen", "notiz", "mein", "deren")
    )
    AgendaItemPosition.objects.create(organization=org, agenda_item=_top(body, "pos", position), position="for")
    AgendaItemPosition.objects.create(organization=org, agenda_item=_top(body, "offen", offen), position="open")
    AgendaItemNote.objects.create(organization=org, agenda_item=_top(body, "notiz", notiz))
    AgendaPrivateNote.objects.create(organization=org, author=ich, agenda_item=_top(body, "mein", meine_notiz))
    AgendaPrivateNote.objects.create(organization=org, author=andere, agenda_item=_top(body, "deren", deren_notiz))

    kommentar, kommentar_privat = _vorlage(body, "kommentar"), _vorlage(body, "kprivat")
    PaperComment.objects.create(paper=kommentar, organization=org, author=andere, visibility="organization")
    PaperComment.objects.create(paper=kommentar_privat, organization=org, author=andere, visibility="private")
    PaperComment.objects.create(paper=offen, organization=fremd, visibility="organization")

    sitzung = FactionMeeting.objects.create(organization=org, title="Fraktion", start=timezone.now())
    oeffentlich, intern = _vorlage(body, "ftop"), _vorlage(body, "fintern")
    FactionAgendaItem.objects.create(meeting=sitzung, number="1", title="A", visibility="public").related_papers.add(
        oeffentlich
    )
    FactionAgendaItem.objects.create(meeting=sitzung, number="2", title="B", visibility="internal").related_papers.add(
        intern
    )
    ris_sitzung = OParlMeeting.objects.create(external_id=f"{RIS}/meeting/vorbereitet", body=body, name="Rat")
    MeetingPreparation.objects.create(organization=org, meeting=ris_sitzung)

    bezug = fraktionsbezug(org, ich)

    assert bezug.eigene_antraege == {str(antrag.pk), str(entwurf_ich.pk)}
    assert bezug.bearbeitet == {str(p.pk) for p in (position, notiz, meine_notiz, kommentar, oeffentlich)}
    assert bezug.sitzungen == {str(ris_sitzung.pk)}
    assert bezug.text("vorgang", str(antrag.pk)) == "Eigener Antrag"
    assert bezug.text("vorgang", str(position.pk)) == "Von der Fraktion bearbeitet"
    assert bezug.text("vorgang", str(intern.pk)) == ""
    assert bezug.faktor("vorgang", str(antrag.pk)) == FAKTOR_EIGENER_ANTRAG


@pytest.mark.django_db
def test_gaeste_und_fehlende_mitgliedschaft_ohne_bezug(org: Any, body: OParlBody, make_member: Any) -> None:
    gast = make_member(org, ["ris.view"], email="gast@example.org")
    gast.is_guest = True
    gast.save(update_fields=["is_guest"])
    Motion.objects.create(
        organization=org, title="Antrag", visibility="organization", related_paper=_vorlage(body, "x")
    )

    assert fraktionsbezug(org, gast) == Fraktionsbezug()
    assert fraktionsbezug(org, None) == Fraktionsbezug()
    assert not Fraktionsbezug().boost()


@pytest.mark.django_db
def test_gremien_ohne_vertretung_eigene_vor_denen_der_fraktion(org: Any, body: OParlBody, make_member: Any) -> None:
    rat = OParlOrganization.objects.create(external_id=f"{RIS}/org/rat", body=body, name="Rat", classification="Rat")
    bau = OParlOrganization.objects.create(
        external_id=f"{RIS}/org/bau", body=body, name="Bauausschuss", classification="Ausschuss"
    )
    schule = OParlOrganization.objects.create(
        external_id=f"{RIS}/org/schule", body=body, name="Schulausschuss", classification="Ausschuss"
    )
    ich = make_member(org, ["ris.view"], email="ich@example.org")
    kollegin = make_member(org, ["ris.view"], email="kollegin@example.org")
    kollegin.oparl_committees.add(schule, rat)

    fraktion = fraktionsbezug(org, ich)
    assert (fraktion.gremien, fraktion.gremien_quelle) == (("Schulausschuss",), "fraktion")
    assert fraktion.text("vorgang", "x", ["Schulausschuss"]) == "In Gremien der Fraktion"

    ich.followed_organizations.add(bau, rat)
    meine = fraktionsbezug(org, ich)
    assert (meine.gremien, meine.gremien_quelle) == (("Bauausschuss",), "meine")
    assert meine.text("vorgang", "x", ["Rat", "Bauausschuss"]) == "In Ihren Gremien"
    assert meine.text("vorgang", "x", ["Rat"]) == ""


@pytest.mark.django_db
def test_bald_auf_der_tagesordnung_ueber_die_fassade(org: Any, body: OParlBody, make_member: Any) -> None:
    jetzt = timezone.now()
    bald, spaeter, vorbei = _vorlage(body, "bald"), _vorlage(body, "spaeter"), _vorlage(body, "vorbei")
    _top(body, "bald", bald, start=jetzt + timedelta(days=5))
    _top(body, "spaeter", spaeter, start=jetzt + timedelta(days=60))
    _top(body, "vorbei", vorbei, start=jetzt - timedelta(days=5))
    ich = make_member(org, ["ris.view"], email="ich@example.org")

    bezug = fraktionsbezug(org, ich, selectors.bodies_for_organization(org))

    assert bezug.anstehend == {str(bald.pk)}
    assert ris.paper_ids_on_upcoming_agendas([body], until=jetzt + timedelta(days=90)) == {bald.pk, spaeter.pk}
    # Bald beraten zählt, steht aber nicht als eigener Satz am Treffer (der Stand-Satz sagt es schon)
    assert bezug.text("vorgang", str(bald.pk)) == "" and bezug.faktor("vorgang", str(bald.pk)) > 1
