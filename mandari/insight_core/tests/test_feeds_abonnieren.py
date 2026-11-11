# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Issue #1274: Abonnieren ohne Konto. Atom-Feeds je Auswahl, Suche, Vorgang, Gremium und für die Beschlüsse,
Kalenderdatei je Sitzung, Etikett „angekündigt“, Cache, Drosselung und Indexierung.

Jeder Feed wird gegen die Pflichtangaben von RFC 4287 geprüft (``_atom``): ein ``id``, ``title`` und ``updated`` je
Feed und Eintrag, Autor, ``link rel="self"``, je Eintrag genau ein ``link rel="alternate"``, Zeitpunkte nach RFC 3339,
eindeutige Kennungen, neueste Einträge zuerst.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import Client
from django.utils import timezone

from insight_core import publication
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlSource,
    VorgangUrheber,
)
from insight_core.services import feeds
from insight_core.services.feed_eintraege import ANGEKUENDIGT_SATZ

pytestmark = pytest.mark.django_db

ATOM = "{http://www.w3.org/2005/Atom}"
RIS = "https://ris.feedstadt.example/oparl"
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
FEED_READER = "Feeder/2.9 (Android)"


@pytest.fixture(autouse=True)
def _ohne_suchdienst(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suche über den Datenbank-Rückfall, ohne auf einen Suchdienst zu warten."""

    def _nicht_erreichbar() -> Any:
        raise ConnectionError("kein Suchdienst im Test")

    monkeypatch.setattr("insight_core.services.search_service.get_search_service", _nicht_erreichbar)


@pytest.fixture
def bestand(db: Any) -> dict[str, Any]:
    """Kommune mit Rat und Fraktion, einer vergangenen Sitzung mit Ergebnis und einer kommenden ohne Tagesordnung."""
    jetzt = timezone.now()
    source = OParlSource.objects.create(name="RIS Feedstadt", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Feedstadt", slug="feedstadt")
    rat = OParlOrganization.objects.create(external_id=f"{RIS}/org/rat", body=body, name="Rat")
    fraktion = OParlOrganization.objects.create(
        external_id=f"{RIS}/org/gruene", body=body, name="Fraktion Grüne", organization_type="Fraktion"
    )
    vergangen = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/1", body=body, name="Rat", start=jetzt - timedelta(days=10)
    )
    vergangen.organizations.add(rat)
    kommend = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/2", body=body, name="Rat", start=jetzt + timedelta(days=14), location_name="Rathaus"
    )
    kommend.organizations.add(rat)
    antrag = OParlPaper.objects.create(
        external_id=f"{RIS}/paper/1",
        body=body,
        name="Radweg an der Hauptstraße",
        reference="A 2026/0815",
        paper_type="Antrag",
        date=(jetzt - timedelta(days=30)).date(),
        raw_json={"web": "https://ris.feedstadt.example/vo020?id=1"},
    )
    vorlage = OParlPaper.objects.create(
        external_id=f"{RIS}/paper/2",
        body=body,
        name="Haushaltssatzung 2027",
        reference="V 2026/0100",
        paper_type="Beschlussvorlage",
        date=(jetzt - timedelta(days=5)).date(),
    )
    top = OParlAgendaItem.objects.create(
        external_id=f"{RIS}/top/1",
        meeting=vergangen,
        number="5",
        name="Radweg an der Hauptstraße",
        public=True,
        result="einstimmig beschlossen",
    )
    for nummer, sitzung, punkt in ((1, vergangen, top), (2, kommend, None)):
        OParlConsultation.objects.create(
            external_id=f"{RIS}/consultation/{nummer}",
            body=body,
            paper=antrag,
            paper_external_id=antrag.external_id,
            meeting_external_id=sitzung.external_id,
            agenda_item_external_id=punkt.external_id if punkt else None,
        )
    datei = OParlFile.objects.create(
        external_id=f"{RIS}/file/1",
        body=body,
        paper=antrag,
        name="Stellungnahme der Verwaltung",
        file_date=jetzt - timedelta(days=3),
    )
    VorgangUrheber.objects.create(
        paper_id=antrag.pk,
        body_id=body.pk,
        organisation_id=fraktion.pk,
        bezeichnung="Fraktion Grüne",
        kurzform="Grüne",
        herkunft=VorgangUrheber.SOURCE,
    )
    return {
        "body": body,
        "rat": rat,
        "fraktion": fraktion,
        "vergangen": vergangen,
        "kommend": kommend,
        "antrag": antrag,
        "vorlage": vorlage,
        "top": top,
        "datei": datei,
    }


def _zeit(wert: str | None) -> datetime:
    assert wert is not None and RFC3339.match(wert), wert
    return datetime.fromisoformat(wert.replace("Z", "+00:00"))


def _atom(antwort: Any) -> ET.Element:
    """Atom-Feed der Antwort, geprüft gegen die Pflichtangaben von RFC 4287."""
    assert antwort.status_code == 200, antwort.content[:300]
    assert antwort["Content-Type"] == "application/atom+xml; charset=utf-8"
    assert antwort["X-Robots-Tag"] == "noindex"
    assert antwort.content.startswith(b'<?xml version="1.0" encoding="utf-8"?>')
    feed = ET.fromstring(antwort.content)
    assert feed.tag == f"{ATOM}feed"
    for name in ("id", "title", "updated"):
        assert len(feed.findall(f"{ATOM}{name}")) == 1, name
    assert (feed.findtext(f"{ATOM}id") or "").startswith(feeds.KENNUNG_PRAEFIX)
    assert feed.findtext(f"{ATOM}author/{ATOM}name")
    links = {link.get("rel"): link.get("href") or "" for link in feed.findall(f"{ATOM}link")}
    assert links["self"].startswith("http") and links["alternate"].startswith("http")
    stand = _zeit(feed.findtext(f"{ATOM}updated"))
    kennungen: list[str] = []
    zeiten: list[datetime] = []
    for eintrag in feed.findall(f"{ATOM}entry"):
        for name in ("id", "title", "updated"):
            assert len(eintrag.findall(f"{ATOM}{name}")) == 1, name
        kennung = eintrag.findtext(f"{ATOM}id") or ""
        assert kennung.startswith(feeds.KENNUNG_PRAEFIX), kennung
        alternativ = [link for link in eintrag.findall(f"{ATOM}link") if link.get("rel", "alternate") == "alternate"]
        assert len(alternativ) == 1 and (alternativ[0].get("href") or "").startswith("http")
        kennungen.append(kennung)
        zeiten.append(_zeit(eintrag.findtext(f"{ATOM}updated")))
    assert len(kennungen) == len(set(kennungen)), "Kennungen doppelt"
    assert zeiten == sorted(zeiten, reverse=True), "nicht neueste zuerst"
    if zeiten:
        assert stand == zeiten[0]
    return feed


def _eintraege(feed: ET.Element) -> dict[str, dict[str, str]]:
    """Einträge je Kennung: Titel, Zusammenfassung, Link."""
    return {
        eintrag.findtext(f"{ATOM}id") or "": {
            "titel": eintrag.findtext(f"{ATOM}title") or "",
            "text": eintrag.findtext(f"{ATOM}summary") or "",
            "link": next(
                (link.get("href") or "" for link in eintrag.findall(f"{ATOM}link") if link.get("rel") == "alternate"),
                "",
            ),
            "original": next(
                (link.get("href") or "" for link in eintrag.findall(f"{ATOM}link") if link.get("rel") == "related"),
                "",
            ),
        }
        for eintrag in feed.findall(f"{ATOM}entry")
    }


def _kennung(*teile: object) -> str:
    return feeds.kennung(*teile)


# ---------------------------------------------------------------------------
# Format und Grenzen
# ---------------------------------------------------------------------------


class TestAtomBaustein:
    def _eintrag(self, nummer: int, tage: int = 0, **felder: Any) -> feeds.Eintrag:
        return feeds.Eintrag(
            kennung=feeds.kennung("test", nummer),
            titel=felder.pop("titel", f"Eintrag {nummer}"),
            zeitpunkt=datetime(2026, 11, 1, tzinfo=UTC) + timedelta(days=tage),
            link=f"https://mandari.example/insight/{nummer}/",
            **felder,
        )

    def _feed(self, eintraege: list[feeds.Eintrag]) -> feeds.Feed:
        return feeds.Feed(
            kennung=feeds.kennung("feed", "test"),
            titel="Test",
            untertitel="Ein Test",
            selbst="https://mandari.example/insight/test.atom",
            seite="https://mandari.example/insight/",
            eintraege=eintraege,
            basis="https://mandari.example",
        )

    def test_hoechstens_hundert_eintraege_neueste_zuerst(self) -> None:
        feed = self._feed([self._eintrag(n, tage=n % 50) for n in range(130)])

        wurzel = ET.fromstring(feeds.atom(feed))

        eintraege = wurzel.findall(f"{ATOM}entry")
        assert len(eintraege) == feeds.EINTRAEGE_HOECHSTENS
        zeiten = [_zeit(e.findtext(f"{ATOM}updated")) for e in eintraege]
        assert zeiten == sorted(zeiten, reverse=True)
        assert wurzel.findtext(f"{ATOM}updated") == "2026-12-20T00:00:00Z"

    def test_jede_kennung_einmal(self) -> None:
        feed = self._feed([self._eintrag(1, tage=2), self._eintrag(1, tage=1), self._eintrag(2)])

        kennungen = [e.findtext(f"{ATOM}id") for e in ET.fromstring(feeds.atom(feed)).findall(f"{ATOM}entry")]

        assert kennungen == [feeds.kennung("test", 1), feeds.kennung("test", 2)]

    def test_steuerzeichen_und_ueberlange_texte(self) -> None:
        roh = "Radweg\x0b an der\x00 Hauptstraße " + "sehr " * 300
        feed = self._feed([self._eintrag(1, titel=roh, zusammenfassung=roh)])

        eintrag = ET.fromstring(feeds.atom(feed)).find(f"{ATOM}entry")

        assert eintrag is not None
        titel = eintrag.findtext(f"{ATOM}title") or ""
        assert titel.startswith("Radweg an der Hauptstraße sehr") and titel.endswith("…")
        assert len(titel) <= feeds.TITEL_HOECHSTENS
        assert len(eintrag.findtext(f"{ATOM}summary") or "") <= feeds.ZUSAMMENFASSUNG_HOECHSTENS

    def test_ungueltige_zeichen_sind_genau_die_von_xml_verbotenen(self) -> None:
        """C0-Steuerzeichen ohne Tab, Zeilenumbruch und Wagenrücklauf, dazu U+FFFE und U+FFFF – sonst nichts."""
        alle = "".join(chr(c) for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF)
        gemeint = {chr(c) for c in (*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0xFFFE, 0xFFFF)}

        assert set(feeds._UNGUELTIG.findall(alle)) == gemeint

    def test_leerer_feed_hat_ein_festes_datum(self) -> None:
        erster = feeds.atom(self._feed([]))

        assert ET.fromstring(erster).findtext(f"{ATOM}updated") == "2026-01-01T00:00:00Z"
        assert feeds.atom(self._feed([])) == erster


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------


class TestVorgangsliste:
    def test_neue_vorgaenge_und_ergebnisse(self, bestand: dict[str, Any]) -> None:
        antrag, vorlage = bestand["antrag"], bestand["vorlage"]

        feed = _atom(Client().get("/insight/vorgaenge/feed.atom", {"kommune": str(bestand["body"].pk)}))

        eintraege = _eintraege(feed)
        assert _kennung("vorgang", antrag.pk) in eintraege
        assert _kennung("vorgang", vorlage.pk) in eintraege
        beratung = eintraege[_kennung("vorgang", antrag.pk, "beratung", bestand["vergangen"].pk)]
        assert beratung["titel"] == "Rat: beschlossen – Radweg an der Hauptstraße"
        assert "„einstimmig beschlossen“" in beratung["text"]
        assert beratung["link"].endswith(f"/insight/vorgaenge/{antrag.pk}/")
        assert "Gestellt von Grüne." in eintraege[_kennung("vorgang", antrag.pk)]["text"]
        # Angesetzte Beratungen und Dokumente nur im Feed des Vorgangs selbst
        assert _kennung("vorgang", antrag.pk, "termin", bestand["kommend"].pk) not in eintraege
        assert feed.findtext(f"{ATOM}title") == "Vorgänge in Feedstadt"

    def test_filter_der_liste_gelten(self, bestand: dict[str, Any]) -> None:
        feed = _atom(
            Client().get("/insight/vorgaenge/feed.atom", {"kommune": str(bestand["body"].pk), "type": "Antrag"})
        )

        eintraege = _eintraege(feed)
        assert _kennung("vorgang", bestand["antrag"].pk) in eintraege
        assert _kennung("vorgang", bestand["vorlage"].pk) not in eintraege
        assert feed.findtext(f"{ATOM}title") == "Vorgänge in Feedstadt (Auswahl)"

    def test_fraktion_wie_in_der_liste(self, bestand: dict[str, Any]) -> None:
        feed = _atom(
            Client().get(
                "/insight/vorgaenge/feed.atom", {"kommune": str(bestand["body"].pk), "fraktion": "Fraktion Grüne"}
            )
        )

        assert set(_eintraege(feed)) >= {_kennung("vorgang", bestand["antrag"].pk)}
        assert _kennung("vorgang", bestand["vorlage"].pk) not in _eintraege(feed)

    def test_ohne_kommune_404(self, bestand: dict[str, Any]) -> None:
        assert Client().get("/insight/vorgaenge/feed.atom").status_code == 404
        assert Client().get("/insight/vorgaenge/feed.atom", {"kommune": "kaputt"}).status_code == 404

    def test_kommune_aus_der_sitzung_im_browser(self, bestand: dict[str, Any]) -> None:
        client = Client()
        client.get(f"/insight/kommune/{bestand['body'].pk}/")

        _atom(client.get("/insight/vorgaenge/feed.atom"))

    def test_selbst_link_ohne_fremde_parameter(self, bestand: dict[str, Any]) -> None:
        feed = _atom(
            Client().get(
                "/insight/vorgaenge/feed.atom", {"kommune": str(bestand["body"].pk), "type": "Antrag", "x": "<b>"}
            )
        )

        selbst = next(link.get("href") or "" for link in feed.findall(f"{ATOM}link") if link.get("rel") == "self")
        assert parse_qs(urlsplit(selbst).query) == {"kommune": [str(bestand["body"].pk)], "type": ["Antrag"]}


class TestVorgang:
    def test_neues_zu_diesem_vorgang(self, bestand: dict[str, Any]) -> None:
        antrag = bestand["antrag"]

        feed = _atom(Client().get(f"/insight/vorgaenge/{antrag.pk}/feed.atom"))

        eintraege = _eintraege(feed)
        assert eintraege[_kennung("vorgang", antrag.pk)]["original"] == "https://ris.feedstadt.example/vo020?id=1"
        assert _kennung("vorgang", antrag.pk, "beratung", bestand["vergangen"].pk) in eintraege
        termin = eintraege[_kennung("vorgang", antrag.pk, "termin", bestand["kommend"].pk)]
        assert termin["titel"].startswith("Rat: Beratung am ")
        assert termin["link"].endswith(f"/insight/termine/{bestand['kommend'].pk}/")
        dokument = eintraege[_kennung("vorgang", antrag.pk, "dokument", bestand["datei"].pk)]
        assert dokument["titel"] == "Dokument: Stellungnahme der Verwaltung – Radweg an der Hauptstraße"
        assert feed.findtext(f"{ATOM}title") == "Antrag A 2026/0815: Radweg an der Hauptstraße"

    def test_kennungen_bleiben_bei_aenderungen(self, bestand: dict[str, Any]) -> None:
        """Umbenannte Punkte, verschobene Sitzungen und neue Beratungen ändern vorhandene Kennungen nicht."""
        antrag = bestand["antrag"]
        vorher = set(_eintraege(_atom(Client().get(f"/insight/vorgaenge/{antrag.pk}/feed.atom"))))
        OParlAgendaItem.objects.filter(pk=bestand["top"].pk).update(name="Neuer Titel", number="7")
        OParlMeeting.objects.filter(pk=bestand["vergangen"].pk).update(start=timezone.now() - timedelta(days=11))
        neu = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/3", body=bestand["body"], name="Rat", start=timezone.now() - timedelta(days=2)
        )
        OParlAgendaItem.objects.create(
            external_id=f"{RIS}/top/3", meeting=neu, number="1", name="Radweg", public=True, result="vertagt"
        )
        OParlConsultation.objects.create(
            external_id=f"{RIS}/consultation/3",
            body=bestand["body"],
            paper=antrag,
            meeting_external_id=neu.external_id,
            agenda_item_external_id=f"{RIS}/top/3",
        )
        from django.core.cache import cache

        cache.clear()

        nachher = set(_eintraege(_atom(Client().get(f"/insight/vorgaenge/{antrag.pk}/feed.atom"))))

        assert vorher < nachher
        assert nachher - vorher == {_kennung("vorgang", antrag.pk, "beratung", neu.pk)}

    def test_gleiche_kennung_in_allen_feeds(self, bestand: dict[str, Any]) -> None:
        antrag, body = bestand["antrag"], bestand["body"]
        beratung = _kennung("vorgang", antrag.pk, "beratung", bestand["vergangen"].pk)

        vorgang = _eintraege(_atom(Client().get(f"/insight/vorgaenge/{antrag.pk}/feed.atom")))
        liste = _eintraege(_atom(Client().get("/insight/vorgaenge/feed.atom", {"kommune": str(body.pk)})))
        suche = _eintraege(_atom(Client().get("/insight/suche/feed.atom", {"q": "Radweg", "kommune": str(body.pk)})))

        assert beratung in vorgang and beratung in liste and beratung in suche
        assert vorgang[beratung]["titel"] == liste[beratung]["titel"] == suche[beratung]["titel"]

    def test_zurueckgenommener_vorgang_410(self, bestand: dict[str, Any]) -> None:
        from insight_core.models import SESSION_OPARL_MARKERS

        paper = OParlPaper.objects.create(
            external_id="https://mandari.example/" + "/".join(SESSION_OPARL_MARKERS) + "/paper/9",
            body=bestand["body"],
            name="Zurückgenommen",
            deleted=True,
        )

        antwort = Client().get(f"/insight/vorgaenge/{paper.pk}/feed.atom")

        assert antwort.status_code == 410
        assert "Zurückgenommen" not in antwort.content.decode()


class TestSuche:
    def test_vorgaenge_unter_den_treffern(self, bestand: dict[str, Any]) -> None:
        feed = _atom(Client().get("/insight/suche/feed.atom", {"q": "Haushalt", "kommune": str(bestand["body"].pk)}))

        eintraege = _eintraege(feed)
        assert _kennung("vorgang", bestand["vorlage"].pk) in eintraege
        assert _kennung("vorgang", bestand["antrag"].pk) not in eintraege
        assert feed.findtext(f"{ATOM}title") == "Suche „Haushalt“ in Feedstadt"

    def test_mit_suchdienst_neueste_treffer(self, bestand: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        aufrufe: list[dict[str, Any]] = []

        class Suchdienst:
            def search_grouped(self, query: str, **kwargs: Any) -> dict[str, Any]:
                aufrufe.append({"query": query, **kwargs})
                return {"groups": [{"kind": "paper", "key": str(bestand["antrag"].pk)}]}

        monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: Suchdienst())

        feed = _atom(Client().get("/insight/suche/feed.atom", {"q": "Fahrrad", "kommune": str(bestand["body"].pk)}))

        assert _kennung("vorgang", bestand["antrag"].pk) in _eintraege(feed)
        assert aufrufe[0]["sort"] == "newest" and aufrufe[0]["kinds"] == {"paper"}
        assert aufrufe[0]["body_id"] == str(bestand["body"].pk)

    def test_ohne_suchbegriff_404(self, bestand: dict[str, Any]) -> None:
        assert Client().get("/insight/suche/feed.atom", {"q": "a"}).status_code == 404


class TestGremiumUndBeschluesse:
    def test_gremium_sitzungen_und_beschluesse(self, bestand: dict[str, Any]) -> None:
        rat, kommend, top = bestand["rat"], bestand["kommend"], bestand["top"]

        feed = _atom(Client().get(f"/insight/gremien/{rat.pk}/feed.atom"))

        eintraege = _eintraege(feed)
        sitzung = eintraege[_kennung("sitzung", kommend.pk)]
        assert sitzung["titel"].startswith("Rat: Sitzung am ")
        assert f"angekündigt: {ANGEKUENDIGT_SATZ}" in sitzung["text"]
        assert "Ort: Rathaus." in sitzung["text"]
        assert _kennung("sitzung", kommend.pk, "tagesordnung") not in eintraege
        beschluss = eintraege[_kennung("beschluss", top.pk)]
        assert beschluss["titel"] == "Beschlossen: Radweg an der Hauptstraße"
        assert beschluss["link"].endswith(f"/insight/termine/{bestand['vergangen'].pk}/#top-{top.pk}")

    def test_tagesordnung_veroeffentlicht(self, bestand: dict[str, Any]) -> None:
        kommend = bestand["kommend"]
        OParlAgendaItem.objects.create(external_id=f"{RIS}/top/2", meeting=kommend, number="1", name="Einwohnerfragen")

        eintraege = _eintraege(_atom(Client().get(f"/insight/gremien/{bestand['rat'].pk}/feed.atom")))

        assert "1 öffentliche Punkt." in eintraege[_kennung("sitzung", kommend.pk, "tagesordnung")]["text"]
        assert "angekündigt" not in eintraege[_kennung("sitzung", kommend.pk)]["text"]

    def test_fraktion_mit_ihren_vorgaengen(self, bestand: dict[str, Any]) -> None:
        eintraege = _eintraege(_atom(Client().get(f"/insight/gremien/{bestand['fraktion'].pk}/feed.atom")))

        assert _kennung("vorgang", bestand["antrag"].pk) in eintraege
        assert _kennung("vorgang", bestand["vorlage"].pk) not in eintraege

    def test_beschluesse_der_kommune_und_je_gremium(self, bestand: dict[str, Any]) -> None:
        body, top = bestand["body"], bestand["top"]
        anderes = OParlOrganization.objects.create(external_id=f"{RIS}/org/bv", body=body, name="Bezirksvertretung")

        alle = _eintraege(_atom(Client().get("/insight/beschluesse/feed.atom", {"kommune": str(body.pk)})))
        rat = _eintraege(
            _atom(
                Client().get(
                    "/insight/beschluesse/feed.atom", {"kommune": str(body.pk), "gremium": str(bestand["rat"].pk)}
                )
            )
        )
        bv = _eintraege(
            _atom(Client().get("/insight/beschluesse/feed.atom", {"kommune": str(body.pk), "gremium": str(anderes.pk)}))
        )

        assert _kennung("beschluss", top.pk) in alle and _kennung("beschluss", top.pk) in rat
        assert bv == {}

    def test_fremdes_oder_kaputtes_gremium_404(self, bestand: dict[str, Any]) -> None:
        fremd = OParlBody.objects.create(external_id=f"{RIS}/body/2", source=bestand["body"].source, name="Anderswo")
        gremium = OParlOrganization.objects.create(external_id=f"{RIS}/org/x", body=fremd, name="Rat")
        kommune = str(bestand["body"].pk)

        for wert in ("kaputt", str(gremium.pk)):
            antwort = Client().get("/insight/beschluesse/feed.atom", {"kommune": kommune, "gremium": wert})
            assert antwort.status_code == 404, wert


# ---------------------------------------------------------------------------
# Abruf: Cache, bedingte Abrufe, Drosselung, Sitzung, Veröffentlichung
# ---------------------------------------------------------------------------


class TestAbruf:
    def test_bedingter_abruf_304(self, bestand: dict[str, Any]) -> None:
        pfad = f"/insight/vorgaenge/{bestand['antrag'].pk}/feed.atom"
        erste = Client().get(pfad)
        assert erste.status_code == 200 and erste["ETag"] and erste["Last-Modified"]
        # Nachgelagerte Caches fragen nach (bedingter Abruf), damit Rücknahmen sofort wirken
        assert erste["Cache-Control"] == "public, no-cache"

        zweite = Client().get(pfad, headers={"If-None-Match": erste["ETag"]})

        assert zweite.status_code == 304
        assert zweite.content == b""

    def test_drosselung_nur_beim_neubau(self, bestand: dict[str, Any], settings: Any) -> None:
        settings.INSIGHT_FEEDS_PER_IP_MINUTE = 1
        client = Client(REMOTE_ADDR="203.0.113.7")
        vorgang = f"/insight/vorgaenge/{bestand['antrag'].pk}/feed.atom"

        assert client.get(vorgang).status_code == 200
        # Aus dem Cache: zählt nicht
        assert client.get(vorgang).status_code == 200
        gedrosselt = client.get(f"/insight/vorgaenge/{bestand['vorlage'].pk}/feed.atom")
        assert gedrosselt.status_code == 429
        assert gedrosselt["Retry-After"] == "60"
        # Andere Adresse: eigene Grenze
        assert (
            Client(REMOTE_ADDR="203.0.113.8").get(f"/insight/gremien/{bestand['rat'].pk}/feed.atom").status_code == 200
        )

    def test_feed_reader_legt_keine_sitzung_an(self, bestand: dict[str, Any]) -> None:
        vorher = Session.objects.count()
        client = Client(HTTP_USER_AGENT=FEED_READER)

        for pfad, parameter in (
            ("/insight/vorgaenge/feed.atom", {"kommune": str(bestand["body"].pk)}),
            ("/insight/suche/feed.atom", {"q": "Radweg", "kommune": str(bestand["body"].pk)}),
            ("/insight/beschluesse/feed.atom", {"kommune": str(bestand["body"].pk)}),
            (f"/insight/vorgaenge/{bestand['antrag'].pk}/feed.atom", {}),
            (f"/insight/termine/{bestand['kommend'].pk}/kalender.ics", {}),
        ):
            antwort = client.get(pfad, parameter)
            assert antwort.status_code == 200, pfad
            assert settings.SESSION_COOKIE_NAME not in antwort.cookies, pfad
        assert Session.objects.count() == vorher

    @pytest.mark.parametrize(("modus", "status"), [(publication.PAUSED, 503), (publication.WITHDRAWN, 410)])
    def test_beendete_veroeffentlichung_sperrt_feeds(self, bestand: dict[str, Any], modus: str, status: int) -> None:
        from django.core.cache import cache

        cache.set(publication.CACHE_KEY, {str(bestand["body"].pk): (modus, None)}, 300)
        kommune = {"kommune": str(bestand["body"].pk)}

        for pfad, parameter in (
            ("/insight/vorgaenge/feed.atom", kommune),
            ("/insight/beschluesse/feed.atom", kommune),
            ("/insight/suche/feed.atom", {"q": "Radweg", **kommune}),
            (f"/insight/vorgaenge/{bestand['antrag'].pk}/feed.atom", {}),
            (f"/insight/gremien/{bestand['rat'].pk}/feed.atom", {}),
            (f"/insight/termine/{bestand['kommend'].pk}/kalender.ics", {}),
        ):
            antwort = Client().get(pfad, parameter)
            assert antwort.status_code == status, pfad
            assert not antwort["Content-Type"].startswith(("application/atom+xml", "text/calendar")), pfad
            assert "Hauptstraße" not in antwort.content.decode(), pfad


# ---------------------------------------------------------------------------
# Kalender
# ---------------------------------------------------------------------------


def _ics_felder(inhalt: bytes) -> dict[str, str]:
    """Felder des ersten VEVENT (entfaltete Zeilen)."""
    text = inhalt.decode().replace("\r\n ", "")
    ereignis = text.split("BEGIN:VEVENT\r\n", 1)[1].split("END:VEVENT", 1)[0]
    return dict(zeile.split(":", 1) for zeile in ereignis.splitlines() if ":" in zeile)


class TestKalender:
    def test_kalenderdatei_je_sitzung(self, bestand: dict[str, Any]) -> None:
        kommend = bestand["kommend"]

        antwort = Client().get(f"/insight/termine/{kommend.pk}/kalender.ics")

        assert antwort.status_code == 200
        assert antwort["Content-Type"] == "text/calendar; charset=utf-8"
        assert antwort["Content-Disposition"].startswith('attachment; filename="sitzung-')
        assert antwort["X-Robots-Tag"] == "noindex"
        felder = _ics_felder(antwort.content)
        assert felder["UID"] == f"meeting-{kommend.pk}@mandari.de"
        assert felder["SUMMARY"] == "Rat"
        assert felder["STATUS"] == "CONFIRMED"
        assert felder["LOCATION"] == "Rathaus"
        assert (
            "angekündigt: Der Termin steht fest\\, die Tagesordnung ist noch nicht veröffentlicht."
            in felder["DESCRIPTION"]
        )
        assert felder["URL"].endswith(f"/insight/termine/{kommend.pk}/")
        assert int(felder["SEQUENCE"]) > 0

    def test_dieselbe_uid_wie_im_kalender_abo(self, bestand: dict[str, Any]) -> None:
        kommend = bestand["kommend"]

        abo = Client().get("/insight/termine/kalender.ics", {"kommune": str(bestand["body"].pk)}).content.decode()
        datei = _ics_felder(Client().get(f"/insight/termine/{kommend.pk}/kalender.ics").content)

        assert f"UID:{datei['UID']}" in abo
        assert "angekündigt" in abo.replace("\r\n ", "")

    def test_abgesagt_und_ohne_termin(self, bestand: dict[str, Any]) -> None:
        kommend = bestand["kommend"]
        OParlMeeting.objects.filter(pk=kommend.pk).update(cancelled=True)
        ohne = OParlMeeting.objects.create(external_id=f"{RIS}/meeting/9", body=bestand["body"], name="Rat")

        felder = _ics_felder(Client().get(f"/insight/termine/{kommend.pk}/kalender.ics").content)

        assert felder["STATUS"] == "CANCELLED" and felder["SUMMARY"] == "Rat (abgesagt)"
        assert Client().get(f"/insight/termine/{ohne.pk}/kalender.ics").status_code == 404


# ---------------------------------------------------------------------------
# Seiten: Feed im Kopf, Knöpfe, „angekündigt“, Anleitung
# ---------------------------------------------------------------------------


def _alternate(html: str) -> str:
    treffer = re.search(r'<link rel="alternate" type="application/atom\+xml" title="[^"]*" href="([^"]+)">', html)
    assert treffer, "kein Feed im Seitenkopf"
    return treffer.group(1).replace("&amp;", "&")


class TestSeiten:
    def test_vorgang_mit_feed_und_knopf(self, bestand: dict[str, Any]) -> None:
        antrag = bestand["antrag"]

        html = Client().get(f"/insight/vorgaenge/{antrag.pk}/").content.decode()

        assert _alternate(html).endswith(f"/insight/vorgaenge/{antrag.pk}/feed.atom")
        assert 'data-testid="vorgang-abonnieren"' in html
        assert "Neues zu diesem Vorgang abonnieren" in html
        assert "/insight/abonnieren/?feed=%2Finsight%2Fvorgaenge%2F" in html

    def test_liste_und_suche_mit_der_auswahl(self, bestand: dict[str, Any]) -> None:
        client = Client()
        client.get(f"/insight/kommune/{bestand['body'].pk}/")

        liste = client.get("/insight/vorgaenge/", {"type": "Antrag"}).content.decode()
        suche = client.get("/insight/suche/", {"q": "Radweg"}).content.decode()

        adresse = urlsplit(_alternate(liste))
        assert adresse.path == "/insight/vorgaenge/feed.atom"
        assert parse_qs(adresse.query) == {"kommune": [str(bestand["body"].pk)], "type": ["Antrag"]}
        assert 'data-testid="auswahl-abonnieren"' in liste
        assert parse_qs(urlsplit(_alternate(suche)).query)["q"] == ["Radweg"]
        assert 'data-testid="suche-abonnieren"' in suche

    def test_liste_im_htmx_ausschnitt_folgt_dem_filter(self, bestand: dict[str, Any]) -> None:
        client = Client()
        client.get(f"/insight/kommune/{bestand['body'].pk}/")

        teil = client.get("/insight/vorgaenge/", {"q": "Haushalt"}, headers={"HX-Request": "true"}).content.decode()

        assert "q%3DHaushalt" in teil

    def test_gremium_mit_feed(self, bestand: dict[str, Any]) -> None:
        html = Client().get(f"/insight/gremien/{bestand['rat'].pk}/").content.decode()

        assert _alternate(html).endswith(f"/insight/gremien/{bestand['rat'].pk}/feed.atom")
        assert 'data-testid="gremium-abonnieren"' in html

    def test_termin_speichern_nur_fuer_kommende_sitzungen(self, bestand: dict[str, Any]) -> None:
        kommend = Client().get(f"/insight/termine/{bestand['kommend'].pk}/").content.decode()
        vergangen = Client().get(f"/insight/termine/{bestand['vergangen'].pk}/").content.decode()

        assert f'href="/insight/termine/{bestand["kommend"].pk}/kalender.ics" download' in kommend
        assert 'data-testid="termin-speichern"' not in vergangen

    def test_sitzungsliste_mit_etikett_angekuendigt(self, bestand: dict[str, Any]) -> None:
        mit_tagesordnung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/4", body=bestand["body"], name="Rat", start=timezone.now() + timedelta(days=3)
        )
        OParlAgendaItem.objects.create(external_id=f"{RIS}/top/4", meeting=mit_tagesordnung, number="1", name="Bericht")
        client = Client()
        client.get(f"/insight/kommune/{bestand['body'].pk}/")

        antwort = client.get("/insight/termine/")

        etiketten = [m for m in antwort.context["meetings"] if getattr(m, "angekuendigt", None)]
        assert [m.pk for m in etiketten] == [bestand["kommend"].pk]
        assert antwort.content.decode().count('data-ergebnis="angekuendigt"') == 1


class TestAnleitung:
    def test_mit_feed_adresse(self, bestand: dict[str, Any]) -> None:
        pfad = f"/insight/vorgaenge/{bestand['antrag'].pk}/feed.atom"

        antwort = Client().get("/insight/abonnieren/", {"feed": pfad})

        html = antwort.content.decode()
        assert antwort.status_code == 200
        assert antwort["X-Robots-Tag"] == "noindex"
        assert '<meta name="robots" content="noindex, follow">' in html
        assert f'value="{settings.SITE_URL.rstrip("/")}{pfad}"' in html
        assert "Neues zu „Radweg an der Hauptstraße“" in html
        assert f'href="/insight/vorgaenge/{bestand["antrag"].pk}/"' in html

    @pytest.mark.parametrize(
        "feed",
        [
            "https://boese.example/feed.atom",
            "//boese.example/insight/suche/feed.atom",
            "/insight/vorgaenge/",
            "/admin/",
        ],
    )
    def test_nur_eigene_feeds(self, bestand: dict[str, Any], feed: str) -> None:
        html = Client().get("/insight/abonnieren/", {"feed": feed}).content.decode()

        assert "boese.example" not in html
        assert 'id="feed-adresse"' not in html
        assert "Feeds gibt es für jede Vorgangsliste" in html

    def test_zurueckgenommene_eintraege_ohne_titel(self, bestand: dict[str, Any]) -> None:
        from insight_core.models import SESSION_OPARL_MARKERS

        kennung = "https://mandari.example/" + "/".join(SESSION_OPARL_MARKERS)
        paper = OParlPaper.objects.create(
            external_id=f"{kennung}/paper/9", body=bestand["body"], name="Geheimer Betreff", deleted=True
        )
        gremium = OParlOrganization.objects.create(
            external_id=f"{kennung}/org/9", body=bestand["body"], name="Geheimes Gremium", deleted=True
        )

        for feed in (f"/insight/vorgaenge/{paper.pk}/feed.atom", f"/insight/gremien/{gremium.pk}/feed.atom"):
            html = Client().get("/insight/abonnieren/", {"feed": feed}).content.decode()

            assert "Geheim" not in html, feed
            assert 'id="feed-adresse"' not in html, feed

    @pytest.mark.parametrize("modus", [publication.PAUSED, publication.WITHDRAWN])
    def test_beendete_veroeffentlichung_ohne_titel(self, bestand: dict[str, Any], modus: str) -> None:
        from django.core.cache import cache

        cache.set(publication.CACHE_KEY, {str(bestand["body"].pk): (modus, None)}, 300)
        kommune = str(bestand["body"].pk)

        for feed in (
            f"/insight/vorgaenge/{bestand['antrag'].pk}/feed.atom",
            f"/insight/gremien/{bestand['fraktion'].pk}/feed.atom",
            f"/insight/vorgaenge/feed.atom?kommune={kommune}",
            f"/insight/beschluesse/feed.atom?kommune={kommune}",
            f"/insight/suche/feed.atom?q=Radweg&kommune={kommune}",
        ):
            html = Client().get("/insight/abonnieren/", {"feed": feed}).content.decode()

            assert "Hauptstraße" not in html and "Grüne" not in html, feed
            assert 'id="feed-adresse"' not in html, feed

    def test_suche_ohne_text_der_anfrage_im_titel(self, bestand: dict[str, Any]) -> None:
        feed = f"/insight/suche/feed.atom?q=Rufen+Sie+0900+an&kommune={bestand['body'].pk}"

        html = Client().get("/insight/abonnieren/", {"feed": feed}).content.decode()

        titel = re.search(r'<h2 id="feed-titel"[^>]*>([^<]*)</h2>', html)
        assert titel and titel.group(1) == "Neue Treffer dieser Suche"
        assert 'id="feed-adresse"' in html


def test_kopf_aktion_mit_kurzem_text_am_handy() -> None:
    from django.template import engines
    from django_cotton.compiler_regex import CottonCompiler

    def render(quelle: str) -> str:
        return engines["django"].from_string(CottonCompiler().process(quelle)).render({})

    abo = render(
        '<c-insight.kopf-aktion href="/a/" icon="rss" kurz="Abonnieren" data-testid="x">Neues abonnieren</c-insight.kopf-aktion>'
    )
    assert '<a href="/a/"' in abo and 'data-testid="x"' in abo and 'data-lucide="rss"' in abo
    assert '<span class="max-sm:hidden">Neues abonnieren</span><span class="sm:hidden">Abonnieren</span>' in abo
    assert " download" not in abo
    termin = render(
        '<c-insight.kopf-aktion href="/t.ics" icon="calendar-plus" download>Termin speichern</c-insight.kopf-aktion>'
    )
    assert '<a href="/t.ics" download' in termin and "Termin speichern" in termin and "sm:hidden" not in termin
