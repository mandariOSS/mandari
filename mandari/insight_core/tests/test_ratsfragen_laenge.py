# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratsfragen und Antworten sind serverseitig auf die angekündigte Länge begrenzt (Frage 2000, Antwort 5000 Zeichen),
bevor sie gespeichert und in die Moderationsmail gerendert werden. Bisher stand die Grenze nur im ``maxlength`` des
Eingabefelds. Ein Zeilenumbruch zählt wie im Browser als ein Zeichen, auch wenn er als ``\\r\\n`` ankommt.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core import mail
from django.test import Client

from insight_core.forms import ANSWER_TEXT_MAX_LENGTH, QUESTION_TEXT_MAX_LENGTH, PublicAnswerForm, PublicQuestionForm
from insight_core.models import OParlBody, OParlMembership, OParlOrganization, OParlPerson, OParlSource, PublicQuestion

pytestmark = pytest.mark.django_db

RIS = "https://ris.laenge.example/oparl"
FORMULAR = {
    "questioner_name": "Frieda",
    "questioner_email": "frieda@example.org",
    "subject": "Radweg Hauptstraße",
    "topic": "verkehr",
    "privacy_accepted": "on",
}


def _frage(text: str) -> PublicQuestionForm:
    return PublicQuestionForm(data={**FORMULAR, "question_text": text})


def _antwort(text: str) -> PublicAnswerForm:
    return PublicAnswerForm(data={"answer_text": text})


def test_grenzen_wie_angekuendigt() -> None:
    assert (QUESTION_TEXT_MAX_LENGTH, ANSWER_TEXT_MAX_LENGTH) == (2000, 5000)
    assert PublicQuestionForm().fields["question_text"].widget.attrs["maxlength"] == QUESTION_TEXT_MAX_LENGTH
    assert PublicAnswerForm().fields["answer_text"].widget.attrs["maxlength"] == ANSWER_TEXT_MAX_LENGTH


def test_frage_bis_zur_grenze_gueltig() -> None:
    assert _frage("x" * QUESTION_TEXT_MAX_LENGTH).is_valid()
    # Wie im Browser: 50 Umbrüche als \r\n zählen 50 Zeichen, nicht 100; der Text bleibt unverändert
    text = "x" * (QUESTION_TEXT_MAX_LENGTH - 150) + "\r\nab" * 50
    assert len(text) > QUESTION_TEXT_MAX_LENGTH
    form = _frage(text)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["question_text"] == text


def test_frage_ueber_der_grenze_abgelehnt() -> None:
    form = _frage("x" * (QUESTION_TEXT_MAX_LENGTH + 1))
    assert not form.is_valid()
    assert form.errors["question_text"] == ["Die Frage darf höchstens 2000 Zeichen lang sein."]


def test_mindestlaenge_der_frage_unveraendert() -> None:
    form = _frage("Zu kurz?")
    assert not form.is_valid()
    assert form.errors["question_text"] == ["Die Frage muss mindestens 50 Zeichen lang sein."]


def test_antwort_bis_zur_grenze_gueltig() -> None:
    assert _antwort("y" * ANSWER_TEXT_MAX_LENGTH).is_valid()
    text = "y" * (ANSWER_TEXT_MAX_LENGTH - 150) + "\r\nab" * 50
    assert len(text) > ANSWER_TEXT_MAX_LENGTH
    form = _antwort(text)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["answer_text"] == text


def test_antwort_ueber_der_grenze_abgelehnt() -> None:
    form = _antwort("y" * (ANSWER_TEXT_MAX_LENGTH + 1))
    assert not form.is_valid()
    assert form.errors["answer_text"] == ["Die Antwort darf höchstens 5000 Zeichen lang sein."]
    zu_kurz = _antwort("Ja.")
    assert not zu_kurz.is_valid()
    assert zu_kurz.errors["answer_text"] == ["Die Antwort muss mindestens 20 Zeichen lang sein."]


@pytest.fixture
def person() -> OParlPerson:
    source = OParlSource.objects.create(name="Längen-RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=source, name="Längenstadt", slug="laengenstadt", is_listed=True
    )
    fraktion = OParlOrganization.objects.create(
        external_id=f"{RIS}/organization/1", body=body, name="Fraktion Mitte", organization_type="Fraktion"
    )
    anna = OParlPerson.objects.create(
        external_id=f"{RIS}/person/1", body=body, name="Anna Rat", family_name="Rat", email="anna@example.org"
    )
    OParlMembership.objects.create(external_id=f"{RIS}/membership/1", person=anna, organization=fraktion)
    return anna


def test_zu_lange_frage_wird_nicht_gespeichert(person: OParlPerson, settings: Any) -> None:
    """Eine Frage bis zur Körpergrenze der Anfrage endet im Formular: nichts gespeichert, keine Mail."""
    settings.INSIGHT_QUESTIONS_ENABLED = True  # Ratsfragen sind standardmäßig pausiert (#734)
    client = Client(REMOTE_ADDR="198.51.100.31")
    client.get(f"/insight/kommune/{person.body_id}/")
    mail.outbox.clear()

    antwort = client.post(
        f"/insight/personen/{person.id}/frage-stellen/", {**FORMULAR, "question_text": "x" * 2_600_000}
    )

    assert antwort.status_code == 200
    assert "höchstens 2000 Zeichen" in antwort.content.decode()
    assert not PublicQuestion.objects.exists()
    assert mail.outbox == []
