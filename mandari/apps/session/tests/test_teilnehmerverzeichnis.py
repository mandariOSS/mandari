# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Teilnehmerverzeichnis der Niederschrift: vorzeitig Gegangene und Verspätete.

Wer an der Sitzung teilgenommen hat, steht unter „Anwesend“ – auch wer vorzeitig gegangen ist
(Status „Vorzeitig gegangen“ zählt für die Beschlussfähigkeit nicht mehr als „im Raum“, gehört aber
ins Verzeichnis). Der Vermerk nennt die erfasste Uhrzeit von Ankunft bzw. Abgang. Seite, PDF
(öffentliche und interne Fassung) und der Text der öffentlichen Fassung zeigen dasselbe.
"""

from __future__ import annotations

from datetime import time

import pytest

from apps.session.models import SessionAttendance
from apps.session.services import protocol_publication, protocol_service
from apps.session.tests._niederschrift import Welt, base, client, pdf_text, person, welt

pytestmark = pytest.mark.django_db

#: Nachname -> erwarteter Eintrag im Verzeichnis
ERWARTET = {
    "Gegangen": "P Gegangen (Mitglied, vorzeitig gegangen, bis 19:45 Uhr)",
    "Frueh": "P Frueh (Mitglied, vorzeitig gegangen)",
    "Spaet": "P Spaet (Mitglied, verspätet, ab 17:20 Uhr)",
}


def _welt() -> Welt:
    w = welt("verzeichnis", status="draft")
    for name, status, felder in (
        ("Gegangen", "left_early", {"departure_time": time(19, 45)}),
        ("Frueh", "left_early", {}),
        ("Spaet", "joined_late", {"arrival_time": time(17, 20)}),
    ):
        SessionAttendance.objects.create(meeting=w.sitzung, person=person(w.tenant, name), status=status, **felder)
    return w


def test_vorzeitig_gegangene_stehen_unter_anwesend_mit_vermerk() -> None:
    w = _welt()

    verzeichnis = protocol_service.participant_directory(w.sitzung)

    vermerke = {a.person.family_name: a.presence_note for a in verzeichnis["present"]}
    assert vermerke["Gegangen"] == "vorzeitig gegangen, bis 19:45 Uhr"
    assert vermerke["Frueh"] == "vorzeitig gegangen"
    assert vermerke["Spaet"] == "verspätet, ab 17:20 Uhr"
    assert vermerke["Amsel"] == ""
    assert verzeichnis["other"] == []


@pytest.mark.parametrize("internal", [False, True])
def test_pdf_nennt_vorzeitig_gegangene(internal: bool) -> None:
    w = _welt()

    text = " ".join(pdf_text(protocol_service.build_protocol_pdf(w.protokoll, internal=internal)).split())

    for eintrag in ERWARTET.values():
        assert eintrag in text


def test_oeffentlicher_text_nennt_vorzeitig_gegangene() -> None:
    w = _welt()

    text = protocol_publication.public_text(w.protokoll)

    for eintrag in ERWARTET.values():
        assert eintrag in text


def test_seite_nennt_vorzeitig_gegangene() -> None:
    w = _welt()

    seite = client(w.leser).get(f"{base(w)}/meetings/{w.sitzung.pk}/protocol/")

    assert seite.status_code == 200
    html = seite.content.decode()
    for eintrag in ERWARTET.values():
        assert eintrag in html
