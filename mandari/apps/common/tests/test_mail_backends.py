# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mail-Backends unter MAILERS (Django ≥ 6.1, Issue #80).

Der Produktionsrückfall vom 18.09.2026: Djangos SMTP-Backend liest für jede Option, die None
ist, ``settings.EMAIL_*`` – neben ``MAILERS`` ein AttributeError. Diese Tests instanziieren
die echten SMTP-Backends (ohne Netz) mit der Test-Konfiguration, in der MAILERS gesetzt ist.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.core.cache import cache
from django.core.mail import EmailMessage
from django.core.mail.backends.smtp import EmailBackend as SMTPEmailBackend

from apps.common import email as email_modul
from apps.common.email_backend import ConsoleOrSiteSettingsBackend, SiteSettingsEmailBackend
from apps.common.mail_backends import SMTP_BACKEND, build_backend, send_with, smtp_options


@pytest.fixture(autouse=True)
def _cache_leeren() -> Any:
    """SiteSettings liegen im Cache – gespeicherte SMTP-Daten dürfen andere Tests nicht erreichen."""
    cache.clear()
    yield
    cache.clear()


def test_smtp_backend_wird_ohne_email_settings_gebaut(settings: Any) -> None:
    assert settings.MAILERS, "Testkonfiguration muss MAILERS setzen"
    backend = build_backend(SMTP_BACKEND, host="smtp.example.org", port=None, username=None, password=None)
    assert isinstance(backend, SMTPEmailBackend)
    assert (backend.host, backend.port, backend.username, backend.use_tls) == ("smtp.example.org", 587, "", True)
    assert backend.ssl_keyfile == "" and backend.ssl_certfile == ""


def test_smtp_host_ist_pflicht() -> None:
    with pytest.raises(ValueError):
        smtp_options(host="")


def test_locmem_bekommt_keine_smtp_optionen() -> None:
    backend = build_backend("django.core.mail.backends.locmem.EmailBackend", host="x", port=25)
    assert backend.__class__.__name__ == "EmailBackend" and not hasattr(backend, "host")


@pytest.mark.django_db
def test_sitesettings_backend_ohne_argumente_wie_ueber_mailers(settings: Any) -> None:
    """MAILERS instanziiert das Backend mit leeren OPTIONS – so wie Django es tut."""
    settings.SMTP_FALLBACK = {
        "host": "smtp.fallback.example",
        "port": 465,
        "username": "u",
        "password": "p",
        "use_tls": False,
        "use_ssl": True,
        "timeout": 9,
    }
    backend = cast(Any, SiteSettingsEmailBackend)()
    assert (backend.host, backend.port, backend.use_ssl, backend.timeout) == ("smtp.fallback.example", 465, True, 9)
    settings.DEBUG = False
    assert cast(Any, ConsoleOrSiteSettingsBackend)().host == "smtp.fallback.example"


@pytest.mark.django_db
def test_send_email_ueber_sitesettings_smtp(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.common.models import SiteSettings

    site = SiteSettings.get_settings()
    site.email_host = "smtp.site.example"
    site.email_port = 587
    site.email_backend = SMTP_BACKEND
    cast(Any, site).save()
    gesendet: list[EmailMessage] = []
    monkeypatch.setattr(SMTPEmailBackend, "open", lambda self: True)
    monkeypatch.setattr(SMTPEmailBackend, "close", lambda self: None)

    def merken(self: Any, msgs: list[EmailMessage]) -> int:
        gesendet.extend(msgs)
        return len(msgs)

    monkeypatch.setattr(SMTPEmailBackend, "send_messages", merken)

    assert email_modul.send_email("Betreff", "Text", ["ziel@example.org"]) is True
    assert len(gesendet) == 1 and gesendet[0].to == ["ziel@example.org"]


def test_send_with_liefert_anzahl() -> None:
    backend = build_backend("django.core.mail.backends.locmem.EmailBackend")
    assert send_with(backend, EmailMessage("a", "b", "von@example.org", ["zu@example.org"])) == 1
