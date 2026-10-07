# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abruf der RIS-Dateien (Issue #919, ``docs/adr/20261007-dokumentkette.md``, Abschnitte 2 bis 5, 8 und 10).

Ohne Netz: Die Quelle bildet ein ``httpx.MockTransport`` nach, die robots.txt der Fixture ``_robots_ohne_netz``
(alles erlaubt) bzw. ``robots._fetch``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from hub.ris import abruf, abruf_kennzahlen
from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import file_cache, robots

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.4 Anlage " + b"x" * 64
WORKER = override_settings(TEXT_EXTRACTION_RUNNER="worker")


@pytest.fixture(autouse=True)
def _ablage(tmp_path: Path, settings: Any) -> Iterator[Path]:
    settings.OPARL_FILES_ROOT = str(tmp_path / "ablage")
    settings.FILE_CACHE_MIN_FREE_GB = 0
    cache.clear()
    yield tmp_path / "ablage"
    cache.clear()


def _quelle(*, gelistet: bool = True, sync_config: dict[str, Any] | None = None, **felder: Any) -> OParlBody:
    source = OParlSource.objects.create(
        name=f"Quelle {uuid.uuid4().hex[:6]}",
        url=f"https://ris.example.org/{uuid.uuid4().hex[:6]}/system",
        sync_config=sync_config or {},
        **felder,
    )
    return OParlBody.objects.create(
        source=source, external_id=f"https://ris.example.org/bodies/{uuid.uuid4()}", name="Beispiel", is_listed=gelistet
    )


def _datei(body: OParlBody, *, alter: timedelta | None = None, **felder: Any) -> OParlFile:
    datei = OParlFile.objects.create(
        body=body,
        external_id=f"https://ris.example.org/files/{uuid.uuid4()}",
        name="Vorlage",
        mime_type="application/pdf",
        download_url=f"https://ris.example.org/dokumente/{uuid.uuid4().hex[:8]}.pdf",
        **felder,
    )
    if alter is not None:
        OParlFile.objects.filter(pk=datei.pk).update(created_at=timezone.now() - alter)
        datei.refresh_from_db()
    return datei


def _quelle_antwortet(antwort: Callable[[httpx.Request], httpx.Response]) -> tuple[httpx.Client, list[str]]:
    abrufe: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        abrufe.append(str(request.url))
        return antwort(request)

    return httpx.Client(transport=httpx.MockTransport(handler)), abrufe


def _pdf(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})


def _status(code: int, inhalt: bytes = b"") -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(code, content=inhalt)


