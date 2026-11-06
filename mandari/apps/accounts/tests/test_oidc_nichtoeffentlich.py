# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OpenID-Connect-Anbieter für das Profil Nichtöffentliches (Issue #565): Regeln am Autorisierungs- und Token-Endpunkt.

- ``ratsdaten:nichtoeffentlich`` nur im Code-Ablauf und nur für Anwendungen, die ihn ausdrücklich dürfen; zweiter
  Faktor Pflicht, und zwar bei der Anmeldung selbst (ein erst danach eingerichteter Faktor genügt nicht); höchstens
  sieben Tage nach der Anmeldung, danach endet der Refresh
- ``sitzung:lesen`` nur für Dienste mit Bindung
- ``private_key_jwt`` (RFC 7523): ``aud`` nur der Aussteller als einzelne Zeichenkette (draft-ietf-oauth-rfc7523bis),
  ``exp`` höchstens fünf Minuten, ``jti`` nur einmal, nur RS256 und ES256, nur der eingetragene Schlüssel, kein
  Client-Secret für solche Anwendungen

Was die nichtöffentliche Ausgabe daraus macht, prüft ``apps/session/tests/test_profil_nichtoeffentlich.py``.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone
from jwcrypto import jwk, jwt

from apps.accounts.models import SecurityAuditLog, User
from apps.accounts.oidc import catalog, client_assertion, reauth
from apps.accounts.oidc.models import OAuthApplication, OAuthClientAssertion, OAuthRefreshToken
from apps.accounts.tests import oidc_hilfen as h
from apps.accounts.tests.test_login_2fa import code_step, current_code, enable_2fa, make_user, password_step

pytestmark = pytest.mark.django_db

NOE = catalog.RATSDATEN_NICHTOEFFENTLICH
TAG = 24 * 60 * 60


@pytest.fixture(autouse=True)
def anbieter() -> Iterator[None]:
    cache.clear()
    with h.eingeschaltet(OIDC_TOKEN_RATE_LIMIT=0):
        yield
    cache.clear()


@pytest.fixture
def person() -> User:
    user = make_user("rat@example.org")
    enable_2fa(user)
    return user


def _angemeldet(user: User, *, zweiter_faktor: bool = True) -> Client:
    client = Client()
    client.force_login(user)
    if zweiter_faktor:
        h.zweiter_faktor(client)
    return client


def _app(scopes: list[str] | None = None) -> tuple[OAuthApplication, str]:
    return h.anwendung(name="Ratsapp", scopes=scopes if scopes is not None else [catalog.OPENID, NOE])


def _tokens(person: User, app: OAuthApplication, secret: str) -> dict[str, Any]:
    client = _angemeldet(person)
    verifier, challenge = h.pkce()
    code = h.code_holen(client, app, challenge, scope=f"openid {NOE}")
    return h.tokens_holen(client, app, secret, code, verifier)


def _refresh(app: OAuthApplication, secret: str, refresh_token: str) -> Any:
    return Client().post(
        "/oauth/token/", {"grant_type": "refresh_token", "refresh_token": refresh_token}, **h.basic(app, secret)
    )


def _anmeldung_vor(refresh_token: str, sekunden: int) -> None:
    from apps.accounts.oidc.access import checksum

    OAuthRefreshToken.objects.filter(token_checksum=checksum(refresh_token)).update(
        auth_time=timezone.now() - timedelta(seconds=sekunden)
    )


# =============================================================================
# Katalog und Anwendungen
# =============================================================================


