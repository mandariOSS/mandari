# SPDX-License-Identifier: AGPL-3.0-or-later
"""Namensregeln für Ereignisse und Befehle (Issue #517)."""

from __future__ import annotations

import pytest

from hub.contracts.naming import COMMAND, EVENT, check_name, last_word

# Startumfang laut Ereigniskatalog und ADR Befehle: alle Namen müssen die Regeln erfüllen.
EREIGNISSE = [
    "ris.meeting.scheduled",
    "ris.meeting.changed",
    "ris.meeting.invited",
    "ris.agendaitem.changed",
    "ris.paper.created",
    "ris.paper.released",
    "ris.consultation.changed",
    "ris.file.changed",
    "ris.file.text_extracted",
    "ris.voting.recorded",
    "ris.resolution.adopted",
    "ris.resolution.implementation_changed",
    "ris.protocol.approved",
    "ris.protocol.published",
    "ris.object.depublished",
    "ris.source.published",
    "submission.received",
    "submission.status_changed",
    "attendance.response_recorded",
    "session.allowance.approved",
    "session.payment.exported",
    "work.document.status_changed",
    "work.factionmeeting.invited",
    "work.task.assigned",
    "core.membership.changed",
    "core.user.registered",
]
BEFEHLE = ["submission.submit", "submission.withdraw", "attendance.respond", "invitation.acknowledge"]


@pytest.mark.parametrize("name", EREIGNISSE)
def test_ereignisse_des_startumfangs_sind_gueltig(name: str) -> None:
    assert check_name(name, EVENT) == []


@pytest.mark.parametrize("name", BEFEHLE)
def test_befehle_des_startumfangs_sind_gueltig(name: str) -> None:
    assert check_name(name, COMMAND) == []


@pytest.mark.parametrize(
    "name",
    [
        "ris",  # nur ein Teil
        "ris.paper.file.released",  # vier Teile
        "Ris.paper.released",  # Großbuchstaben
        "ris.Paper.released",
        "ris.paper-file.released",  # Bindestrich
        "ris.paper.status__changed",  # doppelter Unterstrich
        "ris.paper.changed_",  # Unterstrich am Ende
        "ris._paper.changed",
        "ris.paper.released.v2",  # Version im Namen
        "ris..released",
        "1ris.paper.released",
        " ris.paper.released",
        "ris.paper.released ",
        "ris.paper." + "x" * 100 + "ed",
    ],
)
def test_ungueltige_syntax_wird_abgelehnt(name: str) -> None:
    assert check_name(name, EVENT)


def test_name_muss_zeichenkette_sein() -> None:
    assert check_name(None, EVENT) == ["Name muss eine Zeichenkette sein"]


def test_unbekannter_bereich_wird_abgelehnt() -> None:
    problems = check_name("billing.invoice.paid", EVENT)
    assert len(problems) == 1
    assert "unbekannter Bereich „billing“" in problems[0]


@pytest.mark.parametrize("name", ["ris.paper.release", "ris.paper.status", "submission.submit", "work.task.assign"])
def test_ereignis_braucht_vergangenheitsform(name: str) -> None:
    problems = check_name(name, EVENT)
    assert len(problems) == 1
    assert "Vergangenheitsform" in problems[0]


@pytest.mark.parametrize("name", ["submission.submitted", "attendance.responded", "invitation.acknowledged"])
def test_befehl_braucht_imperativ(name: str) -> None:
    problems = check_name(name, COMMAND)
    assert len(problems) == 1
    assert "Imperativ" in problems[0]


@pytest.mark.parametrize("name", ["work.invitation.sent", "ris.meeting.held", "submission.withdrawn"])
def test_unregelmaessige_vergangenheit_gilt_fuer_ereignisse(name: str) -> None:
    assert check_name(name, EVENT) == []


def test_gleiche_form_ist_fuer_ereignis_und_befehl_zulaessig() -> None:
    assert check_name("core.password.reset", EVENT) == []
    assert check_name("core.password.reset", COMMAND) == []


def test_grundform_auf_ed_ist_keine_vergangenheit() -> None:
    assert check_name("work.task.feed", COMMAND) == []
    assert check_name("work.task.feed", EVENT)


def test_letztes_wort() -> None:
    assert last_word("submission.status_changed") == "changed"
    assert last_word("ris.paper.released") == "released"
