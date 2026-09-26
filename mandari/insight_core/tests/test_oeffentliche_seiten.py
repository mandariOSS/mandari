# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Seiten im Bürgerportal: Rücknahme bei Personen und Gremien, Ausgabe der Suche,
robuste Parameter und Bestätigungslinks aus E-Mails.

- Personen und Gremien, die mandari Session zurückgenommen hat (Löschung auf Antrag, deaktivierter
  Mandant), antworten wie Sitzungen und Vorgänge mit 410; Mitgliedschaften zeigen nur Sichtbares.
- Die Suche gibt Namen aus der Quelle immer maskiert aus, auch ohne Elasticsearch.
- Ungültige Seitenzahlen führen nicht zu Serverfehlern.
- Links in Bestätigungs- und Abmeldemails ändern beim bloßen Aufruf nichts. Mail-Scanner rufen
  Links vorab auf; erst der Klick auf der Bestätigungsseite (POST) wirkt. Alte Links bleiben gültig.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core import mail
from django.test import Client
from django.utils import timezone

from insight_core.models import (
    InsightSubscriber,
    OParlBody,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)

pytestmark = pytest.mark.django_db

BASIS = "https://mandari.example/session/nord/api/oparl/"
XSS = "<img src=x onerror=alert(1)>"


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Bezirk Nord (Session)", url=BASIS + "system/")
    return OParlBody.objects.create(external_id=BASIS + "body/1/", source=source, name="Bezirk Nord", slug="nord")


@pytest.fixture
def welt(body: OParlBody) -> dict[str, Any]:
    rat = OParlOrganization.objects.create(external_id=BASIS + "organization/1/", body=body, name="Bezirksrat")
    ausschuss = OParlOrganization.objects.create(
        external_id=BASIS + "organization/2/", body=body, name="Geheimer Ausschuss"
    )
    meier = OParlPerson.objects.create(
        external_id=BASIS + "person/1/", body=body, name="Petra Meier", family_name="Meier", email="meier@example.org"
    )
    schulz = OParlPerson.objects.create(
        external_id=BASIS + "person/2/", body=body, name="Karl Schulz", family_name="Schulz"
    )
    for nummer, person, gremium in ((1, meier, rat), (2, schulz, rat), (3, meier, ausschuss)):
        OParlMembership.objects.create(
            external_id=f"{BASIS}membership/{nummer}/", person=person, organization=gremium, role="Mitglied"
        )
    return {"body": body, "rat": rat, "ausschuss": ausschuss, "meier": meier, "schulz": schulz}


def _zuruecknehmen(obj: Any) -> None:
    type(obj).objects.filter(pk=obj.pk).update(deleted=True, deleted_at=timezone.now())


# =============================================================================
# Personen und Gremien
# =============================================================================


class TestPersonenUndGremien:
    def test_zurueckgenommene_person_410(self, welt: dict[str, Any]) -> None:
        _zuruecknehmen(welt["meier"])
        response = Client().get(f"/insight/personen/{welt['meier'].id}/")
        assert response.status_code == 410
        inhalt = response.content.decode()
        assert "Meier" not in inhalt
        assert "meier@example.org" not in inhalt
        assert response["Cache-Control"] == "no-store"

    def test_zurueckgenommenes_gremium_410(self, welt: dict[str, Any]) -> None:
        _zuruecknehmen(welt["ausschuss"])
        response = Client().get(f"/insight/gremien/{welt['ausschuss'].id}/")
        assert response.status_code == 410
        assert "Geheimer Ausschuss" not in response.content.decode()

    def test_gremium_ohne_zurueckgenommene_personen(self, welt: dict[str, Any]) -> None:
        client = Client()
        assert "Petra Meier" in client.get(f"/insight/gremien/{welt['rat'].id}/").content.decode()
        _zuruecknehmen(welt["meier"])
        seite = client.get(f"/insight/gremien/{welt['rat'].id}/").content.decode()
        assert "Petra Meier" not in seite
        assert "Karl Schulz" in seite

    def test_person_ohne_zurueckgenommene_gremien(self, welt: dict[str, Any]) -> None:
        client = Client()
        assert "Geheimer Ausschuss" in client.get(f"/insight/personen/{welt['meier'].id}/").content.decode()
        _zuruecknehmen(welt["ausschuss"])
        seite = client.get(f"/insight/personen/{welt['meier'].id}/").content.decode()
        assert "Geheimer Ausschuss" not in seite
        assert "Bezirksrat" in seite

    def test_geloeschte_mitgliedschaft_nicht_gelistet(self, welt: dict[str, Any]) -> None:
        OParlMembership.objects.filter(person=welt["meier"], organization=welt["ausschuss"]).update(deleted=True)
        seite = Client().get(f"/insight/personen/{welt['meier'].id}/").content.decode()
        assert "Geheimer Ausschuss" not in seite

    def test_deaktivierter_mandant(self, welt: dict[str, Any]) -> None:
        """Deaktivieren nimmt alle gespiegelten Einträge zurück (retract_source) – die Seiten folgen."""
        for obj in (welt["meier"], welt["schulz"], welt["rat"], welt["ausschuss"]):
            obj.mark_deleted()
        client = Client()
        assert client.get(f"/insight/personen/{welt['schulz'].id}/").status_code == 410
        assert client.get(f"/insight/gremien/{welt['rat'].id}/").status_code == 410

    def test_fremdquelle_bleibt_transparent(self, body: OParlBody) -> None:
        fremd = OParlPerson.objects.create(
            external_id="https://ris.fremd.example/oparl/person/1", body=body, name="Fremde Person", family_name="F"
        )
        fremd.mark_deleted()
        response = Client().get(f"/insight/personen/{fremd.id}/")
        assert response.status_code == 200
        assert "zurückgezogen" in response.content.decode()


