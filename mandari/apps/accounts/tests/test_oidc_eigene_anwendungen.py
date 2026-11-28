# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigene Oberflächen und Dienste (Issue #1591): Scopes für Session und Work, Anwendungen einer Kommune oder Fraktion,
Grenzen je Anwendung und Widerruf auf der Profilseite.

- Katalog: ``session:lesen``, ``work:lesen``, ``work:schreiben`` nur im Namen einer Person (Session schreibt mit
  ``sitzung:schreiben``, #1588);
  Scopes zum Schreiben sind gekennzeichnet.
- Anwendungen eines Bereichs (``owner``): nur Code-Ablauf, immer mit Zustimmung, nur die Scopes ihrer Art, keine
  Bindung und keine Schlüssel; Anlegen, Sperren, neues Secret und Löschen nur im eigenen Bereich, jeweils im
  Sicherheitsprotokoll, Sperren und Löschen widerrufen alle Tokens.
- Grenzen: Anfragen je Minute über alle Tokens der Anwendung (429 über :class:`access.RateLimited`), Einträge je Seite.
- Bei Ressourcen derselben Art gilt eine Anwendung nur für ihren Bereich (``bindings.allows``).
- Zustimmungsseite und „Verbundene Anwendungen“ nennen den Bereich; die Profilseite widerruft und führt zurück.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Iterator
from typing import Any

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.template import Context, Template
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import SecurityAuditLog, User
from apps.accounts.oidc import access, bindings, catalog, registration
from apps.accounts.oidc.models import OAuthAccessToken, OAuthApplication
from apps.accounts.tests import oidc_hilfen as h
from apps.accounts.tests.test_login_2fa import make_user
from apps.session.models import SessionTenant

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def anbieter() -> Iterator[None]:
    """Eingeschaltet mit Testschlüssel: Anwendungen im Code-Ablauf signieren ID-Tokens mit RS256."""
    with h.eingeschaltet():
        yield


@pytest.fixture
def kommune() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now())


@pytest.fixture
def bereich(kommune: SessionTenant) -> str:
    return bindings.compose(bindings.TENANT, str(kommune.pk))


@pytest.fixture
def person() -> User:
    return make_user("person@example.org")


def _registrieren(bereich: str, person: Any, **angaben: Any) -> registration.Registration:
    werte: dict[str, Any] = {
        "name": "Rats-App der Stadt",
        "confidential": True,
        "redirect_uris": [h.REDIRECT_URI],
        "scopes": [catalog.OPENID, catalog.RATSDATEN_LESEN, catalog.SESSION_LESEN],
        **angaben,
    }
    return registration.register(None, bereich, user=person, **werte)


def _protokoll(aktion: str) -> list[dict[str, Any]]:
    return [
        dict(eintrag.details)
        for eintrag in SecurityAuditLog.objects.filter(event="oidc_anwendung").order_by("created_at")
        if eintrag.details.get("aktion") == aktion
    ]


class TestKatalog:
    def test_scopes_fuer_session_und_work_nur_im_namen_einer_person(self) -> None:
        for name in (catalog.SESSION_LESEN, catalog.SITZUNG_SCHREIBEN, catalog.WORK_LESEN, catalog.WORK_SCHREIBEN):
            scope = catalog.get(name)
            assert scope is not None and scope.person and not scope.service, name
        assert catalog.allowed_for([catalog.SESSION_LESEN], person=False) == []

    def test_scopes_zum_schreiben(self) -> None:
        assert {catalog.SITZUNG_SCHREIBEN, catalog.WORK_SCHREIBEN, catalog.EINREICHUNG_SCHREIBEN} <= catalog.WRITES
        assert catalog.SESSION_LESEN not in catalog.WRITES and catalog.RATSDATEN_LESEN not in catalog.WRITES


class TestBereich:
    def test_anwendung_gilt_nur_bei_ihrem_bereich_derselben_art(self) -> None:
        eigener = bindings.compose(bindings.TENANT, "a")
        assert bindings.allows("", bindings.TENANT, "b")
        assert bindings.allows(eigener, bindings.TENANT, "a")
        assert not bindings.allows(eigener, bindings.TENANT, "b")
        assert bindings.allows(eigener, bindings.ORGANIZATION, "b")

    def test_kommune_und_organisation_melden_ihre_scopes_an(self, bereich: str) -> None:
        assert catalog.SESSION_LESEN in bindings.registrable_scopes(bereich)
        assert catalog.WORK_LESEN not in bindings.registrable_scopes(bereich)
        organisation = bindings.compose(bindings.ORGANIZATION, "00000000-0000-0000-0000-000000001591")
        assert catalog.WORK_LESEN in bindings.registrable_scopes(organisation)
        assert catalog.RATSDATEN_NICHTOEFFENTLICH not in bindings.registrable_scopes(organisation)
        assert bindings.registrable_scopes("unbekannt:1") == ()


