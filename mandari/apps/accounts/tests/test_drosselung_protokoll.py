# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Von der Drosselung abgewiesene Anmeldeversuche im Sicherheitsprotokoll (Issue #366).

Geprüft wird: eigenes Ereignis ``login_throttled`` je Anmeldeschritt, höchstens ein Eintrag je Adresse
(IPv6: je /64-Netz) und Zeitfenster, ein neues Fenster schreibt wieder, keine Kennung und kein Passwort im
Klartext, und ein Fehler beim Protokollieren ändert nichts an der Abweisung.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import security_audit
from apps.accounts.models import LoginAttempt, SecurityAuditLog
from apps.accounts.tests.test_login_2fa import code_step, enable_2fa, make_user, password_step
from apps.accounts.views import LOGIN_FAILURES_PER_ACCOUNT, LOGIN_FAILURES_PER_IP, MAX_2FA_FAILURES
from apps.common import audit_chain

pytestmark = pytest.mark.django_db

FALSCH = "Falsches-Geheimwort-987!"
KENNUNG = "angegriffen@example.org"


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def mitte_des_fensters() -> Iterator[dt.datetime]:
    """Feste Zeit in der Mitte eines 15-Minuten-Fensters: kein Fensterwechsel mitten im Test."""
    jetzt = timezone.now()
    mitte = jetzt.replace(minute=jetzt.minute // 15 * 15 + 7, second=0, microsecond=0)
    with mock.patch("django.utils.timezone.now", return_value=mitte):
        yield mitte


def _gedrosselt() -> list[SecurityAuditLog]:
    return list(SecurityAuditLog.objects.filter(event=security_audit.EVENT_THROTTLED).order_by("seq"))


def _fehlversuche(client: Client, email: str, anzahl: int, adresse: str) -> None:
    for _ in range(anzahl):
        client.post(reverse("accounts:login"), {"email": email, "password": FALSCH}, REMOTE_ADDR=adresse)


def _meldungen(response: Any) -> str:
    return " ".join(str(m) for m in get_messages(response.wsgi_request))


def _alle_werte() -> str:
    return "\n".join(json.dumps(zeile, default=str, ensure_ascii=False) for zeile in SecurityAuditLog.objects.values())


class TestPasswortschritt:
    def test_abgewiesene_versuche_ein_eintrag_je_adresse_und_fenster(self) -> None:
        client = Client()
        _fehlversuche(client, KENNUNG, LOGIN_FAILURES_PER_IP, "192.0.2.10")
        assert _gedrosselt() == []  # Bis zur Schwelle nur Fehlversuche, keine Abweisung

        # Viele weitere Versuche in demselben Fenster: genau ein Eintrag
        _fehlversuche(client, KENNUNG, 20, "192.0.2.10")
        (eintrag,) = _gedrosselt()
        assert eintrag.ip_address == "192.0.2.10"
        assert eintrag.user_ref is None
        assert eintrag.identifier_hash == security_audit.identifier_hash(KENNUNG)
        assert eintrag.details["grund"] == security_audit.REASON_THROTTLED
        assert eintrag.details["schritt"] == security_audit.STEP_PASSWORD
        assert eintrag.details["schwelle"] == security_audit.LIMIT_ADDRESS
        assert eintrag.details["fenster_minuten"] == 15
        # Abgewiesene Versuche zählen nicht als Fehlversuch und landen nicht in LoginAttempt
        assert LoginAttempt.objects.filter(ip_address="192.0.2.10").count() == LOGIN_FAILURES_PER_IP
        # Datenschutz: weder Kennung noch Passwort im Klartext
        werte = _alle_werte()
        assert KENNUNG not in werte and FALSCH not in werte
        assert audit_chain.verify(audit_chain.SECURITY, None).ok

    def test_andere_adresse_eigener_eintrag(self) -> None:
        client = Client()
        _fehlversuche(client, KENNUNG, LOGIN_FAILURES_PER_IP + 3, "192.0.2.10")
        _fehlversuche(client, "andere@example.org", LOGIN_FAILURES_PER_IP + 3, "198.51.100.7")
        assert sorted(str(e.ip_address) for e in _gedrosselt()) == ["192.0.2.10", "198.51.100.7"]

    def test_neues_fenster_schreibt_wieder(self, mitte_des_fensters: dt.datetime) -> None:
        client = Client()
        _fehlversuche(client, KENNUNG, LOGIN_FAILURES_PER_IP + 2, "192.0.2.10")
        assert len(_gedrosselt()) == 1
        spaeter = mitte_des_fensters + dt.timedelta(minutes=16)
        # Die Fehlversuche liegen weiter im Zählfenster der Drosselung, das Protokollfenster ist neu
        LoginAttempt.objects.update(timestamp=spaeter - dt.timedelta(minutes=1))
        with mock.patch("django.utils.timezone.now", return_value=spaeter):
            _fehlversuche(client, KENNUNG, 3, "192.0.2.10")
        erstes, zweites = _gedrosselt()
        assert zweites.details["fenster_ab"] > erstes.details["fenster_ab"]

    def test_ipv6_je_netz_zusammengefasst(self) -> None:
        client = Client()
        _fehlversuche(client, KENNUNG, LOGIN_FAILURES_PER_IP, "2001:db8:1:2::1")
        # Wechselnde Adressen aus demselben /64-Netz: ein Eintrag mit dem Netz
        for host in range(2, 12):
            _fehlversuche(client, KENNUNG, 1, f"2001:db8:1:2::{host}")
        (eintrag,) = _gedrosselt()
        assert eintrag.details["netz"] == "2001:db8:1:2::/64"
        assert eintrag.details["schwelle"] in (security_audit.LIMIT_ADDRESS, security_audit.LIMIT_NETWORK)

    def test_kontoschwelle_aus_vielen_adressen(self) -> None:
        client = Client()
        for nummer in range(LOGIN_FAILURES_PER_ACCOUNT):
            _fehlversuche(client, KENNUNG, 1, f"203.0.113.{nummer + 1}")
        _fehlversuche(client, KENNUNG, 1, "203.0.113.200")
        (eintrag,) = _gedrosselt()
        assert eintrag.details["schwelle"] == security_audit.LIMIT_ACCOUNT
        assert eintrag.ip_address == "203.0.113.200"

    @pytest.mark.parametrize("schreibweise", [f"  {KENNUNG}", KENNUNG.upper(), f" {KENNUNG.title()} "])
    def test_kontoschwelle_unabhaengig_von_der_schreibweise(self, schreibweise: str) -> None:
        """Leerraum am Rand und Großbuchstaben zählen zum selben Konto (eine Form für Zählung und Protokoll)."""
        client = Client()
        for nummer in range(LOGIN_FAILURES_PER_ACCOUNT):
            _fehlversuche(client, schreibweise, 1, f"203.0.113.{nummer + 1}")
        assert set(LoginAttempt.objects.values_list("email", flat=True)) == {KENNUNG}

        _fehlversuche(client, KENNUNG, 1, "203.0.113.200")
        (eintrag,) = _gedrosselt()
        assert eintrag.details["schwelle"] == security_audit.LIMIT_ACCOUNT
        assert eintrag.identifier_hash == security_audit.identifier_hash(KENNUNG)

    def test_ohne_cache_ipv6_je_netz(self) -> None:
        client = Client()
        _fehlversuche(client, KENNUNG, LOGIN_FAILURES_PER_IP, "2001:db8:5:6::1")
        with mock.patch.object(cache, "add", side_effect=ConnectionError("weg")):
            for host in range(2, 6):
                _fehlversuche(client, KENNUNG, 1, f"2001:db8:5:6::{host}")
        (eintrag,) = _gedrosselt()
        assert eintrag.details["netz"] == "2001:db8:5:6::/64"

    def test_ohne_cache_entscheidet_die_datenbank(self) -> None:
        client = Client()
        _fehlversuche(client, KENNUNG, LOGIN_FAILURES_PER_IP, "192.0.2.10")
        with mock.patch.object(cache, "add", side_effect=ConnectionError("weg")):
            _fehlversuche(client, KENNUNG, 5, "192.0.2.10")
        assert len(_gedrosselt()) == 1

    def test_protokollfehler_aendert_nichts_an_der_abweisung(self) -> None:
        client = Client()
        _fehlversuche(client, KENNUNG, LOGIN_FAILURES_PER_IP, "192.0.2.10")
        with mock.patch.object(SecurityAuditLog.objects, "create", side_effect=RuntimeError("weg")):
            response = client.post(
                reverse("accounts:login"), {"email": KENNUNG, "password": FALSCH}, REMOTE_ADDR="192.0.2.10"
            )
        assert response.status_code == 200
        assert "Zu viele fehlgeschlagene Anmeldeversuche" in _meldungen(response)

    def test_abweisung_zeigt_leeres_formular(self) -> None:
        """Die Abweisung zeigt ein leeres Formular und schreibt keinen weiteren Fehlversuch (keine Flut)."""
        client = Client()
        _fehlversuche(client, KENNUNG, LOGIN_FAILURES_PER_IP, "192.0.2.10")
        fehlversuche = SecurityAuditLog.objects.filter(event="login_failed").count()
        with mock.patch("apps.accounts.forms.authenticate") as authenticate:
            _fehlversuche(client, KENNUNG, 10, "192.0.2.10")
        authenticate.assert_not_called()
        assert SecurityAuditLog.objects.filter(event="login_failed").count() == fehlversuche
        assert len(_gedrosselt()) == 1


class TestZweiterFaktor:
    def test_gesperrter_code_schritt_mit_kontoreferenz(self) -> None:
        user = make_user("zwei@example.org")
        enable_2fa(user)
        client = Client()
        password_step(client, user)
        for _ in range(MAX_2FA_FAILURES):
            code_step(client, "000000")
        assert _gedrosselt() == []

        # Der nächste Code wird abgewiesen (die Sitzung des Anmeldeschritts endet dabei)
        code_step(client, "000000")
        (eintrag,) = _gedrosselt()
        assert eintrag.user_ref == user.pk
        assert eintrag.identifier_hash == ""
        assert eintrag.details["schritt"] == security_audit.STEP_SECOND_FACTOR
        assert eintrag.details["schwelle"] == security_audit.LIMIT_ACCOUNT
        # Nur Angaben zur Abweisung, kein eingegebener Code
        assert set(eintrag.details) == {"grund", "schritt", "schwelle", "fenster_ab", "fenster_minuten"}


def test_record_throttled_ohne_request_daten() -> None:
    """Ohne Adresse entsteht trotzdem höchstens ein Eintrag je Fenster."""
    request: Any = mock.Mock(META={})
    for _ in range(3):
        security_audit.record_throttled(
            request, step=security_audit.STEP_PASSWORD, limit=security_audit.LIMIT_ACCOUNT, window_minutes=15
        )
    (eintrag,) = _gedrosselt()
    assert eintrag.ip_address is None


def test_datenexport_findet_anmeldeversuche_jeder_schreibweise() -> None:
    """Der Datenexport der Person findet die normalisiert gespeicherten Anmeldeversuche."""
    from apps.work.organization.export_service import DsgvoExportService

    user = make_user("Gross.Schreibung@Example.org")
    Client().post(
        reverse("accounts:login"),
        {"email": " Gross.Schreibung@Example.org", "password": FALSCH},
        REMOTE_ADDR="192.0.2.50",
    )
    (versuch,) = DsgvoExportService()._collect_login_attempts(user)
    assert versuch["was_successful"] is False
