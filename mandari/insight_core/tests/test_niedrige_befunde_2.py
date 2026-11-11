# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kleine Korrekturen im Bürgerportal.

- Zwischengespeicherte Feeds und Kartenpunkte entstehen nach einer Rücknahme neu.
- Die Rolle im Rat stammt nur aus sichtbaren Mitgliedschaften.
- Personenfotos mit übergroßer Fläche werden vor dem Dekodieren abgewiesen.
- Die Größengrenze für Downloads zählt übertragene Bytes; kodierte Antworten werden abgewiesen.
- Der Befehl zum Anlegen einer Quelle ruft nur öffentliche Ziele ab.
"""

from __future__ import annotations

import gzip
import io
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from hub.ris import retraction
from insight_core.management.commands.add_oparl_source import Command as QuelleAnlegen
from insight_core.models import (
    OParlBody,
    OParlFile,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    PaperLocation,
)
from insight_core.services import person_photos, question_service, ruecknahme_stand, safe_fetch
from insight_core.tests.conftest import CENTER_LAT, CENTER_LON

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _leerer_cache() -> Any:
    cache.clear()
    yield
    cache.clear()


def _mit_kommune(body: OParlBody) -> Client:
    client = Client()
    client.get(f"/insight/kommune/{body.pk}/")
    return client


# --- Stand der Rücknahmen ---------------------------------------------------------------------------------


def test_stand_aendert_sich_mit_einer_ruecknahme(
    geo_body: OParlBody, make_paper: Callable[..., OParlPaper], django_capture_on_commit_callbacks: Any
) -> None:
    vorgang = make_paper(geo_body)
    vorher = ruecknahme_stand.stand(geo_body.pk)

    with django_capture_on_commit_callbacks(execute=True):
        assert retraction.retract(vorgang, reason=retraction.REASON_WITHDRAWN) is True

    # Die Rücknahme in der Anwendung verwirft den Stand sofort
    assert ruecknahme_stand.stand(geo_body.pk) != vorher
    assert ruecknahme_stand.stand() != ""


def test_markierung_ausserhalb_der_anwendung_wird_nach_ablauf_gesehen(
    geo_body: OParlBody, make_paper: Callable[..., OParlPaper]
) -> None:
    vorgang = make_paper(geo_body)
    vorher = ruecknahme_stand.stand(geo_body.pk)
    OParlPaper.objects.filter(pk=vorgang.pk).update(deleted=True)

    # Innerhalb der Geltungsdauer bleibt der gelesene Stand, danach zählt die nächste Ausgabe neu
    assert ruecknahme_stand.stand(geo_body.pk) == vorher
    cache.delete(ruecknahme_stand._value_key(geo_body.pk))
    assert ruecknahme_stand.stand(geo_body.pk) != vorher


def test_stand_unterscheidet_wiederherstellung_und_neue_ruecknahme(
    geo_body: OParlBody, make_paper: Callable[..., OParlPaper]
) -> None:
    """Ein Vorgang kehrt zurück, ein anderer entfällt: gleiche Zahl, anderer Stand."""
    alt, neu = make_paper(geo_body), make_paper(geo_body)
    frueher = timezone.now() - timedelta(hours=2)
    OParlPaper.objects.filter(pk=alt.pk).update(deleted=True, deleted_at=frueher)
    vorher = ruecknahme_stand.stand(geo_body.pk)

    # Wie der Ingestor: ohne die Anwendung, mit Zeitpunkt der Markierung
    OParlPaper.objects.filter(pk=alt.pk).update(deleted=False, deleted_at=None)
    OParlPaper.objects.filter(pk=neu.pk).update(deleted=True, deleted_at=timezone.now())
    cache.delete(ruecknahme_stand._value_key(geo_body.pk))

    assert ruecknahme_stand.stand(geo_body.pk) != vorher


def test_stand_aendert_sich_mit_der_ruecknahme_einer_datei(
    geo_body: OParlBody, make_paper: Callable[..., OParlPaper], django_capture_on_commit_callbacks: Any
) -> None:
    vorgang = make_paper(geo_body)
    datei = OParlFile.objects.create(
        external_id="https://ris.beispielstadt.example/oparl/file/1", paper=vorgang, name="Anlage 1"
    )
    vorher = ruecknahme_stand.stand(geo_body.pk)

    with django_capture_on_commit_callbacks(execute=True):
        assert retraction.retract(datei, reason=retraction.REASON_WITHDRAWN) is True

    nachher = ruecknahme_stand.stand(geo_body.pk)
    assert nachher != vorher
    # Auch die Zählung selbst sieht die Datei (Markierung ohne die Anwendung)
    assert nachher.split(":", 1)[1] != vorher.split(":", 1)[1]


def test_stand_aendert_sich_mit_der_ruecknahme_einer_beratung_durch_den_ingestor(
    geo_body: OParlBody, make_paper: Callable[..., OParlPaper]
) -> None:
    from insight_core.models import OParlConsultation

    vorgang = make_paper(geo_body)
    beratung = OParlConsultation.objects.create(
        external_id="https://ris.beispielstadt.example/oparl/consultation/1", paper=vorgang
    )
    vorher = ruecknahme_stand.stand(geo_body.pk)

    # Wie der Ingestor: Markierung ohne die Anwendung, die Kommune steht nur am Vorgang
    OParlConsultation.objects.filter(pk=beratung.pk).update(deleted=True, deleted_at=timezone.now())
    cache.delete(ruecknahme_stand._value_key(geo_body.pk))

    assert ruecknahme_stand.stand(geo_body.pk) != vorher


def test_alte_zaehlung_ueberdeckt_den_neuen_stand_nicht(
    geo_body: OParlBody, make_paper: Callable[..., OParlPaper], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eine Ausgabe zählt, währenddessen wird zurückgenommen: Ihr Ergebnis gilt nicht für den neuen Stand."""
    vorgang = make_paper(geo_body)
    echte_zaehlung = ruecknahme_stand._count
    laeufe: list[str] = []

    def zaehlen(body_id: Any) -> str:
        ergebnis = echte_zaehlung(body_id)
        if not laeufe:
            # Zwischen Zählung und Ablage: Rücknahme samt forget
            OParlPaper.objects.filter(pk=vorgang.pk).update(deleted=True, deleted_at=timezone.now())
            ruecknahme_stand.forget()
        laeufe.append(ergebnis)
        return ergebnis

    monkeypatch.setattr(ruecknahme_stand, "_count", zaehlen)

    veraltet = ruecknahme_stand.stand(geo_body.pk)
    aktuell = ruecknahme_stand.stand(geo_body.pk)

    assert len(laeufe) == 2, "nach der Rücknahme wird neu gezählt"
    assert aktuell != veraltet and aktuell.split(":", 1)[1] == laeufe[1]