class TestKatalog:
    def test_nichtoeffentlich_nur_fuer_personen_und_geschuetzt(self) -> None:
        scope = catalog.get(NOE)
        assert scope is not None and scope.person and not scope.service and scope.protected
        assert timedelta(days=7) == catalog.PROTECTED_MAX_LOGIN_AGE

    def test_sitzung_lesen_nur_fuer_gebundene_dienste(self) -> None:
        scope = catalog.get(catalog.SITZUNG_LESEN)
        assert scope is not None and scope.service and not scope.person and scope.bound
        assert catalog.allowed_for([catalog.SITZUNG_LESEN], person=False) == []
        assert catalog.allowed_for([catalog.SITZUNG_LESEN], person=False, bound=True) == [catalog.SITZUNG_LESEN]

    def test_dienst_ohne_bindung_darf_sitzung_lesen_nicht(self) -> None:
        app = OAuthApplication(
            name="Dienst",
            client_type=OAuthApplication.CLIENT_CONFIDENTIAL,
            authorization_grant_type=OAuthApplication.GRANT_CLIENT_CREDENTIALS,
            algorithm=OAuthApplication.NO_ALGORITHM,
            allowed_scopes=[catalog.SITZUNG_LESEN],
            client_secret="s" * 40,
        )
        with pytest.raises(ValidationError, match="Nur für Dienste mit Bindung"):
            app.full_clean(exclude=["client_id"])

    def test_bindung_nur_fuer_dienste_und_bekannte_arten(self) -> None:
        app = OAuthApplication(
            name="App",
            client_type=OAuthApplication.CLIENT_CONFIDENTIAL,
            authorization_grant_type=OAuthApplication.GRANT_AUTHORIZATION_CODE,
            redirect_uris=h.REDIRECT_URI,
            algorithm=OAuthApplication.RS256_ALGORITHM,
            allowed_scopes=[catalog.OPENID],
            binding="gibtsnicht:123",
            client_secret="s" * 40,
        )
        with pytest.raises(ValidationError) as fehler:
            app.full_clean(exclude=["client_id"])
        meldungen = " ".join(fehler.value.message_dict["binding"])
        assert "unbekannte Art" in meldungen and "nur Dienste" in meldungen

    @pytest.mark.parametrize(
        ("schluessel", "meldung"),
        [
            (json.loads(jwk.JWK.generate(kty="EC", crv="P-256").export_private()), "private Teile"),
            (json.loads(jwk.JWK.generate(kty="EC", crv="P-384").export_public()), "P-256"),
            (json.loads(jwk.JWK.generate(kty="RSA", size=1024).export_public()), "2048"),
            (json.loads(jwk.JWK.generate(kty="OKP", crv="Ed25519").export_public()), "nur RSA"),
        ],
    )
    def test_nur_oeffentliche_rsa_und_p256_schluessel(self, schluessel: dict[str, Any], meldung: str) -> None:
        assert any(meldung in problem for problem in client_assertion.jwks_problems({"keys": [schluessel]}))


# =============================================================================
# Personen: Freigabe, zweiter Faktor, sieben Tage
# =============================================================================


