# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Metriken-Endpunkt (Issue #231, Teil 2): Histogramm je View-Name nach einer Anfrage, Zugriff
nur aus erlaubten Netzen oder mit Token (sonst 404), Kardinalität über den View-Namen statt
über den Pfad, Zähler für Mailversand und PDF-Erzeugung.
"""

from __future__ import annotations

import pytest
from django.core import mail
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory
from django.urls import ResolverMatch
from prometheus_client import REGISTRY

from apps.common import metrics

AUSSEN = "203.0.113.5"  # Dokumentationsnetz, in keinem privaten Bereich


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _summe(name: str, **labels: str) -> float:
    """Summe aller Samples von ``name``, deren Labels ``labels`` enthalten (z. B. über alle Statusklassen)."""
    return sum(
        sample.value
        for familie in REGISTRY.collect()
        for sample in familie.samples
        if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items())
    )


def test_endpunkt_liefert_histogramm_nach_einer_anfrage(client: Client) -> None:
    client.get("/health/live/")

    response = client.get("/metrics/")  # Test-Client kommt von 127.0.0.1

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    text = response.content.decode()
    assert 'mandari_http_request_duration_seconds_bucket{le="+Inf",status_class="2xx",view="health_live"}' in text
    assert 'mandari_http_requests_total{status_class="2xx",view="health_live"}' in text
    assert "/health/live/" not in text  # Pfad ist kein Label


def test_metriken_abruf_zaehlt_sich_nicht_selbst(client: Client) -> None:
    client.get("/metrics/")
    client.get("/metrics/")

    assert _sample("mandari_http_requests_total", status_class="2xx", view="metrics") == 0.0


def test_von_aussen_404(client: Client) -> None:
    response = client.get("/metrics/", REMOTE_ADDR=AUSSEN)

    assert response.status_code == 404


def test_weitergeleitete_adresse_zaehlt(client: Client) -> None:
    """Hinter Caddy steht die echte Adresse in X-Forwarded-For; 127.0.0.1 ist dann nur der Proxy."""
    response = client.get("/metrics/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR=AUSSEN)

    assert response.status_code == 404


def test_token_erlaubt_zugriff_von_aussen(client: Client, settings: object) -> None:
    settings.METRICS_TOKEN = "sehr-geheim"  # type: ignore[attr-defined]

    ok = client.get("/metrics/", REMOTE_ADDR=AUSSEN, HTTP_AUTHORIZATION="Bearer sehr-geheim")
    falsch = client.get("/metrics/", REMOTE_ADDR=AUSSEN, HTTP_AUTHORIZATION="Bearer raten")

    assert ok.status_code == 200
    assert falsch.status_code == 404


def test_erlaubtes_netz_konfigurierbar(client: Client, settings: object) -> None:
    settings.METRICS_ALLOWED_NETWORKS = ["203.0.113.0/24"]  # type: ignore[attr-defined]
    metrics._parse_networks.cache_clear()
    try:
        assert client.get("/metrics/", REMOTE_ADDR=AUSSEN).status_code == 200
        assert client.get("/metrics/", REMOTE_ADDR="127.0.0.1").status_code == 404
    finally:
        metrics._parse_networks.cache_clear()


@pytest.mark.django_db
def test_kardinalitaet_view_name_statt_pfad(client: Client) -> None:
    """Zwei Pfade mit verschiedenen Parametern landen im selben Label (problem_report_done)."""
    vorher = _summe("mandari_http_requests_total", view="problem_report_done")

    client.get("/feedback/abc-111/danke/")
    client.get("/feedback/xyz-222/danke/")

    assert _summe("mandari_http_requests_total", view="problem_report_done") == vorher + 2
    text = client.get("/metrics/").content.decode()
    assert "abc-111" not in text
    assert "xyz-222" not in text


def test_serverfehler_werden_gezaehlt(rf: RequestFactory) -> None:
    def kaputt(request: HttpRequest) -> HttpResponse:
        request.resolver_match = ResolverMatch(kaputt, (), {}, url_name="kaputte_view")
        return HttpResponse(status=500)

    vorher = _sample("mandari_http_request_errors_total", view="kaputte_view")
    metrics.RequestMetricsMiddleware(kaputt)(rf.get("/egal/"))

    assert _sample("mandari_http_request_errors_total", view="kaputte_view") == vorher + 1
    assert _sample("mandari_http_requests_total", status_class="5xx", view="kaputte_view") >= 1


def test_ausnahme_zaehlt_als_5xx(rf: RequestFactory) -> None:
    def explodiert(request: HttpRequest) -> HttpResponse:
        raise RuntimeError("boom")

    vorher = _sample("mandari_http_requests_total", status_class="5xx", view="unresolved")
    with pytest.raises(RuntimeError):
        metrics.RequestMetricsMiddleware(explodiert)(rf.get("/egal/"))

    assert _sample("mandari_http_requests_total", status_class="5xx", view="unresolved") == vorher + 1


@pytest.mark.django_db
def test_mailversand_wird_gezaehlt() -> None:
    from apps.common.email import send_email

    vorher = _sample("mandari_emails_total", result="sent")
    send_email("Test", "Hallo", ["empfang@example.org"])

    assert len(mail.outbox) == 1
    assert _sample("mandari_emails_total", result="sent") == vorher + 1


@pytest.mark.django_db
def test_fehlgeschlagener_mailversand_wird_gezaehlt(monkeypatch: pytest.MonkeyPatch) -> None:
    from django.core.mail import EmailMessage

    from apps.common.email import send_email

    def scheitert(self: EmailMessage, fail_silently: bool = False) -> int:
        raise ConnectionError("SMTP weg")

    monkeypatch.setattr(EmailMessage, "send", scheitert)
    vorher = _sample("mandari_emails_total", result="failed")

    assert send_email("Test", "Hallo", ["empfang@example.org"], fail_silently=True) is False
    assert _sample("mandari_emails_total", result="failed") == vorher + 1


def test_pdf_erzeugung_wird_gezaehlt() -> None:
    from apps.common.pdf import html_to_pdf

    vorher = _sample("mandari_pdf_documents_total", result="ok")
    dauer_vorher = _sample("mandari_pdf_generation_seconds_count")

    assert html_to_pdf("<html><body><p>Hallo</p></body></html>").startswith(b"%PDF")
    assert _sample("mandari_pdf_documents_total", result="ok") == vorher + 1
    assert _sample("mandari_pdf_generation_seconds_count") == dauer_vorher + 1


def test_sammler_schweigen_ohne_redis_und_pool(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ohne Pool und ohne Redis-Backend liefern die Sammler nichts, statt zu scheitern.

    Der Pool wird gezielt abgeschaltet: In der CI läuft der Test gegen PostgreSQL mit Pool,
    lokal gegen SQLite ohne – das Ergebnis darf davon nicht abhängen.
    """
    monkeypatch.setattr(metrics, "db_pool_stats", lambda: None)
    monkeypatch.setattr(metrics, "db_open_connections", lambda: None)
    monkeypatch.setattr(metrics, "cache_stats", lambda: None)

    text = client.get("/metrics/").content.decode()

    assert "mandari_cache_hit_ratio" not in text
    assert "mandari_db_pool_connections" not in text
    assert "mandari_db_connections_open" not in text


def test_pool_statistik_erscheint_als_metrik(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        metrics,
        "db_pool_stats",
        lambda: {"pool_min": 2, "pool_max": 10, "pool_size": 4, "pool_available": 1, "requests_waiting": 3},
    )

    text = client.get("/metrics/").content.decode()

    assert 'mandari_db_pool_connections{state="in_use"} 3.0' in text
    assert 'mandari_db_pool_connections{state="available"} 1.0' in text
    assert 'mandari_db_pool_connections{state="max"} 10.0' in text
    assert "mandari_db_pool_requests_waiting 3.0" in text


def test_cache_statistik_erscheint_als_metrik(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "cache_stats", lambda: {"hits": 75, "misses": 25})

    text = client.get("/metrics/").content.decode()

    assert "mandari_cache_keyspace_hits_total 75.0" in text
    assert "mandari_cache_hit_ratio 0.75" in text
