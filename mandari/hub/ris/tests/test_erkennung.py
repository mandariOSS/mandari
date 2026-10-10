# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Texterkennung aus der Ablage (Issue #919, ``docs/adr/20261007-dokumentkette.md``, Abschnitte 1, 2, 4, 6, 10, 11).

- Fitnessfunktion: Der Auftrag ``file.extract_text`` öffnet keine Verbindung zur Quelle (gesperrtes Netz, Inhalt nur
  im Objektspeicher-Ersatz moto); fehlt der Inhalt, setzt er nur Zustände zurück; ein gestörter Objektspeicher löst
  keinen Quellabruf aus.
- Bedingtes Schreiben, Altbestand ohne SHA-256, neue Erkennungsversion.
- ``extract_texts`` plant nur ein, ``dokumentkette nacharbeiten``, Kennzahlen und Prüfung ``dokumenttext``.
- ``TEXT_EXTRACTION_RUNNER=worker`` verweigert den Start ohne ``TASKS_BACKEND=journal``.
- Vertrag ``ris.file.text_extracted`` v1 mit dem optionalen Feld ``sha256``.

Die Texterkennung selbst ist ersetzt (``erkennung.extract_text``).
"""

from __future__ import annotations

import copy
import hashlib
import runpy
import socket
import uuid
from collections.abc import Iterator
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.conf import settings as django_settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone
from mandari_dokumente import ExtractionResult

from apps.events.models import Task, TaskStatus
from hub.contracts import get_registry
from hub.contracts.compatibility import breaking_changes
from hub.ris import erkennung, erkennung_kennzahlen, text_extraction
from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import file_cache, file_reconcile, file_store, object_storage, text_extraction_job

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.4 Anlage zur Vorlage"
ENDPUNKT = "http://objektspeicher.test"
BUCKET = "mandari-test"
WORKER = override_settings(TEXT_EXTRACTION_RUNNER="worker")


@pytest.fixture(autouse=True)
def _ablage(tmp_path: Path, settings: Any) -> Path:
    settings.OPARL_FILES_ROOT = str(tmp_path / "ablage")
    settings.FILE_STORE_LAYOUT = "sha256"
    settings.FILE_CACHE_MIN_FREE_GB = 0
    settings.OBJ_ENABLED = False
    return tmp_path / "ablage"


@pytest.fixture
def ohne_netz(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Netz gesperrt: Jeder Verbindungsversuch wird vermerkt und scheitert (Datenbank und moto brauchen keins)."""
    versuche: list[str] = []

    def gesperrt(*args: Any, **_kwargs: Any) -> Any:
        versuche.append(repr(args[:2]))
        raise OSError("Netz im Test gesperrt")

    monkeypatch.setattr(socket.socket, "connect", gesperrt)
    monkeypatch.setattr(socket.socket, "connect_ex", gesperrt)
    monkeypatch.setattr(socket, "create_connection", gesperrt)
    monkeypatch.setattr(socket, "getaddrinfo", gesperrt)
    return versuche