class TestPerson:
    def test_anwendung_ohne_freigabe_bekommt_keinen_code(self, person: User) -> None:
        app, _secret = _app([catalog.OPENID, catalog.RATSDATEN_LESEN])
        _verifier, challenge = h.pkce()
        antwort = _angemeldet(person).get("/oauth/authorize/", h.autorisierung(app, challenge, scope=f"openid {NOE}"))
        assert antwort.status_code == 302
        assert h.rueckweg(antwort["Location"])["error"] == "invalid_scope"

    def test_dienst_kann_den_scope_nicht_haben(self) -> None:
        app, secret = h.anwendung(ablauf=OAuthApplication.GRANT_CLIENT_CREDENTIALS, scopes=[catalog.RATSDATEN_LESEN])
        antwort = Client().post(
            "/oauth/token/", {"grant_type": "client_credentials", "scope": NOE}, **h.basic(app, secret)
        )
        assert antwort.status_code == 400
        assert antwort.json()["error"] == "invalid_scope"

    def test_ohne_zweiten_faktor_zur_einrichtung(self) -> None:
        user = make_user("ohne@example.org")
        app, _secret = _app()
        _verifier, challenge = h.pkce()
        client = _angemeldet(user)
        antwort = client.get("/oauth/authorize/", h.autorisierung(app, challenge, scope=f"openid {NOE}"))
        assert antwort.status_code == 302
        assert antwort["Location"].startswith("/accounts/") and "next=" in antwort["Location"]
        stumm = client.get("/oauth/authorize/", h.autorisierung(app, challenge, scope=f"openid {NOE}", prompt="none"))
        assert h.rueckweg(stumm["Location"])["error"] == "login_required"
        # Ohne den geschützten Scope geht es wie bisher ohne zweiten Faktor
        app2, _ = _app([catalog.OPENID, catalog.RATSDATEN_LESEN])
        antwort = client.get("/oauth/authorize/", h.autorisierung(app2, challenge))
        assert antwort.status_code == 200

    def test_anmeldung_ohne_zweiten_faktor_verlangt_neue(self, person: User) -> None:
        # Konto mit Faktor, aber die Anmeldung dieser Sitzung war ohne ihn: erst neu anmelden
        app, _secret = _app()
        _verifier, challenge = h.pkce()
        client = _angemeldet(person, zweiter_faktor=False)
        antwort = client.get("/oauth/authorize/", h.autorisierung(app, challenge, scope=f"openid {NOE}"))
        assert antwort.status_code == 302
        assert "/accounts/login/" in antwort["Location"]
        stumm = client.get("/oauth/authorize/", h.autorisierung(app, challenge, scope=f"openid {NOE}", prompt="none"))
        assert h.rueckweg(stumm["Location"])["error"] == "login_required"
        # Auch das Formular der Zustimmungsseite führt dann zur Anmeldung, nicht zum Code
        formular = client.post(
            "/oauth/authorize/?" + urlencode(h.autorisierung(app, challenge, scope=f"openid {NOE}")),
            {**h.autorisierung(app, challenge, scope=f"openid {NOE}"), "allow": "Erlauben"},
        )
        assert formular.status_code == 302
        assert "/accounts/login/" in formular["Location"]
        # Ohne den geschützten Scope genügt die Anmeldung wie bisher
        app2, _ = _app([catalog.OPENID, catalog.RATSDATEN_LESEN])
        assert client.get("/oauth/authorize/", h.autorisierung(app2, challenge)).status_code == 200

    def test_faktor_erst_nach_der_anmeldung_eingerichtet(self) -> None:
        # Anmeldung nur mit Passwort, dann in derselben Sitzung einen Faktor eingerichtet: kein Code ohne neue
        # Anmeldung; mit Passwort und Code geht es
        user = make_user("spaet@example.org")
        app, secret = _app()
        verifier, challenge = h.pkce()
        client = Client()
        assert password_step(client, user).status_code == 302
        geheimnis = enable_2fa(user)
        assert not reauth.login_with_second_factor(client)
        antwort = client.get("/oauth/authorize/", h.autorisierung(app, challenge, scope=f"openid {NOE}"))
        assert antwort.status_code == 302
        assert "/accounts/login/" in antwort["Location"]
        password_step(client, user)
        assert code_step(client, current_code(geheimnis)).status_code == 302
        code = h.code_holen(client, app, challenge, scope=f"openid {NOE}")
        assert NOE in h.tokens_holen(client, app, secret, code, verifier)["scope"].split()

    def test_anmeldung_aelter_als_sieben_tage_verlangt_neue(self, person: User) -> None:
        app, _secret = _app()
        _verifier, challenge = h.pkce()
        client = _angemeldet(person)
        h.anmeldung_zurueckdatieren(client, 8 * TAG)
        antwort = client.get("/oauth/authorize/", h.autorisierung(app, challenge, scope=f"openid {NOE}"))
        assert antwort.status_code == 302
        assert "/accounts/login/" in antwort["Location"]

    def test_code_und_refresh_innerhalb_von_sieben_tagen(self, person: User) -> None:
        app, secret = _app()
        tokens = _tokens(person, app, secret)
        assert NOE in tokens["scope"].split()
        _anmeldung_vor(tokens["refresh_token"], 6 * TAG)
        erneuert = _refresh(app, secret, tokens["refresh_token"])
        assert erneuert.status_code == 200, erneuert.content
        assert erneuert.json()["expires_in"] <= 900

    def test_refresh_endet_nach_sieben_tagen(self, person: User) -> None:
        app, secret = _app()
        tokens = _tokens(person, app, secret)
        _anmeldung_vor(tokens["refresh_token"], 7 * TAG + 1)
        antwort = _refresh(app, secret, tokens["refresh_token"])
        assert antwort.status_code == 400
        assert antwort.json()["error"] == "invalid_grant"
        assert antwort["Cache-Control"] == "no-store"

    def test_access_token_laeuft_spaetestens_nach_sieben_tagen_ab(self, person: User) -> None:
        app, secret = _app()
        tokens = _tokens(person, app, secret)
        _anmeldung_vor(tokens["refresh_token"], 7 * TAG - 120)
        antwort = _refresh(app, secret, tokens["refresh_token"])
        assert antwort.status_code == 200
        assert 0 < antwort.json()["expires_in"] <= 120

    def test_refresh_ohne_zweiten_faktor_endet(self, person: User) -> None:
        app, secret = _app()
        tokens = _tokens(person, app, secret)
        person.totp_device.delete()
        assert _refresh(app, secret, tokens["refresh_token"]).json()["error"] == "invalid_grant"