def _wirft(fehler: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def antwort(_request: httpx.Request) -> httpx.Response:
        raise fehler

    return antwort


# =============================================================================
# Tabelle des Abrufs (ADR Abschnitt 4)
# =============================================================================


@pytest.mark.parametrize(
    ("antwort", "zustand", "code"),
    [
        (_wirft(httpx.ReadTimeout("zu langsam")), "retry", "timeout"),
        (_wirft(httpx.ConnectError("abgelehnt")), "retry", "verbindung"),
        (_status(429), "retry", "http_429"),
        (_status(503), "retry", "http_5xx"),
        (_status(200), "retry", "leer"),
        (_status(404), "retry", "nicht_gefunden"),
        (_status(410), "retry", "nicht_gefunden"),
        (_status(200, b"<!DOCTYPE html><html><body>Bitte warten</body></html>"), "refused", "html"),
        (_status(403), "error", "http_4xx"),
    ],
)
def test_abruffehler_setzen_den_zustand_und_nie_die_texterkennung(
    antwort: Callable[[httpx.Request], httpx.Response], zustand: str, code: str
) -> None:
    datei = _datei(_quelle(), text_extraction_status="pending")
    client, abrufe = _quelle_antwortet(antwort)

    assert abruf.abrufen(datei, client=client) == zustand

    datei.refresh_from_db()
    assert (datei.local_status, datei.fetch_error) == (zustand, code)
    assert datei.fetch_attempts == 1
    assert datei.text_extraction_status == "pending", "ein Abruffehler ändert die Texterkennung nie"
    assert len(abrufe) == 1
    if zustand == "retry":
        assert datei.fetch_next_at is not None
        assert timedelta(minutes=14) < datei.fetch_next_at - timezone.now() <= timedelta(minutes=15)
    if zustand == "error":
        assert datei.fetch_next_at is not None and datei.fetch_next_at - timezone.now() > timedelta(days=6)
    if zustand == "refused":
        assert datei.fetch_next_at is None
        assert datei.local_error == abruf.HTML_TEXT, "ältere Images erkennen den Text wieder"


def test_robots_txt_verbietet_oder_ist_nicht_erreichbar(monkeypatch: pytest.MonkeyPatch) -> None:
    gesperrt = _datei(_quelle())
    client, abrufe = _quelle_antwortet(_pdf)
    monkeypatch.setattr(robots, "_fetch", lambda url, *a, **k: (200, b"User-agent: *\nDisallow: /dokumente/\n"))
    assert abruf.abrufen(gesperrt, client=client) == "refused"
    gesperrt.refresh_from_db()
    assert (gesperrt.local_status, gesperrt.fetch_error) == ("refused", "robots")
    assert gesperrt.local_error.startswith(robots.SKIP_ERROR_PREFIX)

    cache.clear()
    stoerung = _datei(_quelle())
    monkeypatch.setattr(robots, "_fetch", lambda url, *a, **k: (503, b""))
    assert abruf.abrufen(stoerung, client=client) == "retry"
    stoerung.refresh_from_db()
    assert (stoerung.local_status, stoerung.fetch_error) == ("retry", "robots_unerreichbar")
    assert abrufe == [], "die Datei selbst wird in beiden Fällen nicht angefragt"


def test_zu_gross(settings: Any) -> None:
    settings.FILE_CACHE_MAX_MB = 0
    datei = _datei(_quelle())
    client, _ = _quelle_antwortet(_pdf)
    assert abruf.abrufen(datei, client=client) == "too_large"
    datei.refresh_from_db()
    assert (datei.local_status, datei.fetch_error) == ("too_large", "zu_gross")


def test_wiederholungen_mit_wachsendem_abstand_dann_fehler() -> None:
    jetzt = timezone.now()
    erwartet = [timedelta(minutes=15), timedelta(hours=1), timedelta(hours=6), timedelta(hours=24), timedelta(hours=72)]
    for versuch, abstand in enumerate(erwartet, start=1):
        assert abruf.failure_state("http_5xx", versuch, jetzt, jetzt) == ("retry", jetzt + abstand)
    assert abruf.failure_state("http_5xx", 6, jetzt, jetzt) == ("error", jetzt + timedelta(days=7))


def test_404_nur_in_den_ersten_sieben_tagen_wiederholt() -> None:
    jetzt = timezone.now()
    assert abruf.failure_state("nicht_gefunden", 1, jetzt - timedelta(days=6), jetzt)[0] == "retry"
    assert abruf.failure_state("nicht_gefunden", 6, jetzt - timedelta(days=1), jetzt) == ("missing", None)
    assert abruf.failure_state("nicht_gefunden", 1, jetzt - timedelta(days=8), jetzt) == ("missing", None)

    alt = _datei(_quelle(), alter=timedelta(days=30))
    client, _ = _quelle_antwortet(_status(404))
    assert abruf.abrufen(alt, client=client) == "missing"


def test_erfolg_legt_ab_und_setzt_die_zaehler_zurueck() -> None:
    datei = _datei(_quelle())
    OParlFile.objects.filter(pk=datei.pk).update(
        local_status="retry", fetch_attempts=3, fetch_next_at=timezone.now() - timedelta(minutes=1), fetch_error="leer"
    )
    datei.refresh_from_db()
    client, abrufe = _quelle_antwortet(_pdf)

    assert abruf.abrufen(datei, client=client) == "ok"

    datei.refresh_from_db()
    assert (datei.local_status, datei.fetch_attempts, datei.fetch_next_at, datei.fetch_error) == ("ok", 0, None, "")
    assert Path(datei.local_path or "").read_bytes() == PDF
    assert len(abrufe) == 1
    assert abruf.abrufen(datei, client=client) == "skipped", "schon abgelegt: kein zweiter Abruf"
    assert len(abrufe) == 1


# =============================================================================
# Beanspruchung (ADR Abschnitt 2)
# =============================================================================


def test_nur_ein_abruf_beansprucht_die_datei() -> None:
    datei = _datei(_quelle())
    erste = abruf.claim(datei.pk)
    assert erste is not None and erste.status == "none"
    assert abruf.claim(datei.pk) is None, "schon beansprucht"

    client, abrufe = _quelle_antwortet(_pdf)
    datei.refresh_from_db()
    assert datei.local_status == "fetching"
    assert abruf.abrufen(datei, client=client) == "skipped"
    assert abrufe == [], "zwei Aufträge, ein Quellabruf"


def test_von_hand_auch_verweigerte_und_nicht_faellige_abrufe() -> None:
    """Admin-Aktion „Lokal zwischenspeichern“: wie bisher jede Datei ohne Kopie (``force``)."""
    body = _quelle()
    verweigert = _datei(body)
    OParlFile.objects.filter(pk=verweigert.pk).update(local_status="refused", fetch_error="html")
    verweigert.refresh_from_db()
    client, abrufe = _quelle_antwortet(_pdf)
    assert abruf.abrufen(verweigert, client=client) == "skipped"
    assert abrufe == []
    assert file_cache.fetch_and_cache(verweigert, client=client, force=True) == "ok"
    verweigert.refresh_from_db()
    assert (verweigert.local_status, verweigert.fetch_error) == ("ok", "")


def test_nicht_faellige_wiederholung_wird_nicht_beansprucht() -> None:
    datei = _datei(_quelle())
    OParlFile.objects.filter(pk=datei.pk).update(
        local_status="retry", fetch_next_at=timezone.now() + timedelta(hours=1)
    )
    assert abruf.claim(datei.pk) is None
    OParlFile.objects.filter(pk=datei.pk).update(fetch_next_at=timezone.now() - timedelta(seconds=1))
    assert abruf.claim(datei.pk) is not None


@pytest.mark.parametrize(
    "felder",
    [
        {"deleted": True},
        {"deleted": True, "deletion_reason": "datenschutz"},
        {"source_missing_since": timezone.now()},
        {"content_purged_at": timezone.now()},
    ],
)
def test_ausgeschlossene_dateien_werden_nie_abgerufen(felder: dict[str, Any]) -> None:
    datei = _datei(_quelle(), **felder)
    client, abrufe = _quelle_antwortet(_pdf)
    assert abruf.claim(datei.pk) is None
    assert abruf.abrufen(datei, client=client) == "excluded"
    assert datei.pk not in set(abruf.pending_queryset().values_list("pk", flat=True))
    assert abrufe == []


def test_ohne_ergebnis_wird_der_zustand_wiederhergestellt(monkeypatch: pytest.MonkeyPatch) -> None:
    datei = _datei(_quelle())
    faellig = timezone.now() - timedelta(minutes=5)
    OParlFile.objects.filter(pk=datei.pk).update(local_status="retry", fetch_attempts=2, fetch_next_at=faellig)
    datei.refresh_from_db()
    client, abrufe = _quelle_antwortet(_pdf)

    monkeypatch.setattr(file_cache, "has_room_for", lambda size: False)
    assert abruf.abrufen(datei, client=client) == "disk_full"
    datei.refresh_from_db()
    assert (datei.local_status, datei.fetch_attempts, datei.fetch_next_at) == ("retry", 2, faellig)
    assert abrufe == []

    monkeypatch.undo()
    monkeypatch.setattr("insight_core.services.host_pacing.wait", lambda *a, **k: False)
    assert abruf.abrufen(datei, client=client, max_wait=0.1) == "busy"
    datei.refresh_from_db()
    assert (datei.local_status, datei.fetch_attempts) == ("retry", 2), "Takt belegt zählt keinen Versuch"
    assert abrufe == []


def test_quelle_in_schonung_oder_abgeschaltet_kein_versuch() -> None:
    client, abrufe = _quelle_antwortet(_pdf)
    for body in (
        _quelle(consecutive_failures=5),
        _quelle(sync_config={"file_downloads": False}),
        _quelle(is_active=False),
    ):
        datei = _datei(body)
        assert abruf.abrufen(datei, client=client) == "paused"
        datei.refresh_from_db()
        assert (datei.local_status, datei.fetch_attempts) == ("none", 0)
    assert abrufe == []


def test_liegen_gebliebene_beanspruchungen_werden_freigegeben() -> None:
    body = _quelle()
    neu, wieder = _datei(body), _datei(body)
    vorbei = timezone.now() - timedelta(minutes=1)
    OParlFile.objects.filter(pk=neu.pk).update(local_status="fetching", fetch_next_at=vorbei)
    OParlFile.objects.filter(pk=wieder.pk).update(local_status="fetching", fetch_next_at=vorbei, fetch_attempts=2)
    laeuft = _datei(body)
    OParlFile.objects.filter(pk=laeuft.pk).update(
        local_status="fetching", fetch_next_at=timezone.now() + timedelta(minutes=10)
    )

    assert abruf.release_stale() == 2

    assert OParlFile.objects.get(pk=neu.pk).local_status == "none"
    assert OParlFile.objects.get(pk=wieder.pk).local_status == "retry"
    assert OParlFile.objects.get(pk=laeuft.pk).local_status == "fetching"


# =============================================================================
# Auswahl und Lauf (ADR Abschnitte 1, 8)
# =============================================================================


def test_auswahl_faellige_wiederholungen_zuerst_ohne_nicht_faellige() -> None:
    body = _quelle()
    neu = _datei(body)
    faellig = _datei(body)
    OParlFile.objects.filter(pk=faellig.pk).update(
        local_status="retry", fetch_next_at=timezone.now() - timedelta(minutes=1)
    )
    spaeter = _datei(body)
    OParlFile.objects.filter(pk=spaeter.pk).update(
        local_status="retry", fetch_next_at=timezone.now() + timedelta(hours=1)
    )
    alter_fehler = _datei(body)
    OParlFile.objects.filter(pk=alter_fehler.pk).update(local_status="error", fetch_next_at=None)

    assert list(abruf.pending_queryset().values_list("pk", flat=True)) == [faellig.pk, neu.pk]
    mit_fehlern = set(abruf.pending_queryset(retry_errors=True).values_list("pk", flat=True))
    assert alter_fehler.pk in mit_fehlern and spaeter.pk not in mit_fehlern


def test_lauf_gibt_frei_und_haelt_die_grenze_je_quelle(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.DOCUMENT_FETCH_MAX_QUEUED = 2
    eins, zwei = _quelle(), _quelle()
    for _ in range(3):
        _datei(eins)
    _datei(zwei)
    haengt = _datei(zwei)
    OParlFile.objects.filter(pk=haengt.pk).update(
        local_status="fetching", fetch_next_at=timezone.now() - timedelta(minutes=1)
    )
    client, abrufe = _quelle_antwortet(_pdf)
    monkeypatch.setattr(abruf, "fetch_client", lambda: client)

    ergebnis = abruf.nachladen(limit=10, sleep=0)

    assert ergebnis["ok"] == 4, ergebnis
    assert OParlFile.objects.filter(body=eins, local_status="ok").count() == 2
    assert OParlFile.objects.filter(body=zwei, local_status="ok").count() == 2, "liegen gebliebene Datei mit dabei"
    assert len(abrufe) == 4


# =============================================================================
# Ablage für alle Quellen mit Stichtag (ADR Abschnitt 5)
# =============================================================================


def test_regel_der_ablage() -> None:
    gestern = (timezone.now() - timedelta(days=1)).isoformat()
    gelistet = _quelle()
    pilot = _quelle(gelistet=False)
    pilot_mit_stichtag = _quelle(gelistet=False, sync_config={"document_since": gestern})
    pilot_ausstehend = _quelle(gelistet=False, sync_config={"document_since": "ausstehend"})
    pilot_nachholen = _quelle(gelistet=False, sync_config={"document_since": gestern, "document_backfill": True})
    aus = _quelle(sync_config={"file_downloads": False})

    def regel(body: OParlBody, alter: timedelta | None = None) -> bool:
        datei = _datei(body, alter=alter)
        ergebnis = abruf.stores_file(datei)
        in_abfrage = OParlFile.objects.filter(abruf.storable_q(), pk=datei.pk).exists()
        assert ergebnis == in_abfrage, "stores_file und storable_q stimmen überein"
        return ergebnis

    # Bisheriger Betrieb (TEXT_EXTRACTION_RUNNER=ingestor): nur gelistete Kommunen
    assert regel(gelistet, timedelta(days=900))
    assert not regel(pilot_mit_stichtag)
    assert not regel(aus)
    with WORKER:
        assert regel(gelistet, timedelta(days=900)), "leerer Stichtag: bisher abgelegt, alles"
        assert not regel(pilot), "leerer Stichtag gilt nur für bisher abgelegte Quellen"
        assert regel(pilot_mit_stichtag), "neue Datei ab Stichtag"
        assert not regel(pilot_mit_stichtag, timedelta(days=3)), "Altbestand ohne Freigabe"
        assert regel(pilot_nachholen, timedelta(days=3)), "Altbestand mit document_backfill"
        assert not regel(pilot_ausstehend), "erster vollständiger Sync noch nicht beendet"
        assert not regel(aus)


def test_stichtage_setzen() -> None:
    jetzt = timezone.now()
    gelistet = _quelle(last_successful_full_sync=jetzt).source
    bekannt = _quelle(gelistet=False, last_successful_full_sync=jetzt - timedelta(days=2)).source
    neu = _quelle(gelistet=False).source
    fertig = _quelle(
        gelistet=False, sync_config={"document_since": "ausstehend"}, last_successful_full_sync=jetzt
    ).source
    assert gelistet is not None and bekannt is not None and neu is not None and fertig is not None

    vorschau = abruf.stichtage_setzen(now=jetzt, ausfuehren=False)
    assert vorschau == {
        str(bekannt.pk): jetzt.isoformat(),
        str(neu.pk): "ausstehend",
        str(fertig.pk): jetzt.isoformat(),
    }
    assert "document_since" not in OParlSource.objects.get(pk=bekannt.pk).sync_config, "Probelauf schreibt nicht"

    abruf.stichtage_setzen(now=jetzt)
    assert abruf.stichtage_setzen(now=jetzt + timedelta(hours=1)) == {}, "idempotent"
    assert "document_since" not in OParlSource.objects.get(pk=gelistet.pk).sync_config
    assert OParlSource.objects.get(pk=neu.pk).sync_config["document_since"] == "ausstehend"


def test_neue_inhalte_gehen_sofort_in_den_objektspeicher(monkeypatch: pytest.MonkeyPatch) -> None:
    hochgeladen: list[str] = []
    monkeypatch.setattr("insight_core.services.file_store.upload_now", lambda sha256: hochgeladen.append(sha256))
    datei = _datei(_quelle())
    client, _ = _quelle_antwortet(_pdf)
    assert abruf.abrufen(datei, client=client) == "ok"
    datei.refresh_from_db()
    assert hochgeladen == [datei.blob_id]


# =============================================================================
# Freigeben, Zurücksetzen, Befehl
# =============================================================================


def _zustand(datei: OParlFile, **felder: Any) -> OParlFile:
    OParlFile.objects.filter(pk=datei.pk).update(**felder)
    datei.refresh_from_db()
    return datei


def test_zuruecksetzen_fuer_ein_aelteres_image_idempotent() -> None:
    body = _quelle()
    robots_datei = _zustand(
        _datei(body), local_status="refused", fetch_error="robots", local_error="robots.txt sperrt X"
    )
    html = _zustand(_datei(body), local_status="refused", fetch_error="html", local_error="")
    wiederholung = _zustand(_datei(body), local_status="retry", fetch_attempts=2)
    laeuft = _zustand(_datei(body), local_status="fetching")
    abgelegt = _zustand(_datei(body), local_status="ok", text_content="Text bleibt")

    zahlen = abruf.zuruecksetzen()
    assert zahlen == {
        "verweigert_robots": 1,
        "verweigert_html": 1,
        "verweigert_sonstige": 0,
        "wiederholung_oder_laufend": 2,
    }
    for datei in (robots_datei, html, wiederholung, laeuft, abgelegt):
        datei.refresh_from_db()
    assert (robots_datei.local_status, robots_datei.local_error) == ("error", "robots.txt sperrt X")
    assert (html.local_status, html.local_error) == ("error", abruf.HTML_TEXT)
    assert wiederholung.local_status == laeuft.local_status == "none"
    assert wiederholung.fetch_attempts == 2, "nur Zustandsspalten"
    assert (abgelegt.local_status, abgelegt.text_content) == ("ok", "Text bleibt")
    assert set(abruf.zuruecksetzen().values()) == {0}


def test_befehl_dokumentkette() -> None:
    body = _quelle()
    datei = _zustand(_datei(body), local_status="refused", fetch_error="html", local_error=abruf.HTML_TEXT)
    andere = _zustand(_datei(_quelle()), local_status="refused", fetch_error="html")
    assert body.source is not None

    out = StringIO()
    call_command("dokumentkette", "freigeben", str(body.source.pk), "--probelauf", stdout=out)
    assert "1 verweigerte Abrufe würden neu eingereiht" in out.getvalue()
    datei.refresh_from_db()
    assert datei.local_status == "refused"

    call_command("dokumentkette", "freigeben", str(body.source.pk), stdout=StringIO())
    datei.refresh_from_db()
    andere.refresh_from_db()
    assert (datei.local_status, datei.fetch_error, datei.local_error) == ("none", "", "")
    assert andere.local_status == "refused", "nur die genannte Quelle"

    out = StringIO()
    call_command("dokumentkette", "zuruecksetzen", stdout=out)
    assert "verweigert_html=1" in out.getvalue()
    andere.refresh_from_db()
    assert andere.local_status == "error"

    neu = _quelle(gelistet=False).source
    assert neu is not None
    out = StringIO()
    call_command("dokumentkette", "umschalten", "--probelauf", stdout=out)
    assert "1 Stichtage würden gesetzt" in out.getvalue()
    assert "document_since" not in OParlSource.objects.get(pk=neu.pk).sync_config
    call_command("dokumentkette", "umschalten", stdout=StringIO())
    assert OParlSource.objects.get(pk=neu.pk).sync_config["document_since"] == "ausstehend"


# =============================================================================
# Kennzahlen und Prüfung (ADR Abschnitt 10)
# =============================================================================


def test_kennzahlen_und_pruefung(settings: Any) -> None:
    body = _quelle()
    _datei(body)
    lange = _datei(body)
    OParlFile.objects.filter(pk=lange.pk).update(
        local_status="retry", fetch_next_at=timezone.now() - timedelta(hours=7)
    )
    schonung = _datei(_quelle(consecutive_failures=5))
    OParlFile.objects.filter(pk=schonung.pk).update(
        local_status="retry", fetch_next_at=timezone.now() - timedelta(hours=7)
    )

    # Kennzahlen zählen alle wartenden Abrufe, auch die der Quelle in Schonung; die Prüfung nicht
    assert abruf.counts() == {"queued": 3, "retry_due": 2}

    familien = {familie.name: familie for familie in abruf_kennzahlen.AbrufCollector().collect()}
    assert set(familien) == {"mandari_files_fetch_queued", "mandari_files_fetch_retry_due"}
    assert familien["mandari_files_fetch_retry_due"].samples[0].value == 2

    pruefung = abruf_kennzahlen.check_fetch()
    assert not pruefung.ok
    assert pruefung.detail == "1 fällige Wiederholungen länger als 6 h offen", "Quelle in Schonung zählt nicht"
    settings.DOCUMENT_FETCH_RETRY_ALERT_HOURS = 8
    assert abruf_kennzahlen.check_fetch().ok
    settings.EVENTS_SCHEDULES_DISABLED = ("befehl:cache_files",)
    settings.DOCUMENT_FETCH_RETRY_ALERT_HOURS = 6
    assert abruf_kennzahlen.check_fetch().detail == "Zeitplan abgeschaltet"


def test_fehler_werden_je_quelle_und_code_gezaehlt() -> None:
    body = _quelle()
    assert body.source is not None
    zaehler = abruf.FETCH_ERRORS.labels(source=str(body.source.pk), code="http_5xx")
    vorher = zaehler._value.get()
    client, _ = _quelle_antwortet(_status(502))
    abruf.abrufen(_datei(body), client=client)
    assert zaehler._value.get() == vorher + 1


# =============================================================================
# Ein Weg zur Quelle (ADR Abschnitt 7)
# =============================================================================


def test_vertrag_ein_weg_zur_quelle_bleibt_bestehen() -> None:
    """Der import-linter-Vertrag ``abruf-ein-weg`` steht nicht im Ratchet der Schichtregeln: hier festgehalten."""
    import tomllib

    konfiguration = tomllib.loads((Path(__file__).resolve().parents[3] / "pyproject.toml").read_text(encoding="utf-8"))
    vertraege = {v["id"]: v for v in konfiguration["tool"]["importlinter"]["contracts"]}
    vertrag = vertraege["abruf-ein-weg"]
    assert vertrag["type"] == "protected"
    assert vertrag["protected_modules"] == ["hub.ris.abruf"]
    assert sorted(vertrag["allowed_importers"]) == [
        "hub.ris",
        "insight_core.services.file_cache",
        "insight_core.services.file_reconcile",
        "insight_core.views.files",
    ]
    assert vertrag["ignore_imports"] == ["**.tests.** -> **"]
