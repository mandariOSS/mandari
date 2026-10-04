# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rückmeldung am Seitenende des Bürgerportals („War diese Seite hilfreich? Ja / Nein“).

Geprüft wird, dass nur Antwort, optionaler Satz, Seitentyp, Pfad, Kommune und Tag gespeichert werden
(keine IP-Adresse, kein Cookie), der Spamschutz und die grobe Ratenbegrenzung greifen, der Satz nur mit
gültigem Zeichen an die eigene Antwort kommt, die Auswertung im Admin zählt und nach zwölf Monaten gelöscht wird.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from insight_core.models import OParlBody, OParlPaper, OParlSource, PageFeedback
from insight_core.services import page_feedback as service

pytestmark = pytest.mark.django_db

URL = "/insight/rueckmeldung/"
HTMX = {"HX-Request": "true"}


def _kommune(nummer: int) -> OParlBody:
    basis = f"https://ris{nummer}.example/oparl/"
    source = OParlSource.objects.create(name=f"Quelle {nummer}", url=basis + "system")
    return OParlBody.objects.create(external_id=basis + "body/1", source=source, name=f"Kommune {nummer}")


@pytest.fixture
def vorgang() -> OParlPaper:
    body = _kommune(1)
    return OParlPaper.objects.create(external_id="https://ris1.example/oparl/paper/1", body=body, name="Radweg")


def _antwort(client: Client, vorgang: OParlPaper, helpful: str = "ja", **extra: str) -> Any:
    daten = {
        "page_type": "paper_detail",
        "path": f"/insight/vorgaenge/{vorgang.id}/",
        "body": str(vorgang.body_id),
        "helpful": helpful,
        "website": "",
        **extra,
    }
    return client.post(URL, daten, headers=HTMX)


def _zeichen(html: str) -> str:
    treffer = re.search(r'name="token" value="([^"]+)"', html)
    assert treffer, html
    return treffer.group(1)


class TestFormular:
    def test_seitenende_mit_frage_seite_und_kommune_des_objekts(self, vorgang: OParlPaper) -> None:
        andere = _kommune(2)
        client = Client()
        client.get(f"/insight/kommune/{andere.id}/")  # gewählte Kommune ist eine andere
        seite = client.get(f"/insight/vorgaenge/{vorgang.id}/").content.decode()
        assert "War diese Seite hilfreich?" in seite
        assert 'name="page_type" value="paper_detail"' in seite
        assert f'name="path" value="/insight/vorgaenge/{vorgang.id}/"' in seite
        assert f'name="body" value="{vorgang.body_id}"' in seite
        assert "Eine IP-Adresse speichern wir dazu" in seite and "setzen kein Cookie" in seite
        assert "/datenschutz/#rueckmeldung" in seite
        # Keine absolute Zusage: Zugriffsprotokolle des Webservers und der Freitext können Personenbezug haben
        assert "Anonym" not in seite
        assert 'name="website"' in seite and 'tabindex="-1"' in seite

    def test_seiten_ohne_rueckmeldung(self, vorgang: OParlPaper) -> None:
        seite = Client().post(URL, {"page_type": "paper_detail", "helpful": "ja"}).content.decode()
        assert 'id="rueckmeldung"' not in seite