# =============================================================================
# private_key_jwt
# =============================================================================


def _schluessel(kid: str = "k1", art: str = "EC") -> jwk.JWK:
    if art == "RSA":
        return jwk.JWK.generate(kty="RSA", size=2048, kid=kid)
    return jwk.JWK.generate(kty="EC", crv="P-256", kid=kid)


def _dienst(key: jwk.JWK) -> OAuthApplication:
    app = OAuthApplication(
        name="Dienst mit Schlüssel",
        client_type=OAuthApplication.CLIENT_CONFIDENTIAL,
        authorization_grant_type=OAuthApplication.GRANT_CLIENT_CREDENTIALS,
        algorithm=OAuthApplication.NO_ALGORITHM,
        allowed_scopes=[catalog.RATSDATEN_LESEN],
        jwks={"keys": [json.loads(key.export_public())]},
        client_secret="",
    )
    app.full_clean(exclude=["client_id", "client_secret"])
    app.save()
    return app


def _assertion(app: OAuthApplication, key: jwk.JWK, *, alg: str = "ES256", **angaben: Any) -> str:
    jetzt = int(time.time())
    claims = {
        "iss": app.client_id,
        "sub": app.client_id,
        "aud": client_assertion.audience(),
        "iat": jetzt,
        "exp": jetzt + 60,
        "jti": secrets.token_hex(12),
        **angaben,
    }
    token = jwt.JWT(header={"alg": alg, "kid": key["kid"]}, claims=claims)
    token.make_signed_token(key)
    return str(token.serialize())


def _token(assertion: str, **weitere: str) -> Any:
    return Client().post(
        "/oauth/token/",
        {
            "grant_type": "client_credentials",
            "client_assertion_type": client_assertion.ASSERTION_TYPE,
            "client_assertion": assertion,
            **weitere,
        },
    )