# =============================================================================
# Suche
# =============================================================================


class _KaputteSuche:
    def search_all(self, **kwargs: Any) -> Any:
        raise ConnectionError("Elasticsearch nicht erreichbar")


class TestSuche:
    def test_rueckfall_ohne_elasticsearch_maskiert(self, body: OParlBody, monkeypatch: Any) -> None:
        monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: _KaputteSuche())
        OParlPaper.objects.create(external_id="https://ris.fremd.example/oparl/paper/1", body=body, name=f"Bad {XSS}")
        OParlPerson.objects.create(
            external_id="https://ris.fremd.example/oparl/person/9", body=body, name=f"Bad {XSS}", family_name="Bad"
        )
        client = Client()
        client.get(f"/insight/kommune/{body.id}/")
        for dropdown in ("", "&dropdown=1"):
            seite = client.get(f"/insight/suche/partials/results/?q=Bad{dropdown}").content.decode()
            assert "Bad" in seite
            assert "<img src=x" not in seite
            assert "&lt;img src=x" in seite

    def test_formatierung_maskiert_rueckfallwerte(self) -> None:
        from insight_core.services.search_service import format_search_result

        ergebnisse = [
            format_search_result({"type": "paper", "id": "1", "reference": XSS}),
            format_search_result({"type": "person", "id": "2", "given_name": XSS, "family_name": "X"}),
            format_search_result({"type": "organization", "id": "3", "name": None}),
            format_search_result({"type": "meeting", "id": "4", "name": XSS}),
            format_search_result({"type": "file", "id": "5", "file_name": XSS, "text_preview": XSS}),
            format_search_result({"type": "unbekannt", "id": XSS}),
        ]
        for ergebnis in ergebnisse:
            for feld in ("title", "text_preview"):
                assert "<img" not in str(ergebnis.get(feld) or "")

    def test_dateivorschau_nur_ueber_den_eigenen_proxy(self) -> None:
        from insight_core.services.search_service import format_search_result

        datei_id = "6f1c1f5e-2d0a-4c1b-9a51-0e8c2b7d9a10"
        ergebnis = format_search_result(
            {"type": "file", "id": datei_id, "name": "Plan", "access_url": "javascript:alert(document.domain)"}
        )
        assert ergebnis["access_url"] == f"/insight/dokumente/{datei_id}/preview/"
        kaputt = format_search_result({"type": "file", "id": "x", "access_url": "https://ris.example/a.pdf"})
        assert kaputt["access_url"] == ""

    def test_hervorhebung_bleibt(self) -> None:
        from insight_core.services.search_service import HIGHLIGHT_POST, HIGHLIGHT_PRE, format_search_result

        ergebnis = format_search_result(
            {
                "type": "paper",
                "id": "1",
                "name": "Radweg",
                "_formatted": {"name": f"{HIGHLIGHT_PRE}Rad{HIGHLIGHT_POST}weg {XSS}"},
            }
        )
        assert f"{HIGHLIGHT_PRE}Rad{HIGHLIGHT_POST}" in ergebnis["title"]
        assert "<img" not in ergebnis["title"]

    @pytest.mark.parametrize("seite", ["x", "-1", "0", "99999999999999999999"])
    def test_ungueltige_seitenzahl(self, body: OParlBody, monkeypatch: Any, seite: str) -> None:
        monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: _KaputteSuche())
        client = Client()
        client.get(f"/insight/kommune/{body.id}/")
        assert client.get(f"/insight/suche/partials/results/?q=Rad&page={seite}").status_code == 200
        assert client.get(f"/insight/dokumente/?page={seite}").status_code == 200


