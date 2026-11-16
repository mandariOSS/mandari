# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zusammenfassung auf Abruf und Rücknahme schließen einander aus.

Eine Zusammenfassung fasst den Text aller Anlagen eines Vorgangs zusammen. Wird der Vorgang oder eine Anlage
zurückgenommen, darf ihr Text nicht in einer gespeicherten Zusammenfassung weiterleben:

- Der Dienst prüft nach dem KI-Aufruf, ob etwas zurückgenommen wurde, und noch einmal unmittelbar vor dem Speichern,
  dann unter der Sperre des Vorgangs. Eine Rücknahme zwischen beiden Prüfungen führt zu keiner gespeicherten
  Zusammenfassung mehr.
- Die Rücknahme schreibt die Zeile des Vorgangs auch dann, wenn noch keine Zusammenfassung da ist. Nur so wartet sie
  auf ein gleichzeitiges Speichern und verwirft danach, was eben gespeichert wurde.

Nur erfundene Namen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from itertools import count
from typing import Any, cast

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from insight_ai.services.summarizer import SummaryRevokedError, SummaryService
from insight_core.models import (
    REASON_PRIVACY,
    REASON_WITHDRAWN,
    OParlBody,
    OParlFile,
    OParlPaper,
    OParlSource,
)
from insight_core.services import summary_store

pytestmark = pytest.mark.django_db

RIS = "https://ris.example.org/oparl"
_nummer = count()


@dataclass
class _Antwort:
    content: str
    input_tokens: int = 1
    output_tokens: int = 1


class _Anbieter:
    """
    Ersatz für den KI-Anbieter. ``zwischen`` läuft genau einmal, wenn der Dienst den Hinweis auf den Anbieter liest:
    nach seiner ersten Prüfung auf Rücknahmen und vor dem Speichern.
    """

    def __init__(self, zwischen: Callable[[], None] | None = None) -> None:
        self.aufrufe = 0
        self._zwischen = zwischen

    def is_available(self) -> bool:
        return True

    def chat_completion(self, messages: list[Any], **kwargs: Any) -> _Antwort:
        self.aufrufe += 1
        return _Antwort("Kurzfassung")

    @property
    def zusammenfassung_hinweis(self) -> str:
        if self._zwischen is not None:
            zwischen, self._zwischen = self._zwischen, None
            zwischen()
        return "Erstellt mit einem Testmodell"

    @property
    def zwischenschritt_gelaufen(self) -> bool:
        return self._zwischen is None


@pytest.fixture
def vorgang() -> OParlPaper:
    quelle = OParlSource.objects.create(name="Quelle", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=quelle, name="Stadt")
    return OParlPaper.objects.create(external_id=f"{RIS}/paper/{next(_nummer)}", body=body, name="Vorlage")


def _anlage(paper: OParlPaper) -> OParlFile:
    nummer = next(_nummer)
    return OParlFile.objects.create(
        external_id=f"{RIS}/file/{nummer}",
        body=paper.body,
        paper=paper,
        name=f"Anlage {nummer}",
        mime_type="application/pdf",
        text_extraction_status="completed",
        text_content="Text der Anlage",
        text_extracted_at=timezone.now() - timedelta(days=1),
    )


def _anlage_zuruecknehmen(paper: OParlPaper, anlage: OParlFile) -> None:
    anlage.mark_deleted(reason=REASON_WITHDRAWN)


def _vorgang_zuruecknehmen(paper: OParlPaper, anlage: OParlFile) -> None:
    OParlPaper.objects.get(pk=paper.pk).mark_deleted(reason=REASON_PRIVACY)


def _anlage_in_der_quelle_verschwunden(paper: OParlPaper, anlage: OParlFile) -> None:
    OParlFile.objects.filter(pk=anlage.pk).update(source_missing_since=timezone.now())


def _vorgang_endgueltig_geloescht(paper: OParlPaper, anlage: OParlFile) -> None:
    OParlFile.objects.filter(pk=anlage.pk).delete()
    OParlPaper.objects.filter(pk=paper.pk).delete()


