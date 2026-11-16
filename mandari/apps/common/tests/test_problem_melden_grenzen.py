# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Problem melden“: Die Beschreibung hat eine feste Höchstlänge wie die übrigen Felder des Formulars.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.common.models import ProblemReport
from apps.common.views_feedback import MAX_MESSAGE_LENGTH

pytestmark = pytest.mark.django_db


def test_beschreibung_wird_auf_die_hoechstlaenge_gekuerzt(client: Client) -> None:
    antwort = client.post(reverse("problem_report"), {"message": "Die Seite lädt nicht. " + "x" * 20_000})

    assert antwort.status_code == 302
    meldung = ProblemReport.objects.get()
    assert len(meldung.message) == MAX_MESSAGE_LENGTH == 10_000
    assert meldung.message.startswith("Die Seite lädt nicht.")


def test_kurze_beschreibung_bleibt_unveraendert(client: Client) -> None:
    text = "Beim Speichern erscheint eine Fehlerseite."

    client.post(reverse("problem_report"), {"message": f"  {text}  "})

    assert ProblemReport.objects.get().message == text


def test_formular_begrenzt_die_eingabe(client: Client) -> None:
    seite = client.get(reverse("problem_report")).content.decode()

    assert f'maxlength="{MAX_MESSAGE_LENGTH}"' in seite
