# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratsfragen pausieren und einfrieren (Issue #734), Schalter ``INSIGHT_QUESTIONS_ENABLED``, Standard aus.

Pausiert: Portal und Detailseiten bleiben lesbar (mit Hinweis), Stellen, Bestätigen und Antworten
antworten mit 404, keine Mails, keine Erinnerungen – auch nicht über den Cron-Befehl
``send_question_reminders`` oder die Admin-Aktionen –, keine Antwortquote je Person und Fraktion.
Eingeschaltet: Verhalten wie bisher.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from insight_core.models import OParlBody, OParlMembership, OParlOrganization, OParlPerson, OParlSource, PublicQuestion
from insight_core.services import question_service

pytestmark = pytest.mark.django_db

RIS = "https://ris.pause.example/oparl"
FORMULAR = {
    "questioner_name": "Frieda",
    "questioner_email": "frieda@example.org",
    "subject": "Radweg Hauptstraße",
    "question_text": "Wann kommt der Radweg an der Hauptstraße? Die Baustelle steht seit Monaten still.",
    "topic": "verkehr",
    "privacy_accepted": "on",
}


@pytest.fixture
def welt() -> dict[str, Any]:
    source = OParlSource.objects.create(name="Pause-RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=source, name="Pausestadt", slug="pausestadt", is_listed=True
    )
    fraktion = OParlOrganization.objects.create(
        external_id=f"{RIS}/organization/1", body=body, name="Fraktion Mitte", organization_type="Fraktion"
    )
    anna = OParlPerson.objects.create(
        external_id=f"{RIS}/person/1", body=body, name="Anna Rat", family_name="Rat", email="anna@example.org"
    )
    OParlMembership.objects.create(external_id=f"{RIS}/membership/1", person=anna, organization=fraktion)
    vor_30_tagen = timezone.now() - timedelta(days=30)
    offen = PublicQuestion.objects.create(
        body=body,
        recipient=anna,
        questioner_name="Bert",
        questioner_email="bert@example.org",
        subject="Offene Frage zur Schule",
        question_text="Wann wird die Schule saniert?",
        status="published",
        published_at=vor_30_tagen,
    )
    beantwortet = PublicQuestion.objects.create(
        body=body,
        recipient=anna,
        questioner_name="Clara",
        questioner_email="clara@example.org",
        subject="Beantwortete Frage zum Park",
        question_text="Wird der Park beleuchtet?",
        status="published",
        published_at=vor_30_tagen,
        answer_text="Ja, im Frühjahr.",
        answer_status="published",
        answered_at=vor_30_tagen + timedelta(days=3),
    )
    wartend = PublicQuestion.objects.create(
        body=body,
        recipient=anna,
        questioner_name="Dora",
        questioner_email="dora@example.org",
        subject="Wartende Frage",
        question_text="Kommt die Buslinie zurück?",
        status="pending",
    )
    unbestaetigt = PublicQuestion(
        body=body,
        recipient=anna,
        questioner_name="Emil",
        questioner_email="emil@example.org",
        subject="Unbestätigte Frage",
        question_text="Gibt es mehr Bänke?",
    )
    unbestaetigt.issue_token()
    unbestaetigt.save()
    return {
        "body": body,
        "fraktion": fraktion,
        "anna": anna,
        "offen": offen,
        "beantwortet": beantwortet,
        "wartend": wartend,
        "unbestaetigt": unbestaetigt,
    }


@pytest.fixture
def client(welt: dict[str, Any]) -> Client:
    client = Client()
    client.get(f"/insight/kommune/{welt['body'].id}/")
    return client


@pytest.fixture
def admin_client_(db: Any) -> Client:
    user = get_user_model()(email="admin@example.org", is_staff=True, is_superuser=True, is_active=True)
    user.set_password("geheim-123")
    user.save()
    client = Client()
    client.force_login(user)
    return client


def test_standard_ist_pausiert() -> None:
    assert django_settings.INSIGHT_QUESTIONS_ENABLED is False
    assert question_service.questions_enabled() is False