@pytest.mark.parametrize(
    "ruecknahme",
    [
        _anlage_zuruecknehmen,
        _vorgang_zuruecknehmen,
        _anlage_in_der_quelle_verschwunden,
        _vorgang_endgueltig_geloescht,
    ],
    ids=["anlage", "vorgang", "anlage_verschwunden", "vorgang_geloescht"],
)
def test_ruecknahme_vor_dem_speichern_speichert_und_zeigt_nichts(
    vorgang: OParlPaper, ruecknahme: Callable[[OParlPaper, OParlFile], None]
) -> None:
    """
    Die Rücknahme fällt hinter die erste Prüfung des Dienstes. Sie findet noch keine Zusammenfassung vor, die sie
    verwerfen könnte; ohne die zweite Prüfung unter der Sperre legte das Speichern danach eine ab.
    """
    anlage = _anlage(vorgang)
    anbieter = _Anbieter(zwischen=lambda: ruecknahme(vorgang, anlage))

    with pytest.raises(SummaryRevokedError):
        SummaryService(provider=anbieter).generate_summary(vorgang)

    assert anbieter.aufrufe == 1 and anbieter.zwischenschritt_gelaufen
    assert not OParlPaper.objects.filter(pk=vorgang.pk).exclude(summary__isnull=True).exists()


def test_ohne_ruecknahme_wird_unter_der_sperre_gespeichert(
    vorgang: OParlPaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    _anlage(vorgang)
    gesperrt: list[tuple[Any, str | None]] = []
    sperren = summary_store.sperren

    def merken(vorgang_id: Any) -> bool:
        # Gesperrt wird vor dem Speichern: Zu diesem Zeitpunkt steht noch keine Zusammenfassung in der Zeile
        gesperrt.append((vorgang_id, OParlPaper.objects.get(pk=vorgang_id).summary))
        return sperren(vorgang_id)

    monkeypatch.setattr(summary_store, "sperren", merken)

    assert SummaryService(provider=_Anbieter()).generate_summary(vorgang) == "Kurzfassung"

    assert gesperrt == [(vorgang.pk, None)]
    vorgang.refresh_from_db()
    assert (vorgang.summary, vorgang.summary_hinweis) == ("Kurzfassung", "Erstellt mit einem Testmodell")


def test_ohne_speichern_keine_sperre(vorgang: OParlPaper, monkeypatch: pytest.MonkeyPatch) -> None:
    """``save=False`` schreibt nichts und braucht deshalb weder Sperre noch Transaktion."""
    _anlage(vorgang)

    def nie(vorgang_id: Any) -> bool:
        raise AssertionError("ohne Speichern wird nicht gesperrt")

    monkeypatch.setattr(summary_store, "sperren", nie)

    assert SummaryService(provider=_Anbieter()).generate_summary(vorgang, save=False) == "Kurzfassung"
    vorgang.refresh_from_db()
    assert vorgang.summary is None


def test_sperren_meldet_einen_fehlenden_vorgang(vorgang: OParlPaper) -> None:
    assert summary_store.sperren(vorgang.pk) is True
    kennung = vorgang.pk
    OParlPaper.objects.filter(pk=kennung).delete()
    assert summary_store.sperren(kennung) is False


@pytest.mark.parametrize("mit_zusammenfassung", [False, True], ids=["ohne", "mit"])
def test_ruecknahme_schreibt_die_zeile_des_vorgangs_in_jedem_fall(
    vorgang: OParlPaper, mit_zusammenfassung: bool
) -> None:
    """
    Das Verwerfen ist ein ``UPDATE`` auf die Zeile des Vorgangs ohne Bedingung auf eine vorhandene Zusammenfassung.
    Mit der Bedingung träfe es während eines gleichzeitigen Speicherns keine Zeile und wartete nicht auf dessen Sperre;
    die Zusammenfassung aus der zurückgenommenen Anlage bliebe stehen.
    """
    anlage = _anlage(vorgang)
    if mit_zusammenfassung:
        OParlPaper.objects.filter(pk=vorgang.pk).update(summary="Alt", summary_einfach="Alt", summary_stand="abc")
    tabelle = OParlPaper._meta.db_table

    with CaptureQueriesContext(connection) as abfragen:
        cast(Any, anlage)._forget_summaries()

    geschrieben = [a["sql"] for a in abfragen if a["sql"].startswith("UPDATE") and f'"{tabelle}"' in a["sql"]]
    assert len(geschrieben) == 1, geschrieben
    assert "IS NOT NULL" not in geschrieben[0]
    vorgang.refresh_from_db()
    assert (vorgang.summary, vorgang.summary_einfach, vorgang.summary_stand) == (None, None, "")
