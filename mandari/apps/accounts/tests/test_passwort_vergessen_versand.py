# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Passwort vergessen“: Die Antwort hängt nicht am Versand.

- Scheitert der Versand, bekommt die Person dieselbe Antwort wie sonst; der Fehler steht im Protokoll.
- Mit dem Backend ``journal`` geht die Mail als Auftrag raus, auch ohne ``MAIL_QUEUE``.
- Ohne ``journal`` bleibt es beim Versand im Aufruf.
"""

from __future__ import annotations

import smtplib
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from django.core import mail as django_mail
from django.core.cache import cache
from django.tasks import task_backends
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.common import mail
from apps.common.models import MailOutbox, SiteSettings
from apps.events.task_runner import run_pending
from apps.events.tasks_backend import JournalBackend

pytestmark = pytest.mark.django_db

PASSWORT = "Sicheres-Passwort-2026!"
ADRESSE = "person@example.org"


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def konto() -> Any:
    return User.objects.create_user(email=ADRESSE, password=PASSWORT)  # type: ignore[no-untyped-call]


@pytest.fixture
def journal(settings: Any) -> Iterator[JournalBackend]:
    """Aufträge laufen im Worker; ``MAIL_QUEUE`` bleibt leer wie in der Vorgabe."""
    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": "apps.events.tasks_backend.JournalBackend"}}
    settings.MAIL_QUEUE = []
    cache.delete(SiteSettings.CACHE_KEY)
    backend = task_backends["default"]
    assert isinstance(backend, JournalBackend)
    yield backend
    cache.delete(SiteSettings.CACHE_KEY)


def _anfordern(adresse: str) -> Any:
    return Client().post(reverse("accounts:password_reset"), {"email": adresse})


def test_gescheiterter_versand_aendert_die_antwort_nicht(konto: Any) -> None:
    with mock.patch("apps.common.mail.delivery.deliver", side_effect=smtplib.SMTPServerDisconnected("weg")) as versand:
        mit_konto = _anfordern(ADRESSE)
        ohne_konto = _anfordern("niemand@example.org")

    assert versand.call_count == 1, "nur für die Adresse mit Konto wird versendet"
    assert mit_konto.status_code == ohne_konto.status_code == 302
    assert mit_konto["Location"] == ohne_konto["Location"] == reverse("accounts:password_reset_done")


def test_mit_journal_geht_die_mail_als_auftrag_raus(konto: Any, journal: JournalBackend) -> None:
    assert mail.queue_enabled("konto.passwort")
    assert not mail.queue_enabled("konto.sicherheit"), "andere Mailarten folgen weiter MAIL_QUEUE"

    with mock.patch("apps.common.mail.delivery.deliver") as versand:
        antwort = _anfordern(ADRESSE)

    assert antwort.status_code == 302
    versand.assert_not_called()
    assert MailOutbox.objects.filter(kind="konto.passwort", status=MailOutbox.Status.WARTEND).count() == 1

    run_pending()

    [nachricht] = django_mail.outbox
    assert nachricht.to == [ADRESSE]
    assert "/accounts/" in nachricht.body


def test_ohne_journal_geht_die_mail_weiter_sofort_raus(konto: Any) -> None:
    assert not mail.queue_enabled("konto.passwort")

    antwort = _anfordern(ADRESSE)

    assert antwort.status_code == 302
    [nachricht] = django_mail.outbox
    assert nachricht.to == [ADRESSE]
    assert not MailOutbox.objects.exists()
