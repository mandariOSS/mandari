# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``add_oparl_source`` hängt keine Kommune einer anderen Quelle um.

Bisher legte der Befehl jede Kommune der Antwort per ``update_or_create`` über ihre Kennung an und setzte dabei die
Quelle; eine Kommune einer anderen Quelle wanderte so an die neue Quelle, deren Abgleich danach ihre Objekte
schreiben durfte. Jetzt bricht der Befehl vor dem ersten Schreiben ab. Antworten kommen aus einem Ersatz der
Abrufmethoden, ohne Netz.
"""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from insight_core.management.commands import add_oparl_source
from insight_core.models import OParlBody, OParlSource

pytestmark = pytest.mark.django_db

SYSTEM = "https://ris.example.org/oparl/v1.1/system"
LISTE = "https://ris.example.org/oparl/v1.1/body"
FREMD = "https://andere.example.org/oparl/v1.1/body/1"
EIGEN = "https://ris.example.org/oparl/v1.1/body/2"


def _kommune(kennung: str, name: str) -> dict[str, Any]:
    return {"id": kennung, "type": "https://schema.oparl.org/1.1/Body", "name": name}


@pytest.fixture
def antworten(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    daten: dict[str, Any] = {
        SYSTEM: {"id": SYSTEM, "type": "https://schema.oparl.org/1.1/System", "name": "Neue Quelle", "body": LISTE},
        LISTE: {"data": [_kommune(FREMD, "Übernommen"), _kommune(EIGEN, "Eigene")], "links": {}},
    }
    monkeypatch.setattr(add_oparl_source.Command, "fetch_oparl_system", lambda self, url: daten.get(url))
    monkeypatch.setattr(add_oparl_source.Command, "fetch_oparl_body", lambda self, url: daten.get(url))
    return daten


def test_kommune_einer_anderen_quelle_bleibt_dort(antworten: dict[str, Any]) -> None:
    andere = OParlSource.objects.create(name="Andere", url="https://andere.example.org/oparl/v1.1/system")
    body = OParlBody.objects.create(source=andere, external_id=FREMD, name="Stadt B")

    with pytest.raises(CommandError, match="schon an einer anderen Quelle") as abbruch:
        call_command("add_oparl_source", SYSTEM, stdout=StringIO())

    assert andere.url in str(abbruch.value)
    # Nichts geschrieben: keine neue Quelle, keine neue Kommune, die vorhandene unverändert
    assert not OParlSource.objects.filter(url=SYSTEM).exists()
    assert not OParlBody.objects.filter(external_id=EIGEN).exists()
    body.refresh_from_db()
    assert (body.source_id, body.name) == (andere.pk, "Stadt B")


def test_eigene_kommunen_werden_wie_bisher_angelegt_und_aktualisiert(antworten: dict[str, Any]) -> None:
    antworten[LISTE]["data"] = [_kommune(EIGEN, "Eigene")]

    call_command("add_oparl_source", SYSTEM, stdout=StringIO())
    antworten[LISTE]["data"] = [_kommune(EIGEN, "Eigene, neuer Name")]
    call_command("add_oparl_source", SYSTEM, stdout=StringIO())

    source = OParlSource.objects.get(url=SYSTEM)
    assert list(OParlBody.objects.filter(source=source).values_list("external_id", "name")) == [
        (EIGEN, "Eigene, neuer Name")
    ]
