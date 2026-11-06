# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OpenID-Connect-Anbieter: Härtung vor dem Einschalten (Issue #1215).

- Ein Widerruf zwischen Prüfung und Speichern eines Refresh (oder ein gleichzeitiger Refresh mit demselben Token)
  gibt keine neuen Tokens heraus; die Kette ist danach widerrufen
- ``max_age`` und ``auth_time`` beziehen sich auf die Anmeldung in der Sitzung, nicht auf ``User.last_login``
- ``id_token_hint`` für eine andere Person als die angemeldete: ``login_required``
- „Alle Sitzungen beenden“, Passwortwechsel und Passwort-Zurücksetzen widerrufen die Zugänge verbundener Anwendungen
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from jwcrypto import jwk, jwt

from apps.accounts.models import SecurityAuditLog, User
from apps.accounts.oidc import access, audit, reauth, services
from apps.accounts.oidc.models import (
    OAuthAccessToken,
    OAuthApplication,
    OAuthGrant,
    OAuthIDToken,
    OAuthRefreshToken,
)
from apps.accounts.oidc.validator import MandariOAuth2Validator, hinted_subject
from apps.accounts.services import PasswordService, SessionService
from apps.accounts.tests import oidc_hilfen as h
from apps.accounts.tests.test_login_2fa import PASSWORD, make_user

pytestmark = pytest.mark.django_db

NEUES_PASSWORT = "Neues-Sicheres-Passwort-2027"


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
    return make_user("person@example.org")


