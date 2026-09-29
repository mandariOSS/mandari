# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Insight-Abos (Themen und Orte) per Einstellung abschaltbar, Standard aus.

Ausgeschaltet: keine Links im Bürgerportal, Abo-Seiten antworten mit 404, die Befehle
``generate_alerts``/``send_digest`` brechen mit Hinweis ab. Abmelden bleibt immer möglich.
Beschluss-Abos sind davon unabhängig.
"""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.core import mail
from django.core.management import call_command
from django.test import Client

from insight_core.models import InsightSubscriber, OParlBody, OParlSource, SubscriptionAlert

pytestmark = pytest.mark.django_db

ABO_SEITE = "/insight/benachrichtigungen/"


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Beispiel-RIS", url="https://ris.beispiel.example/oparl/system")
    return OParlBody.objects.create(
        external_id="https://ris.beispiel.example/oparl/body/1",
        source=source,
        name="Beispielstadt",
        slug="beispiel",
        is_listed=True,
    )


@pytest.fixture
def client(body: OParlBody) -> Client:
    client = Client()
    client.get(f"/insight/kommune/{body.id}/")
    return client


@pytest.fixture
def abonnent(body: OParlBody) -> InsightSubscriber:
    return InsightSubscriber.objects.create(
        email="leser@example.org", body=body, keyword="Radweg", keyword_active=True, confirmed=True
    )


def test_standard_ist_aus() -> None:
    assert django_settings.INSIGHT_SUBSCRIPTIONS_ENABLED is False


class TestAusgeschaltet:
    def test_abo_seite_und_formular_404(self, client: Client) -> None:
        mail.outbox.clear()
        assert client.get(ABO_SEITE).status_code == 404
        antwort = client.post(ABO_SEITE, {"email": "neu@example.org", "keyword": "Radweg"})
        assert antwort.status_code == 404
        assert not InsightSubscriber.objects.filter(email="neu@example.org").exists()
        assert mail.outbox == []

    def test_bestaetigen_und_verwalten_404(self, client: Client, abonnent: InsightSubscriber) -> None:
        assert client.get(f"/insight/abo/bestaetigen/{abonnent.token}/").status_code == 404
        assert client.get(f"/insight/abo/verwalten/{abonnent.token}/").status_code == 404

    def test_abmelden_bleibt_moeglich(self, client: Client, abonnent: InsightSubscriber) -> None:
        url = f"/insight/abo/abmelden/{abonnent.token}/"
        assert client.get(url).status_code == 200
        seite = client.post(url)
        assert seite.status_code == 200
        abonnent.refresh_from_db()
        assert abonnent.unsubscribed_at is not None
        assert ABO_SEITE not in seite.content.decode()

    def test_keine_links_im_portal(self, client: Client) -> None:
        for url in ("/insight/", "/insight/suche/?q=Radweg", "/insight/nachbarschaft/"):
            seite = client.get(url)
            assert seite.status_code == 200, url
            inhalt = seite.content.decode()
            assert ABO_SEITE not in inhalt, url
            assert "Abonnieren" not in inhalt, url

    def test_befehle_brechen_mit_hinweis_ab(self, abonnent: InsightSubscriber) -> None:
        mail.outbox.clear()
        for befehl in ("generate_alerts", "send_digest"):
            ausgabe = StringIO()
            call_command(befehl, stdout=ausgabe)
            assert "INSIGHT_SUBSCRIPTIONS_ENABLED" in ausgabe.getvalue(), befehl
        assert SubscriptionAlert.objects.count() == 0
        assert mail.outbox == []


class TestEingeschaltet:
    @pytest.fixture(autouse=True)
    def _an(self, settings: Any) -> None:
        settings.INSIGHT_SUBSCRIPTIONS_ENABLED = True

    def test_abo_seite_und_links(self, client: Client) -> None:
        assert client.get(ABO_SEITE).status_code == 200
        assert ABO_SEITE in client.get("/insight/suche/?q=Radweg").content.decode()

    def test_befehle_laufen(self, abonnent: InsightSubscriber, monkeypatch: pytest.MonkeyPatch) -> None:
        class _OhneSuchdienst:
            def search_all(self, **kwargs: Any) -> Any:
                raise RuntimeError("kein Elasticsearch im Test")

        # Schlagwort-Abos fallen dann auf die Datenbanksuche zurück (kein Verbindungsaufbau)
        monkeypatch.setattr("insight_core.services.search_service.get_search_service", _OhneSuchdienst)
        ausgabe = StringIO()
        call_command("generate_alerts", "--dry-run", stdout=ausgabe)
        assert "aktive Abonnenten" in ausgabe.getvalue()
