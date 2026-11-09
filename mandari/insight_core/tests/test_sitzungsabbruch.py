# SPDX-License-Identifier: AGPL-3.0-or-later
"""Abgebrochene oder insgesamt vertagte Sitzung als ein Hinweis statt vieler Zeilen „vertagt“ (Issue #1269)."""

from __future__ import annotations

import re
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource
from insight_core.services.ergebnis import ergebnis
from insight_core.services.sitzungsabbruch import MIN_PUNKTE, buendeln

ABBRUCH = "wg. Sitzungsabbruch nicht behandelt (vertagt)"


def _punkte(*ergebnisse: str | None) -> list[SimpleNamespace]:
    return [SimpleNamespace(ergebnis=ergebnis(roh)) for roh in ergebnisse]


def test_abgebrochene_sitzung() -> None:
    punkte = _punkte("Kenntnisnahme", *[ABBRUCH] * 20, None)
    hinweis = buendeln(punkte)
    assert hinweis is not None
    assert hinweis.ergebnis.schluessel == "vertagt" and hinweis.anzahl == 20 and hinweis.roh == ABBRUCH
    assert hinweis.satz == (
        "Die Sitzung wurde abgebrochen: 20 Punkte wurden nicht behandelt und vertagt. Ein Punkt hat ein eigenes Ergebnis."
    )
    assert [getattr(p, "gebuendelt", False) for p in punkte] == [False, *[True] * 20, False]


def test_insgesamt_vertagt_und_abgesetzt() -> None:
    hinweis = buendeln(_punkte(*["vertagt"] * 6))
    assert (
        hinweis is not None and hinweis.satz == "Die Sitzung wurde insgesamt vertagt: 6 Punkte wurden nicht behandelt."
    )
    hinweis = buendeln(_punkte(*["abgesetzt"] * 5, "Absetzung beschlossen"))
    assert hinweis is not None and hinweis.ergebnis.schluessel == "abgesetzt" and hinweis.roh == ""
    assert hinweis.satz == "6 Punkte wurden von der Tagesordnung abgesetzt und nicht beraten."


@pytest.mark.parametrize(
    "ergebnisse",
    [
        ["vertagt"] * (MIN_PUNKTE - 1),  # zu wenige
        ["vertagt"] * 6 + ["beschlossen"] * 2,  # eine gewöhnliche Sitzung mit einigen Vertagungen
        ["beschlossen"] * 10,
        [None] * 10,
    ],
)
def test_gewoehnliche_sitzung_bleibt_einzeln(ergebnisse: list[Any]) -> None:
    punkte = _punkte(*ergebnisse)
    assert buendeln(punkte) is None
    assert not any(getattr(p, "gebuendelt", False) for p in punkte)


@pytest.mark.django_db
def test_sitzungsseite_zeigt_einen_hinweis() -> None:
    quelle = OParlSource.objects.create(name="RIS Abbruchstadt", url="https://ris.abbruch.example/oparl/system")
    body = OParlBody.objects.create(
        external_id="https://ris.abbruch.example/oparl/body/1", source=quelle, name="Abbruchstadt", slug="abbruchstadt"
    )
    sitzung = OParlMeeting.objects.create(
        external_id="https://ris.abbruch.example/oparl/meeting/1",
        body=body,
        name="Bezirksvertretung Süd",
        start=timezone.now() - timedelta(days=3),
    )
    OParlAgendaItem.objects.create(
        external_id=f"{sitzung.external_id}/top/1",
        meeting=sitzung,
        number="1",
        name="Eröffnung",
        result="Kenntnisnahme",
    )
    for nummer in range(2, 32):
        OParlAgendaItem.objects.create(
            external_id=f"{sitzung.external_id}/top/{nummer}",
            meeting=sitzung,
            number=str(nummer),
            name=f"Anfrage {nummer}",
            result=ABBRUCH,
        )
    html = Client().get(f"/insight/termine/{sitzung.pk}/").content.decode()
    treffer = re.search(r'<section aria-labelledby="tagesordnung-titel">(.*?)</section>', html, re.S)
    assert treffer is not None
    teil = treffer.group(1)
    assert 'data-testid="sitzung-abbruch"' in teil
    assert "Die Sitzung wurde abgebrochen: 30 Punkte wurden nicht behandelt und vertagt." in teil
    assert f"Im Ratsinformationssystem: „{ABBRUCH}“" in teil
    # Ein Etikett „vertagt“ am Hinweis, eines „zur Kenntnis genommen“ am behandelten Punkt, keine 30 Zeilen
    assert teil.count('data-ergebnis="vertagt"') == 1
    assert teil.count('data-ergebnis="kenntnis"') == 1
    assert teil.count(ABBRUCH) == 1
    # Die Tagesordnung bleibt vollständig
    assert "Anfrage 31" in teil
