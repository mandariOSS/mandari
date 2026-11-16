# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorrang der Texterkennung aus der Zusammenfassung in den Grenzen der Dokumentkette.

Die Zusammenfassung eines Vorgangs lässt sich ohne Anmeldung auslösen und plant die Erkennung seiner Anlagen mit
Vorrang ein. Das darf weder die erste Stufe (``text``) umgehen noch die Warteschlange ``ocr`` über ihre Tiefe füllen
noch ihren einen Platz über viele Anfragen für eine Quelle belegen.

Nur erfundene Namen.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import count
from typing import Any

import pytest
from django.tasks import task_backends

from apps.events.models import Task, TaskStatus
from apps.events.tasks_backend import JournalBackend
from hub.ris import erkennung
from insight_ai.services import summarizer
from insight_ai.services.summarizer import EXTRACTION_PRIORITY, MAX_PRIORITY_WAITING, SummaryService, TextPendingError
from insight_core.background_tasks import file_extract_text
from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlSource
from insight_core.services.text_extraction_job import waiting_tasks

pytestmark = pytest.mark.django_db

_nummer = count()


class _Anbieter:
    """Eingerichteter Anbieter, der nie gefragt wird: Ohne Text entsteht keine Zusammenfassung."""

    def is_available(self) -> bool:
        return True

    def chat_completion(self, messages: list[Any], **kwargs: Any) -> Any:
        raise AssertionError("ohne Text keine Anfrage an den Anbieter")


@pytest.fixture(autouse=True)
def journal(settings: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[JournalBackend]:
    """Aufträge landen im Journal, der Worker erkennt den Text; kein Anbieter wird je gefragt."""
    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": "apps.events.tasks_backend.JournalBackend"}}
    settings.TEXT_EXTRACTION_RUNNER = "worker"
    monkeypatch.setattr("insight_ai.services.summarizer.get_summary_provider", lambda: _Anbieter())
    backend = task_backends["default"]
    assert isinstance(backend, JournalBackend)
    yield backend


def _kommune(name: str) -> OParlBody:
    quelle = OParlSource.objects.create(name=f"Quelle {name}", url=f"https://ris-{name}.example.org/oparl/system")
    return OParlBody.objects.create(
        external_id=f"https://ris-{name}.example.org/oparl/body/1", source=quelle, name=f"Stadt {name}"
    )


def _vorgang(body: OParlBody, anlagen: int, *, vermerkt: bool = False) -> tuple[OParlPaper, list[OParlFile]]:
    """Vorgang mit abgelegten Anlagen, deren Text noch aussteht; ``vermerkt``: Die erste Stufe verlangt die Erkennung."""
    paper = OParlPaper.objects.create(
        external_id=f"{body.external_id}/paper/{next(_nummer)}", body=body, name="Vorlage"
    )
    dateien = []
    for _ in range(anlagen):
        nummer = next(_nummer)
        inhalt = f"{nummer:064x}"
        dateien.append(
            OParlFile.objects.create(
                external_id=f"{body.external_id}/file/{nummer}",
                body=body,
                paper=paper,
                name=f"Anlage {nummer}",
                download_url=f"https://ris.example.org/dokumente/{nummer}.pdf",
                mime_type="application/pdf",
                text_extraction_status="pending",
                local_status="ok",
                sha256_hash=inhalt,
                text_ocr_needed_sha256=inhalt if vermerkt else None,
            )
        )
    return paper, dateien


def _klick(paper: OParlPaper) -> None:
    with pytest.raises(TextPendingError):
        SummaryService().generate_summary(paper)


def _vorrang(queue: str) -> set[str]:
    """Dateien der wartenden Aufträge mit Vorrang in ``queue``."""
    zeilen = Task.objects.filter(queue=queue, status=TaskStatus.WARTEND, priority__gte=EXTRACTION_PRIORITY)
    return {str(zeile.args["args"][0]) for zeile in zeilen}


def test_viele_anfragen_belegen_den_platz_nicht_fuer_eine_quelle() -> None:
    """Acht Vorgänge einer Quelle mit je drei Anlagen: In ``ocr`` warten höchstens so viele wie aus einer Anfrage."""
    stadt = _kommune("a")
    vorgaenge = [_vorgang(stadt, 3) for _ in range(8)]

    for paper, _ in vorgaenge:
        _klick(paper)

    assert MAX_PRIORITY_WAITING == 3
    assert _vorrang("ocr") == {str(datei.pk) for datei in vorgaenge[0][1]}
    assert Task.objects.count() == 3
    # Eine andere Quelle bekommt ihren Auftrag auch dann erst, wenn wieder Platz ist
    paper, _ = _vorgang(_kommune("b"), 1)
    _klick(paper)
    assert Task.objects.count() == 3


def test_weitere_anfragen_fuer_dieselbe_quelle_warten_andere_quellen_nicht() -> None:
    stadt_a, stadt_b = _kommune("a"), _kommune("b")
    erster, erste_dateien = _vorgang(stadt_a, 1)
    zweiter, _ = _vorgang(stadt_a, 1)
    dritter, dritte_dateien = _vorgang(stadt_b, 1)

    for paper in (erster, zweiter, dritter):
        _klick(paper)

    # Der zweite Vorgang derselben Quelle bekommt keinen Vorrang, solange der erste wartet; die andere Quelle schon
    assert _vorrang("ocr") == {str(erste_dateien[0].pk), str(dritte_dateien[0].pk)}


def test_vorrang_fuellt_die_warteschlange_nicht_ueber_ihre_tiefe(settings: Any) -> None:
    settings.TEXT_EXTRACTION_QUEUE_DEPTH = 2
    stadt = _kommune("a")
    _, wartende = _vorgang(stadt, 2)
    for datei in wartende:
        file_extract_text.enqueue(str(datei.pk))
    paper, _ = _vorgang(_kommune("b"), 3)

    _klick(paper)

    assert _vorrang("ocr") == set() and waiting_tasks("ocr") == 2


def test_ohne_vermerk_geht_die_datei_mit_vorrang_an_die_erste_stufe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bedient ein Worker ``text``, landen nur Dateien in ``ocr``, für die die erste Stufe die Erkennung verlangt."""
    monkeypatch.setattr(erkennung, "text_stufe_aktiv", lambda: True)
    stadt = _kommune("a")
    ohne, ohne_dateien = _vorgang(stadt, 3)
    mit, mit_dateien = _vorgang(_kommune("b"), 2, vermerkt=True)

    for _ in range(2):  # ein wiederholter Klick reiht nichts doppelt ein, in keiner Stufe
        _klick(ohne)
        _klick(mit)

    assert _vorrang("text") == {str(datei.pk) for datei in ohne_dateien}
    assert _vorrang("ocr") == {str(datei.pk) for datei in mit_dateien}
    assert Task.objects.count() == 5


def test_unbekannte_datei_und_leere_liste_reihen_nichts_ein() -> None:
    assert summarizer.plan_priority_extraction([]) == 0
    assert summarizer.plan_priority_extraction(["00000000-0000-0000-0000-000000000000"]) == 0
    assert not Task.objects.exists()
