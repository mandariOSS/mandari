# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlussfassung aus den Spalten des RIS-Bestands (Issue #525): offene Schnittstelle, Lese-Fassade und die
Datenmigration, die bestehende Zeilen aus ``raw_json`` befüllt.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from django.apps import apps as django_apps
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from mandari_oparl.extensions import agenda_item_columns, meeting_columns

from hub.api.tests.konformitaet import pruefe
from hub.ris import selectors
from hub.ris.tests import test_mapping_bestand as bestand
from hub.ris.tests.test_mapping_bestand import RIS, _abbildung, _json
from insight_core.models import OParlAgendaItem, OParlMeeting

pytestmark = pytest.mark.django_db

#: Bestand einer Kommune aus den Tests der Abbildung (Sitzung, Tagesordnungspunkt, Vorlage …)
welt = bestand.welt

MIGRATION = importlib.import_module("insight_core.migrations.0049_beschlussfassung_befuellen")

TOP_JSON: dict[str, Any] = {
    "mandari:resolutionNumber": "B/2026/7",
    "mandari:vote": {"method": "roll_call", "methodLabel": "Namentlich", "result": "approved", "yes": 2, "no": 1},
    "mandari:rollCall": [
        {"name": "Petra Muster", "vote": "yes", "voteLabel": "Ja"},
        {"name": "Max Beispiel", "vote": "no", "voteLabel": "Nein"},
    ],
    "mandari:implementation": {
        "status": "done",
        "statusLabel": "Erledigt",
        "deadline": "2026-12-31",
        "note": "Umgesetzt.",
        "modified": "2026-09-20T10:00:00+00:00",
    },
}


def test_schnittstelle_gibt_die_beschlussfassung_aus_den_spalten_aus(welt: dict[str, Any]) -> None:
    top, sitzung = welt["agendaitem"], welt["meeting"]
    OParlAgendaItem.objects.filter(pk=top.pk).update(**agenda_item_columns(TOP_JSON))
    folge = OParlMeeting.objects.create(external_id=f"{RIS}/meeting/9", body=welt["body"], name="Folgesitzung")
    genehmigung = {"mode": "follow_up", "date": "2026-09-15", "meeting": folge.external_id}
    OParlMeeting.objects.filter(pk=sitzung.pk).update(**meeting_columns({"mandari:protocolApproval": genehmigung}))

    daten = _json(f"/oparl/v1/meeting/{sitzung.pk}")

    assert daten["mandari:protocolApproval"] == {
        "mode": "follow_up",
        "date": "2026-09-15",
        # Verweis auf die eigene Schnittstelle, nicht auf die Quelle
        "meeting": _abbildung().uris.obj("meeting", folge.pk),
    }
    punkt = daten["agendaItem"][0]
    assert punkt["mandari:resolutionNumber"] == "B/2026/7"
    assert punkt["mandari:vote"] == {
        "method": "roll_call",
        "methodLabel": "Namentlich",
        "result": "approved",
        "resultLabel": "Angenommen",
        "yes": 2,
        "no": 1,
    }
    assert punkt["mandari:rollCall"] == TOP_JSON["mandari:rollCall"]
    assert punkt["mandari:implementation"] == TOP_JSON["mandari:implementation"]
    assert pruefe(daten, "Meeting") == []


def test_genehmigende_sitzung_ausserhalb_des_bestands_wird_nicht_genannt(welt: dict[str, Any]) -> None:
    sitzung = welt["meeting"]
    genehmigung = {"mode": "follow_up", "date": "2026-09-15", "meeting": f"{RIS}/meeting/unbekannt"}
    OParlMeeting.objects.filter(pk=sitzung.pk).update(**meeting_columns({"mandari:protocolApproval": genehmigung}))

    assert _json(f"/oparl/v1/meeting/{sitzung.pk}")["mandari:protocolApproval"] == {
        "mode": "follow_up",
        "date": "2026-09-15",
    }
    gelesen = selectors.protocol_approval(OParlMeeting.objects.get(pk=sitzung.pk))
    assert gelesen is not None and gelesen.approved_in is None


def test_genehmigende_sitzungen_kosten_eine_abfrage_je_seite(welt: dict[str, Any]) -> None:
    pfad = f"/oparl/v1/body/{welt['body'].pk}/meetings"
    folge = OParlMeeting.objects.create(external_id=f"{RIS}/meeting/9", body=welt["body"], name="Folgesitzung")
    genehmigung = {"mandari:protocolApproval": {"mode": "follow_up", "meeting": folge.external_id}}

    def abfragen() -> int:
        cache.clear()
        with CaptureQueriesContext(connection) as erfasst:
            _json(pfad)
        return len(erfasst)

    OParlMeeting.objects.filter(pk=welt["meeting"].pk).update(**meeting_columns(genehmigung))
    eine = abfragen()
    for nummer in range(2, 6):
        OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/{nummer}", body=welt["body"], name="Sitzung", **meeting_columns(genehmigung)
        )

    assert abfragen() == eine


def test_ohne_erweiterungen_bleibt_alles_leer(welt: dict[str, Any]) -> None:
    top = welt["agendaitem"]

    beschluss = selectors.decision(top)

    assert beschluss == selectors.Decision(resolution_number=None, vote=None, roll_call=None, implementation=None)
    assert selectors.protocol_approval(welt["meeting"]) is None
    punkt = _json(f"/oparl/v1/agendaitem/{top.pk}")
    assert not {k for k in punkt if k.startswith("mandari:") and k != "mandari:originalId"}


def test_einzelstimmen_nur_bei_namentlicher_abstimmung(welt: dict[str, Any]) -> None:
    top = welt["agendaitem"]
    offen = {**TOP_JSON, "mandari:vote": {**TOP_JSON["mandari:vote"], "method": "open"}}
    OParlAgendaItem.objects.filter(pk=top.pk).update(**agenda_item_columns(offen))
    top.refresh_from_db()
    assert top.roll_call is None
    # Auch eine Zeile, die (von Hand) Einzelstimmen trägt, gibt sie ohne namentliche Abstimmung nicht aus
    OParlAgendaItem.objects.filter(pk=top.pk).update(roll_call=[{"name": "Petra Muster", "vote": "yes"}])
    top.refresh_from_db()

    assert selectors.decision(top).roll_call is None
    assert "mandari:rollCall" not in _json(f"/oparl/v1/agendaitem/{top.pk}")


def test_datenmigration_befuellt_bestehende_zeilen_idempotent(welt: dict[str, Any]) -> None:
    top = welt["agendaitem"]
    OParlAgendaItem.objects.filter(pk=top.pk).update(raw_json=TOP_JSON)
    fremd = OParlAgendaItem.objects.create(
        external_id=f"{RIS}/agendaitem/2", meeting=welt["meeting"], name="Fremd", raw_json={"result": "angenommen"}
    )

    MIGRATION.befuellen(django_apps, None)
    top.refresh_from_db()
    erwartet = agenda_item_columns(TOP_JSON)
    assert {name: getattr(top, name) for name in erwartet} == erwartet
    fremd.refresh_from_db()
    assert fremd.vote_result is None and fremd.resolution_number is None

    # Ein zweiter Lauf ändert nichts
    MIGRATION.befuellen(django_apps, None)
    top.refresh_from_db()
    assert {name: getattr(top, name) for name in erwartet} == erwartet
