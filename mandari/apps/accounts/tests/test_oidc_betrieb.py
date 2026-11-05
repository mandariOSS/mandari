# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Betrieb des OpenID-Connect-Anbieters (Issue #1215).

- ``manage.py oidc_anwendung``: Anlegen mit denselben Regeln wie im Admin, das Client-Secret genau einmal in der
  Ausgabe und nur gehasht gespeichert, Liste ohne Secret, Sperren mit Widerruf aller Tokens, Entsperren.
- ``manage.py oidc_pruefen``: Schalter, Schlüssel, Aussteller, Discovery und JWKS; auch ausgeschaltet.
- Nebenbei behoben: kein leeres oder kurzes Client-Secret bei vertraulichen Anwendungen, Ratenbegrenzung auch an
  Widerruf und Introspektion, Ausschalten im Admin widerruft die Tokens.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from django.conf import settings
from django.contrib import admin as django_admin
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import Client, RequestFactory, override_settings

from apps.accounts.models import SecurityAuditLog
from apps.accounts.oidc import access, readiness, signing_keys
from apps.accounts.oidc.admin import OAuthApplicationAdmin
from apps.accounts.oidc.models import OAuthApplication, OAuthRefreshToken
from apps.accounts.tests import oidc_hilfen as h
from apps.accounts.tests.test_login_2fa import make_user

pytestmark = pytest.mark.django_db

ISSUER = "https://mandari.example.org"
REDIRECT = "https://app.example.org/rueckweg"


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def _an(**weitere: Any) -> override_settings:
    """Anbieter an, Aussteller mit https und passendem Host."""
    provider = {"OIDC_ISS_ENDPOINT": ISSUER, **weitere.pop("provider", {})}
    weitere.setdefault("ALLOWED_HOSTS", ["mandari.example.org", "testserver"])
    return h.eingeschaltet(provider=provider, **weitere)


def _aus_mit_aussteller(**provider: Any) -> override_settings:
    """Anbieter aus (wie ohne OIDC_PROVIDER_ENABLED), Aussteller mit https; Schlüssel nur, wenn übergeben."""
    return override_settings(
        OIDC_PROVIDER_ENABLED=False,
        OAUTH2_PROVIDER={**settings.OAUTH2_PROVIDER, "OIDC_ISS_ENDPOINT": ISSUER, **provider},
        ALLOWED_HOSTS=["mandari.example.org", "testserver"],
    )


def _befehl(*args: str) -> str:
    ausgabe = StringIO()
    call_command(*args, stdout=ausgabe, stderr=StringIO())
    return ausgabe.getvalue()


def _anlegen(*args: str) -> str:
    return _befehl("oidc_anwendung", "anlegen", *args)


def _wert(ausgabe: str, feld: str) -> str:
    treffer = re.search(rf"^\s*{re.escape(feld)}:\s*(\S+)", ausgabe, re.MULTILINE)
    assert treffer, ausgabe
    return treffer.group(1)


def _pem(key: Any) -> str:
    return str(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
    )


# ---------------------------------------------------------------------------
# oidc_anwendung anlegen
# ---------------------------------------------------------------------------


