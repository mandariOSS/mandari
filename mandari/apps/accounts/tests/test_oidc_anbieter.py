# SPDX-License-Identifier: AGPL-3.0-or-later
"""
mandari als OpenID-Connect-Anbieter (Issue #1206).

Geprüft wird das Zusammenspiel mit django-oauth-toolkit 3.4 unter Django 6.1: Schalter (aus → 404), Discovery
und JWKS, Code-Ablauf mit PKCE samt Fehlerfällen, Refresh-Token mit Rotation und Schutz vor erneuter Nutzung,
Client Credentials nur mit erlaubten Scopes, Widerruf, Introspektion, Anmeldung mit zweitem Faktor,
``prompt``/``max_age``, Ratenbegrenzung, Prüfprotokoll und dass Tokens nur als Prüfsumme gespeichert werden.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from django.core.cache import cache
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from jwcrypto import jwk, jwt

from apps.accounts.models import SecurityAuditLog, User
from apps.accounts.oidc import access, reauth
from apps.accounts.oidc.models import OAuthAccessToken, OAuthApplication, OAuthGrant, OAuthRefreshToken
from apps.accounts.tests import oidc_hilfen as h
from apps.accounts.tests.test_login_2fa import PASSWORD, enable_2fa, make_user, password_step
from apps.accounts.two_factor_policy import POLICY_CACHE_SESSION_KEY

pytestmark = pytest.mark.django_db

ENDPUNKTE = (
    "/.well-known/openid-configuration",
    "/oauth/authorize/",
    "/oauth/token/",
    "/oauth/revoke/",
    "/oauth/introspect/",
    "/oauth/userinfo/",
    "/oauth/jwks/",
    "/accounts/verbundene-anwendungen/",
)


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def anbieter() -> Iterator[None]:
    with h.eingeschaltet():
        yield


@pytest.fixture
def person() -> User:
    user = make_user("person@example.org")
    user.first_name, user.last_name = "Petra", "Person"
    user.email_verified = True
    user.save()
    return user


def _angemeldet(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _ereignisse(event: str) -> list[SecurityAuditLog]:
    return list(SecurityAuditLog.objects.filter(event=event).order_by("created_at"))


# ---------------------------------------------------------------------------
# Schalter, Discovery, JWKS
# ---------------------------------------------------------------------------


class TestSchalter:
    @pytest.mark.parametrize("pfad", ENDPUNKTE)
    def test_aus_antworten_alle_endpunkte_mit_404(self, pfad: str, person: User) -> None:
        client = _angemeldet(person)
        assert client.get(pfad).status_code == 404
        assert client.post(pfad).status_code == 404

    def test_aus_nimmt_kein_access_token_an(self, person: User) -> None:
        with h.eingeschaltet():
            app, _secret = h.anwendung()
            raw = h.access_token(app, person)
            assert access.authenticate(raw).user == person
        with pytest.raises(access.InvalidAccessToken):
            access.authenticate(raw)

    def test_standard_ist_aus(self) -> None:
        from django.conf import settings

        assert settings.OIDC_PROVIDER_ENABLED is False


class TestDiscovery:
    def test_nennt_nur_was_mandari_anbietet(self, anbieter: None) -> None:
        response = Client().get("/.well-known/openid-configuration")
        assert response.status_code == 200
        data = response.json()
        issuer = data["issuer"]
        assert data["authorization_endpoint"] == f"{issuer}/oauth/authorize/"
        assert data["token_endpoint"] == f"{issuer}/oauth/token/"
        assert data["userinfo_endpoint"] == f"{issuer}/oauth/userinfo/"
        assert data["jwks_uri"] == f"{issuer}/oauth/jwks/"
        assert data["revocation_endpoint"] == f"{issuer}/oauth/revoke/"
        assert data["introspection_endpoint"] == f"{issuer}/oauth/introspect/"
        assert data["response_types_supported"] == ["code"]
        assert data["grant_types_supported"] == ["authorization_code", "refresh_token", "client_credentials"]
        assert data["code_challenge_methods_supported"] == ["S256"]
        assert data["id_token_signing_alg_values_supported"] == ["RS256"]
        assert data["authorization_response_iss_parameter_supported"] is True
        assert set(data["scopes_supported"]) == {
            "openid",
            "profile",
            "email",
            "ratsdaten:lesen",
            "einreichung:schreiben",
            "mandari:bereitstellung",
        }
        assert {"sub", "name", "email", "email_verified"} <= set(data["claims_supported"])

    def test_jwks_mit_aktivem_und_bisherigem_schluessel(self, anbieter: None) -> None:
        keys = Client().get("/oauth/jwks/").json()["keys"]
        erwartet = {jwk.JWK.from_pem(h.rsa_schluessel(n).encode()).thumbprint() for n in (1, 2)}
        assert {key["kid"] for key in keys} == erwartet
        assert all(key["alg"] == "RS256" and key["use"] == "sig" and "d" not in key for key in keys)


# ---------------------------------------------------------------------------
# Code-Ablauf mit PKCE
# ---------------------------------------------------------------------------


class TestCodeAblauf:
    def test_vollstaendig_mit_zustimmung_id_token_und_userinfo(self, anbieter: None, person: User) -> None:
        app, secret = h.anwendung(name="Ratsapp")
        client = _angemeldet(person)
        verifier, challenge = h.pkce()

        seite = client.get("/oauth/authorize/", h.autorisierung(app, challenge, scope="openid profile email"))
        assert seite.status_code == 200
        inhalt = seite.content.decode()
        assert "Ratsapp" in inhalt and "Zulassen" in inhalt and "Deine E-Mail-Adresse" in inhalt
        assert seite.context["redirect_host"] == "app.example.org"
        assert f">{seite.context['redirect_host']}</strong>" in inhalt

        code = h.code_holen(client, app, challenge, scope="openid profile email")
        tokens = h.tokens_holen(Client(), app, secret, code, verifier)
        assert tokens["access_token"].startswith("mandari_at_")
        assert tokens["refresh_token"].startswith("mandari_rt_")
        assert tokens["expires_in"] == 900
        assert set(tokens["scope"].split()) == {"openid", "profile", "email"}

        jwks = jwk.JWKSet.from_json(Client().get("/oauth/jwks/").content)
        claims = json.loads(jwt.JWT(jwt=tokens["id_token"], key=jwks).claims)
        assert claims["sub"] == str(person.pk)
        assert claims["aud"] == app.client_id
        assert claims["nonce"] == "nonce-456"
        assert claims["email"] == "person@example.org" and claims["name"] == "Petra Person"

        info = Client().get("/oauth/userinfo/", HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}").json()
        assert info == {
            "sub": str(person.pk),
            "name": "Petra Person",
            "given_name": "Petra",
            "family_name": "Person",
            "email": "person@example.org",
            "email_verified": True,
        }

    def test_ohne_scope_profile_keine_personenangaben(self, anbieter: None, person: User) -> None:
        app, secret = h.anwendung()
        client = _angemeldet(person)
        verifier, challenge = h.pkce()
        tokens = h.tokens_holen(Client(), app, secret, h.code_holen(client, app, challenge), verifier)
        info = Client().get("/oauth/userinfo/", HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}").json()
        assert info == {"sub": str(person.pk)}

    def test_tokens_stehen_nur_als_pruefsumme_in_der_datenbank(self, anbieter: None, person: User) -> None:
        app, secret = h.anwendung()
        verifier, challenge = h.pkce()
        tokens = h.tokens_holen(Client(), app, secret, h.code_holen(_angemeldet(person), app, challenge), verifier)
        gespeichert = OAuthAccessToken.objects.get()
        assert gespeichert.token == ""
        assert gespeichert.token_checksum == access.checksum(tokens["access_token"])
        refresh = OAuthRefreshToken.objects.get()
        assert refresh.token == "" and refresh.token_checksum == access.checksum(tokens["refresh_token"])
        app.refresh_from_db()
        assert app.client_secret != secret and app.client_secret.startswith("md5$")

    def test_vertrauenswuerdige_anwendung_ohne_zustimmungsseite(self, anbieter: None, person: User) -> None:
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        response = _angemeldet(person).get("/oauth/authorize/", h.autorisierung(app, challenge))
        assert response.status_code == 302
        params = h.rueckweg(response["Location"])
        assert params["code"] and params["state"] == "zustand-123"
        assert params["iss"]  # RFC 9207

    def test_ablehnen_gibt_access_denied(self, anbieter: None, person: User) -> None:
        app, _secret = h.anwendung()
        client = _angemeldet(person)
        _verifier, challenge = h.pkce()
        werte = h.autorisierung(app, challenge)
        form = client.get("/oauth/authorize/", werte).context["form"]
        daten = {name: form[name].value() or "" for name in form.fields if name != "allow"}
        response = client.post("/oauth/authorize/", daten)
        assert h.rueckweg(response["Location"])["error"] == "access_denied"
        assert not OAuthGrant.objects.exists()

    def test_falsche_redirect_uri_leitet_nicht_weiter(self, anbieter: None, person: User) -> None:
        app, _secret = h.anwendung()
        _verifier, challenge = h.pkce()
        for falsch in ("https://angreifer.example.org/rueckweg", h.REDIRECT_URI + "/", h.REDIRECT_URI + "?x=1"):
            response = _angemeldet(person).get(
                "/oauth/authorize/", h.autorisierung(app, challenge, redirect_uri=falsch)
            )
            assert response.status_code == 400, falsch
            assert "Anmeldung nicht möglich" in response.content.decode()
        assert not OAuthGrant.objects.exists()

    def test_ohne_pkce_kein_code(self, anbieter: None, person: User) -> None:
        app, _secret = h.anwendung()
        response = _angemeldet(person).get("/oauth/authorize/", h.autorisierung(app))
        assert response.status_code == 302
        assert h.rueckweg(response["Location"])["error"] == "invalid_request"
        assert not OAuthGrant.objects.exists()

    def test_pkce_plain_wird_abgelehnt(self, anbieter: None, person: User) -> None:
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        verifier, _challenge = h.pkce()
        response = _angemeldet(person).get(
            "/oauth/authorize/", h.autorisierung(app, code_challenge=verifier, code_challenge_method="plain")
        )
        assert h.rueckweg(response["Location"])["error"] == "invalid_request"
        assert not OAuthGrant.objects.exists()

    def test_falscher_code_verifier_gibt_keine_tokens(self, anbieter: None, person: User) -> None:
        app, secret = h.anwendung()
        _verifier, challenge = h.pkce()
        code = h.code_holen(_angemeldet(person), app, challenge)
        response = Client().post(
            "/oauth/token/",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": h.REDIRECT_URI,
                "code_verifier": "x" * 50,
            },
            **h.basic(app, secret),
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"
        assert not OAuthAccessToken.objects.exists()

    def test_code_gilt_nur_einmal(self, anbieter: None, person: User) -> None:
        app, secret = h.anwendung()
        verifier, challenge = h.pkce()
        code = h.code_holen(_angemeldet(person), app, challenge)
        h.tokens_holen(Client(), app, secret, code, verifier)
        response = Client().post(
            "/oauth/token/",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": h.REDIRECT_URI,
                "code_verifier": verifier,
            },
            **h.basic(app, secret),
        )
        assert response.status_code == 400 and response.json()["error"] == "invalid_grant"

    def test_implicit_wird_abgelehnt(self, anbieter: None, person: User) -> None:
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        response = _angemeldet(person).get("/oauth/authorize/", h.autorisierung(app, challenge, response_type="token"))
        assert response.status_code in (302, 400)
        if response.status_code == 302:
            assert "access_token" not in response["Location"]
            assert h.rueckweg(response["Location"])["error"] in ("unsupported_response_type", "unauthorized_client")
        assert not OAuthAccessToken.objects.exists()

    def test_password_grant_wird_abgelehnt(self, anbieter: None, person: User) -> None:
        app, secret = h.anwendung()
        response = Client().post(
            "/oauth/token/",
            {"grant_type": "password", "username": person.email, "password": PASSWORD, "scope": "ratsdaten:lesen"},
            **h.basic(app, secret),
        )
        assert response.status_code in (400, 401)
        assert "access_token" not in response.json()

    def test_nicht_erlaubter_scope_gibt_keinen_code(self, anbieter: None, person: User) -> None:
        app, _secret = h.anwendung(scopes=["openid"], vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        response = _angemeldet(person).get(
            "/oauth/authorize/", h.autorisierung(app, challenge, scope="openid ratsdaten:lesen")
        )
        assert h.rueckweg(response["Location"])["error"] == "invalid_scope"

    def test_anonym_geht_zur_anmeldung_von_mandari(self, anbieter: None) -> None:
        app, _secret = h.anwendung()
        _verifier, challenge = h.pkce()
        response = Client().get("/oauth/authorize/", h.autorisierung(app, challenge))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login") + "?next=")

    def test_nach_anmeldung_mit_passwort_weiter_zur_zustimmung(self, anbieter: None, person: User) -> None:
        app, _secret = h.anwendung()
        _verifier, challenge = h.pkce()
        client = Client()
        weiter = client.get("/oauth/authorize/", h.autorisierung(app, challenge))["Location"]
        next_url = h.parse_qs_first(weiter, "next")
        response = password_step(client, person, next_url)
        assert response["Location"] == next_url
        assert client.get(next_url).status_code == 200


# ---------------------------------------------------------------------------
# Zweiter Faktor
# ---------------------------------------------------------------------------


class TestZweiterFaktor:
    def test_sitzung_ohne_erfuellten_zweiten_faktor_gilt_nicht_als_angemeldet(
        self, anbieter: None, person: User
    ) -> None:
        enable_2fa(person)
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        client = Client()
        password_step(client, person)  # Passwort richtig, Code fehlt noch
        response = client.get("/oauth/authorize/", h.autorisierung(app, challenge))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))
        assert not OAuthGrant.objects.exists()

    def test_prompt_none_ohne_zweiten_faktor_gibt_login_required(self, anbieter: None, person: User) -> None:
        enable_2fa(person)
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        client = Client()
        password_step(client, person)
        response = client.get("/oauth/authorize/", h.autorisierung(app, challenge, prompt="none"))
        assert h.rueckweg(response["Location"])["error"] == "login_required"
        assert not OAuthGrant.objects.exists()

    @override_settings(TWO_FACTOR_ENFORCEMENT=True)
    def test_offene_pflicht_fuehrt_zur_einrichtung(self, anbieter: None) -> None:
        admin = make_user("admin@example.org")
        admin.is_staff = True
        admin.save()
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        client = _angemeldet(admin)  # angemeldet, bevor die Pflicht galt
        response = client.get("/oauth/authorize/", h.autorisierung(app, challenge))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:two_factor_enroll"))
        assert not OAuthGrant.objects.exists()

    @override_settings(TWO_FACTOR_ENFORCEMENT=True)
    def test_eigene_pruefung_auch_bei_veraltetem_zwischenstand_der_middleware(self, anbieter: None) -> None:
        """Die Middleware merkt sich die Richtlinie fünf Minuten; der Autorisierungs-Endpunkt prüft selbst."""
        admin = make_user("admin@example.org")
        admin.is_staff = True
        admin.save()
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        client = _angemeldet(admin)
        sitzung = client.session
        sitzung[POLICY_CACHE_SESSION_KEY] = {"user": str(admin.pk), "at": int(time.time()), "need": ""}
        sitzung.save()

        response = client.get("/oauth/authorize/", h.autorisierung(app, challenge))
        assert response["Location"].startswith(reverse("accounts:two_factor_enroll"))
        stumm = client.get("/oauth/authorize/", h.autorisierung(app, challenge, prompt="none"))
        assert h.rueckweg(stumm["Location"])["error"] == "login_required"
        assert not OAuthGrant.objects.exists()

    @override_settings(TWO_FACTOR_ENFORCEMENT=True)
    def test_mit_erfuellter_pflicht_gibt_es_den_code(self, anbieter: None) -> None:
        admin = make_user("admin@example.org")
        admin.is_staff = True
        admin.save()
        enable_2fa(admin)
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        response = _angemeldet(admin).get("/oauth/authorize/", h.autorisierung(app, challenge))
        assert h.rueckweg(response["Location"])["code"]


class TestPrompt:
    def test_prompt_none_vertrauenswuerdig_still(self, anbieter: None, person: User) -> None:
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        response = _angemeldet(person).get("/oauth/authorize/", h.autorisierung(app, challenge, prompt="none"))
        assert h.rueckweg(response["Location"])["code"]

    def test_prompt_none_fremde_anwendung_braucht_zustimmung(self, anbieter: None, person: User) -> None:
        app, _secret = h.anwendung()
        _verifier, challenge = h.pkce()
        response = _angemeldet(person).get("/oauth/authorize/", h.autorisierung(app, challenge, prompt="none"))
        assert h.rueckweg(response["Location"])["error"] == "consent_required"

    def test_prompt_none_anonym(self, anbieter: None) -> None:
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        response = Client().get("/oauth/authorize/", h.autorisierung(app, challenge, prompt="none"))
        assert h.rueckweg(response["Location"])["error"] == "login_required"

    def test_prompt_login_verlangt_erneute_anmeldung(self, anbieter: None, person: User) -> None:
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        client = _angemeldet(person)
        response = client.get("/oauth/authorize/", h.autorisierung(app, challenge, prompt="login"))
        assert response.status_code == 302
        location = response["Location"]
        assert location.startswith(reverse("accounts:login") + "?erneut=1")
        assert "prompt" not in h.parse_qs_first(location, "next")
        # Die Anmeldeseite zeigt das Formular, obwohl die Person angemeldet ist
        assert client.get(location).status_code == 200
        assert not OAuthGrant.objects.exists()

    def test_max_age_ueberschritten_verlangt_erneute_anmeldung(self, anbieter: None, person: User) -> None:
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        client = _angemeldet(person)
        User.objects.filter(pk=person.pk).update(last_login=timezone.now() - timedelta(hours=2))
        response = client.get("/oauth/authorize/", h.autorisierung(app, challenge, max_age="600"))
        assert response["Location"].startswith(reverse("accounts:login") + "?erneut=1")
        frisch = client.get("/oauth/authorize/", h.autorisierung(app, challenge, max_age="86400", state="neue-anfrage"))
        assert h.rueckweg(frisch["Location"])["code"]

    @pytest.mark.parametrize("verlangt", [{"prompt": "login"}, {"max_age": "600"}])
    def test_weiterleitung_ohne_erneute_anmeldung_gibt_keinen_code(
        self, anbieter: None, person: User, verlangt: dict[str, str]
    ) -> None:
        """Das Ziel der Weiterleitung führt erst nach einer Anmeldung in dieser Sitzung zum Code."""
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        client = _angemeldet(person)
        User.objects.filter(pk=person.pk).update(last_login=timezone.now() - timedelta(hours=2))
        response = client.get("/oauth/authorize/", h.autorisierung(app, challenge, **verlangt))
        weiter = h.parse_qs_first(response["Location"], "next")

        nochmal = client.get(weiter)
        assert nochmal.status_code == 302
        assert nochmal["Location"].startswith(reverse("accounts:login") + "?erneut=1")
        still = client.get(f"{weiter}&prompt=none")
        assert h.rueckweg(still["Location"])["error"] == "login_required"
        assert not OAuthGrant.objects.exists()

        client.force_login(person)  # die erneute Anmeldung
        assert h.rueckweg(client.get(weiter)["Location"])["code"]

    def test_zustimmung_ohne_erneute_anmeldung_gibt_keinen_code(self, anbieter: None, person: User) -> None:
        """Auch das Formular der Zustimmungsseite gibt bis zur erneuten Anmeldung keinen Code."""
        app, _secret = h.anwendung()
        _verifier, challenge = h.pkce()
        client = _angemeldet(person)
        werte = h.autorisierung(app, challenge)
        form = client.get("/oauth/authorize/", werte).context["form"]
        daten = {name: form[name].value() or "" for name in form.fields if name != "allow"}
        client.get("/oauth/authorize/", {**werte, "prompt": "login"})

        response = client.post(f"/oauth/authorize/?{urlencode(werte)}", {**daten, "allow": "on"})
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login") + "?erneut=1")
        assert not OAuthGrant.objects.exists()

    def test_viele_offene_wuensche_sperren_alles_bis_zur_anmeldung(self, anbieter: None, person: User) -> None:
        """Wer die Sitzung mit Wünschen flutet, verdrängt keinen: Dann gibt es gar keinen Code mehr."""
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        client = _angemeldet(person)
        for nummer in range(reauth.MAX_PENDING + 5):
            client.get("/oauth/authorize/", h.autorisierung(app, challenge, prompt="login", state=f"s{nummer}"))
        response = client.get("/oauth/authorize/", h.autorisierung(app, challenge, state="ganz-neu"))
        assert response["Location"].startswith(reverse("accounts:login") + "?erneut=1")
        assert not OAuthGrant.objects.exists()
        client.force_login(person)
        response = client.get("/oauth/authorize/", h.autorisierung(app, challenge, state="s0"))
        assert h.rueckweg(response["Location"])["code"]

    def test_approval_prompt_der_anfrage_gilt_nicht(self, anbieter: None, person: User) -> None:
        """Fremde Anwendungen fragen jedes Mal, auch mit ``approval_prompt=auto`` und gültigem Token."""
        app, _secret, _tokens = _code_tokens(person)
        _verifier, challenge = h.pkce()
        response = _angemeldet(person).get("/oauth/authorize/", h.autorisierung(app, challenge, approval_prompt="auto"))
        assert response.status_code == 200
        assert "Zulassen" in response.content.decode()
        assert not OAuthGrant.objects.exists()


# ---------------------------------------------------------------------------
# Refresh, Widerruf, Introspektion
# ---------------------------------------------------------------------------


def _code_tokens(person: User, **anwendung: Any) -> tuple[OAuthApplication, str, dict[str, Any]]:
    app, secret = h.anwendung(**anwendung)
    verifier, challenge = h.pkce()
    tokens = h.tokens_holen(Client(), app, secret, h.code_holen(_angemeldet(person), app, challenge), verifier)
    return app, secret, tokens


def _refresh(app: OAuthApplication, secret: str, refresh_token: str) -> Any:
    return Client().post(
        "/oauth/token/", {"grant_type": "refresh_token", "refresh_token": refresh_token}, **h.basic(app, secret)
    )


class TestRefreshToken:
    def test_rotation_und_schutz_vor_erneuter_nutzung(self, anbieter: None, person: User) -> None:
        app, secret, erste = _code_tokens(person)
        zweite = _refresh(app, secret, erste["refresh_token"]).json()
        assert zweite["refresh_token"] != erste["refresh_token"]
        assert zweite["access_token"] != erste["access_token"]
        with pytest.raises(access.InvalidAccessToken):
            access.authenticate(erste["access_token"])
        assert access.authenticate(zweite["access_token"]).user == person

        # Das alte Refresh-Token noch einmal: abgelehnt, und die ganze Kette ist widerrufen
        nochmal = _refresh(app, secret, erste["refresh_token"])
        assert nochmal.status_code == 400 and nochmal.json()["error"] == "invalid_grant"
        with pytest.raises(access.InvalidAccessToken):
            access.authenticate(zweite["access_token"])
        assert _refresh(app, secret, zweite["refresh_token"]).status_code == 400


class TestWiderruf:
    def test_widerruf_durch_die_anwendung(self, anbieter: None, person: User) -> None:
        app, secret, tokens = _code_tokens(person)
        response = Client().post("/oauth/revoke/", {"token": tokens["access_token"]}, **h.basic(app, secret))
        assert response.status_code == 200
        with pytest.raises(access.InvalidAccessToken):
            access.authenticate(tokens["access_token"])
        assert _refresh(app, secret, tokens["refresh_token"]).status_code == 400
        assert (
            Client().get("/oauth/userinfo/", HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}").status_code == 401
        )
        eintrag = _ereignisse("oidc_widerruf")[-1]
        assert eintrag.user_ref == person.pk
        assert eintrag.details == {"anwendung": app.client_id, "durch": "Anwendung"}

    def test_fremde_anwendung_kann_nicht_widerrufen(self, anbieter: None, person: User) -> None:
        _app, _secret, tokens = _code_tokens(person)
        fremd, fremd_secret = h.anwendung(name="Fremd")
        Client().post("/oauth/revoke/", {"token": tokens["access_token"]}, **h.basic(fremd, fremd_secret))
        assert access.authenticate(tokens["access_token"]).user == person

    def test_kontoseite_verbundene_anwendungen(self, anbieter: None, person: User) -> None:
        app, secret, tokens = _code_tokens(person, name="Ratsapp")
        client = _angemeldet(person)
        seite = client.get(reverse("accounts:connected_applications"))
        assert seite.status_code == 200
        inhalt = seite.content.decode()
        assert "Ratsapp" in inhalt and "Widerrufen" in inhalt
        assert "Ratsdaten lesen" in inhalt

        response = client.post(reverse("accounts:connected_applications"), {"application_id": app.pk}, follow=True)
        assert "Der Zugang wurde widerrufen" in response.content.decode()
        with pytest.raises(access.InvalidAccessToken):
            access.authenticate(tokens["access_token"])
        assert _refresh(app, secret, tokens["refresh_token"]).status_code == 400
        assert "Ratsapp" not in client.get(reverse("accounts:connected_applications")).content.decode()
        assert _ereignisse("oidc_widerruf")[-1].details["durch"] == "Person"

    def test_kontoseite_zeigt_nur_eigene_anwendungen(self, anbieter: None, person: User) -> None:
        _code_tokens(person, name="Ratsapp")
        andere = make_user("andere@example.org")
        seite = _angemeldet(andere).get(reverse("accounts:connected_applications"))
        assert "Ratsapp" not in seite.content.decode()

    def test_kontoseite_widerruft_nicht_fuer_andere(self, anbieter: None, person: User) -> None:
        app, _secret, tokens = _code_tokens(person)
        andere = make_user("andere@example.org")
        _angemeldet(andere).post(reverse("accounts:connected_applications"), {"application_id": app.pk})
        assert access.authenticate(tokens["access_token"]).user == person

    def test_ausgeschaltete_anwendung_verliert_ihre_tokens(self, anbieter: None, person: User) -> None:
        app, secret, tokens = _code_tokens(person)
        OAuthApplication.objects.filter(pk=app.pk).update(is_active=False)
        with pytest.raises(access.InvalidAccessToken):
            access.authenticate(tokens["access_token"])
        assert (
            Client().get("/oauth/userinfo/", HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}").status_code == 401
        )
        assert _refresh(app, secret, tokens["refresh_token"]).status_code in (400, 401)

    def test_gesperrtes_konto_verliert_seine_tokens(self, anbieter: None, person: User) -> None:
        app, secret, tokens = _code_tokens(person)
        User.objects.filter(pk=person.pk).update(is_active=False)
        with pytest.raises(access.InvalidAccessToken):
            access.authenticate(tokens["access_token"])
        assert (
            Client().get("/oauth/userinfo/", HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}").status_code == 401
        )
        ausgaben = len(_ereignisse("oidc_token"))
        response = _refresh(app, secret, tokens["refresh_token"])
        assert response.status_code == 400 and response.json()["error"] == "invalid_grant"
        assert len(_ereignisse("oidc_token")) == ausgaben

    def test_entzogener_scope_gilt_sofort_nicht_mehr(self, anbieter: None, person: User) -> None:
        app, secret = h.anwendung()
        verifier, challenge = h.pkce()
        code = h.code_holen(_angemeldet(person), app, challenge, scope="openid profile email ratsdaten:lesen")
        tokens = h.tokens_holen(Client(), app, secret, code, verifier)
        assert access.authenticate(tokens["access_token"]).has_scope("ratsdaten:lesen")
        OAuthApplication.objects.filter(pk=app.pk).update(allowed_scopes=["openid"])
        assert not access.authenticate(tokens["access_token"]).has_scope("ratsdaten:lesen")
        nur_sub = {"sub": str(person.pk)}
        assert Client().get("/oauth/userinfo/", HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}").json() == nur_sub

        # Auch die Refresh-Kette behält den Scope nicht
        neu = _refresh(app, secret, tokens["refresh_token"]).json()
        assert neu["scope"] == "openid"
        assert Client().get("/oauth/userinfo/", HTTP_AUTHORIZATION=f"Bearer {neu['access_token']}").json() == nur_sub
        ausdruecklich = Client().post(
            "/oauth/token/",
            {"grant_type": "refresh_token", "refresh_token": neu["refresh_token"], "scope": "openid profile"},
            **h.basic(app, secret),
        )
        assert ausdruecklich.status_code == 400 and ausdruecklich.json()["error"] == "invalid_scope"

        # Ohne openid gibt userinfo nichts mehr heraus
        OAuthApplication.objects.filter(pk=app.pk).update(allowed_scopes=[])
        assert Client().get("/oauth/userinfo/", HTTP_AUTHORIZATION=f"Bearer {neu['access_token']}").status_code in (
            401,
            403,
        )


class TestIntrospektion:
    def test_nur_eigene_tokens(self, anbieter: None, person: User) -> None:
        app, secret, tokens = _code_tokens(person)
        eigen = Client().post("/oauth/introspect/", {"token": tokens["access_token"]}, **h.basic(app, secret)).json()
        assert eigen["active"] is True
        assert eigen["client_id"] == app.client_id and eigen["sub"] == str(person.pk)
        assert set(eigen["scope"].split()) == {"openid", "ratsdaten:lesen"}
        assert "username" not in eigen

        fremd, fremd_secret = h.anwendung(name="Fremd")
        antwort = Client().post("/oauth/introspect/", {"token": tokens["access_token"]}, **h.basic(fremd, fremd_secret))
        assert antwort.json() == {"active": False}

    def test_ohne_gueltiges_secret_401(self, anbieter: None, person: User) -> None:
        app, _secret, tokens = _code_tokens(person)
        response = Client().post("/oauth/introspect/", {"token": tokens["access_token"]}, **h.basic(app, "falsch"))
        assert response.status_code == 401

    def test_access_token_reicht_nicht(self, anbieter: None, person: User) -> None:
        _app, _secret, tokens = _code_tokens(person)
        response = Client().post(
            "/oauth/introspect/",
            {"token": tokens["access_token"]},
            HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}",
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Client Credentials
# ---------------------------------------------------------------------------


class TestClientCredentials:
    def _dienst(self) -> tuple[OAuthApplication, str]:
        return h.anwendung(
            name="Bereitstellung",
            ablauf=OAuthApplication.GRANT_CLIENT_CREDENTIALS,
            scopes=["ratsdaten:lesen", "mandari:bereitstellung"],
        )

    def _token(self, app: OAuthApplication, secret: str, **daten: str) -> Any:
        return Client().post("/oauth/token/", {"grant_type": "client_credentials", **daten}, **h.basic(app, secret))

    def test_ohne_angabe_die_erlaubten_scopes(self, anbieter: None) -> None:
        app, secret = self._dienst()
        tokens = self._token(app, secret).json()
        assert set(tokens["scope"].split()) == {"ratsdaten:lesen", "mandari:bereitstellung"}
        assert "refresh_token" not in tokens and "id_token" not in tokens
        zugang = access.authenticate(tokens["access_token"])
        assert zugang.user is None and not zugang.acts_for_person

    def test_nur_erlaubte_scopes(self, anbieter: None) -> None:
        app, secret = self._dienst()
        assert self._token(app, secret, scope="ratsdaten:lesen").json()["scope"] == "ratsdaten:lesen"
        for verboten in ("einreichung:schreiben", "openid", "email", "ratsdaten:lesen unbekannt"):
            response = self._token(app, secret, scope=verboten)
            assert response.status_code == 400, verboten
            assert response.json()["error"] == "invalid_scope"

    def test_falsches_secret(self, anbieter: None) -> None:
        app, _secret = self._dienst()
        response = self._token(app, "falsch")
        assert response.status_code == 401 and response.json()["error"] == "invalid_client"

    def test_code_anwendung_darf_keine_client_credentials(self, anbieter: None) -> None:
        app, secret = h.anwendung()
        response = self._token(app, secret)
        assert response.status_code in (400, 401)
        assert response.json()["error"] == "unauthorized_client"

    def test_geraetefreigabe_gibt_es_nicht(self, anbieter: None) -> None:
        app, secret = self._dienst()
        response = Client().post(
            "/oauth/token/",
            {"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": "x"},
            **h.basic(app, secret),
        )
        assert response.status_code == 400 and response.json()["error"] == "unsupported_grant_type"


# ---------------------------------------------------------------------------
# Ratenbegrenzung und Prüfprotokoll
# ---------------------------------------------------------------------------


class TestRatenbegrenzung:
    @override_settings(OIDC_TOKEN_RATE_LIMIT=2)
    def test_token_endpunkt(self, anbieter: None) -> None:
        app, _secret = h.anwendung(ablauf=OAuthApplication.GRANT_CLIENT_CREDENTIALS, scopes=["ratsdaten:lesen"])
        client = Client(REMOTE_ADDR="203.0.113.7")
        statuses = [
            client.post("/oauth/token/", {"grant_type": "client_credentials"}, **h.basic(app, "falsch")).status_code
            for _ in range(4)
        ]
        assert statuses[:2] == [401, 401] and statuses[2:] == [429, 429]
        response = client.post("/oauth/token/", {"grant_type": "client_credentials"})
        assert int(response["Retry-After"]) > 0
        assert len(_ereignisse("login_throttled")) == 1
        assert _ereignisse("login_throttled")[0].details["schritt"] == "OIDC-Token-Endpunkt"
        # Eine andere Adresse zählt für sich
        anders = Client(REMOTE_ADDR="203.0.113.8")
        assert anders.post("/oauth/token/", {"grant_type": "client_credentials"}).status_code != 429


class TestPruefprotokoll:
    def test_ausgabe_ohne_token_im_protokoll(self, anbieter: None, person: User) -> None:
        app, _secret, tokens = _code_tokens(person)
        eintrag = _ereignisse("oidc_token")[-1]
        assert eintrag.user_ref == person.pk
        assert eintrag.details["anwendung"] == app.client_id
        assert eintrag.details["ablauf"] == "authorization_code"
        assert set(eintrag.details["scopes"].split()) == {"openid", "ratsdaten:lesen"}
        text = json.dumps(eintrag.details)
        assert tokens["access_token"] not in text and tokens["refresh_token"] not in text

    def test_aenderungen_an_anwendungen_im_admin(self, anbieter: None) -> None:
        admin = make_user("admin@example.org")
        admin.is_staff = admin.is_superuser = True
        admin.save()
        client = _angemeldet(admin)
        url = reverse("admin:accounts_oauthapplication_add")
        form = client.get(url).context["adminform"].form
        daten = {
            "name": "Neue App",
            "client_id": form["client_id"].value(),
            "client_secret": "ein-sehr-langes-geheimnis-1234567890",
            "client_type": "confidential",
            "authorization_grant_type": "authorization-code",
            "redirect_uris": "https://neu.example.org/rueckweg",
            "allowed_scopes": ["openid", "ratsdaten:lesen"],
            "algorithm": "RS256",
            "is_active": "on",
        }
        response = client.post(url, daten)
        assert response.status_code == 302, response.context["adminform"].form.errors if response.context else ""
        app = OAuthApplication.objects.get(name="Neue App")
        assert app.allowed_scopes == ["openid", "ratsdaten:lesen"]
        assert app.client_secret != daten["client_secret"]
        eintrag = _ereignisse("oidc_anwendung")[-1]
        assert eintrag.details["aktion"] == "angelegt" and eintrag.details["anwendung"] == app.client_id
        assert daten["client_secret"] not in json.dumps(eintrag.details)
        assert eintrag.user_ref == admin.pk

        client.post(reverse("admin:accounts_oauthapplication_delete", args=[app.pk]), {"post": "yes"})
        assert _ereignisse("oidc_anwendung")[-1].details["aktion"] == "gelöscht"


# ---------------------------------------------------------------------------
# Modell, Migrationen, Verzeichnis
# ---------------------------------------------------------------------------


def _fehler(app: OAuthApplication) -> dict[str, list[str]]:
    from django.core.exceptions import ValidationError

    try:
        app.clean()
    except ValidationError as exc:
        return exc.message_dict
    return {}


class TestAnwendungPruefen:
    def _app(self, **werte: Any) -> OAuthApplication:
        basis: dict[str, Any] = {
            "name": "App",
            "client_type": "confidential",
            "authorization_grant_type": "authorization-code",
            "redirect_uris": "https://app.example.org/cb",
            "algorithm": "RS256",
            "allowed_scopes": ["openid"],
        }
        return OAuthApplication(**{**basis, **werte})

    def test_gueltig(self, anbieter: None) -> None:
        assert _fehler(self._app()) == {}

    @pytest.mark.parametrize(
        "uri",
        ["http://app.example.org/cb", "http://localhost:8080/cb", "https://app.example.org/cb#frag", "app://cb"],
    )
    def test_nur_https_redirects(self, anbieter: None, uri: str) -> None:
        assert "redirect_uris" in _fehler(self._app(redirect_uris=uri))

    def test_http_localhost_nur_bei_debug(self, anbieter: None) -> None:
        with override_settings(DEBUG=True):
            assert _fehler(self._app(redirect_uris="http://localhost:8080/cb")).get("redirect_uris") is None
            assert "redirect_uris" in _fehler(self._app(redirect_uris="http://app.example.org/cb"))

    @pytest.mark.parametrize("ablauf", ["implicit", "password", "openid-hybrid"])
    def test_nur_code_und_client_credentials(self, anbieter: None, ablauf: str) -> None:
        assert "authorization_grant_type" in _fehler(self._app(authorization_grant_type=ablauf))

    def test_scopes_aus_dem_katalog_und_passend_zum_ablauf(self, anbieter: None) -> None:
        assert "allowed_scopes" in _fehler(self._app(allowed_scopes=["admin"]))
        assert "allowed_scopes" in _fehler(self._app(allowed_scopes=["mandari:bereitstellung"]))
        dienst = self._app(
            authorization_grant_type="client-credentials", redirect_uris="", algorithm="", allowed_scopes=["openid"]
        )
        assert "allowed_scopes" in _fehler(dienst)

    def test_dienst_braucht_secret_und_kein_id_token(self, anbieter: None) -> None:
        dienst = self._app(
            authorization_grant_type="client-credentials",
            client_type="public",
            redirect_uris="",
            algorithm="RS256",
            allowed_scopes=["ratsdaten:lesen"],
        )
        fehler = _fehler(dienst)
        assert "client_type" in fehler and "algorithm" in fehler

    def test_code_ablauf_nur_rs256(self, anbieter: None) -> None:
        assert "algorithm" in _fehler(self._app(algorithm="HS256"))
        assert "algorithm" in _fehler(self._app(algorithm=""))

    def test_secret_immer_gehasht(self, anbieter: None) -> None:
        app = self._app(hash_client_secret=False, client_secret="klartext-geheimnis-123456", registration_source="dcr")
        app.save()
        app.refresh_from_db()
        assert app.hash_client_secret is True and app.client_secret != "klartext-geheimnis-123456"
        assert app.registration_source == "manual"


class TestMigrationen:
    def test_eigene_modelle_vor_den_migrationen_der_bibliothek(self) -> None:
        plan = MigrationLoader(connection).graph.forwards_plan(
            ("oauth2_provider", "0022_refreshtoken_token_family_index")
        )
        assert plan.index(("accounts", "0009_oidc_anbieter")) < plan.index(("oauth2_provider", "0001_initial"))

    def test_keine_tabellen_der_bibliothek(self) -> None:
        tabellen = connection.introspection.table_names()
        assert not [name for name in tabellen if name.startswith("oauth2_provider_")]
        assert {"accounts_oauthapplication", "accounts_oauthaccesstoken", "accounts_oauthrefreshtoken"} <= set(tabellen)


class TestTokenErzeugung:
    def test_praefix_und_laenge(self) -> None:
        token = access.generate_access_token()
        assert token.startswith("mandari_at_") and len(token) == len("mandari_at_") + 43
        assert access.generate_refresh_token().startswith("mandari_rt_")
        # Nie mit einem API-Token der Session-API (64 Zeichen ohne Präfix) zu verwechseln
        assert len(token) != 64


class TestZeitplan:
    def test_aufraeumen_nur_bei_eingeschaltetem_anbieter(self) -> None:
        from apps.common import schedules
        from apps.events.schedule import ScheduleRegistry

        aus = ScheduleRegistry()
        schedules.registrieren(ziel=aus)
        assert aus.get("befehl:cleartokens") is None
        with override_settings(OIDC_PROVIDER_ENABLED=True):
            an = ScheduleRegistry()
            schedules.registrieren(ziel=an)
        assert an.get("befehl:cleartokens") is not None
