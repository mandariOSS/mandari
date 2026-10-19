# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kennzeichnung der KI-Zusammenfassung eines Vorgangs (EU AI Act Art. 50, Issue #938, docs/KI_TRANSPARENZ.md).

Sichtbar steht unter dem Text „Mit KI aus den Dokumenten erstellt. Die Zusammenfassung kann Fehler enthalten.“,
maschinenlesbar trägt genau der KI-Text ``data-ai-generated="true"`` – auf der Vorgangsseite wie im HTMX-Ausschnitt
nach dem Erstellen. Die OParl-Schnittstelle kennzeichnet sie mit ``mandari:summaryAiGenerated``
(``hub/ris/tests/test_mapping_bestand.py``), Description und JSON-LD mit „KI-Zusammenfassung:“
(``test_seo_titel.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pytest
from django.test import Client

from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db

SICHTBAR = "Mit KI aus den Dokumenten erstellt. Die Zusammenfassung kann Fehler enthalten."
#: Der gekennzeichnete Block und sein Inhalt (``linebreaks`` setzt ihn in Absätze)
MERKMAL_RE = re.compile(r'<div[^>]*data-ai-generated="true"[^>]*>(.*?)</div>', re.S)


@dataclass
class _Antwort:
    content: str
    input_tokens: int = 1
    output_tokens: int = 1


class _Provider:
    def is_available(self) -> bool:
        return True

    def chat_completion(self, messages: list[Any], **kwargs: Any) -> _Antwort:
        return _Antwort("Der Radweg wird gebaut.")


@pytest.fixture
def paper() -> OParlPaper:
    source = OParlSource.objects.create(name="Fremd-RIS", url="https://ris.fremd.example/oparl/system")
    body = OParlBody.objects.create(
        external_id="https://ris.fremd.example/oparl/body/1", source=source, name="Beispielstadt", slug="beispiel"
    )
    vorgang = OParlPaper.objects.create(external_id="https://ris.fremd.example/oparl/paper/1", body=body, name="Radweg")
    OParlFile.objects.create(
        external_id="https://ris.fremd.example/oparl/file/1",
        body=body,
        paper=vorgang,
        name="Vorlage",
        text_content="Der Rat beschließt den Radweg.",
    )
    return vorgang


def _gekennzeichnet(inhalt: str) -> list[str]:
    return [" ".join(re.sub(r"<[^>]+>", " ", block).split()) for block in MERKMAL_RE.findall(inhalt)]


def test_vorgangsseite_kennzeichnet_gespeicherte_zusammenfassung(paper: OParlPaper) -> None:
    OParlPaper.objects.filter(pk=paper.pk).update(summary="Der Radweg wird gebaut.")

    inhalt = Client().get(f"/insight/vorgaenge/{paper.id}/").content.decode()

    assert _gekennzeichnet(inhalt) == ["Der Radweg wird gebaut."]
    assert SICHTBAR in inhalt


def test_vorgangsseite_ohne_zusammenfassung_ohne_merkmal(paper: OParlPaper) -> None:
    inhalt = Client().get(f"/insight/vorgaenge/{paper.id}/").content.decode()

    assert "data-ai-generated" not in inhalt and SICHTBAR not in inhalt


def test_neu_erstellte_zusammenfassung_ist_gekennzeichnet(paper: OParlPaper, monkeypatch: Any) -> None:
    monkeypatch.setattr("insight_ai.services.summarizer.get_insight_provider", lambda: _Provider())

    antwort = Client().post(f"/insight/vorgaenge/{paper.id}/zusammenfassung/", HTTP_HX_REQUEST="true")

    assert antwort.status_code == 200
    inhalt = antwort.content.decode()
    assert _gekennzeichnet(inhalt) == ["Der Radweg wird gebaut."]
    assert SICHTBAR in inhalt