class TestRegeln:
    @pytest.mark.parametrize(
        ("aenderung", "feld"),
        [
            ({"authorization_grant_type": OAuthApplication.GRANT_CLIENT_CREDENTIALS}, "authorization_grant_type"),
            ({"skip_authorization": True}, "skip_authorization"),
            ({"allowed_scopes": [catalog.OPENID, catalog.WORK_LESEN]}, "allowed_scopes"),
            ({"allowed_scopes": [catalog.RATSDATEN_NICHTOEFFENTLICH]}, "allowed_scopes"),
            ({"owner": "session-mandant:00000000-0000-0000-0000-000000000000"}, "owner"),
        ],
    )
    def test_anwendung_eines_bereichs(self, bereich: str, person: User, aenderung: dict[str, Any], feld: str) -> None:
        app = _registrieren(bereich, person).application
        for name, wert in aenderung.items():
            setattr(app, name, wert)
        with pytest.raises(ValidationError) as fehler:
            app.full_clean(exclude=["client_secret"])
        assert feld in fehler.value.message_dict

    def test_grenzen_haben_obergrenzen(self, bereich: str, person: User) -> None:
        app = _registrieren(bereich, person).application
        app.max_page_size = 0
        app.rate_limit_per_minute = 10**6
        with pytest.raises(ValidationError) as fehler:
            app.full_clean(exclude=["client_secret"])
        assert {"max_page_size", "rate_limit_per_minute"} <= set(fehler.value.message_dict)