class TestPausiert:
    def test_keine_frage_stellbar(self, client: Client, welt: dict[str, Any]) -> None:
        anna = welt["anna"]
        mail.outbox.clear()
        assert client.get("/insight/fragen/stellen/").status_code == 404
        assert client.get(f"/insight/personen/{anna.id}/frage-stellen/").status_code == 404
        antwort = client.post(f"/insight/personen/{anna.id}/frage-stellen/", FORMULAR)
        assert antwort.status_code == 404
        assert not PublicQuestion.objects.filter(questioner_email="frieda@example.org").exists()
        assert client.get("/insight/fragen/gesendet/").status_code == 404
        assert mail.outbox == []

    def test_bestaetigen_und_antworten_gesperrt(self, client: Client, welt: dict[str, Any]) -> None:
        unbestaetigt, offen = welt["unbestaetigt"], welt["offen"]
        mail.outbox.clear()
        verifizieren = f"/insight/fragen/verifizieren/{unbestaetigt.plain_token}/"
        assert client.get(verifizieren).status_code == 404
        assert client.post(verifizieren).status_code == 404
        antworten = f"/insight/fragen/antworten/{offen.answer_token}/"
        assert client.get(antworten).status_code == 404
        assert client.post(antworten, {"answer_text": "Bald – wir sind dran. " * 3}).status_code == 404
        unbestaetigt.refresh_from_db()
        offen.refresh_from_db()
        assert unbestaetigt.status == "unverified"
        assert offen.answer_status == "none"
        assert mail.outbox == []

    def test_vorhandene_fragen_lesbar(self, client: Client, welt: dict[str, Any]) -> None:
        for frage in (welt["offen"], welt["beantwortet"]):
            seite = client.get(f"/insight/fragen/{frage.id}/")
            assert seite.status_code == 200
            inhalt = seite.content.decode()
            assert frage.subject in inhalt
            assert 'data-testid="ratsfragen-pausiert"' in inhalt
        assert "Ja, im Frühjahr." in client.get(f"/insight/fragen/{welt['beantwortet'].id}/").content.decode()

        portal = client.get("/insight/fragen/")
        assert portal.status_code == 200
        inhalt = portal.content.decode()
        assert "Offene Frage zur Schule" in inhalt and "Beantwortete Frage zum Park" in inhalt
        assert 'data-testid="ratsfragen-pausiert"' in inhalt
        assert "/insight/fragen/stellen/" not in inhalt
        assert "Ohne Antwort" in inhalt
        assert "Offen seit" not in inhalt

    def test_keine_antwortquote(self, client: Client, welt: dict[str, Any]) -> None:
        anna = welt["anna"]
        portal = client.get("/insight/fragen/")
        inhalt = portal.content.decode()
        assert portal.context["faction_ranking"] == []
        assert "Antwortquote nach Fraktion" not in inhalt
        assert ">Antwortquote</p>" not in inhalt
        assert "Ø Antwortzeit" not in inhalt
        assert "1 von 2 beantwortet" not in inhalt
        # Die Fraktion bleibt als Filter wählbar
        assert f'value="{welt["fraktion"].id}"' in inhalt

        detail = client.get(f"/insight/fragen/{welt['offen'].id}/")
        assert detail.context["answer_stats"] is None
        assert detail.context["can_ask"] is False
        detail_inhalt = detail.content.decode()
        assert "von 2 Fragen beantwortet" not in detail_inhalt
        assert "Antwortet im Schnitt" not in detail_inhalt
        assert "Weitere Frage stellen" not in detail_inhalt
        assert f"/insight/personen/{anna.id}/frage-stellen/" not in detail_inhalt

        person = client.get(f"/insight/personen/{anna.id}/")
        assert person.status_code == 200
        assert "answer_stats" not in person.context
        assert person.context["questions_tab"] is True
        person_inhalt = person.content.decode()
        assert "Offene Frage zur Schule" in person_inhalt
        assert 'data-testid="ratsfragen-pausiert"' in person_inhalt
        assert "1 von 2 Fragen beantwortet" not in person_inhalt
        assert f"/insight/personen/{anna.id}/frage-stellen/" not in person_inhalt

    def test_reiter_ohne_bisherige_fragen_ausgeblendet(self, client: Client, welt: dict[str, Any]) -> None:
        PublicQuestion.objects.filter(recipient=welt["anna"]).delete()
        person = client.get(f"/insight/personen/{welt['anna'].id}/")
        assert person.context["questions_tab"] is False
        assert "Seien Sie die erste Person" not in person.content.decode()

    def test_startseite_laedt_nicht_zum_fragen_ein(self, client: Client) -> None:
        inhalt = client.get("/insight/").content.decode()
        assert "Frag deine Ratsmitglieder" not in inhalt
        assert "derzeit pausiert" in inhalt

    def test_keine_mails_aus_dem_dienst(self, welt: dict[str, Any]) -> None:
        mail.outbox.clear()
        wartend, offen = welt["wartend"], welt["offen"]
        assert question_service.publish_question(wartend) is False
        wartend.refresh_from_db()
        assert wartend.status == "pending"
        assert question_service.send_verification_email(welt["unbestaetigt"]) is False
        assert question_service.send_question_notification_to_recipient(offen) is False
        assert question_service.send_question_published_to_questioner(offen) is False
        assert question_service.send_answer_notification_to_questioner(offen) is False
        assert question_service.send_answer_reminder(offen) is False
        assert question_service.send_moderation_notification(offen) is False
        assert question_service.send_due_reminders() == 0
        assert mail.outbox == []

    def test_cron_befehl_ohne_wirkung(self, welt: dict[str, Any]) -> None:
        mail.outbox.clear()
        ausgabe = StringIO()
        call_command("send_question_reminders", stdout=ausgabe)
        assert "INSIGHT_QUESTIONS_ENABLED" in ausgabe.getvalue()
        welt["offen"].refresh_from_db()
        assert welt["offen"].reminder_sent_at is None
        assert mail.outbox == []

    def test_admin_aktionen_ohne_wirkung(self, admin_client_: Client, welt: dict[str, Any]) -> None:
        url = reverse("admin:insight_core_publicquestion_changelist")
        mail.outbox.clear()
        for aktion, frage in (("approve_questions", welt["wartend"]), ("send_reminders", welt["offen"])):
            antwort = admin_client_.post(url, {"action": aktion, "_selected_action": [str(frage.pk)]}, follow=True)
            assert antwort.status_code == 200
            assert "Ratsfragen sind pausiert" in antwort.content.decode()
        welt["wartend"].refresh_from_db()
        welt["offen"].refresh_from_db()
        assert welt["wartend"].status == "pending"
        assert welt["offen"].reminder_sent_at is None
        assert mail.outbox == []