def test_feed_nennt_zurueckgenommenen_vorgang_nicht_aus_dem_cache(
    geo_body: OParlBody, make_paper: Callable[..., OParlPaper], django_capture_on_commit_callbacks: Any
) -> None:
    vorgang = make_paper(geo_body, name="Neubau der Feuerwache")
    client = Client()
    adresse = "/insight/vorgaenge/feed.atom"

    erste = client.get(adresse, {"kommune": str(geo_body.pk)})
    assert "Neubau der Feuerwache" in erste.content.decode()
    assert erste["Cache-Control"] == "public, no-cache"

    with django_capture_on_commit_callbacks(execute=True):
        retraction.retract(vorgang, reason=retraction.REASON_WITHDRAWN)

    assert "Neubau der Feuerwache" not in client.get(adresse, {"kommune": str(geo_body.pk)}).content.decode()


def test_kartenpunkte_ohne_zurueckgenommenen_vorgang(
    geo_body: OParlBody, make_paper: Callable[..., OParlPaper], django_capture_on_commit_callbacks: Any
) -> None:
    vorgang = make_paper(geo_body, name="Umbau Rathausplatz", date=timezone.localdate())
    PaperLocation.objects.create(
        paper=vorgang, body=geo_body, latitude=CENTER_LAT, longitude=CENTER_LON, name="Rathausplatz"
    )
    client = _mit_kommune(geo_body)
    adresse = reverse("insight_core:insight:map_markers")

    assert "Umbau Rathausplatz" in client.get(adresse, {"all": "1"}).content.decode()

    with django_capture_on_commit_callbacks(execute=True):
        retraction.retract(vorgang, reason=retraction.REASON_WITHDRAWN)

    assert "Umbau Rathausplatz" not in client.get(adresse, {"all": "1"}).content.decode()