class TestRegistrierung:
    def test_anlegen_secret_nur_jetzt_und_im_protokoll(self, bereich: str, person: User) -> None:
        neu = _registrieren(bereich, person)
        app = OAuthApplication.objects.get(pk=neu.application.pk)
        assert app.owner == bereich and app.acts_for_person and not app.skip_authorization
        assert neu.secret and neu.secret not in app.client_secret
        assert app.scope_list == [catalog.OPENID, catalog.RATSDATEN_LESEN, catalog.SESSION_LESEN]
        eintrag = _protokoll("angelegt")[-1]
        assert eintrag["bereich"] == bereich and eintrag["anwendung"] == app.client_id
        assert neu.secret not in str(eintrag)

    def test_oeffentliche_anwendung_ohne_secret(self, bereich: str, person: User) -> None:
        neu = _registrieren(bereich, person, confidential=False)
        assert neu.secret == ""
        assert neu.application.client_type == OAuthApplication.CLIENT_PUBLIC

    def test_regeln_als_meldungen(self, bereich: str, person: User) -> None:
        with pytest.raises(registration.RegistrationError) as fehler:
            _registrieren(bereich, person, name=" ", redirect_uris=["http://app.example.org/"], scopes=["work:lesen"])
        meldungen = " ".join(fehler.value.messages)
        assert "Namen" in meldungen and "https" in meldungen and "work:lesen" in meldungen
        assert not OAuthApplication.objects.exists()

    def test_sperren_widerruft_und_entsperren(self, anbieter: None, bereich: str, person: User) -> None:
        app = _registrieren(bereich, person).application
        raw = h.access_token(app, person, scope=catalog.SESSION_LESEN)
        assert registration.set_active(None, bereich, app.pk, user=person, active=False) is not None
        with pytest.raises(access.InvalidAccessToken):
            access.authenticate(raw)
        assert not OAuthAccessToken.objects.filter(application=app).exists()
        registration.set_active(None, bereich, app.pk, user=person, active=True)
        assert OAuthApplication.objects.get(pk=app.pk).is_active
        assert [eintrag["aktion"] for eintrag in _protokoll("gesperrt") + _protokoll("entsperrt")] == [
            "gesperrt",
            "entsperrt",
        ]

    def test_neues_secret_nur_fuer_vertrauliche(self, bereich: str, person: User) -> None:
        neu = _registrieren(bereich, person)
        erneuert = registration.new_secret(None, bereich, neu.application.pk, user=person)
        assert erneuert is not None and erneuert.secret != neu.secret
        oeffentlich = _registrieren(bereich, person, name="App", confidential=False).application
        assert registration.new_secret(None, bereich, oeffentlich.pk, user=person) is None

    def test_loeschen(self, anbieter: None, bereich: str, person: User) -> None:
        app = _registrieren(bereich, person).application
        raw = h.access_token(app, person, scope=catalog.SESSION_LESEN)
        assert registration.delete(None, bereich, app.pk, user=person) == "Rats-App der Stadt"
        assert not OAuthApplication.objects.filter(pk=app.pk).exists()
        with pytest.raises(access.InvalidAccessToken):
            access.authenticate(raw)
        assert _protokoll("gelöscht")[-1]["bereich"] == bereich

    def test_bereich_nimmt_scope_zurueck(self, bereich: str, person: User, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _registrieren(bereich, person).application
        art = bindings.kind_of(bereich)
        assert art is not None
        monkeypatch.setitem(bindings._KINDS, bindings.TENANT, dataclasses.replace(art, scopes=(catalog.OPENID,)))
        assert OAuthApplication.objects.get(pk=app.pk).scope_list == [catalog.OPENID]

    def test_fremder_bereich_erreicht_die_anwendung_nicht(self, bereich: str, person: User) -> None:
        app = _registrieren(bereich, person).application
        anderer = bindings.compose(bindings.TENANT, "00000000-0000-0000-0000-000000000000")
        assert registration.get_owned(anderer, app.pk) is None
        assert registration.set_active(None, anderer, app.pk, user=person, active=False) is None
        assert registration.delete(None, anderer, app.pk, user=person) is None
        assert registration.new_secret(None, anderer, app.pk, user=person) is None
        betreiber = h.anwendung(name="Betreiber")[0]
        assert registration.get_owned(bereich, betreiber.pk) is None
        assert OAuthApplication.objects.get(pk=app.pk).is_active


class TestGrenzen:
    def test_anfragen_je_minute_ueber_alle_tokens(
        self, anbieter: None, person: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fester Zeitpunkt: kein Wechsel der Minute zwischen den Abrufen
        monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
        app = h.anwendung()[0]
        app.rate_limit_per_minute = 2
        app.save()
        erstes = h.access_token(app, person)
        zweites = h.access_token(app, make_user("zweite@example.org"))
        access.authenticate(erstes)
        access.authenticate(zweites)
        with pytest.raises(access.RateLimited) as grenze:
            access.authenticate(erstes)
        assert grenze.value.retry_after == 60
        # Eine andere Anwendung zählt für sich
        andere = h.anwendung(name="Andere")[0]
        access.authenticate(h.access_token(andere, person))

    def test_ohne_grenze(self, anbieter: None, person: User) -> None:
        app = h.anwendung()[0]
        app.rate_limit_per_minute = 0
        app.save()
        raw = h.access_token(app, person)
        for _ in range(5):
            access.authenticate(raw)

    def test_eintraege_je_seite(self, anbieter: None, person: User) -> None:
        app = h.anwendung()[0]
        app.max_page_size = 25
        app.save()
        zugriff = access.authenticate(h.access_token(app, person))
        assert zugriff.page_size(200) == 25
        assert zugriff.page_size(10) == 10
        assert zugriff.page_size(0) == 1

    def test_bereich_im_zugriff(self, anbieter: None, bereich: str, kommune: SessionTenant, person: User) -> None:
        app = _registrieren(bereich, person).application
        zugriff = access.authenticate(h.access_token(app, person, scope=catalog.SESSION_LESEN))
        assert zugriff.owner == bereich
        assert zugriff.allows(bindings.TENANT, str(kommune.pk))
        assert not zugriff.allows(bindings.TENANT, "00000000-0000-0000-0000-000000000000")
        assert zugriff.has_any_scope((catalog.RATSDATEN_LESEN, catalog.SESSION_LESEN))


class TestPerson:
    def test_zustimmungsseite_nennt_den_bereich(self, anbieter: None, bereich: str, person: User) -> None:
        app = _registrieren(bereich, person).application
        client = Client()
        client.force_login(person)
        _verifier, challenge = h.pkce()
        seite = client.get("/oauth/authorize/", h.autorisierung(app, challenge))
        assert seite.status_code == 200
        assert "Registriert von" in seite.content.decode()
        assert "Stadt Musterstadt" in seite.content.decode()

    def test_profilseite_widerruft_und_fuehrt_zurueck(self, anbieter: None, bereich: str, person: User) -> None:
        app = _registrieren(bereich, person).application
        raw = h.access_token(app, person, scope=catalog.SESSION_LESEN)
        client = Client()
        client.force_login(person)
        antwort = client.post(
            reverse("accounts:connected_applications"),
            {"application_id": app.pk, "next": "/work/", "zurueck": "1"},
        )
        assert antwort.status_code == 302 and antwort["Location"] == "/work/"
        with pytest.raises(access.InvalidAccessToken):
            access.authenticate(raw)

    def test_liste_fuer_profilseiten(self, anbieter: None, bereich: str, person: User) -> None:
        app = _registrieren(bereich, person).application
        h.access_token(app, person, scope=catalog.SESSION_LESEN)
        request = RequestFactory().get("/work/")
        request.user = person
        html = Template('{% include "accounts/oidc/_verbundene_liste.html" %}').render(Context({"request": request}))
        assert "Rats-App der Stadt" in html and "Registriert von" in html and 'name="zurueck"' in html