@pytest.fixture
def objektspeicher(settings: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    moto = pytest.importorskip("moto")
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", ENDPUNKT)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    settings.OBJ_ENABLED = True
    settings.OBJ_ENDPOINT = ENDPUNKT
    settings.OBJ_BUCKET = BUCKET
    settings.OBJ_KEY = "test"
    settings.OBJ_SECRET = "test"
    settings.OBJ_REGION = ""
    object_storage._client.cache_clear()
    with moto.mock_aws():
        object_storage.client().create_bucket(Bucket=BUCKET)
        yield object_storage.client()
    object_storage._client.cache_clear()


def _quelle(*, gelistet: bool = True, sync_config: dict[str, Any] | None = None) -> OParlBody:
    source = OParlSource.objects.create(
        name=f"Quelle {uuid.uuid4().hex[:6]}",
        url=f"https://ris.example.org/{uuid.uuid4().hex[:6]}/system",
        sync_config=sync_config or {},
    )
    return OParlBody.objects.create(
        source=source, external_id=f"https://ris.example.org/bodies/{uuid.uuid4()}", name="Beispiel", is_listed=gelistet
    )


def _datei(body: OParlBody, inhalt: bytes | None = None, **felder: Any) -> OParlFile:
    """Datei; mit ``inhalt`` abgelegt (``local_status = ok``). Zustände nach dem Ablegen gesetzt."""
    zustand = {k: felder.pop(k) for k in list(felder) if k.startswith(("text_", "local_status", "sha256_hash"))}
    datei = OParlFile.objects.create(
        body=body,
        external_id=f"https://ris.example.org/files/{uuid.uuid4()}",
        name="Vorlage",
        file_name="vorlage.pdf",
        mime_type="application/pdf",
        download_url=f"https://ris.example.org/dokumente/{uuid.uuid4().hex[:8]}.pdf",
        **felder,
    )
    if inhalt is not None:
        file_cache.store_bytes(datei, inhalt)
    if zustand:
        OParlFile.objects.filter(pk=datei.pk).update(**zustand)
    datei.refresh_from_db()
    return datei


def _sha(inhalt: bytes) -> str:
    return hashlib.sha256(inhalt).hexdigest()


def _erkannt(text: str = "Beschluss zum Radweg") -> Any:
    return mock.patch.object(erkennung, "extract_text", return_value=ExtractionResult(text, "pypdf", 2))


# =============================================================================
# Fitnessfunktion: der Auftrag fasst die Quelle nie an
# =============================================================================


def test_auftrag_liest_nur_aus_dem_objektspeicher_ohne_netz(objektspeicher: Any, ohne_netz: list[str]) -> None:
    datei = _datei(_quelle(), PDF)
    file_store.upload_pending()
    Path(str(datei.local_path)).unlink()  # lokal verdrängt, der Inhalt liegt nur im Objektspeicher

    with _erkannt() as erkennen:
        assert erkennung.erkennen(str(datei.pk)) == erkennung.ERLEDIGT

    assert erkennen.call_args.args[0].read_bytes() == PDF, "aus dem Objektspeicher geholt (Hash geprüft)"
    datei.refresh_from_db()
    assert (datei.text_extraction_status, datei.text_content) == ("completed", "Beschluss zum Radweg")
    assert datei.text_source_sha256 == _sha(PDF)
    assert ohne_netz == [], "keine Verbindung nach außen"


def test_fehlt_der_inhalt_setzt_der_auftrag_nur_zustaende_zurueck(objektspeicher: Any, ohne_netz: list[str]) -> None:
    datei = _datei(_quelle(), PDF)
    file_store.upload_pending()
    Path(str(datei.local_path)).unlink()
    objektspeicher.delete_object(Bucket=BUCKET, Key=f"sha256/{_sha(PDF)[:2]}/{_sha(PDF)}")

    with mock.patch.object(erkennung, "extract_text", side_effect=AssertionError("keine Erkennung")):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.OHNE_INHALT

    datei.refresh_from_db()
    assert (datei.local_status, datei.text_extraction_status) == ("none", "pending"), "den Abruf übernimmt der Abrufweg"
    assert ohne_netz == []
    assert not Task.objects.exists(), "kein Auftrag: erst muss der Inhalt wieder abgelegt sein"


def test_gestoerter_objektspeicher_loest_keinen_quellabruf_aus(
    objektspeicher: Any, ohne_netz: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    datei = _datei(_quelle(), PDF)
    file_store.upload_pending()
    Path(str(datei.local_path)).unlink()

    def kaputt(sha256: str, target: Any, **kwargs: Any) -> None:
        raise ConnectionError("Objektspeicher antwortet nicht")

    monkeypatch.setattr(object_storage, "download", kaputt)
    with mock.patch.object(erkennung, "extract_text", side_effect=AssertionError("keine Erkennung")):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.ZURUECKGESTELLT

    datei.refresh_from_db()
    assert (datei.local_status, datei.text_extraction_status, datei.text_extraction_attempts) == ("ok", "pending", 0)
    assert ohne_netz == []
    (auftrag,) = Task.objects.all()
    assert auftrag.task_path == text_extraction_job.TASK_PATH and auftrag.run_after > timezone.now()


# =============================================================================
# Externe Texterkennung: öffentliche RIS-Datei, nur mit freigegebenem Endpunkt (Issue #950)
# =============================================================================

#: Beispiel-Host, den der Test ausdrücklich freigibt (``KI_ERLAUBTE_HOSTS`` hat keinen Standard)
ERLAUBT_HOST = "api.openai-compat.model-serving.eu01.onstackit.cloud"


@pytest.mark.parametrize(("freigegeben", "extern"), [([ERLAUBT_HOST], True), ([], False)])
def test_auftrag_erlaubt_externe_erkennung_nur_mit_freigegebenem_endpunkt(
    settings: Any, freigegeben: list[str], extern: bool
) -> None:
    """Der Auftrag ist der einzige Erkennungsweg für öffentliche RIS-Dateien und gibt ``allow_external=True``.

    Ob Mistral tatsächlich gefragt wird, entscheidet danach allein die Positivliste: mit freigegebenem Host ja,
    ohne Freigabe bleibt es bei der Erkennung im eigenen Betrieb (fail-closed).
    """
    settings.MISTRAL_API_KEY = "schluessel"
    settings.MISTRAL_BASE_URL = f"https://{ERLAUBT_HOST}/v1"
    settings.KI_ERLAUBTE_HOSTS = freigegeben
    datei = _datei(_quelle(), PDF)

    with _erkannt() as erkennen:
        assert erkennung.erkennen(str(datei.pk)) == erkennung.ERLEDIGT

    konfiguration = erkennen.call_args.args[3]
    assert konfiguration.mistral.enabled is extern


# =============================================================================
# Bedingtes Schreiben (ADR Abschnitt 4)
# =============================================================================


def test_ergebnis_zu_ersetztem_inhalt_wird_verworfen() -> None:
    datei = _datei(_quelle(), PDF)

    def inhalt_ersetzt(*_a: Any, **_k: Any) -> ExtractionResult:
        # Der Löschabgleich findet währenddessen eine neue Fassung bei der Quelle
        neu = b"%PDF neu"
        file_reconcile.replace_content(
            OParlFile.objects.get(pk=datei.pk), BytesIO(neu), _sha(neu), "application/pdf", timezone.now()
        )
        return ExtractionResult("Text der alten Fassung", "pypdf", 1)

    with mock.patch.object(erkennung, "extract_text", side_effect=inhalt_ersetzt):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.VERWORFEN

    datei.refresh_from_db()
    assert datei.text_content is None, "kein Text der alten Fassung zum neuen Inhalt"
    assert (datei.sha256_hash, datei.text_extraction_status) == (_sha(b"%PDF neu"), "pending")
    assert datei.text_source_sha256 is None


def test_neuer_inhalt_ohne_zuruecksetzen_wird_eigens_erkannt() -> None:
    """Wird ein anderer Inhalt abgelegt, ohne die Erkennung zurückzusetzen, wartet die Datei danach wieder."""
    datei = _datei(_quelle(), PDF)

    def neu_abgelegt(*_a: Any, **_k: Any) -> ExtractionResult:
        OParlFile.objects.filter(pk=datei.pk).update(sha256_hash=_sha(b"anders"))
        return ExtractionResult("Text", "pypdf", 1)

    with mock.patch.object(erkennung, "extract_text", side_effect=neu_abgelegt):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.VERWORFEN
    datei.refresh_from_db()
    assert (datei.text_extraction_status, datei.text_content, datei.text_extraction_attempts) == ("pending", None, 0)


def test_geleerte_datei_bekommt_ihren_text_nicht_zurueck() -> None:
    """Leert der Löschabgleich (#787) die Datei während der Erkennung, schreibt der Auftrag nichts zurück."""
    datei = _datei(_quelle(), PDF)

    def geleert(*_a: Any, **_k: Any) -> ExtractionResult:
        OParlFile.objects.filter(pk=datei.pk).update(
            text_content=None, text_extraction_status="skipped", content_purged_at=timezone.now()
        )
        return ExtractionResult("Text", "pypdf", 1)

    with mock.patch.object(erkennung, "extract_text", side_effect=geleert):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.VERWORFEN
    datei.refresh_from_db()
    assert (datei.text_content, datei.text_extraction_status) == (None, "skipped")


def test_altbestand_ohne_sha256_im_alten_layout(settings: Any, tmp_path: Path) -> None:
    """Ohne ``blob_id`` und ohne ``sha256_hash``: der Auftrag berechnet den Hash, trägt ihn nach und schreibt."""
    kopie = tmp_path / "ablage" / "kommune" / "2025" / "alt.pdf"
    kopie.parent.mkdir(parents=True)
    kopie.write_bytes(PDF)
    datei = _datei(_quelle(), local_path=str(kopie), local_status="ok")
    assert (datei.blob_id, datei.sha256_hash) == (None, None)

    with _erkannt():
        assert erkennung.erkennen(str(datei.pk)) == erkennung.ERLEDIGT
    datei.refresh_from_db()
    assert datei.sha256_hash == datei.text_source_sha256 == _sha(PDF)
    assert datei.text_content == "Beschluss zum Radweg"


# =============================================================================
# Neue Erkennungsversion (ADR Abschnitt 4)
# =============================================================================


@pytest.fixture
def version_2(monkeypatch: pytest.MonkeyPatch) -> None:
    import mandari_dokumente

    monkeypatch.setattr(mandari_dokumente, "EXTRACTION_VERSION", 2)


def test_ohne_neue_version_ist_nichts_veraltet() -> None:
    body = _quelle()
    _datei(body, PDF, text_extraction_status="completed", text_content="Text")
    assert erkennung.counts() == {"stored_without_text": 0, "text_outdated": 0}
    assert erkennung.outdated_versions() == ["0"]


def test_neue_version_ueberschreibt_den_alten_text_erst_mit_dem_neuen(version_2: None) -> None:
    body = _quelle()
    alt = _datei(body, PDF, text_extraction_status="completed", text_content="Alter Text", text_extraction_version="1")
    ohne_version = _datei(body, PDF + b"2", text_extraction_status="completed", text_content="Altbestand")
    assert erkennung.counts()["text_outdated"] == 2

    beanspruchung = erkennung.beanspruchen(alt.pk)
    assert beanspruchung is not None and beanspruchung.vorher == "completed"
    alt.refresh_from_db()
    assert alt.text_content == "Alter Text", "der alte Text bleibt bis zum Überschreiben"
    erkennung.freigeben(beanspruchung)

    with _erkannt("Neuer Text"):
        assert erkennung.erkennen(str(alt.pk)) == erkennung.ERLEDIGT
    alt.refresh_from_db()
    assert (alt.text_content, alt.text_extraction_status, alt.text_extraction_version) == (
        "Neuer Text",
        "completed",
        "2",
    )

    # Liefert die neue Version keinen Text oder scheitert sie, bleibt der alte erledigt
    with mock.patch.object(erkennung, "extract_text", return_value=ExtractionResult("", "none", 1)):
        assert erkennung.erkennen(str(ohne_version.pk)) == erkennung.OHNE_TEXT
    ohne_version.refresh_from_db()
    assert (ohne_version.text_content, ohne_version.text_extraction_status) == ("Altbestand", "completed")
    assert ohne_version.text_extraction_version == "2"

    gescheitert = _datei(body, PDF + b"3", text_extraction_status="completed", text_content="Bleibt")
    with mock.patch.object(erkennung, "extract_text", side_effect=ValueError("kaputt")):
        assert erkennung.erkennen(str(gescheitert.pk)) == erkennung.GESCHEITERT
    gescheitert.refresh_from_db()
    assert (gescheitert.text_content, gescheitert.text_extraction_status) == ("Bleibt", "completed")
    assert gescheitert.text_extraction_version == "2", "nicht immer wieder"
    assert erkennung.counts()["text_outdated"] == 0


# =============================================================================
# extract_texts plant nur ein (ADR Abschnitt 1)
# =============================================================================


def _aufgaben() -> set[str]:
    return {t.args["args"][0] for t in Task.objects.filter(status=TaskStatus.WARTEND)}


def test_extract_texts_braucht_den_worker_und_zaehlt_im_probelauf() -> None:
    datei = _datei(_quelle(), PDF)
    with pytest.raises(CommandError, match="TEXT_EXTRACTION_RUNNER=worker"):
        call_command("extract_texts", stdout=StringIO())
    ausgabe = StringIO()
    call_command("extract_texts", "--dry-run", stdout=ausgabe)
    assert "1 Aufträge würden eingereiht" in ausgabe.getvalue()
    assert not Task.objects.exists()
    datei.refresh_from_db()
    assert datei.text_extraction_status == "pending"


@WORKER
def test_extract_texts_reiht_ein_ohne_zu_laden_oder_zu_erkennen(ohne_netz: list[str]) -> None:
    body = _quelle()
    wartend = _datei(body, PDF)
    ohne_inhalt = _datei(body)
    erledigt = _datei(body, PDF + b"e", text_extraction_status="completed", text_content="Text")
    gescheitert = _datei(body, PDF + b"g", text_extraction_status="failed", text_extraction_error="Kein PDF")
    fremd = _datei(_quelle(), PDF + b"f")

    with mock.patch.object(erkennung, "extract_text", side_effect=AssertionError("keine Erkennung im Befehl")):
        ausgabe = StringIO()
        call_command("extract_texts", "--body", str(body.pk), stdout=ausgabe)
        assert _aufgaben() == {str(wartend.pk)}
        assert "Ohne abgelegten Inhalt (holt zuerst der Abruf): 1" in ausgabe.getvalue()
        # Erneut: Für die wartende Datei wartet schon ein Auftrag
        call_command("extract_texts", "--body", str(body.pk), stdout=StringIO())
        assert Task.objects.count() == 1

        call_command("extract_texts", "--body", str(body.pk), "--reprocess", stdout=StringIO())
    assert _aufgaben() == {str(wartend.pk), str(erledigt.pk), str(gescheitert.pk)}
    for datei in (wartend, ohne_inhalt, erledigt, gescheitert, fremd):
        datei.refresh_from_db()
    assert (erledigt.text_extraction_status, erledigt.text_content) == ("completed", "Text"), "Text bleibt sichtbar"
    assert erledigt.text_extraction_version == erkennung.VERSION_REQUESTED
    assert gescheitert.text_extraction_status == "pending"
    assert {d.text_extraction_status for d in (wartend, ohne_inhalt, fremd)} == {"pending"}
    assert ohne_netz == []


@WORKER
def test_extract_texts_reiht_ohne_limit_hoechstens_die_freien_plaetze_ein(settings: Any) -> None:
    """Kein Fluten des Journals (ADR Abschnitte 2, 4 und 8): den Rest plant der Zeitplan schrittweise ein."""
    settings.TEXT_EXTRACTION_QUEUE_DEPTH = 3
    body = _quelle()
    for nummer in range(4):
        _datei(body, PDF + b"w%d" % nummer)
    for nummer in range(3):
        _datei(body, PDF + b"e%d" % nummer, text_extraction_status="completed", text_content="Text")

    ausgabe = StringIO()
    call_command("extract_texts", stdout=ausgabe)
    assert Task.objects.count() == 3, "höchstens TEXT_EXTRACTION_QUEUE_DEPTH"
    assert "Übrige 1 Dateien plant der Zeitplan texterkennung_einplanen schrittweise ein" in ausgabe.getvalue()

    # Warteschlange voll: --reprocess markiert nur, eingereiht wird nichts
    ausgabe = StringIO()
    call_command("extract_texts", "--reprocess", stdout=ausgabe)
    assert Task.objects.count() == 3
    assert OParlFile.objects.filter(text_extraction_version=erkennung.VERSION_REQUESTED).count() == 3
    assert "Übrige 4 Dateien" in ausgabe.getvalue()

    # Ein Auftrag erledigt: ein Platz frei
    Task.objects.filter(pk=Task.objects.order_by("pk").values_list("pk", flat=True)[0]).update(
        status=TaskStatus.ERLEDIGT
    )
    call_command("extract_texts", stdout=StringIO())
    assert Task.objects.filter(status=TaskStatus.WARTEND).count() == 3

    # Ein ausdrückliches --limit gilt auch über die freien Plätze hinaus, mit Hinweis
    ausgabe = StringIO()
    call_command("extract_texts", "--limit", "2", stdout=ausgabe)
    assert Task.objects.filter(status=TaskStatus.WARTEND).count() == 5
    assert "--limit über den freien Plätzen in ocr (0)" in ausgabe.getvalue()

    # Der Probelauf zählt dieselbe Grenze
    ausgabe = StringIO()
    call_command("extract_texts", "--dry-run", stdout=ausgabe)
    assert "Probelauf: 0 Aufträge würden eingereiht" in ausgabe.getvalue()


# =============================================================================
# dokumentkette nacharbeiten (ADR Abschnitt 11)
# =============================================================================


def _download_fehler(body: OParlBody, inhalt: bytes | None = None, **felder: Any) -> OParlFile:
    return _datei(
        body,
        inhalt,
        text_extraction_status="failed",
        text_extraction_error=felder.pop("text_extraction_error", "Download failed: HTTP 503"),
        **felder,
    )


def _nacharbeit_bestand() -> dict[str, OParlFile]:
    body = _quelle()
    stichtag = timezone.now() - timedelta(days=10)
    altbestand_quelle = _quelle(gelistet=False, sync_config={"document_since": stichtag.isoformat()})
    alt = _download_fehler(altbestand_quelle)
    OParlFile.objects.filter(pk=alt.pk).update(created_at=stichtag - timedelta(days=30))
    return {
        "mit_inhalt": _download_fehler(body, PDF),
        "ohne_inhalt": _download_fehler(body, text_extraction_error="Download fehlgeschlagen"),
        "fehler": _download_fehler(body, local_status="error"),
        "verweigert": _download_fehler(body, local_status="refused"),
        "altbestand": alt,
        "unlesbar": _download_fehler(body, PDF + b"u", text_extraction_error="Extraction failed (ValueError)"),
        "geloescht": _download_fehler(body, deleted=True),
    }


def test_nacharbeiten_zeigt_standardmaessig_nur_den_probelauf() -> None:
    bestand = _nacharbeit_bestand()
    vorher = {k: (d.local_status, d.text_extraction_status) for k, d in bestand.items()}
    ausgabe = StringIO()
    call_command("dokumentkette", "nacharbeiten", stdout=ausgabe)
    text = ausgabe.getvalue()
    assert "Probelauf, nichts geändert" in text
    assert "Erkennung 1, Abruf und Erkennung 2" in text
    assert "unverändert (nicht abzulegen) 1" in text
    assert "TEXT_EXTRACTION_RUNNER ist nicht worker" in text
    for name, datei in bestand.items():
        datei.refresh_from_db()
        assert (datei.local_status, datei.text_extraction_status) == vorher[name]
    with pytest.raises(CommandError, match="TEXT_EXTRACTION_RUNNER=worker"):
        call_command("dokumentkette", "nacharbeiten", "--ausfuehren", stdout=StringIO())


@WORKER
def test_nacharbeiten_setzt_zurueck_und_ist_wiederholbar() -> None:
    bestand = _nacharbeit_bestand()
    stand = erkennung.nacharbeiten(ausfuehren=False)
    assert stand.gesamt() == {
        erkennung.NACH_ERKENNUNG: 1,
        erkennung.NACH_ABRUF: 2,
        erkennung.NACH_WARTET: 1,
        erkennung.NACH_UNVERAENDERT: 1,
    }
    ausgabe = StringIO()
    call_command("dokumentkette", "nacharbeiten", "--ausfuehren", stdout=ausgabe)
    assert "Nachgearbeitet" in ausgabe.getvalue()

    zustand = {}
    for name, datei in bestand.items():
        datei.refresh_from_db()
        zustand[name] = (datei.local_status, datei.text_extraction_status)
    assert zustand == {
        "mit_inhalt": ("ok", "pending"),
        "ohne_inhalt": ("none", "pending"),
        "fehler": ("none", "pending"),
        "verweigert": ("refused", "pending"),
        "altbestand": ("none", "failed"),
        "unlesbar": ("ok", "failed"),
        "geloescht": ("none", "failed"),
    }
    assert bestand["mit_inhalt"].text_extraction_attempts == 0
    assert bestand["fehler"].fetch_attempts == 0
    assert sum(erkennung.nacharbeiten(ausfuehren=True).gesamt().values()) == 1, "nur der Altbestand bleibt"


# =============================================================================
# Kennzahlen und Prüfung dokumenttext (ADR Abschnitt 10)
# =============================================================================


def test_kennzahlen_und_pruefung(settings: Any, version_2: None) -> None:
    body = _quelle()
    alt = _datei(body, PDF)
    OParlFile.objects.filter(pk=alt.pk).update(local_cached_at=timezone.now() - timedelta(hours=30))
    _datei(body, PDF + b"n")
    _datei(body, PDF + b"p", text_extraction_status="processing")
    _datei(body, PDF + b"v", text_extraction_status="completed", text_extraction_version="1")
    _datei(body, PDF + b"a", text_extraction_status="completed", text_extraction_version="2")
    _datei(body)  # ohne Inhalt: wartet auf den Abruf, nicht auf die Erkennung
    _datei(body, PDF + b"g", deleted=True)

    assert erkennung.counts() == {"stored_without_text": 3, "text_outdated": 1}
    # Der Sammler hält die Zahlen eine Minute im gemeinsamen Cache; ein früherer Test im selben Prozess kann ihn
    # gefüllt haben (Reihenfolge der Testteile in der CI)
    cache.delete(erkennung_kennzahlen.CACHE_KEY)
    familien = {f.name: f for f in erkennung_kennzahlen.ErkennungCollector().collect()}
    assert set(familien) == {"mandari_files_stored_without_text", "mandari_files_text_outdated"}
    assert familien["mandari_files_stored_without_text"].samples[0].value == 3

    # Mit dem Ingestor ruht die Prüfung (sonst schlüge ein Altbestand ohne Text schon mit dem Deploy an)
    pruefung = erkennung_kennzahlen.check_text()
    assert pruefung.ok
    assert "TEXT_EXTRACTION_RUNNER ist nicht worker" in pruefung.detail
    settings.TEXT_EXTRACTION_RUNNER = "worker"
    pruefung = erkennung_kennzahlen.check_text()
    assert not pruefung.ok
    assert pruefung.detail == "1 abgelegte Dokumente warten länger als 24 h auf ihren Text"
    settings.TEXT_EXTRACTION_BACKLOG_ALERT_HOURS = 48
    assert erkennung_kennzahlen.check_text().ok


# =============================================================================
# Start nur mit Journal (ADR Abschnitt 11)
# =============================================================================

SETTINGS_PY = Path(django_settings.BASE_DIR) / "mandari" / "settings.py"


def test_worker_verweigert_den_start_ohne_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEXT_EXTRACTION_RUNNER", "worker")
    monkeypatch.delenv("TASKS_BACKEND", raising=False)
    with pytest.raises(ImproperlyConfigured, match="TASKS_BACKEND=journal"):
        runpy.run_path(str(SETTINGS_PY))
    monkeypatch.setenv("TASKS_BACKEND", "journal")
    assert runpy.run_path(str(SETTINGS_PY))["TEXT_EXTRACTION_RUNNER"] == "worker"
    monkeypatch.setenv("TEXT_EXTRACTION_RUNNER", "ingestor")
    monkeypatch.delenv("TASKS_BACKEND")
    assert runpy.run_path(str(SETTINGS_PY))["TEXT_EXTRACTION_RUNNER"] == "ingestor"


# =============================================================================
# Vertrag ris.file.text_extracted v1 (ADR Abschnitt 6)
# =============================================================================


def test_vertrag_text_extracted_um_sha256_ergaenzt() -> None:
    register = get_registry()
    datei = str(uuid.uuid4())
    sha = _sha(PDF)
    assert register.payload_problems("ris.file.text_extracted", 1, {"file": datei, "method": "pypdf"}) == []
    mit = {"file": datei, "method": "pypdf", "characters": 3, "sha256": sha}
    assert register.payload_problems("ris.file.text_extracted", 1, mit) == []
    assert register.payload_problems("ris.file.text_extracted", 1, {**mit, "sha256": "ABC"}) != []
    # Additiv: ohne das neue Feld ist es derselbe Vertrag wie vorher
    neu = register.schema("ris.file.text_extracted", 1)
    vorher = copy.deepcopy(neu)
    del vorher["properties"]["sha256"]
    assert breaking_changes(vorher, neu) == []
    assert "sha256" not in neu["required"]
    # Die Nutzlast des Auftrags nennt den Hash, ohne ihn ist sie die des Ingestors
    assert text_extraction.payload(uuid.UUID(datei), "pypdf", 3, sha256=sha) == mit
    assert text_extraction.payload(uuid.UUID(datei), "pypdf", 3, sha256="kein-hash") == {
        "file": datei,
        "method": "pypdf",
        "characters": 3,
    }


def test_erkennung_kennt_keinen_weg_zur_quelle() -> None:
    """Statisch zur Fitnessfunktion: Das Modul der Erkennung nutzt keinen Abruf- oder HTTP-Weg (ADR Abschnitt 1)."""
    import ast

    quelltext = Path(erkennung.__file__).read_text(encoding="utf-8")
    baum = ast.parse(quelltext)
    importiert = {
        name.name
        for knoten in ast.walk(baum)
        if isinstance(knoten, ast.Import | ast.ImportFrom)
        for name in knoten.names
    }
    module = {knoten.module for knoten in ast.walk(baum) if isinstance(knoten, ast.ImportFrom) and knoten.module}
    assert not importiert & {"httpx", "requests", "urllib", "safe_fetch", "download_and_extract", "download_live"}
    assert not {m for m in module if m.endswith(("safe_fetch", "host_pacing", "robots"))}
    genutzt = {
        knoten.attr
        for knoten in ast.walk(baum)
        if isinstance(knoten, ast.Attribute) and isinstance(knoten.value, ast.Name) and knoten.value.id == "abruf"
    }
    assert not genutzt & {"abrufen", "stream", "head", "download_live", "fetch_client", "nachladen"}
