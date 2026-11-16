# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sperre nach Fehlversuchen: Dritte sperren die Person nicht aus.

Die Zählung je Konto greift weiter für jeden Browser, der das Konto noch nicht nachgewiesen hat. Ein Browser mit
früherer Anmeldung oder eingelöstem Link „Passwort zurücksetzen“ zählt für sich; die Schwelle je Adresse gilt für
alle. Greift die Sperre je Konto, erfährt die Person davon.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts import known_device, security_notifications
from apps.accounts.models import SecurityNotification, User
from apps.accounts.services import TwoFactorService
from apps.accounts.views import LOGIN_FAILURES_PER_ACCOUNT, LOGIN_FAILURES_PER_IP
from apps.common.models import MailOutbox

pytestmark = pytest.mark.django_db

PASSWORT = "Sicheres-Passwort-2026!"
NEUES_PASSWORT = "Noch-sichereres-Passwort-2027!"
ADRESSE = "person@example.org"
EIGENE_IP = "198.51.100.7"


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def konto() -> User:
    return cast(User, User.objects.create_user(email=ADRESSE, password=PASSWORT))  # type: ignore[no-untyped-call]


def _anmelden(client: Client, passwort: str = PASSWORT, ip: str = EIGENE_IP, email: str = ADRESSE) -> Any:
    return client.post(reverse("accounts:login"), {"email": email, "password": passwort}, REMOTE_ADDR=ip)


def _angemeldet(client: Client) -> bool:
    return "_auth_user_id" in client.session


def _abmelden(client: Client) -> None:
    client.post(reverse("accounts:logout"))
    assert not _angemeldet(client)


def _fremde_fehlversuche(anzahl: int = 30) -> None:
    """Fehlversuche Dritter aus vielen Adressen: mehr, als die Sperre je Konto zulässt."""
    for nummer in range(anzahl):
        _anmelden(Client(), "falsches-passwort", ip=f"203.0.113.{nummer + 1}")


def test_bekannter_browser_kommt_trotz_fremder_fehlversuche_hinein(konto: User) -> None:
    eigener = Client()
    _anmelden(eigener)
    assert _angemeldet(eigener)
    assert known_device.COOKIE_NAME in eigener.cookies
    _abmelden(eigener)

    _fremde_fehlversuche()

    _anmelden(eigener)
    assert _angemeldet(eigener)


def test_unbekannter_browser_bleibt_gesperrt(konto: User) -> None:
    _fremde_fehlversuche()

    fremder = Client()
    _anmelden(fremder, ip="198.51.100.200")

    assert not _angemeldet(fremder)


def test_cookie_gilt_nur_fuer_das_eigene_konto(konto: User) -> None:
    User.objects.create_user(email="andere@example.org", password=PASSWORT)  # type: ignore[no-untyped-call]
    browser = Client()
    _anmelden(browser, email="andere@example.org")
    _abmelden(browser)

    _fremde_fehlversuche()

    _anmelden(browser)
    assert not _angemeldet(browser), "das Gerät hat nur das andere Konto nachgewiesen"


def test_veraendertes_cookie_zaehlt_nicht(konto: User) -> None:
    browser = Client()
    _anmelden(browser)
    _abmelden(browser)
    wert = browser.cookies[known_device.COOKIE_NAME].value
    browser.cookies[known_device.COOKIE_NAME] = wert[:-2] + ("aa" if not wert.endswith("aa") else "bb")

    _fremde_fehlversuche()

    _anmelden(browser)
    assert not _angemeldet(browser)


def test_bekanntes_geraet_hat_eine_eigene_grenze(konto: User) -> None:
    """Auch mit Cookie sind es je Zeitfenster nicht mehr Versuche, als die Sperre je Konto zulässt."""
    browser = Client()
    _anmelden(browser)
    _abmelden(browser)

    for nummer in range(LOGIN_FAILURES_PER_ACCOUNT):
        # Wechselnde Adressen, damit nicht die Schwelle je Adresse greift
        _anmelden(browser, "falsches-passwort", ip=f"192.0.2.{nummer + 1}")

    _anmelden(browser, ip="192.0.2.200")
    assert not _angemeldet(browser)


def test_mehrere_bekannte_geraete_teilen_sich_die_grenze(konto: User) -> None:
    """
    Jede Anmeldung ohne Cookie ergibt ein neues Cookie. Zusammen haben sie je Zeitfenster so viele Versuche wie eines
    allein: Wer Cookies sammelt, vervielfacht die Versuche nicht.
    """
    erster, zweiter = Client(), Client()
    for browser in (erster, zweiter):
        _anmelden(browser)
        _abmelden(browser)
    assert erster.cookies[known_device.COOKIE_NAME].value != zweiter.cookies[known_device.COOKIE_NAME].value

    haelfte = LOGIN_FAILURES_PER_ACCOUNT // 2
    for nummer in range(haelfte):
        # Wechselnde Adressen, damit nicht die Schwelle je Adresse greift
        _anmelden(erster, "falsches-passwort", ip=f"192.0.2.{nummer + 1}")
    for nummer in range(LOGIN_FAILURES_PER_ACCOUNT - haelfte):
        _anmelden(zweiter, "falsches-passwort", ip=f"192.0.2.{nummer + 101}")

    _anmelden(erster, ip="192.0.2.200")
    assert not _angemeldet(erster), "die Fehlversuche beider Geräte zählen zusammen"
    _anmelden(zweiter, ip="192.0.2.201")
    assert not _angemeldet(zweiter)