class TestEingeschaltet:
    @pytest.fixture(autouse=True)
    def _an(self, settings: Any) -> None:
        settings.INSIGHT_QUESTIONS_ENABLED = True

    def test_frage_stellen_wie_bisher(self, client: Client, welt: dict[str, Any]) -> None:
        anna = welt["anna"]
        mail.outbox.clear()
        assert client.get("/insight/fragen/stellen/").status_code == 200
        assert client.get(f"/insight/personen/{anna.id}/frage-stellen/").status_code == 200
        antwort = client.post(f"/insight/personen/{anna.id}/frage-stellen/", FORMULAR)
        assert antwort.status_code == 302
        assert PublicQuestion.objects.filter(questioner_email="frieda@example.org", status="unverified").exists()
        assert len(mail.outbox) == 1

    def test_antwortquote_sichtbar(self, client: Client, welt: dict[str, Any]) -> None:
        inhalt = client.get("/insight/fragen/").content.decode()
        assert "Antwortquote nach Fraktion" in inhalt
        assert "/insight/fragen/stellen/" in inhalt
        assert 'data-testid="ratsfragen-pausiert"' not in inhalt
        person = client.get(f"/insight/personen/{welt['anna'].id}/")
        assert person.context["answer_stats"]["rate"] == 50
        assert "1 von 2 Fragen beantwortet" in person.content.decode()

    def test_erinnerungen_laufen(self, welt: dict[str, Any]) -> None:
        mail.outbox.clear()
        ausgabe = StringIO()
        call_command("send_question_reminders", stdout=ausgabe)
        assert "1 Erinnerung(en) gesendet" in ausgabe.getvalue()
        assert [m.to for m in mail.outbox] == [["anna@example.org"]]
