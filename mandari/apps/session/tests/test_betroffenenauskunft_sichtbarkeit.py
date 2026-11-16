# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Betroffenenauskunft und Nichtöffentliches.

Die Auskunft erstellt, wer das Einstellungsrecht hat. Ohne das Recht auf nichtöffentliche Sitzungen bzw. Vorlagen
nennt sie deren Bezeichnungen nicht: Die Zeilen bleiben (Datum, Status, eigene Stimme), Sitzungsname, Betreff,
Aktenzeichen und Titel erscheinen als „nichtöffentlich“. Ein TOP ist nur öffentlich, wenn auch seine Sitzung es ist;
den Betreff eines nichtöffentlichen TOP nennt die Auskunft wie bisher nie.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionMeeting,
    SessionPaper,
    SessionPerson,
    SessionVote,
)
from apps.session.services import privacy_service
from apps.session.tests._niederschrift import Welt, base, client, nutzer, welt

pytestmark = pytest.mark.django_db

NOE_RECHTE = ("view_non_public_meetings", "view_non_public_papers")


def _auskunftswelt() -> tuple[Welt, SessionPerson, SessionPaper]:
    w = welt(status="review")
    person = w.stimmberechtigt[0]
    geheim = SessionMeeting.objects.create(
        tenant=w.tenant,
        name="GEHEIMSITZUNG Personalangelegenheit",
        organization=w.gremium,
        start=timezone.now() - timedelta(days=1),
        is_public=False,
    )
    SessionAttendance.objects.create(meeting=geheim, person=person, status="present")
    # Als öffentlich markierter TOP einer nichtöffentlichen Sitzung: nichtöffentlich wie die Sitzung
    markiert = SessionAgendaItem.objects.create(
        meeting=geheim, number="1", order=1, name="GEHEIMBETREFF Beförderung", is_public=True
    )
    for item, stimme in ((w.top, "yes"), (w.top_noe, "no"), (markiert, "abstain")):
        SessionVote.objects.create(agenda_item=item, person=person, vote=stimme)
    SessionPaper.objects.create(tenant=w.tenant, name="Offene Vorlage Radweg", is_public=True, originator_person=person)
    vertraulich = SessionPaper.objects.create(
        tenant=w.tenant, name="GEHEIMVORLAGE Grundstück", is_public=False, originator_person=person
    )
    return w, person, vertraulich


def _abruf(w: Welt, person: SessionPerson, *rechte: str) -> tuple[dict[str, Any], str]:
    konto = nutzer(w.tenant, f"auskunft{len(rechte)}", "manage_settings", *rechte)
    antwort = client(konto).get(f"{base(w)}/persons/{person.pk}/auskunft.json")
    assert antwort.status_code == 200
    text = antwort.content.decode()
    return json.loads(text), text


def test_ohne_noe_recht_keine_nichtoeffentlichen_bezeichnungen() -> None:
    w, person, vertraulich = _auskunftswelt()

    daten, text = _abruf(w, person)

    assert "GEHEIM" not in text
    if vertraulich.reference:
        assert vertraulich.reference not in text
    # Die Zeilen bleiben vollständig: Datum, Status und die eigene Stimme
    assert [zeile["sitzung"] for zeile in daten["anwesenheiten"]] == ["Bauausschuss", privacy_service.NOT_PUBLIC]
    assert all(zeile["datum"] and zeile["status"] for zeile in daten["anwesenheiten"])
    stimmen = {zeile["stimme"]: (zeile["sitzung"], zeile["betreff"]) for zeile in daten["stimmabgaben"]}
    assert stimmen == {
        "Ja": ("Bauausschuss", "Radweg Hauptstraße"),
        "Nein": ("Bauausschuss", privacy_service.NOT_PUBLIC),
        "Enthaltung": (privacy_service.NOT_PUBLIC, privacy_service.NOT_PUBLIC),
    }
    vorlagen = {zeile["name"] for zeile in daten["vorlagen_als_verfasser"]}
    assert vorlagen == {"Offene Vorlage Radweg", privacy_service.NOT_PUBLIC}
    assert daten["auskunft"]["hinweis"] == privacy_service.MASKED_NOTICE


def test_mit_noe_rechten_vollstaendig() -> None:
    w, person, _vertraulich = _auskunftswelt()

    daten, text = _abruf(w, person, *NOE_RECHTE)

    for marke in ("GEHEIMSITZUNG", "GEHEIMBETREFF", "GEHEIMVORLAGE"):
        assert marke in text
    # Den Betreff eines nichtöffentlichen TOP nennt die Auskunft nie, auch nicht mit dem Recht darauf
    assert "GEHEIMTOP" not in text
    assert "hinweis" not in daten["auskunft"]


def test_nur_sitzungsrecht_maskiert_weiter_die_vorlagen() -> None:
    w, person, _vertraulich = _auskunftswelt()

    daten, text = _abruf(w, person, "view_non_public_meetings")

    assert "GEHEIMSITZUNG" in text and "GEHEIMBETREFF" in text
    assert "GEHEIMVORLAGE" not in text
    assert daten["auskunft"]["hinweis"] == privacy_service.MASKED_NOTICE


def test_ohne_angabe_der_rechte_gilt_maskiert() -> None:
    w, person, _vertraulich = _auskunftswelt()

    daten = privacy_service.subject_access_export(w.tenant, person)

    assert "GEHEIM" not in json.dumps(daten, ensure_ascii=False)