def test_bekannte_geraete_eines_anderen_kontos_zaehlen_nicht_mit(konto: User) -> None:
    User.objects.create_user(email="andere@example.org", password=PASSWORT)  # type: ignore[no-untyped-call]
    fremder, eigener = Client(), Client()
    _anmelden(fremder, email="andere@example.org")
    _abmelden(fremder)
    _anmelden(eigener)
    _abmelden(eigener)

    for nummer in range(LOGIN_FAILURES_PER_ACCOUNT):
        _anmelden(fremder, "falsches-passwort", ip=f"192.0.2.{nummer + 1}", email="andere@example.org")

    _anmelden(eigener, ip="192.0.2.200")
    assert _angemeldet(eigener)


def test_schwelle_je_adresse_gilt_auch_fuer_bekannte_browser(konto: User) -> None:
    browser = Client()
    _anmelden(browser)
    _abmelden(browser)

    for _ in range(LOGIN_FAILURES_PER_IP):
        _anmelden(browser, "falsches-passwort")

    _anmelden(browser)
    assert not _angemeldet(browser)


def test_eingeloester_ruecksetzlink_fuehrt_trotz_sperre_hinein(konto: User) -> None:
    _fremde_fehlversuche()
    browser = Client()
    konto.refresh_from_db()
    link = reverse(
        "accounts:password_reset_confirm",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(konto.pk)),
            "token": default_token_generator.make_token(konto),
        },
    )
    formular = browser.get(link)
    antwort = browser.post(formular["Location"], {"new_password1": NEUES_PASSWORT, "new_password2": NEUES_PASSWORT})
    assert antwort.status_code == 302

    _anmelden(browser, NEUES_PASSWORT)

    assert _angemeldet(browser)


def test_anmeldung_mit_zweitem_faktor_merkt_das_geraet_erst_nach_dem_code(konto: User) -> None:
    dienst = TwoFactorService()
    secret = str(dienst.setup_2fa(konto)["secret"])
    code: Callable[[], str] = lambda: dienst._get_totp_code(secret, int(time.time() // dienst.TOTP_INTERVAL))  # noqa: E731
    assert dienst.confirm_2fa(konto, code())
    browser = Client()

    _anmelden(browser)
    assert known_device.COOKIE_NAME not in browser.cookies, "das Passwort allein weist das Konto nicht nach"

    antwort = browser.post(reverse("accounts:login_2fa"), {"code": code()})
    assert antwort.status_code == 302 and _angemeldet(browser)
    assert known_device.COOKIE_NAME in browser.cookies


def test_cookie_ist_fuer_skripte_unsichtbar_und_auf_die_anmeldung_begrenzt(konto: User) -> None:
    browser = Client()
    _anmelden(browser)

    cookie = browser.cookies[known_device.COOKIE_NAME]
    assert cookie["httponly"]
    assert cookie["path"] == "/accounts/"
    assert cookie["samesite"] == "Lax"
    assert ADRESSE not in cookie.value


def test_person_erfaehrt_von_der_sperre_einmal_am_tag(
    konto: User, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    _fremde_fehlversuche()

    with django_capture_on_commit_callbacks(execute=True):
        for nummer in range(3):
            _anmelden(Client(), ip=f"198.51.100.{nummer + 20}")

    [hinweis] = SecurityNotification.objects.filter(user=konto)
    assert hinweis.notification_type == security_notifications.LOGIN_BLOCKED
    assert "Passwort vergessen" in hinweis.message
    # Ohne Postausgang keine Mail: Der Versand in der Anfrage liefe nur für Adressen mit Konto
    assert mail.outbox == []


def test_gescheiterter_hinweis_gibt_den_tag_wieder_frei(konto: User, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lässt sich der Hinweis einmal nicht anlegen, kommt er beim nächsten Anlass und nicht erst am Folgetag."""
    echtes_anlegen = SecurityNotification.objects.create
    aufrufe = []

    def anlegen(**felder: Any) -> SecurityNotification:
        aufrufe.append(felder)
        if len(aufrufe) == 1:
            raise RuntimeError("Datenbank nicht erreichbar")
        return echtes_anlegen(**felder)

    monkeypatch.setattr(SecurityNotification.objects, "create", anlegen)

    security_notifications.notify_login_blocked(ADRESSE)
    assert not SecurityNotification.objects.filter(user=konto).exists()

    security_notifications.notify_login_blocked(ADRESSE)
    security_notifications.notify_login_blocked(ADRESSE)
    assert SecurityNotification.objects.filter(user=konto).count() == 1, "danach wieder höchstens einmal am Tag"


def test_hinweis_geht_per_mail_wenn_sie_als_auftrag_rausgeht(
    konto: User, settings: Any, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    """Mit dem Backend ``journal`` immer, auch ohne Eintrag in ``MAIL_QUEUE``: Sonst erführe die Person nichts."""
    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": "apps.events.tasks_backend.JournalBackend"}}
    settings.MAIL_QUEUE = []

    with django_capture_on_commit_callbacks(execute=True):
        _fremde_fehlversuche(LOGIN_FAILURES_PER_ACCOUNT + 1)

    assert SecurityNotification.objects.filter(user=konto).count() == 1
    assert MailOutbox.objects.filter(kind="konto.sicherheit").count() == 1
    assert mail.outbox == [], "der Versand läuft im Worker, nicht in der Anfrage"


def test_sperre_fuer_unbekannte_adresse_legt_nichts_an() -> None:
    for nummer in range(30):
        _anmelden(Client(), "falsches-passwort", ip=f"203.0.113.{nummer + 1}", email="niemand@example.org")

    antwort = _anmelden(Client(), ip="198.51.100.200", email="niemand@example.org")

    assert antwort.status_code == 200
    assert not SecurityNotification.objects.exists()