# --- Rolle im Rat ---------------------------------------------------------------------------------------------


def test_rolle_im_rat_nur_aus_sichtbaren_mitgliedschaften(geo_body: OParlBody) -> None:
    ris = "https://ris.beispielstadt.example/oparl"
    rat = OParlOrganization.objects.create(external_id=f"{ris}/org/rat", body=geo_body, name="Rat")
    person = OParlPerson.objects.create(external_id=f"{ris}/person/1", body=geo_body, name="Anna Beispiel")
    mitgliedschaft = OParlMembership.objects.create(
        external_id=f"{ris}/membership/1", person=person, organization=rat, role="Ratsmitglied"
    )
    assert question_service.get_council_role(person) == "Ratsmitglied"
    assert question_service.mandate_memberships(person).exists()  # type: ignore[no-untyped-call]

    OParlMembership.objects.filter(pk=mitgliedschaft.pk).update(deleted=True)
    assert question_service.get_council_role(person) is None
    assert not question_service.mandate_memberships(person).exists()  # type: ignore[no-untyped-call]

    OParlMembership.objects.filter(pk=mitgliedschaft.pk).update(deleted=False)
    OParlOrganization.objects.filter(pk=rat.pk).update(deleted=True)
    assert question_service.get_council_role(person) is None


# --- Personenfotos --------------------------------------------------------------------------------------------


def _bild(breite: int, hoehe: int, modus: str = "RGB") -> bytes:
    puffer = io.BytesIO()
    Image.new(modus, (breite, hoehe)).save(puffer, format="PNG")
    return puffer.getvalue()


def test_foto_mit_uebergrosser_flaeche_wird_vor_dem_dekodieren_abgewiesen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(person_photos, "MAX_PIXELS", 1_000_000)

    with pytest.raises(ValueError, match="Bildpunkte"):
        person_photos._normalize_image(_bild(2000, 2000, "1"))

    klein = person_photos._normalize_image(_bild(800, 600))
    with Image.open(io.BytesIO(klein)) as ergebnis:
        assert ergebnis.format == "JPEG" and max(ergebnis.size) <= max(person_photos.MAX_SIZE)


# --- Downloadgrenze ---------------------------------------------------------------------------------------------


