# SPDX-License-Identifier: AGPL-3.0-or-later
"""Werkzeuge des KI-Assistenten nennen keine Hausnummern von Privatanschriften (Issue #1269)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from insight_ai.services.chat_tools import ToolContext, context_for, run_tool
from insight_core.services.ortsschutz import OHNE_NR

from .musterstadt import JETZT, Musterstadt, baue_musterstadt

pytestmark = pytest.mark.django_db


@pytest.fixture
def stadt() -> Musterstadt:
    stadt = baue_musterstadt()
    # Die Vorlage wird zum Bürgerantrag mit Anschrift im Titel und im Text
    stadt.vorlage.name = "Radweg an der Musterstraße 12"
    stadt.vorlage.paper_type = "Bürgerantrag"
    stadt.vorlage.save(update_fields=["name", "paper_type"])
    stadt.datei.text_content = "Absender: Erika Mustermann, Gartenweg 7. Der Radweg wird auf 2,50 m verbreitert."
    stadt.datei.save(update_fields=["text_content"])
    return stadt


@pytest.fixture
def ctx(stadt: Musterstadt) -> ToolContext:
    kontext = context_for(stadt.body.pk, now=JETZT)
    assert kontext is not None
    return kontext


def rufe(ctx: ToolContext, werkzeug: str, /, **args: Any) -> dict[str, Any]:
    ergebnis = json.loads(run_tool(werkzeug, json.dumps(args), ctx))
    assert isinstance(ergebnis, dict)
    return ergebnis


def test_vorgang_ohne_hausnummer(ctx: ToolContext, stadt: Musterstadt) -> None:
    ergebnis = rufe(ctx, "vorgang", id=str(stadt.vorlage.pk))
    assert ergebnis["titel"] == f"Radweg an der Musterstraße {OHNE_NR}"
    assert "Musterstraße 12" not in json.dumps(ergebnis, ensure_ascii=False)
    # Auch die Quellenliste unter der Antwort
    assert all("Musterstraße 12" not in quelle["title"] for quelle in ctx.sources.values())


def test_dokumentabschnitt_ohne_hausnummer(ctx: ToolContext, stadt: Musterstadt) -> None:
    ergebnis = rufe(ctx, "dokument_abschnitt", id=str(stadt.datei.pk), frage="Wer hat den Antrag gestellt?")
    texte = " ".join(abschnitt["text"] for abschnitt in ergebnis["abschnitte"])
    assert f"Gartenweg {OHNE_NR}" in texte and "Gartenweg 7" not in texte
    assert ergebnis["vorgang"] == f"Radweg an der Musterstraße {OHNE_NR}"


def test_tagesordnung_ohne_hausnummer(ctx: ToolContext, stadt: Musterstadt) -> None:
    ergebnis = rufe(ctx, "sitzung", id=str(stadt.rat_sitzung.pk))
    assert "Musterstraße 12" not in json.dumps(ergebnis, ensure_ascii=False)
