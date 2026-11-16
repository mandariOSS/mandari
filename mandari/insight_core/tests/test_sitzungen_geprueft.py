# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einmalige Korrektur des Bestands: Sitzungen mit geprüften Angaben (Issue #1441, Migration 0072).

Der Ingestor prüft Name, Absage und Ende beim Schreiben; unveränderte Sitzungen fasst der inkrementelle Abgleich aber
nicht an. Die Migration bringt den Bestand auf denselben Stand: abgesagt, wenn die Absage nur im Namen steht, Name ohne
Vermerk, kein Ende vor dem Beginn.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from typing import Any

import pytest
from django.apps import apps

from insight_core.models import OParlBody, OParlMeeting, OParlSource

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("insight_core.migrations.0072_sitzungen_geprueft")
RIS = "https://ris.example.org"
BEGINN = datetime(2026, 11, 18, 17, 0, tzinfo=UTC)


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
    return OParlBody.objects.create(source=source, external_id=f"{RIS}/body/1", name="Musterstadt")


def sitzung(body: OParlBody, nr: int, name: str | None, **werte: Any) -> OParlMeeting:
    roh = werte.pop("raw_json", {"name": name})
    return OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/{nr}", body=body, name=name, start=BEGINN, raw_json=roh, **werte
    )


def test_bestand_bekommt_gepruefte_angaben(body: OParlBody) -> None:
    absage = sitzung(body, 1, "Beirat Sitzung wurde abgesagt | Mail Zettelkasten", end=BEGINN.replace(hour=0))
    entfaellt = sitzung(body, 2, "Rat - ENTFÄLLT -", meeting_state="terminiert")
    nur_vermerk = sitzung(body, 3, "Sitzung entfällt!")
    gehalten = sitzung(body, 4, "Rat (Ersatz, da die Sitzung im Mai entfällt)", meeting_state="durchgeführt")
    ohne_rohdaten = sitzung(body, 5, "Ausschuss (abgesagt)", raw_json={})
    kein_vermerk = sitzung(body, 6, "Beratung über abgesagte Veranstaltungen")
    ordentlich = sitzung(body, 7, "Schulausschuss", end=BEGINN.replace(hour=19))
    gleichzeitig = sitzung(body, 8, "Sportausschuss", end=BEGINN)
    vorher = {m.pk: m.updated_at for m in OParlMeeting.objects.all()}

    MIGRATION.pruefen(apps, None)

    def stand(m: OParlMeeting) -> tuple[str | None, bool, datetime | None]:
        m.refresh_from_db()
        return m.name, m.cancelled, m.end

    assert stand(absage) == ("Beirat", True, None)
    assert stand(entfaellt) == ("Rat", True, None)
    assert stand(nur_vermerk) == (None, True, None)
    # Hat die Sitzung laut Quelle stattgefunden, ist das Wort im Namen keine Absage
    assert stand(gehalten) == ("Rat (Ersatz, da die Sitzung im Mai entfällt)", False, None)
    assert stand(ohne_rohdaten) == ("Ausschuss", True, None)
    assert stand(kein_vermerk) == ("Beratung über abgesagte Veranstaltungen", False, None)
    assert stand(ordentlich) == ("Schulausschuss", False, BEGINN.replace(hour=19))
    assert stand(gleichzeitig) == ("Sportausschuss", False, None)
    # Die Angabe der Quelle bleibt nachvollziehbar, der Änderungszeitpunkt bleibt
    assert absage.raw_json == {"name": "Beirat Sitzung wurde abgesagt | Mail Zettelkasten"}
    assert {m.pk: m.updated_at for m in OParlMeeting.objects.all()} == vorher

    # Ein zweiter Lauf ändert nichts mehr
    MIGRATION.pruefen(apps, None)
    assert stand(absage) == ("Beirat", True, None)
    assert stand(entfaellt) == ("Rat", True, None)