def _angemeldet(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _code_tokens(person: User, **anwendung: Any) -> tuple[OAuthApplication, str, dict[str, Any]]:
    app, secret = h.anwendung(**anwendung)
    verifier, challenge = h.pkce()
    tokens = h.tokens_holen(Client(), app, secret, h.code_holen(_angemeldet(person), app, challenge), verifier)
    return app, secret, tokens


def _refresh(app: OAuthApplication, secret: str, refresh_token: str) -> Any:
    return Client().post(
        "/oauth/token/", {"grant_type": "refresh_token", "refresh_token": refresh_token}, **h.basic(app, secret)
    )


def _claims(id_token: str) -> dict[str, Any]:
    jwks = jwk.JWKSet.from_json(Client().get("/oauth/jwks/").content)
    return dict(json.loads(jwt.JWT(jwt=id_token, key=jwks).claims))


def _ereignisse(event: str) -> list[SecurityAuditLog]:
    return list(SecurityAuditLog.objects.filter(event=event).order_by("created_at"))


def _nach_der_pruefung(monkeypatch: pytest.MonkeyPatch, aktion: Callable[[], object]) -> None:
    """``aktion`` läuft, nachdem ``validate_refresh_token`` das Token geprüft hat und bevor gespeichert wird."""
    original = MandariOAuth2Validator.validate_refresh_token

    def geprueft(self: MandariOAuth2Validator, *args: Any, **kwargs: Any) -> bool:
        gilt = bool(original(self, *args, **kwargs))
        if gilt:
            aktion()
        return gilt

    monkeypatch.setattr(MandariOAuth2Validator, "validate_refresh_token", geprueft)


def _abgelehnt_und_kette_widerrufen(app: OAuthApplication, secret: str, antwort: Any, tokens: dict[str, Any]) -> None:
    assert antwort.status_code == 400, antwort.content
    assert antwort.json()["error"] == "invalid_grant"
    assert "access_token" not in antwort.json()
    assert not OAuthRefreshToken.objects.filter(revoked__isnull=True).exists()
    assert not OAuthAccessToken.objects.exists()
    with pytest.raises(access.InvalidAccessToken):
        access.authenticate(tokens["access_token"])
    assert _refresh(app, secret, tokens["refresh_token"]).status_code == 400


# ---------------------------------------------------------------------------
# Refresh: Widerruf und Wettlauf
# ---------------------------------------------------------------------------


class TestWiderrufWaehrendRefresh:
    def test_widerruf_durch_die_person(self, anbieter: None, person: User, monkeypatch: pytest.MonkeyPatch) -> None:
        app, secret, tokens = _code_tokens(person)
        _nach_der_pruefung(monkeypatch, lambda: services.revoke_for_user(None, person, app.pk))
        ausgaben = len(_ereignisse(audit.EVENT_TOKEN))

        antwort = _refresh(app, secret, tokens["refresh_token"])

        _abgelehnt_und_kette_widerrufen(app, secret, antwort, tokens)
        # Das schon signierte ID-Token des abgelehnten Refresh bleibt nicht stehen
        assert not OAuthIDToken.objects.exists()
        assert len(_ereignisse(audit.EVENT_TOKEN)) == ausgaben

    def test_widerruf_des_refresh_tokens(self, anbieter: None, person: User, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wie RFC 7009 und der Admin: nur dieses Refresh-Token wird widerrufen."""
        app, secret, tokens = _code_tokens(person)

        def widerrufen() -> None:
            OAuthRefreshToken.objects.get(
                token_checksum=access.checksum(tokens["refresh_token"]), revoked__isnull=True
            ).revoke()

        _nach_der_pruefung(monkeypatch, widerrufen)
        _abgelehnt_und_kette_widerrufen(app, secret, _refresh(app, secret, tokens["refresh_token"]), tokens)

    def test_gesperrtes_konto(self, anbieter: None, person: User, monkeypatch: pytest.MonkeyPatch) -> None:
        app, secret, tokens = _code_tokens(person)
        _nach_der_pruefung(monkeypatch, lambda: User.objects.filter(pk=person.pk).update(is_active=False))
        antwort = _refresh(app, secret, tokens["refresh_token"])
        assert antwort.status_code == 400 and antwort.json()["error"] == "invalid_grant"
        assert OAuthAccessToken.objects.count() == 1  # nur das bisherige, kein neues

    def test_gleichzeitiger_refresh_mit_demselben_token(
        self, anbieter: None, person: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Der zweite von zwei gleichzeitigen Refreshes gilt als erneute Nutzung: keine leeren Tokens, Kette zu."""
        app, secret, tokens = _code_tokens(person)
        erste: dict[str, Any] = {}

        def zuerst_der_andere() -> None:
            if erste:
                return
            erste["laeuft"] = True
            erste["antwort"] = _refresh(app, secret, tokens["refresh_token"])

        _nach_der_pruefung(monkeypatch, zuerst_der_andere)
        zweite = _refresh(app, secret, tokens["refresh_token"])

        assert erste["antwort"].status_code == 200
        neu = erste["antwort"].json()
        assert neu["access_token"] and neu["refresh_token"]
        _abgelehnt_und_kette_widerrufen(app, secret, zweite, tokens)
        # Auch das Paar des ersten Refresh gilt nicht mehr
        with pytest.raises(access.InvalidAccessToken):
            access.authenticate(neu["access_token"])
        assert _refresh(app, secret, neu["refresh_token"]).status_code == 400

    def test_ohne_eingriff_weiter_rotation(self, anbieter: None, person: User) -> None:
        app, secret, tokens = _code_tokens(person)
        neu = _refresh(app, secret, tokens["refresh_token"]).json()
        assert access.authenticate(neu["access_token"]).user == person
        assert _refresh(app, secret, neu["refresh_token"]).status_code == 200


# ---------------------------------------------------------------------------
# max_age und auth_time je Sitzung
# ---------------------------------------------------------------------------


class TestAnmeldezeitJeSitzung:
    def test_anmeldung_merkt_sich_den_zeitpunkt_in_der_sitzung(self, person: User) -> None:
        vorher = int(time.time())
        client = _angemeldet(person)
        assert vorher <= client.session[reauth.LOGIN_TIME_KEY] <= int(time.time())

    def test_anmeldung_auf_anderem_geraet_macht_alte_sitzung_nicht_jung(self, anbieter: None, person: User) -> None:
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        _verifier, challenge = h.pkce()
        alt = _angemeldet(person)
        h.anmeldung_zurueckdatieren(alt, 2 * 60 * 60)
        _angemeldet(person)  # frische Anmeldung auf dem Telefon: last_login ist jetzt

        antwort = alt.get("/oauth/authorize/", h.autorisierung(app, challenge, max_age="60"))
        assert antwort["Location"].startswith(reverse("accounts:login") + "?erneut=1")
        still = alt.get("/oauth/authorize/", h.autorisierung(app, challenge, max_age="60", prompt="none", state="s2"))
        assert h.rueckweg(still["Location"])["error"] == "login_required"
        assert not OAuthGrant.objects.exists()

    def test_auth_time_aus_der_sitzung_auch_nach_refresh(self, anbieter: None, person: User) -> None:
        app, secret = h.anwendung(vertrauenswuerdig=True)
        client = _angemeldet(person)
        zeitpunkt = h.anmeldung_zurueckdatieren(client, 10 * 60)
        verifier, challenge = h.pkce()
        tokens = h.tokens_holen(Client(), app, secret, h.code_holen(client, app, challenge), verifier)
        assert _claims(tokens["id_token"])["auth_time"] == zeitpunkt

        _angemeldet(person)  # Anmeldung anderswo ändert nichts an dieser Kette
        neu = _refresh(app, secret, tokens["refresh_token"]).json()
        assert _claims(neu["id_token"])["auth_time"] == zeitpunkt
        noch = _refresh(app, secret, neu["refresh_token"]).json()
        assert _claims(noch["id_token"])["auth_time"] == zeitpunkt

    def test_sitzung_ohne_gemerkten_zeitpunkt(self, anbieter: None, person: User) -> None:
        """Angemeldet vor dieser Regel: für max_age zu alt, im ID-Token kein auth_time statt eines falschen."""
        app, secret = h.anwendung(vertrauenswuerdig=True)
        client = _angemeldet(person)
        session = client.session
        del session[reauth.LOGIN_TIME_KEY]
        session.save()
        verifier, challenge = h.pkce()

        antwort = client.get("/oauth/authorize/", h.autorisierung(app, challenge, max_age="86400"))
        assert antwort["Location"].startswith(reverse("accounts:login") + "?erneut=1")

        code = h.code_holen(client, app, challenge, state="ohne-max-age")
        tokens = h.tokens_holen(Client(), app, secret, code, verifier)
        assert "auth_time" not in _claims(tokens["id_token"])


# ---------------------------------------------------------------------------
# id_token_hint
# ---------------------------------------------------------------------------


def _hinweis(sub: Any, schluessel: str, **claims: Any) -> str:
    key = jwk.JWK.from_pem(schluessel.encode())
    token = jwt.JWT(
        header={"alg": "RS256", "kid": key.thumbprint()},
        claims={"iss": "https://mandari.example.org", "aud": "app", "sub": str(sub), **claims},
    )
    token.make_signed_token(key)
    return str(token.serialize())


class TestIdTokenHint:
    def test_hinweis_auf_andere_person_gibt_keinen_code(self, anbieter: None, person: User) -> None:
        app, secret, tokens = _code_tokens(person, vertrauenswuerdig=True)
        andere = _angemeldet(make_user("andere@example.org"))
        _verifier, challenge = h.pkce()
        codes = OAuthGrant.objects.count()

        still = andere.get(
            "/oauth/authorize/", h.autorisierung(app, challenge, prompt="none", id_token_hint=tokens["id_token"])
        )
        assert h.rueckweg(still["Location"])["error"] == "login_required"
        sichtbar = andere.get("/oauth/authorize/", h.autorisierung(app, challenge, id_token_hint=tokens["id_token"]))
        assert h.rueckweg(sichtbar["Location"])["error"] == "login_required"
        assert OAuthGrant.objects.count() == codes

        eigen = _angemeldet(person).get(
            "/oauth/authorize/", h.autorisierung(app, challenge, prompt="none", id_token_hint=tokens["id_token"])
        )
        assert h.rueckweg(eigen["Location"])["code"]

    def test_abgelaufen_und_bisheriger_schluessel_gelten(self, anbieter: None, person: User) -> None:
        abgelaufen = _hinweis(person.pk, h.rsa_schluessel(1), exp=int(time.time()) - 3600)
        assert hinted_subject(abgelaufen) == str(person.pk)
        assert hinted_subject(_hinweis(person.pk, h.rsa_schluessel(2))) == str(person.pk)

    @pytest.mark.parametrize("hinweis", ["kein-token", "a.b.c"])
    def test_nicht_pruefbar(self, anbieter: None, person: User, hinweis: str) -> None:
        assert hinted_subject(hinweis) is None

    def test_fremd_signiert_gibt_keinen_code(self, anbieter: None, person: User) -> None:
        app, _secret = h.anwendung(vertrauenswuerdig=True)
        fremd = _hinweis(person.pk, h.rsa_schluessel(3))
        assert hinted_subject(fremd) is None
        _verifier, challenge = h.pkce()
        antwort = _angemeldet(person).get(
            "/oauth/authorize/", h.autorisierung(app, challenge, prompt="none", id_token_hint=fremd)
        )
        assert h.rueckweg(antwort["Location"])["error"] == "login_required"


# ---------------------------------------------------------------------------
# Alle Sitzungen beenden, Passwortwechsel, Passwort zurücksetzen
# ---------------------------------------------------------------------------


class TestWiderrufMitDemKonto:
    def _widerrufen(self, app: OAuthApplication, secret: str, tokens: dict[str, Any], durch: str) -> None:
        with pytest.raises(access.InvalidAccessToken):
            access.authenticate(tokens["access_token"])
        assert _refresh(app, secret, tokens["refresh_token"]).status_code == 400
        eintraege = [e for e in _ereignisse(audit.EVENT_REVOKED) if e.details.get("anwendung") == app.client_id]
        assert [e.details for e in eintraege] == [{"anwendung": app.client_id, "durch": durch}]

    def test_alle_sitzungen_beenden(self, anbieter: None, person: User) -> None:
        app, secret, tokens = _code_tokens(person)
        zweite, zweites_secret, zweite_tokens = _code_tokens(person, name="Zweite")
        _verifier, challenge = h.pkce()
        h.code_holen(_angemeldet(person), app, challenge)  # offener Code

        SessionService.revoke_all_sessions(person, except_current="diese-sitzung")

        self._widerrufen(zweite, zweites_secret, zweite_tokens, audit.BY_PERSON)
        self._widerrufen(app, secret, tokens, audit.BY_PERSON)
        assert not OAuthGrant.objects.exists()

    def test_passwortwechsel(self, anbieter: None, person: User) -> None:
        app, secret, tokens = _code_tokens(person)
        assert PasswordService.change_password(person, PASSWORD, NEUES_PASSWORT)[0]
        self._widerrufen(app, secret, tokens, audit.BY_PASSWORD_CHANGE)

    def test_falsches_altes_passwort_widerruft_nichts(self, anbieter: None, person: User) -> None:
        _app, _secret, tokens = _code_tokens(person)
        assert not PasswordService.change_password(person, "falsch", NEUES_PASSWORT)[0]
        assert access.authenticate(tokens["access_token"]).user == person

    def test_passwort_zuruecksetzen(self, anbieter: None, person: User) -> None:
        app, secret, tokens = _code_tokens(person)
        uid = urlsafe_base64_encode(force_bytes(person.pk))
        client = Client()
        antwort = client.get(
            reverse(
                "accounts:password_reset_confirm",
                kwargs={"uidb64": uid, "token": default_token_generator.make_token(person)},
            )
        )
        client.post(antwort["Location"], {"new_password1": NEUES_PASSWORT, "new_password2": NEUES_PASSWORT})
        person.refresh_from_db()
        assert person.check_password(NEUES_PASSWORT)
        self._widerrufen(app, secret, tokens, audit.BY_PASSWORD_CHANGE)

    def test_andere_personen_behalten_ihren_zugang(self, anbieter: None, person: User) -> None:
        _app, _secret, tokens = _code_tokens(person)
        andere = make_user("andere@example.org")
        SessionService.revoke_all_sessions(andere)
        assert access.authenticate(tokens["access_token"]).user == person