class TestAnlegen:
    def test_code_ablauf_vertraulich_secret_nur_einmal_und_gehasht(self) -> None:
        with _an():
            ausgabe = _anlegen(
                "--name", "Rats-App", "--typ", "vertraulich", "--grant", "authorization-code",
                "--redirect-uri", REDIRECT, "--scope", "openid", "--scope", "profile ratsdaten:lesen",
            )  # fmt: skip
        client_id, secret = _wert(ausgabe, "Client-ID"), _wert(ausgabe, "Client-Secret")
        app = OAuthApplication.objects.get(client_id=client_id)
        assert ausgabe.count(secret) == 1
        assert app.client_secret != secret and check_password(secret, app.client_secret)
        assert app.name == "Rats-App" and app.client_type == "confidential"
        assert app.authorization_grant_type == "authorization-code" and app.algorithm == "RS256"
        assert app.redirect_uris == REDIRECT
        assert app.allowed_scopes == ["openid", "profile", "ratsdaten:lesen"]
        assert app.skip_authorization is False and app.is_active is True
        eintrag = SecurityAuditLog.objects.filter(event="oidc_anwendung").latest("created_at")
        assert eintrag.details == {
            "aktion": "angelegt",
            "anwendung": client_id,
            "name": "Rats-App",
            "durch": "manage.py oidc_anwendung",
        }
        assert secret not in json.dumps(eintrag.details)

    def test_ausgegebenes_secret_funktioniert(self) -> None:
        with _an():
            ausgabe = _anlegen(
                "--name", "Dienst", "--typ", "vertraulich", "--grant", "client-credentials",
                "--scope", "ratsdaten:lesen",
            )  # fmt: skip
            app = OAuthApplication.objects.get(client_id=_wert(ausgabe, "Client-ID"))
            antwort = Client().post(
                "/oauth/token/", {"grant_type": "client_credentials"}, **h.basic(app, _wert(ausgabe, "Client-Secret"))
            )
        assert antwort.status_code == 200, antwort.content
        assert antwort.json()["scope"] == "ratsdaten:lesen"

    def test_oeffentlich_ohne_secret_in_der_ausgabe(self) -> None:
        with _an():
            ausgabe = _anlegen(
                "--name", "Telefon", "--typ", "öffentlich", "--grant", "authorization-code",
                "--redirect-uri", REDIRECT, "--scope", "openid", "--skip-authorization",
            )  # fmt: skip
        app = OAuthApplication.objects.get(client_id=_wert(ausgabe, "Client-ID"))
        assert app.client_type == "public" and app.skip_authorization is True
        assert "Client-Secret: keins" in ausgabe
        # Gespeichert ist ein zufälliges Secret, kein leeres
        assert app.client_secret and not check_password("", app.client_secret)

    def test_ausgeschaltet_mit_schluessel_moeglich_mit_hinweis(self) -> None:
        with _aus_mit_aussteller(OIDC_RSA_PRIVATE_KEY=h.rsa_schluessel(1)):
            ausgabe = _anlegen(
                "--name", "Vorab", "--typ", "vertraulich", "--grant", "authorization-code",
                "--redirect-uri", REDIRECT, "--scope", "openid",
            )  # fmt: skip
        assert "erst nach dem Einschalten" in ausgabe
        assert OAuthApplication.objects.filter(name="Vorab").exists()

    @pytest.mark.parametrize(
        ("argumente", "meldung"),
        [
            (["--typ", "vertraulich", "--grant", "authorization-code", "--redirect-uri", "http://app.example.org/cb",
              "--scope", "openid"], "https"),
            (["--typ", "vertraulich", "--grant", "authorization-code", "--redirect-uri", f"{REDIRECT}#teil",
              "--scope", "openid"], "Fragment"),
            (["--typ", "vertraulich", "--grant", "authorization-code", "--redirect-uri", REDIRECT,
              "--scope", "admin"], "Unbekannte Scopes: admin"),
            (["--typ", "vertraulich", "--grant", "authorization-code", "--redirect-uri", REDIRECT,
              "--scope", "mandari:bereitstellung"], "Passen nicht zum Ablauf"),
            (["--typ", "vertraulich", "--grant", "client-credentials", "--scope", "openid"], "Passen nicht zum Ablauf"),
            (["--typ", "öffentlich", "--grant", "client-credentials", "--scope", "ratsdaten:lesen"],
             "brauchen ein Client-Secret"),
            (["--typ", "vertraulich", "--grant", "authorization-code", "--scope", "openid"], "Redirect-URI"),
            (["--typ", "vertraulich", "--grant", "client-credentials", "--redirect-uri", REDIRECT,
              "--scope", "ratsdaten:lesen"], "keine Redirect-URIs"),
            (["--typ", "vertraulich", "--grant", "client-credentials", "--scope", "ratsdaten:lesen",
              "--skip-authorization"], "nur für den Code-Ablauf"),
            (["--typ", "vertraulich", "--grant", "authorization-code", "--redirect-uri", REDIRECT], "Scope"),
            (["--typ", "geheim", "--grant", "authorization-code", "--redirect-uri", REDIRECT, "--scope", "openid"],
             "vertraulich oder öffentlich"),
        ],
    )  # fmt: skip
    def test_regeln_wie_im_admin(self, argumente: list[str], meldung: str) -> None:
        with _an(), pytest.raises(CommandError, match=re.escape(meldung)):
            _anlegen("--name", "Abgelehnt", *argumente)
        assert not OAuthApplication.objects.exists()
        assert not SecurityAuditLog.objects.filter(event="oidc_anwendung").exists()

    def test_code_ablauf_ohne_schluessel(self) -> None:
        with _aus_mit_aussteller(), pytest.raises(CommandError, match="Signaturschlüssel"):
            _anlegen(
                "--name", "Ohne", "--typ", "vertraulich", "--grant", "authorization-code",
                "--redirect-uri", REDIRECT, "--scope", "openid",
            )  # fmt: skip
        assert not OAuthApplication.objects.exists()