def _client(monkeypatch: pytest.MonkeyPatch, antwort: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
    gesendet: list[httpx.Request] = []

    def mitschreiben(request: httpx.Request) -> httpx.Response:
        gesendet.append(request)
        return antwort(request)

    def client(**kwargs: Any) -> httpx.Client:
        kwargs.pop("transport", None)
        return httpx.Client(transport=httpx.MockTransport(mitschreiben), **kwargs)

    monkeypatch.setattr(safe_fetch, "guarded_client", client)
    return gesendet


def _laden(grenze: int = 10_000) -> tuple[io.BytesIO, safe_fetch.Download]:
    ziel = io.BytesIO()
    ergebnis = safe_fetch.download_to(
        ziel, "https://ris.beispielstadt.example/datei.pdf", max_bytes=grenze, total_seconds=5, timeout=httpx.Timeout(5)
    )
    return ziel, ergebnis


def test_download_fordert_unkomprimiert_an_und_zaehlt_uebertragene_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    inhalt = b"%PDF-1.4 " + b"x" * 500
    gesendet = _client(
        monkeypatch,
        lambda request: httpx.Response(
            200, stream=httpx.ByteStream(inhalt), headers={"Content-Type": "application/pdf"}
        ),
    )

    ziel, ergebnis = _laden()

    assert gesendet[0].headers["Accept-Encoding"] == "identity"
    assert ziel.getvalue() == inhalt and ergebnis.size == len(inhalt)


def test_download_fordert_auch_mit_eigenen_kopfzeilen_unkomprimiert_an(monkeypatch: pytest.MonkeyPatch) -> None:
    gesendet = _client(monkeypatch, lambda request: httpx.Response(200, stream=httpx.ByteStream(b"%PDF-1.4")))

    safe_fetch.download_to(
        io.BytesIO(),
        "https://ris.beispielstadt.example/datei.pdf",
        max_bytes=10_000,
        total_seconds=5,
        timeout=httpx.Timeout(5),
        headers={"accept-encoding": "gzip", "X-Quelle": "1"},
    )

    assert gesendet[0].headers.get_list("accept-encoding") == ["identity"]
    assert gesendet[0].headers["X-Quelle"] == "1"


def test_gesamtfrist_des_downloads_gilt_ueber_weiterleitungen(monkeypatch: pytest.MonkeyPatch) -> None:
    uhr = [100.0]
    monkeypatch.setattr(safe_fetch, "_now", lambda: uhr[0])
    grenzen: list[float] = []
    dauer = {"erste": 4.0}

    def antwort(request: httpx.Request) -> httpx.Response:
        grenzen.append(request.extensions["timeout"]["connect"])
        if request.url.path == "/datei.pdf":
            uhr[0] += dauer["erste"]
            return httpx.Response(302, headers={"Location": "https://ris.beispielstadt.example/ziel.pdf"})
        return httpx.Response(200, stream=httpx.ByteStream(b"%PDF-1.4"))

    gesendet = _client(monkeypatch, antwort)

    # Nach vier von fünf Sekunden: Die zweite Anfrage bekommt nur noch den Rest
    _laden()
    assert grenzen == [pytest.approx(5.0), pytest.approx(1.0)]

    # Die Frist ist bei der Weiterleitung schon abgelaufen: keine zweite Anfrage
    gesendet.clear()
    dauer["erste"] = 6.0
    with pytest.raises(safe_fetch.DeadlineExceededError):
        _laden()
    assert [str(request.url) for request in gesendet] == ["https://ris.beispielstadt.example/datei.pdf"]


def test_kodierte_antwort_wird_abgewiesen(monkeypatch: pytest.MonkeyPatch) -> None:
    gepackt = gzip.compress(b"0" * 5_000_000)
    assert len(gepackt) < 10_000
    _client(
        monkeypatch,
        lambda request: httpx.Response(200, stream=httpx.ByteStream(gepackt), headers={"Content-Encoding": "gzip"}),
    )

    # Als Abruffehler, nicht als „zu groß“: Der nächste Versuch kann wieder gelingen
    with pytest.raises(httpx.DecodingError):
        _laden()


# --- Quelle anlegen -----------------------------------------------------------------------------------------------


def test_quellen_befehl_folgt_keiner_weiterleitung_ins_interne_netz() -> None:
    befehl = QuelleAnlegen()

    with pytest.raises(safe_fetch.BlockedDestinationError):
        befehl._before_redirect(httpx.Request("GET", "http://127.0.0.1:9200/_cat/indices"))
    with pytest.raises(safe_fetch.BlockedDestinationError):
        befehl._get("http://169.254.169.254/latest/meta-data/", {})


def test_quellen_befehl_verbindet_nur_mit_der_geprueften_adresse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Der Client des Befehls prüft jede Anfrage und bindet die Verbindung an die geprüfte Adresse."""
    erzeugt: list[httpx.Client] = []
    echter = safe_fetch.guarded_client

    def merken(**kwargs: Any) -> httpx.Client:
        erzeugt.append(echter(**kwargs))
        return erzeugt[-1]

    monkeypatch.setattr(safe_fetch, "guarded_client", merken)
    # Erst öffentlich (die Prüfung vorab besteht), beim Verbindungsaufbau intern
    antworten = iter([["93.184.215.14"], ["93.184.215.14"], ["10.0.0.5"]])
    monkeypatch.setattr(safe_fetch, "_resolve", lambda host: next(antworten, ["10.0.0.5"]))
    befehl = QuelleAnlegen()
    monkeypatch.setattr(befehl, "_wait_for_slot", lambda url: None)

    with pytest.raises(safe_fetch.BlockedDestinationError):
        befehl._get("https://wechselt.example.org/oparl/body/1", {})

    assert isinstance(erzeugt[0]._transport, safe_fetch.PinnedTransport)