class TestPrivateKeyJwt:
    @pytest.mark.parametrize(("art", "alg"), [("EC", "ES256"), ("RSA", "RS256")])
    def test_gueltige_assertion_gibt_ein_token(self, art: str, alg: str) -> None:
        key = _schluessel(art=art)
        app = _dienst(key)
        antwort = _token(_assertion(app, key, alg=alg))
        assert antwort.status_code == 200, antwort.content
        assert antwort.json()["scope"] == catalog.RATSDATEN_LESEN
        eintrag = SecurityAuditLog.objects.filter(event="oidc_token").latest("created_at")
        assert eintrag.details["anmeldung"] == "private_key_jwt"

    @pytest.mark.parametrize(
        "angaben",
        [
            {"aud": "https://andere.example.org/oauth/token/"},
            {"aud": ["https://andere.example.org/oauth/token/"]},
            {"aud": "https://andere.example.org"},
            {"exp": -10},
            {"exp": 6 * 60},
            {"exp": None},
            {"jti": None},
            {"iss": "fremd"},
            {"sub": "fremd"},
            {"iat": 3600},
        ],
    )
    def test_falsche_angaben_werden_abgelehnt(self, angaben: dict[str, Any]) -> None:
        key = _schluessel()
        app = _dienst(key)
        jetzt = int(time.time())
        # Zeitangaben relativ zum Lauf, nicht zur Sammlung der Tests
        claims = {
            name: (jetzt + wert if name in ("exp", "iat") else wert)
            for name, wert in angaben.items()
            if wert is not None
        }
        assertion = _assertion(app, key, **claims)
        if None in angaben.values():
            # Angabe ganz weglassen: neu signieren ohne sie
            fehlend = next(name for name, wert in angaben.items() if wert is None)
            jetzt = int(time.time())
            basis = {
                "iss": app.client_id,
                "sub": app.client_id,
                "aud": client_assertion.audience(),
                "exp": jetzt + 60,
                "jti": secrets.token_hex(8),
            }
            basis.pop(fehlend)
            token = jwt.JWT(header={"alg": "ES256", "kid": key["kid"]}, claims=basis)
            token.make_signed_token(key)
            assertion = str(token.serialize())
        antwort = _token(assertion)
        assert antwort.status_code == 401, angaben
        assert antwort.json()["error"] == "invalid_client"

    def test_aud_nur_der_aussteller_als_zeichenkette(self) -> None:
        # draft-ietf-oauth-rfc7523bis: weder der Token-Endpunkt noch eine Liste, auch nicht mit dem Aussteller
        key = _schluessel()
        app = _dienst(key)
        aussteller = client_assertion.audience()
        token_endpunkt = f"{aussteller.rstrip('/')}/oauth/token/"
        for aud in (token_endpunkt, [token_endpunkt], [aussteller], [aussteller, token_endpunkt], f"{aussteller}/"):
            antwort = _token(_assertion(app, key, aud=aud))
            assert antwort.status_code == 401, aud
            assert antwort.json()["error"] == "invalid_client"
        assert _token(_assertion(app, key, aud=aussteller)).status_code == 200

    def test_jti_nur_einmal(self) -> None:
        key = _schluessel()
        app = _dienst(key)
        assertion = _assertion(app, key)
        assert _token(assertion).status_code == 200
        wieder = _token(assertion)
        assert wieder.status_code == 401
        assert wieder.json()["error"] == "invalid_client"
        assert OAuthClientAssertion.objects.filter(client_id=app.client_id).count() == 1

    def test_abgelaufene_sperren_werden_aufgeraeumt(self) -> None:
        key = _schluessel()
        app = _dienst(key)
        OAuthClientAssertion.objects.create(
            client_id=app.client_id, jti="alt", expires=timezone.now() - timedelta(minutes=1)
        )
        assert _token(_assertion(app, key)).status_code == 200
        assert not OAuthClientAssertion.objects.filter(jti="alt").exists()

    def test_falscher_schluessel(self) -> None:
        app = _dienst(_schluessel())
        antwort = _token(_assertion(app, _schluessel()))
        assert antwort.status_code == 401

    def test_nur_rs256_und_es256(self) -> None:
        key = _schluessel()
        app = _dienst(key)
        geheim = jwk.JWK.generate(kty="oct", size=256, kid=key["kid"])
        jetzt = int(time.time())
        claims = {
            "iss": app.client_id,
            "sub": app.client_id,
            "aud": client_assertion.audience(),
            "exp": jetzt + 60,
            "jti": "hs",
        }
        token = jwt.JWT(header={"alg": "HS256", "kid": key["kid"]}, claims=claims)
        token.make_signed_token(geheim)
        assert _token(str(token.serialize())).status_code == 401
        kopf = json.dumps({"alg": "none"}).encode()
        import base64

        ohne = ".".join(
            [
                base64.urlsafe_b64encode(kopf).rstrip(b"=").decode(),
                base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode(),
                "",
            ]
        )
        assert _token(ohne).status_code == 401

    def test_kein_client_secret_fuer_anwendungen_mit_schluessel(self) -> None:
        key = _schluessel()
        app = _dienst(key)
        # Selbst mit dem (nie ausgegebenen) gespeicherten Wert oder einem geratenen Secret: abgelehnt
        antwort = Client().post(
            "/oauth/token/",
            {"grant_type": "client_credentials", "client_id": app.client_id, "client_secret": "irgendwas"},
        )
        assert antwort.status_code == 401
        antwort = Client().post("/oauth/token/", {"grant_type": "client_credentials"}, **h.basic(app, "irgendwas"))
        assert antwort.status_code == 401

    def test_falscher_typ_der_assertion(self) -> None:
        key = _schluessel()
        app = _dienst(key)
        antwort = Client().post(
            "/oauth/token/",
            {
                "grant_type": "client_credentials",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:saml2-bearer",
                "client_assertion": _assertion(app, key),
            },
        )
        assert antwort.status_code == 401

    def test_anwendung_mit_secret_kann_keine_assertion(self) -> None:
        app, _secret = h.anwendung(ablauf=OAuthApplication.GRANT_CLIENT_CREDENTIALS, scopes=[catalog.RATSDATEN_LESEN])
        assert _token(_assertion(app, _schluessel())).status_code == 401

    def test_discovery_nennt_das_verfahren(self) -> None:
        daten = Client().get("/.well-known/openid-configuration").json()
        assert "private_key_jwt" in daten["token_endpoint_auth_methods_supported"]
        assert daten["token_endpoint_auth_signing_alg_values_supported"] == ["RS256", "ES256"]