class TestAntwort:
    def test_ja_wird_ohne_adresse_und_kennung_gespeichert(self, vorgang: OParlPaper) -> None:
        client = Client()
        client.get(f"/insight/vorgaenge/{vorgang.id}/")
        sitzung_vorher = dict(client.session)
        antwort = _antwort(client, vorgang, "ja")
        assert antwort.status_code == 200
        assert "Danke für Ihre Rückmeldung." in antwort.content.decode()
        assert "Möchten Sie noch etwas ergänzen?" in antwort.content.decode()
        eintrag = PageFeedback.objects.get()
        assert (eintrag.page_type, eintrag.path, eintrag.body_id, eintrag.helpful, eintrag.comment) == (
            "paper_detail",
            f"/insight/vorgaenge/{vorgang.id}/",
            vorgang.body_id,
            True,
            "",
        )
        assert eintrag.created_on == timezone.localdate()
        # Keine Spalte für Adresse, Uhrzeit, Browser oder Kennung
        assert {f.name for f in PageFeedback._meta.get_fields()} == {
            "id",
            "body",
            "page_type",
            "path",
            "helpful",
            "comment",
            "created_on",
        }
        # Kein eigenes Cookie und nichts in der Sitzung: nur die Cookies, die das Portal ohnehin erneuert
        assert set(antwort.cookies) <= {"sessionid", "csrftoken"}
        assert dict(client.session) == sitzung_vorher

    def test_nein_fragt_nach_dem_fehlenden(self, vorgang: OParlPaper) -> None:
        antwort = _antwort(Client(), vorgang, "nein")
        assert "Was hat Ihnen auf dieser Seite gefehlt?" in antwort.content.decode()
        assert PageFeedback.objects.get().helpful is False

    def test_honeypot_speichert_nichts(self, vorgang: OParlPaper) -> None:
        antwort = _antwort(Client(), vorgang, website="https://spam.example")
        assert "Danke für Ihre Rückmeldung." in antwort.content.decode()
        assert 'name="token"' not in antwort.content.decode()
        assert not PageFeedback.objects.exists()

    @pytest.mark.parametrize("feld", [{"page_type": "admin"}, {"helpful": "vielleicht"}, {"page_type": ""}])
    def test_ungueltige_angaben(self, vorgang: OParlPaper, feld: dict[str, str]) -> None:
        antwort = _antwort(Client(), vorgang, **feld)
        assert antwort.status_code == 200  # HTMX tauscht nur 2xx ein
        assert "Bitte laden Sie die Seite neu." in antwort.content.decode()
        assert not PageFeedback.objects.exists()
        ohne_js = Client().post(URL, {"page_type": "admin", "helpful": "ja"})
        assert ohne_js.status_code == 400

    def test_unbekannte_kommune_und_pfad_werden_verworfen(self, vorgang: OParlPaper) -> None:
        _antwort(Client(), vorgang, body="keine-uuid", path="//fremd.example/<script>")
        eintrag = PageFeedback.objects.get()
        assert eintrag.body_id is None and eintrag.path == ""

    def test_grobe_ratenbegrenzung(self, vorgang: OParlPaper, settings: Any) -> None:
        settings.INSIGHT_FEEDBACK_PER_IP_HOUR = 2
        client = Client()
        for _ in range(2):
            _antwort(client, vorgang)
        antwort = _antwort(client, vorgang)
        assert "sehr viele Rückmeldungen" in antwort.content.decode()
        assert PageFeedback.objects.count() == 2

    def test_ohne_javascript_mit_rueckweg(self, vorgang: OParlPaper) -> None:
        daten = {"page_type": "paper_detail", "path": f"/insight/vorgaenge/{vorgang.id}/", "helpful": "ja"}
        seite = Client().post(URL, daten).content.decode()
        assert "Danke für Ihre Rückmeldung." in seite
        assert f'href="/insight/vorgaenge/{vorgang.id}/"' in seite and "Zurück zur Seite" in seite
        assert 'id="rueckmeldung"' not in seite  # kein zweites Formular am Seitenende


class TestErgaenzung:
    def test_satz_kommt_an_die_eigene_antwort(self, vorgang: OParlPaper) -> None:
        client = Client()
        zeichen = _zeichen(_antwort(client, vorgang, "nein").content.decode())
        antwort = client.post(URL, {"token": zeichen, "comment": "  Mir fehlt\x07 der   Beschlusstext. "}, headers=HTMX)
        assert "Danke, Ihre Ergänzung ist angekommen." in antwort.content.decode()
        assert PageFeedback.objects.get().comment == "Mir fehlt der Beschlusstext."
        # Ein zweites Mal überschreibt nichts
        nochmal = client.post(URL, {"token": zeichen, "comment": "Anderer Text"}, headers=HTMX)
        assert "Bitte laden Sie die Seite neu." in nochmal.content.decode()
        assert PageFeedback.objects.get().comment == "Mir fehlt der Beschlusstext."

    def test_gefaelschtes_zeichen(self, vorgang: OParlPaper) -> None:
        client = Client()
        _antwort(client, vorgang)
        antwort = client.post(URL, {"token": "1:gefaelscht", "comment": "Hallo"}, headers=HTMX)
        assert "Bitte laden Sie die Seite neu." in antwort.content.decode()
        assert PageFeedback.objects.get().comment == ""

    def test_zu_lang(self, vorgang: OParlPaper) -> None:
        client = Client()
        zeichen = _zeichen(_antwort(client, vorgang).content.decode())
        antwort = client.post(URL, {"token": zeichen, "comment": "x" * 501, "helpful": "ja"}, headers=HTMX)
        html = antwort.content.decode()
        assert "Bitte höchstens 500 Zeichen." in html and 'aria-invalid="true"' in html
        assert "x" * 501 in html  # Text bleibt erhalten
        assert PageFeedback.objects.get().comment == ""

    def test_zeilenumbrueche_zaehlen_wie_im_browser(self, vorgang: OParlPaper) -> None:
        """Der Browser zählt einen Umbruch als ein Zeichen (maxlength), sendet aber CRLF."""
        client = Client()
        zeichen = _zeichen(_antwort(client, vorgang, "nein").content.decode())
        satz = "\r\n".join(["x" * 99] * 5)  # im Browser 499 Zeichen, gesendet 503
        antwort = client.post(URL, {"token": zeichen, "comment": satz}, headers=HTMX)
        assert "Danke, Ihre Ergänzung ist angekommen." in antwort.content.decode()
        assert PageFeedback.objects.get().comment == "\n".join(["x" * 99] * 5)

    def test_leerer_satz(self, vorgang: OParlPaper) -> None:
        client = Client()
        zeichen = _zeichen(_antwort(client, vorgang).content.decode())
        antwort = client.post(URL, {"token": zeichen, "comment": "   "}, headers=HTMX)
        assert "Danke, Ihre Ergänzung ist angekommen." in antwort.content.decode()
        assert PageFeedback.objects.get().comment == ""