# ---------------------------------------------------------------------------
# oidc_anwendung liste, sperren, entsperren
# ---------------------------------------------------------------------------


class TestListeUndSperren:
    def test_liste_ohne_secret(self) -> None:
        with _an():
            app, secret = h.anwendung(name="Ratsapp")
            dienst, _ = h.anwendung(name="Dienst", ablauf="client-credentials", scopes=["ratsdaten:lesen"])
            OAuthApplication.objects.filter(pk=dienst.pk).update(is_active=False)
            ausgabe = _befehl("oidc_anwendung", "liste")
        assert "Ratsapp [aktiv]" in ausgabe and "Dienst [gesperrt]" in ausgabe
        assert app.client_id in ausgabe and dienst.client_id in ausgabe
        assert REDIRECT in ausgabe and "openid profile email ratsdaten:lesen" in ausgabe
        app.refresh_from_db()
        assert secret not in ausgabe and app.client_secret not in ausgabe
        assert "2 Anwendung(en)." in ausgabe

    def test_liste_leer(self) -> None:
        assert "Keine Anwendungen" in _befehl("oidc_anwendung", "liste")

    def test_sperren_widerruft_alles_und_entsperren_belebt_nichts(self) -> None:
        person = make_user("person@example.org")
        with _an():
            app, secret = h.anwendung()
            verifier, challenge = h.pkce()
            angemeldet = Client()
            angemeldet.force_login(person)
            tokens = h.tokens_holen(Client(), app, secret, h.code_holen(angemeldet, app, challenge), verifier)

            ausgabe = _befehl("oidc_anwendung", "sperren", app.client_id, "--grund", "Vorfall 7")
            assert "gesperrt" in ausgabe and "2 Token(s) widerrufen" in ausgabe
            app.refresh_from_db()
            assert app.is_active is False
            with pytest.raises(access.InvalidAccessToken):
                access.authenticate(tokens["access_token"])
            assert not OAuthRefreshToken.objects.filter(application=app, revoked__isnull=True).exists()

            assert "bereits gesperrt" in _befehl("oidc_anwendung", "sperren", app.client_id)

            assert "entsperrt" in _befehl("oidc_anwendung", "entsperren", app.client_id)
            app.refresh_from_db()
            assert app.is_active is True
            with pytest.raises(access.InvalidAccessToken):
                access.authenticate(tokens["access_token"])
            erneut = Client().post(
                "/oauth/token/",
                {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
                **h.basic(app, secret),
            )
            assert erneut.status_code == 400

        aktionen = [e.details for e in SecurityAuditLog.objects.filter(event="oidc_anwendung").order_by("created_at")]
        assert [d["aktion"] for d in aktionen] == ["gesperrt", "entsperrt"]
        assert aktionen[0]["grund"] == "Vorfall 7" and aktionen[0]["durch"] == "manage.py oidc_anwendung"
        widerruf = SecurityAuditLog.objects.filter(event="oidc_widerruf").latest("created_at")
        assert widerruf.details["anzahl"] == 2 and widerruf.details["durch"] == "Plattform-Administration"

    def test_unbekannte_client_id(self) -> None:
        with pytest.raises(CommandError, match="Keine Anwendung"):
            _befehl("oidc_anwendung", "sperren", "gibt-es-nicht")


# ---------------------------------------------------------------------------
# oidc_pruefen
# ---------------------------------------------------------------------------


def _pruefen() -> str:
    return _befehl("oidc_pruefen")


def _pruefen_scheitert() -> str:
    ausgabe = StringIO()
    with pytest.raises(CommandError) as fehler:
        call_command("oidc_pruefen", stdout=ausgabe, stderr=StringIO())
    return ausgabe.getvalue() + "\n" + str(fehler.value)


class TestPruefen:
    def test_eingeschaltet_und_bereit(self) -> None:
        with _an():
            ausgabe = _pruefen()
        assert "OK       Schalter" in ausgabe
        assert re.search(r"OK\s+Signaturschlüssel: RSA mit 2048 Bit", ausgabe)
        assert "HINWEIS  Schlüsselwechsel" in ausgabe  # h.eingeschaltet() setzt einen bisherigen Schlüssel
        assert "OK       Aussteller: https://mandari.example.org" in ausgabe
        assert "OK       Discovery" in ausgabe and "OK       JWKS: antwortet mit 2 Schlüsseln" in ausgabe
        assert "Der Anbieter ist eingeschaltet und bereit." in ausgabe
        assert "PRIVATE KEY" not in ausgabe

    def test_ausgeschaltet_mit_schluessel_aus_der_umgebung(self, tmp_path: Path, monkeypatch: Any) -> None:
        datei = tmp_path / "oidc.pem"
        datei.write_text(h.rsa_schluessel(1), encoding="utf-8")
        monkeypatch.setenv("OIDC_RSA_PRIVATE_KEY_FILE", str(datei))
        with _aus_mit_aussteller(OIDC_RSA_PRIVATE_KEY="", OIDC_RSA_PRIVATE_KEYS_INACTIVE=[]):
            ausgabe = _pruefen()
            # Nach der Prüfung bleibt der Anbieter aus
            assert Client(HTTP_HOST="mandari.example.org").get("/.well-known/openid-configuration").status_code == 404
        assert "HINWEIS  Schalter: Der Anbieter ist ausgeschaltet" in ausgabe
        assert "OK       JWKS: antwortet mit einem Schlüssel" in ausgabe
        assert "Bereit zum Einschalten" in ausgabe
        assert settings.OIDC_PROVIDER_ENABLED is False

    def test_ausgeschaltet_ohne_schluessel(self, monkeypatch: Any) -> None:
        for name in ("OIDC_RSA_PRIVATE_KEY", "OIDC_RSA_PRIVATE_KEY_FILE"):
            monkeypatch.delenv(name, raising=False)
        with _aus_mit_aussteller(OIDC_RSA_PRIVATE_KEY=""):
            ausgabe = _pruefen_scheitert()
        assert "FEHLER   Signaturschlüssel: Kein Schlüssel eingestellt" in ausgabe
        assert "Discovery und JWKS: Nicht abgefragt" in ausgabe
        assert "lässt sich so nicht einschalten" in ausgabe

    @pytest.mark.parametrize(
        ("schluessel", "meldung"),
        [
            (_pem(ec.generate_private_key(ec.SECP256R1())), "RS256 braucht einen RSA-Schlüssel"),
            (_pem(rsa.generate_private_key(public_exponent=65537, key_size=signing_keys.MIN_BITS // 2)), "1024 Bit"),
            ("kein schluessel", "kein lesbarer"),
        ],
    )
    def test_untauglicher_schluessel(self, schluessel: str, meldung: str, monkeypatch: Any) -> None:
        monkeypatch.setenv("OIDC_RSA_PRIVATE_KEY", schluessel)
        monkeypatch.delenv("OIDC_RSA_PRIVATE_KEY_FILE", raising=False)
        with _aus_mit_aussteller(OIDC_RSA_PRIVATE_KEY=""):
            ausgabe = _pruefen_scheitert()
        assert meldung in ausgabe
        assert "PRIVATE KEY" not in ausgabe

    def test_untauglicher_bisheriger_schluessel(self) -> None:
        kurz = _pem(rsa.generate_private_key(public_exponent=65537, key_size=signing_keys.MIN_BITS // 2))
        with _an(provider={"OIDC_RSA_PRIVATE_KEYS_INACTIVE": [kurz]}):
            ausgabe = _pruefen_scheitert()
        assert "Bisheriger Schlüssel 1" in ausgabe and "1024 Bit" in ausgabe

    @pytest.mark.parametrize(
        ("aussteller", "meldung"),
        [
            ("http://mandari.example.org", "muss https verwenden"),
            ("https://mandari.example.org/mandari", "keinen Pfad"),
            ("https://anders.example.org", "nicht in ALLOWED_HOSTS"),
            ("https://localhost", "zeigt auf diesen Rechner"),
            ("", "Kein Aussteller"),
        ],
    )
    def test_falsche_basis_url(self, aussteller: str, meldung: str) -> None:
        with _an(provider={"OIDC_ISS_ENDPOINT": aussteller}, ALLOWED_HOSTS=["mandari.example.org", "localhost"]):
            ausgabe = _pruefen_scheitert()
        assert meldung in ausgabe
        assert "arbeitet nicht wie erwartet" in ausgabe

    def test_http_auf_localhost_nur_mit_debug(self) -> None:
        with _an(provider={"OIDC_ISS_ENDPOINT": "http://localhost:8000"}, DEBUG=True, ALLOWED_HOSTS=["localhost"]):
            ausgabe = _pruefen()
        assert "HINWEIS  Aussteller: http://localhost:8000 ohne https" in ausgabe
        assert "OK       JWKS" in ausgabe

    def test_meldet_anwendungen(self) -> None:
        with _an():
            assert "Noch keine aktive Anwendung" in _pruefen()
            h.anwendung()
            assert "OK       Anwendungen: 1 aktive Anwendung(en)." in _pruefen()


class _Antwort:
    def __init__(self, daten: Any, status: int = 200) -> None:
        self.status_code, self._daten = status, daten

    def json(self) -> Any:
        return self._daten


class TestJwksAbgleich:
    """Die Prüfung des JWKS selbst, mit einer vorgegebenen Antwort."""

    KEYS = readiness._Keys(active=h.rsa_schluessel(1), previous=[])

    def _eintrag(self, nummer: int, **mehr: Any) -> dict[str, Any]:
        return {"kid": readiness._key_id(h.rsa_schluessel(nummer)), "alg": "RS256", "use": "sig", "kty": "RSA", **mehr}

    def test_passend(self) -> None:
        befunde = readiness._check_jwks(lambda _pfad: _Antwort({"keys": [self._eintrag(1)]}), "/oauth/jwks/", self.KEYS)
        assert [b.level for b in befunde] == [readiness.OK]

    def test_andere_schluessel_id(self) -> None:
        befunde = readiness._check_jwks(lambda _pfad: _Antwort({"keys": [self._eintrag(2)]}), "/oauth/jwks/", self.KEYS)
        assert befunde[0].level == readiness.FEHLER and "passt nicht zum aktiven Schlüssel" in befunde[0].text

    def test_privater_schluesselteil(self) -> None:
        antwort = _Antwort({"keys": [self._eintrag(1, d="geheim")]})
        befunde = readiness._check_jwks(lambda _pfad: antwort, "/oauth/jwks/", self.KEYS)
        assert befunde[0].level == readiness.FEHLER and "privaten Schlüssels" in befunde[0].text

    def test_keine_antwort(self) -> None:
        befunde = readiness._check_jwks(lambda _pfad: _Antwort(None, 404), "/oauth/jwks/", self.KEYS)
        assert befunde[0].level == readiness.FEHLER and "404" in befunde[0].text


# ---------------------------------------------------------------------------
# Nebenbei behoben
# ---------------------------------------------------------------------------


def _vertraulich(**werte: Any) -> OAuthApplication:
    basis: dict[str, Any] = {
        "name": "App",
        "client_type": "confidential",
        "authorization_grant_type": "client-credentials",
        "algorithm": "",
        "allowed_scopes": ["ratsdaten:lesen"],
    }
    return OAuthApplication(**{**basis, **werte})


def _fehler(app: OAuthApplication) -> dict[str, list[str]]:
    try:
        app.clean()
    except ValidationError as exc:
        return dict(exc.message_dict)
    return {}


class TestClientSecret:
    def test_vertraulich_nie_leer(self) -> None:
        assert "Vertrauliche Anwendungen brauchen" in _fehler(_vertraulich(client_secret=""))["client_secret"][0]

    def test_vertraulich_nicht_zu_kurz(self) -> None:
        assert "mindestens 32 Zeichen" in _fehler(_vertraulich(client_secret="kurz"))["client_secret"][0]

    def test_gespeichertes_leeres_secret_wird_erkannt(self) -> None:
        fehler = _fehler(_vertraulich(client_secret=make_password("")))
        assert "gespeicherte Client-Secret ist leer" in fehler["client_secret"][0]

    def test_gueltiges_secret(self) -> None:
        assert _fehler(_vertraulich(client_secret="x" * 32)) == {}

    def test_ohne_secret_gespeichert_bekommt_es_ein_zufaelliges(self) -> None:
        app = _vertraulich(client_type="public", authorization_grant_type="authorization-code", client_secret="")
        app.redirect_uris, app.algorithm, app.allowed_scopes = REDIRECT, "RS256", ["openid"]
        app.save()
        assert app.client_secret and not check_password("", app.client_secret)


class TestRatenbegrenzung:
    @override_settings(OIDC_TOKEN_RATE_LIMIT=2)
    @pytest.mark.parametrize("pfad", ["/oauth/revoke/", "/oauth/introspect/"])
    def test_widerruf_und_introspektion(self, pfad: str) -> None:
        with h.eingeschaltet():
            app, _secret = h.anwendung()
            client = Client(REMOTE_ADDR="203.0.113.9")
            statuses = [client.post(pfad, {"token": "x"}, **h.basic(app, "falsch")).status_code for _ in range(3)]
            assert 429 not in statuses[:2] and statuses[2] == 429
            # Jeder Endpunkt zählt für sich
            token = client.post("/oauth/token/", {"grant_type": "client_credentials"}, **h.basic(app, "falsch"))
            assert token.status_code != 429
        eintrag = SecurityAuditLog.objects.get(event="login_throttled")
        assert eintrag.details["schritt"] in ("OIDC-Widerruf", "OIDC-Introspektion")


class TestAdminAusschalten:
    def test_ausschalten_im_admin_widerruft_die_tokens(self) -> None:
        admin = make_user("admin@example.org")
        admin.is_staff = admin.is_superuser = True
        admin.save()
        person = make_user("person@example.org")
        with h.eingeschaltet():
            app, _secret = h.anwendung()
            raw = h.access_token(app, person)
            request = RequestFactory().post("/admin/")
            request.user = admin
            app.is_active = False
            form = SimpleNamespace(changed_data=["is_active"])
            OAuthApplicationAdmin(OAuthApplication, django_admin.site).save_model(request, app, form, change=True)
            app.is_active = True
            app.save()
            with pytest.raises(access.InvalidAccessToken):
                access.authenticate(raw)
        widerruf = SecurityAuditLog.objects.get(event="oidc_widerruf")
        assert widerruf.details["durch"] == "Plattform-Administration" and widerruf.details["anzahl"] == 1
