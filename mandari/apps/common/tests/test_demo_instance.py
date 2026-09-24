# SPDX-License-Identifier: AGPL-3.0-or-later
"""Öffentliche Demo-Instanz (Issue #99): Schalter ``DEMO_INSTANCE`` und was er sperrt."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from django.core import mail
from django.test import Client, RequestFactory, override_settings
from django.urls import resolve, reverse

from apps.common import demo
from apps.common.mail_backends import build_backend
from apps.work.motions.services import MotionAIService


def _post(pfad: str) -> Any:
    anfrage = RequestFactory().post(pfad)
    anfrage.resolver_match = resolve(pfad)
    setattr(anfrage, "_messages", mock.MagicMock())  # noqa: B010 – messages.warning ohne Sitzung
    return anfrage


def test_ohne_demo_sperrt_die_middleware_nichts() -> None:
    middleware = demo.DemoInstanceMiddleware(lambda r: None)  # type: ignore[arg-type,return-value]
    anfrage = _post(reverse("accounts:password_reset"))
    assert middleware.process_view(anfrage, None, (), {}) is None


@override_settings(DEMO_INSTANCE=True)
def test_demo_sperrt_konto_sicherheit_aber_nicht_das_uebrige() -> None:
    middleware = demo.DemoInstanceMiddleware(lambda r: None)  # type: ignore[arg-type,return-value]
    gesperrt = _post(reverse("accounts:password_reset"))
    antwort = middleware.process_view(gesperrt, None, (), {})
    assert antwort is not None and antwort.status_code == 302
    assert antwort["Location"] == reverse("accounts:password_reset")

    erlaubt = _post(reverse("accounts:login"))
    assert middleware.process_view(erlaubt, None, (), {}) is None

    lesen = RequestFactory().get(reverse("accounts:password_reset"))
    lesen.resolver_match = resolve(reverse("accounts:password_reset"))
    assert middleware.process_view(lesen, None, (), {}) is None


def test_gesperrte_seiten_gibt_es_wirklich() -> None:
    """Jeder Eintrag muss auf eine existierende URL zeigen – sonst sperrt die Liste ins Leere."""
    from django.urls import get_resolver

    namen: set[str] = set()

    def sammeln(muster: Any, praefix: str = "") -> None:
        for eintrag in muster:
            if hasattr(eintrag, "url_patterns"):
                ns = eintrag.namespace
                sammeln(eintrag.url_patterns, f"{praefix}{ns}:" if ns else praefix)
            elif getattr(eintrag, "name", None):
                namen.add(f"{praefix}{eintrag.name}")

    sammeln(get_resolver().url_patterns)
    assert namen >= demo.GESPERRT_IN_DER_DEMO, demo.GESPERRT_IN_DER_DEMO - namen


@override_settings(DEMO_INSTANCE=True)
def test_demo_versendet_keine_mail_auch_nicht_ueber_smtp() -> None:
    backend = build_backend("django.core.mail.backends.smtp.EmailBackend", host="smtp.example.org", port=587)
    assert backend.__class__.__module__ == "django.core.mail.backends.locmem"


def test_festes_passwort_nur_in_der_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_PASSWORD", "Musterstadt-oeffentlich")
    assert demo.demo_passwort() is None  # Produktion: nie ein festes Passwort
    with override_settings(DEMO_INSTANCE=True):
        assert demo.demo_passwort() == "Musterstadt-oeffentlich"
        monkeypatch.setenv("DEMO_PASSWORD", "  ")
        assert demo.demo_passwort() is None


@override_settings(DEMO_INSTANCE=True)
def test_demo_ruft_keine_ki_auf() -> None:
    with mock.patch("apps.work.motions.services.httpx.Client") as client:
        antwort = MotionAIService()._call_api([{"role": "user", "content": "Hallo"}])
    assert not antwort.success
    assert "Demo" in (antwort.error or "")
    client.assert_not_called()


@pytest.mark.django_db
def test_hinweis_nur_in_der_demo(client: Client) -> None:
    assert "Demo-Umgebung." not in client.get(reverse("accounts:login")).content.decode()
    with override_settings(DEMO_INSTANCE=True):
        assert "Demo-Umgebung." in client.get(reverse("accounts:login")).content.decode()


@pytest.mark.django_db
@override_settings(DEMO_INSTANCE=True)
def test_passwort_vergessen_ist_in_der_demo_gesperrt(client: Client) -> None:
    antwort = client.post(reverse("accounts:password_reset"), {"email": "demo-vorsitz@demo.mandari.de"})
    assert antwort.status_code == 302
    assert len(mail.outbox) == 0
