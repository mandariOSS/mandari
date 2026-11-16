# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Endgültiges Löschen einer Person nimmt ihre gelesenen Angaben aus den Wortmeldungen der Live-Übertragung.

Name, Fraktion und Funktion einer Wortmeldung sind Kopien als Freitext. ``purge_deleted`` entfernt deshalb die
Wortmeldungen, die Angaben zu einer markierten Person tragen: mit Verweis auf sie und ohne Verweis bei gleichem
Namen in derselben Kommune. Der Verweis einer Wortmeldung hält die Person nicht mehr zurück.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from hub.live.models import (
    Broadcast,
    BroadcastSource,
    BroadcastSpeech,
    BroadcastStatus,
    SpeechAssignment,
    wortmeldungen_zu_personen,
)
from hub.live.tests.conftest import Welt, kennung
from insight_core.models import OParlBody, OParlMeeting, OParlOrganization, OParlPerson, OParlSource

pytestmark = pytest.mark.django_db


def _purge(*argumente: str) -> str:
    ausgabe = StringIO()
    call_command("purge_deleted", *argumente, stdout=ausgabe)
    return ausgabe.getvalue()


def _live_seite(welt: Welt) -> str:
    antwort = Client().get(reverse("insight_core:insight:meeting_live", args=[welt.sitzung.pk]))
    assert antwort.status_code == 200
    return antwort.content.decode()


def _wortmeldung(broadcast: Broadcast, welt: Welt, name: str, minuten: int, **felder: object) -> BroadcastSpeech:
    return BroadcastSpeech.objects.create(
        broadcast=broadcast, name_read=name, started_at=welt.jetzt + timedelta(minutes=minuten), **felder
    )


@pytest.fixture
def wortmeldungen(welt: Welt, laufend: Broadcast) -> dict[str, BroadcastSpeech]:
    """Zur Übertragung aus ``laufend`` (Erika Muster mit Verweis, Gisela Gast ohne) weitere Lesarten."""
    return {
        # Falsch gelesen, aber der Person zugeordnet
        "verlesen": _wortmeldung(
            laufend, welt, "Erika Mustre", 4, person=welt.muster, assignment=SpeechAssignment.UNSICHER
        ),
        # Ohne Verweis, etwa bei zwei gleichnamigen Personen; mit Titel gelesen
        "ohne_verweis": _wortmeldung(laufend, welt, "Dr. Erika Muster", 6, faction_read="Fraktion A"),
        "andere": _wortmeldung(laufend, welt, "Max Beispiel", 8, person=welt.beispiel),
    }


def test_endgueltiges_loeschen_entfernt_die_gelesenen_angaben(
    welt: Welt, laufend: Broadcast, wortmeldungen: dict[str, BroadcastSpeech]
) -> None:
    welt.muster.mark_deleted()
    assert "Erika Muster" in _live_seite(welt)

    ausgabe = _purge("--ids", str(welt.muster.pk), "--yes")

    assert "Wortmeldungen der Live-Übertragung mit gelesenen Angaben zu markierten Personen: 3 (entfernt)" in ausgabe
    # Der Verweis der Wortmeldungen hält die Person nicht zurück
    assert not OParlPerson.objects.filter(pk=welt.muster.pk).exists()
    html = _live_seite(welt)
    for text in ("Erika Muster", "Erika Mustre", "Fraktion A"):
        assert text not in html, text
    assert "Gisela Gast" in html and "Max Beispiel" in html
    laufend.refresh_from_db()
    assert laufend.current_speech is None, "am Wort war die gelöschte Person"
    assert BroadcastSpeech.objects.filter(pk=wortmeldungen["andere"].pk).exists()


def test_probelauf_nennt_die_treffer_und_aendert_nichts(
    welt: Welt, laufend: Broadcast, wortmeldungen: dict[str, BroadcastSpeech]
) -> None:
    welt.muster.mark_deleted()
    vorher = BroadcastSpeech.objects.count()

    ausgabe = _purge("--ids", str(welt.muster.pk))

    assert "zu markierten Personen: 3 (werden mit --yes entfernt)" in ausgabe
    assert BroadcastSpeech.objects.count() == vorher
    assert OParlPerson.objects.filter(pk=welt.muster.pk).exists()


def test_unmarkierte_person_behaelt_ihre_wortmeldungen(
    welt: Welt, laufend: Broadcast, wortmeldungen: dict[str, BroadcastSpeech]
) -> None:
    welt.beispiel.mark_deleted()
    vorher = set(BroadcastSpeech.objects.exclude(pk=wortmeldungen["andere"].pk).values_list("pk", flat=True))

    _purge("--ids", str(welt.beispiel.pk), "--yes")

    assert set(BroadcastSpeech.objects.values_list("pk", flat=True)) == vorher


def test_gleicher_name_in_anderer_kommune_bleibt(welt: Welt, laufend: Broadcast) -> None:
    source = OParlSource.objects.create(name="Nachbarstadt-RIS", url="https://ris.nachbarstadt.example/oparl/system")
    body = OParlBody.objects.create(external_id=kennung("bodies"), source=source, name="Nachbarstadt", slug="nachbar")
    rat = OParlOrganization.objects.create(external_id=kennung("organizations"), body=body, name="Rat")
    sitzung = OParlMeeting.objects.create(external_id=kennung("meetings"), body=body, name="Rat", start=welt.jetzt)
    quelle = BroadcastSource.objects.create(body=body, organization=rat, provider="3q", identifier="x", active=True)
    nachbar = Broadcast.objects.create(source=quelle, meeting=sitzung, status=BroadcastStatus.BEENDET)
    fremd = BroadcastSpeech.objects.create(broadcast=nachbar, name_read="Erika Muster", started_at=welt.jetzt)

    treffer = wortmeldungen_zu_personen([welt.muster])

    assert fremd not in treffer
    assert treffer.count() == 1, "nur die Wortmeldung mit Verweis aus der eigenen Kommune"