# =============================================================================
# Bestätigungs- und Abmeldelinks
# =============================================================================


@pytest.fixture
def abonnent(body: OParlBody) -> InsightSubscriber:
    return InsightSubscriber.objects.create(email="leser@example.org", body=body, keyword="Radweg", keyword_active=True)


class TestLinksAusEmails:
    def test_abo_bestaetigen_erst_nach_klick(self, abonnent: InsightSubscriber) -> None:
        client = Client(enforce_csrf_checks=True)
        url = f"/insight/abo/bestaetigen/{abonnent.token}/"
        seite = client.get(url)
        assert seite.status_code == 200
        abonnent.refresh_from_db()
        assert abonnent.confirmed is False
        assert 'method="post"' in seite.content.decode()
        assert client.post(url, {"csrfmiddlewaretoken": seite.context["csrf_token"]}).status_code == 200
        abonnent.refresh_from_db()
        assert abonnent.confirmed is True

    def test_abo_abmelden_erst_nach_klick(self, abonnent: InsightSubscriber) -> None:
        abonnent.confirmed = True
        abonnent.save()
        client = Client(enforce_csrf_checks=True)
        url = f"/insight/abo/abmelden/{abonnent.token}/"
        seite = client.get(url)
        assert seite.status_code == 200
        abonnent.refresh_from_db()
        assert abonnent.unsubscribed_at is None
        client.post(url, {"csrfmiddlewaretoken": seite.context["csrf_token"]})
        abonnent.refresh_from_db()
        assert abonnent.unsubscribed_at is not None

    def test_abo_post_ohne_csrf_wirkt_nicht(self, abonnent: InsightSubscriber) -> None:
        response = Client(enforce_csrf_checks=True).post(f"/insight/abo/bestaetigen/{abonnent.token}/")
        assert response.status_code == 403
        abonnent.refresh_from_db()
        assert abonnent.confirmed is False

    def test_beschluss_abo_bestaetigen_und_abmelden(self, beschluss_abo: Any) -> None:
        client = Client()
        bestaetigen = f"/insight/beschluesse/abo/bestaetigen/{beschluss_abo.token}/"
        abmelden = f"/insight/beschluesse/abo/abmelden/{beschluss_abo.token}/"
        assert client.get(bestaetigen).status_code == 200
        beschluss_abo.refresh_from_db()
        assert beschluss_abo.confirmed is False
        client.post(bestaetigen)
        beschluss_abo.refresh_from_db()
        assert beschluss_abo.confirmed is True
        assert client.get(abmelden).status_code == 200
        beschluss_abo.refresh_from_db()
        assert beschluss_abo.unsubscribed_at is None
        client.post(abmelden)
        beschluss_abo.refresh_from_db()
        assert beschluss_abo.unsubscribed_at is not None

    def test_frage_verifizieren_erst_nach_klick(self, welt: dict[str, Any]) -> None:
        from insight_core.models import PublicQuestion

        frage = PublicQuestion.objects.create(
            body=welt["body"],
            recipient=welt["schulz"],
            questioner_name="Frieda",
            questioner_email="frieda@example.org",
            subject="Radweg",
            question_text="Wann kommt der Radweg?",
        )
        client = Client()
        url = f"/insight/fragen/verifizieren/{frage.verification_token}/"
        mail.outbox.clear()
        assert client.get(url).status_code == 200
        frage.refresh_from_db()
        assert frage.status == "unverified"
        assert mail.outbox == []
        assert client.post(url).status_code == 200
        frage.refresh_from_db()
        assert frage.status == "pending"
        assert client.post(url).status_code == 404


@pytest.fixture
def beschluss_abo(db: Any) -> Any:
    from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionTenant
    from insight_core.models import DecisionSubscription

    tenant = SessionTenant.objects.create(name="Bezirk Süd", slug="sued")
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss", short_name="HA")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Sitzung HA", organization=gremium, start=timezone.now(), is_public=True
    )
    top = SessionAgendaItem.objects.create(meeting=sitzung, name="Radweg", number="1", order=1)
    return DecisionSubscription.objects.create(agenda_item=top, email="buergerin@example.org")