class TestAufbewahrung:
    def test_nach_zwoelf_monaten_geloescht(self, vorgang: OParlPaper) -> None:
        alt = service.record(page_type="paper_detail", path="/", body_id=None, helpful=True)
        neu = service.record(page_type="paper_detail", path="/", body_id=None, helpful=False)
        PageFeedback.objects.filter(pk=alt.pk).update(created_on=timezone.localdate() - timedelta(days=366))
        PageFeedback.objects.filter(pk=neu.pk).update(created_on=timezone.localdate() - timedelta(days=364))
        assert service.purge_expired() == 1
        assert list(PageFeedback.objects.values_list("pk", flat=True)) == [neu.pk]

    def test_taeglicher_auftrag(self) -> None:
        from apps.events.schedule import Cron, registry
        from insight_core import schedules

        eintrag = registry.get("insight_core.schedules.rueckmeldungen_aufraeumen")
        assert eintrag is not None
        assert eintrag.task is schedules.rueckmeldungen_aufraeumen
        assert eintrag.trigger == Cron("50 3 * * *")
        alt = service.record(page_type="search", path="/insight/suche/", body_id=None, helpful=True)
        PageFeedback.objects.filter(pk=alt.pk).update(created_on=timezone.localdate() - timedelta(days=400))
        assert schedules.rueckmeldungen_aufraeumen.call() == 1


class TestAuswertung:
    def test_zaehlung_je_seitentyp_und_seite(self, vorgang: OParlPaper) -> None:
        for helpful, page_type, path in [
            (True, "paper_detail", "/insight/vorgaenge/1/"),
            (False, "paper_detail", "/insight/vorgaenge/1/"),
            (False, "paper_detail", "/insight/vorgaenge/2/"),
            (True, "search", "/insight/suche/"),
        ]:
            service.record(page_type=page_type, path=path, body_id=vorgang.body_id, helpful=helpful)
        PageFeedback.objects.filter(path="/insight/vorgaenge/2/").update(comment="Text fehlt")
        je_typ = service.summary(PageFeedback.objects.all())
        assert je_typ[0] == {
            "page_type": "paper_detail",
            "total": 3,
            "yes": 1,
            "no": 2,
            "comments": 1,
            "label": "Vorgang",
            "share": 33,
        }
        je_seite = service.summary(PageFeedback.objects.all(), by="path")
        assert je_seite[0]["path"] == "/insight/vorgaenge/1/" and je_seite[0]["total"] == 2

    def test_admin_zeigt_zaehlung_und_freitexte(self, vorgang: OParlPaper) -> None:
        eintrag = service.record(page_type="paper_detail", path="/insight/vorgaenge/1/", body_id=None, helpful=False)
        PageFeedback.objects.filter(pk=eintrag.pk).update(comment="Der Beschlusstext fehlt.")
        admin = get_user_model()(email="admin@example.org", is_staff=True, is_superuser=True, is_active=True)
        admin.set_password("geheim-123")
        admin.save()
        client = Client()
        client.force_login(admin)
        seite = client.get(reverse("admin:insight_core_pagefeedback_changelist")).content.decode()
        assert "Zählung je Seitentyp" in seite and "Vorgang" in seite
        assert "Der Beschlusstext fehlt." in seite
