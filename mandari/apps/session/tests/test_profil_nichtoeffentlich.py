# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nichtöffentliche Ausgabe je Session-Mandant nach dem Profil Nichtöffentliches von kommParl (Issue #565).

Je Regel des Zugriffsmodells (abgestimmt am 05.11.2026) mindestens ein Test:

- Personen: nur mit Scope ``ratsdaten:nichtoeffentlich`` und Recht „API-Zugang“, nur was die Person in Session sieht
  (dieselben Selektoren, Rechte mit Geltungsbereich, befristete Vertretung, Mitwirkungsverbot samt der dort beratenen
  Vorlagen), bei jedem Abruf geprüft, zweiter Faktor
- Dienste: an genau einen Mandanten gebunden, ``private_key_jwt``, ``sitzung:lesen`` liest Nichtöffentliches des
  eigenen Mandanten, nie eines anderen
- Für beide: Prüfprotokoll (auch für Beratungen mit nichtöffentlicher Station und Verweise öffentlicher Objekte auf
  nichtöffentliche; ein Dienst nie unter dem Konto eines mitgeschickten Cookies), ``Cache-Control: private, no-store``,
  nichts im öffentlichen Feed und Snapshot, öffentliche Antworten unverändert, Konformität gegen die Schemas der
  Spezifikation

Die Regeln am Token-Endpunkt (private_key_jwt, sieben Tage, zweiter Faktor) prüft
``apps/accounts/tests/test_oidc_nichtoeffentlich.py``.
"""

from __future__ import annotations

import json
import re
import secrets
import time
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import Client, override_settings
from django.utils import timezone
from jwcrypto import jwk, jwt
from mandari_oparl.ids import canonical_id

from apps.accounts.models import User
from apps.accounts.oidc import bindings, catalog, client_assertion
from apps.accounts.oidc.models import OAuthApplication
from apps.accounts.tests import oidc_hilfen as h
from apps.accounts.tests.test_login_2fa import enable_2fa
from apps.common.models import IdentifierBase
from apps.common.tests.factories import UserFactory
from apps.session import audit
from apps.session.api import nichtoeffentlich
from apps.session.models import (
    SessionAgendaItem,
    SessionAuditLog,
    SessionConsultation,
    SessionFile,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionRole,
    SessionRoleAssignment,
    SessionTenant,
    SessionUser,
    SessionVote,
)
from apps.session.services import file_version_service
from hub.api.tests.ereignisse import ereignis
from hub.api.tests.konformitaet import pruefe, pruefe_liste, pruefe_profil

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example.org"
OEFFENTLICH = "/session/musterstadt/api/oparl/"
NOE = "/session/musterstadt/api/oparl/nichtoeffentlich/"
NOE_URL = f"{SITE}{NOE}"
METADATEN = "/.well-known/oauth-protected-resource/session/musterstadt/api/oparl/nichtoeffentlich/"

ALLE_NOE = {
    "can_view_meetings": True,
    "can_view_non_public_meetings": True,
    "can_view_papers": True,
    "can_view_non_public_papers": True,
    "can_access_api": True,
}
NUR_SEHEN = {"can_view_meetings": True, "can_view_papers": True, "can_access_api": True}


@pytest.fixture(autouse=True)
def eingeschaltet() -> Iterator[None]:
    cache.clear()
    IdentifierBase.objects.update_or_create(pk=1, defaults={"url": SITE})
    with h.eingeschaltet(
        OPARL_NONPUBLIC_ENABLED=True,
        SITE_URL=SITE,
        OPARL_API_RATE_LIMIT=0,
        OIDC_TOKEN_RATE_LIMIT=0,
        provider={"OIDC_ISS_ENDPOINT": SITE},
    ):
        yield
    cache.clear()


@pytest.fixture
def welt() -> dict[str, Any]:
    """Ein Mandant mit öffentlichen und nichtöffentlichen Sitzungen, Tagesordnungspunkten, Vorlagen und Anlagen."""
    tenant = SessionTenant.objects.create(
        name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now()
    )
    finanzen = SessionOrganization.objects.create(tenant=tenant, name="Finanzausschuss", organization_type="committee")
    bau = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss", organization_type="committee")
    jetzt = timezone.now()
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Finanzausschuss", organization=finanzen, start=jetzt, is_public=True, location="Rathaus"
    )
    oeffentlich = SessionAgendaItem.objects.create(
        meeting=sitzung, number="1", order=1, name="Haushalt", is_public=True
    )
    geheim = SessionAgendaItem.objects.create(
        meeting=sitzung, number="2", order=2, name="GRUNDSTUECK-GEHEIM", is_public=False
    )
    befangen = SessionAgendaItem.objects.create(
        meeting=sitzung, number="3", order=3, name="AUFTRAG-BEFANGEN", is_public=False
    )
    oeffentlich.resolution_text = "Der Ausschuss empfiehlt den Haushalt."
    cast(Any, oeffentlich).set_resolution_text_encrypted("KAUFPREIS-GEHEIM")
    oeffentlich.save()
    noe_sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="PERSONALSITZUNG", organization=finanzen, start=jetzt, is_public=False, location="Zimmer 1"
    )
    noe_top = SessionAgendaItem.objects.create(meeting=noe_sitzung, number="1", order=1, name="PERSONAL-TOP")
    bau_sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="BAU-GEHEIM", organization=bau, start=jetzt, is_public=False
    )
    vorlage = SessionPaper.objects.create(
        tenant=tenant, reference="V/1", name="Radweg", is_public=True, status="approved"
    )
    noe_vorlage = SessionPaper.objects.create(
        tenant=tenant, reference="V/2", name="VORLAGE-GEHEIM", is_public=False, status="approved"
    )
    entwurf = SessionPaper.objects.create(
        tenant=tenant, reference="V/3", name="VORLAGE-ENTWURF", is_public=False, status="draft"
    )
    anlage_geheim = _anlage(tenant, "gutachten.pdf", agenda_item=geheim, is_public=False)
    anlage_vorlage = _anlage(tenant, "anlage-geheim.pdf", paper=noe_vorlage, is_public=False)
    anlage_oeffentlich = _anlage(tenant, "oeffentlich.pdf", paper=vorlage, is_public=True)
    return {
        "tenant": tenant,
        "finanzen": finanzen,
        "bau": bau,
        "sitzung": sitzung,
        "oeffentlich": oeffentlich,
        "geheim": geheim,
        "befangen": befangen,
        "noe_sitzung": noe_sitzung,
        "noe_top": noe_top,
        "bau_sitzung": bau_sitzung,
        "vorlage": vorlage,
        "noe_vorlage": noe_vorlage,
        "entwurf": entwurf,
        "anlage_geheim": anlage_geheim,
        "anlage_vorlage": anlage_vorlage,
        "anlage_oeffentlich": anlage_oeffentlich,
    }


def _anlage(tenant: SessionTenant, name: str, *, is_public: bool, **bezug: Any) -> SessionFile:
    datei = SessionFile(tenant=tenant, name=name, mime_type="application/pdf", is_public=is_public, **bezug)
    file_version_service.attach_upload(
        datei, SimpleUploadedFile(name, b"%PDF-1.4 INHALT", content_type="application/pdf"), user=None
    )
    return datei


def konto(
    tenant: SessionTenant, rechte: dict[str, bool] | None = None, *, zwei_faktoren: bool = True, email: str = ""
) -> tuple[User, SessionUser]:
    user = cast(User, UserFactory(**({"email": email} if email else {})))  # type: ignore[no-untyped-call]
    session_user = SessionUser.objects.create(user=user, tenant=tenant, is_active=True)
    if rechte is not None:
        rolle = SessionRole.objects.create(tenant=tenant, name=f"Rolle {uuid.uuid4().hex[:8]}", **rechte)
        session_user.roles.add(rolle)
    if zwei_faktoren:
        enable_2fa(user)
    return user, session_user


def ratsapp(scopes: list[str] | None = None) -> OAuthApplication:
    return h.anwendung(name="Ratsapp", scopes=scopes or [catalog.OPENID, catalog.RATSDATEN_NICHTOEFFENTLICH])[0]


def token_fuer(
    user: User, *, app: OAuthApplication | None = None, scope: str = catalog.RATSDATEN_NICHTOEFFENTLICH
) -> str:
    return h.access_token(app or ratsapp(), user, scope=scope)


def abruf(pfad: str, raw: str | None = None, **parameter: Any) -> Any:
    headers = {"Authorization": f"Bearer {raw}"} if raw else {}
    return Client().get(pfad, parameter, headers=headers)


def namen(antwort: Any) -> set[str]:
    return {str(objekt.get("name")) for objekt in antwort.json()["data"]}


def schluessel(kid: str = "dienst-1") -> jwk.JWK:
    return jwk.JWK.generate(kty="EC", crv="P-256", kid=kid)


def jwks_von(key: jwk.JWK) -> dict[str, Any]:
    return {"keys": [json.loads(key.export_public())]}


def dienst(tenant: SessionTenant, key: jwk.JWK, *, scopes: list[str] | None = None) -> OAuthApplication:
    """Dienst mit Bindung an ``tenant`` und öffentlichem Schlüssel; angelegt durch dieselben Regeln wie im Admin."""
    app = OAuthApplication(
        name=f"Fachverfahren {tenant.slug}",
        client_type=OAuthApplication.CLIENT_CONFIDENTIAL,
        authorization_grant_type=OAuthApplication.GRANT_CLIENT_CREDENTIALS,
        algorithm=OAuthApplication.NO_ALGORITHM,
        allowed_scopes=scopes or [catalog.SITZUNG_LESEN],
        binding=bindings.compose(bindings.TENANT, str(tenant.pk)),
        jwks=jwks_von(key),
        client_secret="",
    )
    app.full_clean(exclude=["client_id", "client_secret"])
    app.save()
    return app


def assertion(app: OAuthApplication, key: jwk.JWK, **angaben: Any) -> str:
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
    token = jwt.JWT(header={"alg": "ES256", "kid": key["kid"]}, claims=claims)
    token.make_signed_token(key)
    return str(token.serialize())


def dienst_token(app: OAuthApplication, key: jwk.JWK, scope: str = catalog.SITZUNG_LESEN) -> str:
    antwort = Client().post(
        "/oauth/token/",
        {
            "grant_type": "client_credentials",
            "scope": scope,
            "client_assertion_type": client_assertion.ASSERTION_TYPE,
            "client_assertion": assertion(app, key),
        },
    )
    assert antwort.status_code == 200, antwort.content
    return str(antwort.json()["access_token"])


def privat(antwort: Any) -> None:
    """Kopfzeilen jeder Antwort der nichtöffentlichen Ausgabe (kommParl, Profil Nichtöffentliches)."""
    assert antwort["Cache-Control"] == "private, no-store"
    assert "Authorization" in antwort["Vary"]
    assert "Access-Control-Allow-Origin" not in antwort
    assert "ETag" not in antwort


def protokoll(tenant: SessionTenant) -> list[SessionAuditLog]:
    return [
        eintrag
        for eintrag in SessionAuditLog.objects.filter(tenant=tenant).order_by("created_at")
        if eintrag.changes.get("zugriff") == audit.API_READ_NONPUBLIC
    ]


def kennung(art: str, pk: Any) -> str:
    return f"{NOE_URL}{art}/{pk}/"


# =============================================================================
# Personen
# =============================================================================


class TestPersonMitRecht:
    def test_sieht_nichtoeffentliches_gekennzeichnet(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], ALLE_NOE)
        raw = token_fuer(user)
        sitzungen = abruf(f"{NOE}meetings/", raw)
        assert sitzungen.status_code == 200, sitzungen.content
        privat(sitzungen)
        assert namen(sitzungen) == {"Finanzausschuss", "PERSONALSITZUNG", "BAU-GEHEIM"}
        sitzung = abruf(f"{NOE}meeting/{welt['sitzung'].pk}/", raw).json()
        tops = {top["name"]: top for top in sitzung["agendaItem"]}
        assert set(tops) == {"Haushalt", "GRUNDSTUECK-GEHEIM", "AUFTRAG-BEFANGEN"}
        assert tops["GRUNDSTUECK-GEHEIM"]["public"] is False
        assert tops["GRUNDSTUECK-GEHEIM"]["kommparl:nonPublic"] is True
        assert "kommparl:publicId" not in tops["GRUNDSTUECK-GEHEIM"]
        assert tops["Haushalt"]["public"] is True
        assert "kommparl:nonPublic" not in tops["Haushalt"]
        assert tops["Haushalt"]["kommparl:publicId"] == f"{SITE}{OEFFENTLICH}agendaitem/{welt['oeffentlich'].pk}/"
        # Nichtöffentlicher Teil des Beschlusstexts in eigener Eigenschaft, der öffentliche bleibt in resolutionText
        assert tops["Haushalt"]["kommparl:nonPublicResolutionText"] == "KAUFPREIS-GEHEIM"
        assert tops["Haushalt"]["resolutionText"] == "Der Ausschuss empfiehlt den Haushalt."
        anlagen = tops["GRUNDSTUECK-GEHEIM"]["auxiliaryFile"]
        assert [anlage["kommparl:nonPublic"] for anlage in anlagen] == [True]
        assert anlagen[0]["accessUrl"].startswith(NOE_URL)
        # Eigene Kennungen; die öffentliche steht nur in kommparl:publicId
        assert sitzung["id"] == kennung("meeting", welt["sitzung"].pk)
        assert sitzung["kommparl:publicId"] == f"{SITE}{OEFFENTLICH}meeting/{welt['sitzung'].pk}/"

    def test_vorlagen_ohne_arbeitsstaende(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], ALLE_NOE)
        vorlagen = abruf(f"{NOE}papers/", token_fuer(user))
        assert namen(vorlagen) == {"Radweg", "VORLAGE-GEHEIM"}
        geheim = next(v for v in vorlagen.json()["data"] if v["name"] == "VORLAGE-GEHEIM")
        assert geheim["kommparl:nonPublic"] is True
        assert geheim["mainFile"]["kommparl:nonPublic"] is True
        assert abruf(f"{NOE}paper/{welt['entwurf'].pk}/", token_fuer(user)).status_code == 404

    def test_datei_nichtoeffentlich_abrufbar_mit_kopfzeilen(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], ALLE_NOE)
        antwort = abruf(f"{NOE}file/{welt['anlage_geheim'].pk}/download/", token_fuer(user))
        assert antwort.status_code == 200
        assert b"".join(cast(Any, antwort).streaming_content) == b"%PDF-1.4 INHALT"
        privat(antwort)

    def test_rechte_bei_jedem_abruf(self, welt: dict[str, Any]) -> None:
        user, session_user = konto(welt["tenant"], ALLE_NOE)
        raw = token_fuer(user)
        assert abruf(f"{NOE}meeting/{welt['noe_sitzung'].pk}/", raw).status_code == 200
        session_user.roles.update(can_view_non_public_meetings=False)
        assert abruf(f"{NOE}meeting/{welt['noe_sitzung'].pk}/", raw).status_code == 404
        session_user.roles.clear()
        assert abruf(f"{NOE}meeting/{welt['noe_sitzung'].pk}/", raw).status_code == 403


class TestPersonOhneRecht:
    def test_nur_oeffentliches_und_404_am_nichtoeffentlichen(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], NUR_SEHEN)
        raw = token_fuer(user)
        assert namen(abruf(f"{NOE}meetings/", raw)) == {"Finanzausschuss"}
        sitzung = abruf(f"{NOE}meeting/{welt['sitzung'].pk}/", raw).json()
        assert [top["name"] for top in sitzung["agendaItem"]] == ["Haushalt"]
        assert "kommparl:nonPublicResolutionText" not in sitzung["agendaItem"][0]
        for pfad in (
            f"meeting/{welt['noe_sitzung'].pk}/",
            f"agendaitem/{welt['geheim'].pk}/",
            f"paper/{welt['noe_vorlage'].pk}/",
            f"file/{welt['anlage_geheim'].pk}/",
            f"file/{welt['anlage_geheim'].pk}/download/",
        ):
            antwort = abruf(f"{NOE}{pfad}", raw)
            assert antwort.status_code == 404, pfad
            assert antwort["Content-Type"].startswith("application/problem+json")
            privat(antwort)
            assert pruefe_profil(antwort.json(), "nonpublic", "NonPublicProblem") == []
        assert protokoll(welt["tenant"]) == []

    def test_ohne_konto_im_mandanten_403(self, welt: dict[str, Any]) -> None:
        user = cast(User, UserFactory())  # type: ignore[no-untyped-call]
        enable_2fa(user)
        antwort = abruf(f"{NOE}meetings/", token_fuer(user))
        assert antwort.status_code == 403
        privat(antwort)

    def test_ohne_api_zugang_403(self, welt: dict[str, Any]) -> None:
        # Der Mandant nimmt der Rolle den API-Zugang: auch mit NÖ-Recht und freigegebener Anwendung keine Daten
        user, session_user = konto(welt["tenant"], {**ALLE_NOE, "can_access_api": False})
        raw = token_fuer(user)
        pfade = ("", "meetings/", f"meeting/{welt['noe_sitzung'].pk}/", f"file/{welt['anlage_geheim'].pk}/download/")
        for pfad in pfade:
            antwort = abruf(f"{NOE}{pfad}", raw)
            assert antwort.status_code == 403, pfad
            assert b"PERSONAL" not in antwort.content
            privat(antwort)
            assert pruefe_profil(antwort.json(), "nonpublic", "NonPublicProblem") == []
        # Bekommt die Rolle den API-Zugang zurück, gilt das ab dem nächsten Abruf
        session_user.roles.update(can_access_api=True)
        assert abruf(f"{NOE}meeting/{welt['noe_sitzung'].pk}/", raw).status_code == 200
        assert len(protokoll(welt["tenant"])) == 1


class TestGeltungsbereichUndVertretung:
    def _rolle(self, tenant: SessionTenant) -> SessionRole:
        return SessionRole.objects.create(tenant=tenant, name=f"NÖ {uuid.uuid4().hex[:6]}", **ALLE_NOE)

    def test_recht_nur_fuer_ein_gremium(self, welt: dict[str, Any]) -> None:
        tenant = welt["tenant"]
        tenant.scoped_permissions_enabled = True
        tenant.save()
        user, session_user = konto(tenant, NUR_SEHEN)
        SessionRoleAssignment.objects.create(
            tenant=tenant,
            user=session_user,
            role=self._rolle(tenant),
            scope_type=SessionRoleAssignment.SCOPE_ORGANIZATION,
            scope_id=welt["finanzen"].pk,
        )
        raw = token_fuer(user)
        assert namen(abruf(f"{NOE}meetings/", raw)) == {"Finanzausschuss", "PERSONALSITZUNG"}
        assert abruf(f"{NOE}meeting/{welt['bau_sitzung'].pk}/", raw).status_code == 404

    @pytest.mark.parametrize(("bis", "sieht"), [(-1, False), (1, True)])
    def test_vertretung_befristet(self, welt: dict[str, Any], bis: int, sieht: bool) -> None:
        tenant = welt["tenant"]
        tenant.scoped_permissions_enabled = True
        tenant.save()
        user, session_user = konto(tenant, NUR_SEHEN)
        heute = timezone.localdate()
        SessionRoleAssignment.objects.create(
            tenant=tenant,
            user=session_user,
            role=self._rolle(tenant),
            valid_from=heute - timedelta(days=10),
            valid_until=heute + timedelta(days=bis),
            source=SessionRoleAssignment.SOURCE_DELEGATION,
        )
        status = abruf(f"{NOE}meeting/{welt['noe_sitzung'].pk}/", token_fuer(user)).status_code
        assert status == (200 if sieht else 404)

    def test_ohne_schalter_wirken_befristete_zuweisungen_nicht(self, welt: dict[str, Any]) -> None:
        tenant = welt["tenant"]
        user, session_user = konto(tenant, NUR_SEHEN)
        SessionRoleAssignment.objects.create(
            tenant=tenant,
            user=session_user,
            role=self._rolle(tenant),
            valid_from=date.today() - timedelta(days=1),
            valid_until=date.today() + timedelta(days=1),
        )
        assert abruf(f"{NOE}meeting/{welt['noe_sitzung'].pk}/", token_fuer(user)).status_code == 404


class TestMitwirkungsverbot:
    def test_sperrt_top_und_anlagen(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], ALLE_NOE, email="rat@example.org")
        person = SessionPerson.objects.create(
            tenant=welt["tenant"], given_name="Rita", family_name="Rat", email="RAT@example.org"
        )
        SessionVote.objects.create(agenda_item=welt["befangen"], person=person, vote="excluded")
        _anlage(welt["tenant"], "vergabe.pdf", agenda_item=welt["befangen"], is_public=False)
        raw = token_fuer(user)
        sitzung = abruf(f"{NOE}meeting/{welt['sitzung'].pk}/", raw).json()
        assert {top["name"] for top in sitzung["agendaItem"]} == {"Haushalt", "GRUNDSTUECK-GEHEIM"}
        assert abruf(f"{NOE}agendaitem/{welt['befangen'].pk}/", raw).status_code == 404
        dateien = abruf(f"{NOE}files/", raw).json()["data"]
        assert "vergabe.pdf" not in {datei["name"] for datei in dateien}
        assert "AUFTRAG-BEFANGEN" not in abruf(f"{NOE}agendaitems/", raw).content.decode()

    def test_sperrt_auch_die_dort_beratenen_vorlagen_und_ihre_anlagen(self, welt: dict[str, Any]) -> None:
        # Die befangene Person hat das Recht auf nichtöffentliche Vorlagen; die am gesperrten TOP beratene
        # nichtöffentliche Vorlage und ihr Gutachten bleiben ihr trotzdem verborgen (kommParl 7-04: „Unterlagen“)
        tenant = welt["tenant"]
        user, _ = konto(tenant, ALLE_NOE, email="rat@example.org")
        person = SessionPerson.objects.create(
            tenant=tenant, given_name="Rita", family_name="Rat", email="rat@example.org"
        )
        SessionVote.objects.create(agenda_item=welt["befangen"], person=person, vote="excluded")
        SessionConsultation.objects.create(
            paper=welt["noe_vorlage"],
            organization=welt["finanzen"],
            meeting=welt["sitzung"],
            agenda_item=welt["befangen"],
        )
        direkt = SessionPaper.objects.create(
            tenant=tenant, reference="V/4", name="VORLAGE-AM-TOP", is_public=False, status="approved"
        )
        welt["befangen"].paper = direkt
        welt["befangen"].save()
        gutachten = _anlage(tenant, "wertgutachten.pdf", paper=direkt, is_public=False)
        raw = token_fuer(user)
        for pfad in (
            f"paper/{welt['noe_vorlage'].pk}/",
            f"file/{welt['anlage_vorlage'].pk}/",
            f"file/{welt['anlage_vorlage'].pk}/download/",
            f"paper/{direkt.pk}/",
            f"file/{gutachten.pk}/download/",
        ):
            assert abruf(f"{NOE}{pfad}", raw).status_code == 404, pfad
        assert namen(abruf(f"{NOE}papers/", raw)) == {"Radweg"}
        dateien = {datei["name"] for datei in abruf(f"{NOE}files/", raw).json()["data"]}
        assert {"anlage-geheim.pdf", "wertgutachten.pdf"}.isdisjoint(dateien)
        assert "oeffentlich.pdf" in dateien
        assert abruf(f"{NOE}consultations/", raw).json()["data"] == []
        # Wer nicht befangen ist, sieht beide Vorlagen
        andere, _ = konto(tenant, ALLE_NOE)
        assert namen(abruf(f"{NOE}papers/", token_fuer(andere))) == {"Radweg", "VORLAGE-GEHEIM", "VORLAGE-AM-TOP"}

    def test_oeffentliche_vorlage_am_gesperrten_top_bleibt_ohne_nichtoeffentliches(self, welt: dict[str, Any]) -> None:
        tenant = welt["tenant"]
        user, _ = konto(tenant, ALLE_NOE, email="rat@example.org")
        person = SessionPerson.objects.create(
            tenant=tenant, given_name="Rita", family_name="Rat", email="rat@example.org"
        )
        SessionVote.objects.create(agenda_item=welt["befangen"], person=person, vote="excluded")
        SessionConsultation.objects.create(
            paper=welt["vorlage"], organization=welt["finanzen"], meeting=welt["sitzung"], agenda_item=welt["befangen"]
        )
        intern = _anlage(tenant, "intern.pdf", paper=welt["vorlage"], is_public=False)
        raw = token_fuer(user)
        vorlage = abruf(f"{NOE}paper/{welt['vorlage'].pk}/", raw).json()
        assert "kommparl:nonPublic" not in vorlage
        assert "intern.pdf" not in json.dumps(vorlage)
        assert abruf(f"{NOE}file/{intern.pk}/download/", raw).status_code == 404
        (beratung,) = vorlage["consultation"]
        assert {"agendaItem", "organization", "role"}.isdisjoint(beratung)


# =============================================================================
# Scopes und Anwendungen
# =============================================================================


class TestScope:
    def test_scope_fehlt(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], ALLE_NOE)
        app = ratsapp([catalog.OPENID, catalog.RATSDATEN_LESEN, catalog.RATSDATEN_NICHTOEFFENTLICH])
        antwort = abruf(f"{NOE}meetings/", token_fuer(user, app=app, scope=catalog.RATSDATEN_LESEN))
        assert antwort.status_code == 403
        assert 'error="insufficient_scope"' in antwort["WWW-Authenticate"]
        assert 'scope="ratsdaten:nichtoeffentlich"' in antwort["WWW-Authenticate"]
        assert f'resource_metadata="{SITE}{METADATEN}"' in antwort["WWW-Authenticate"]
        privat(antwort)
        assert pruefe_profil(antwort.json(), "nonpublic", "NonPublicProblem") == []

    def test_anwendung_ohne_freigabe_des_scopes(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], ALLE_NOE)
        app = ratsapp([catalog.OPENID, catalog.RATSDATEN_LESEN])
        # Das Token nennt den Scope, die Anwendung darf ihn nicht (mehr): er gilt nicht
        antwort = abruf(f"{NOE}meetings/", token_fuer(user, app=app))
        assert antwort.status_code == 403

    def test_ohne_zweiten_faktor_gilt_der_scope_nicht(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], ALLE_NOE, zwei_faktoren=False)
        assert abruf(f"{NOE}meetings/", token_fuer(user)).status_code == 403

    def test_ohne_token_401_mit_metadaten(self, welt: dict[str, Any]) -> None:
        antwort = abruf(f"{NOE}meetings/")
        assert antwort.status_code == 401
        assert antwort["WWW-Authenticate"] == f'Bearer resource_metadata="{SITE}{METADATEN}"'
        privat(antwort)
        assert pruefe_profil(antwort.json(), "nonpublic", "NonPublicProblem") == []

    @pytest.mark.parametrize("wert", ["Bearer mandari_at_falsch", "Basic abc", "Bearer " + "x" * 64])
    def test_ungueltiges_token_401(self, welt: dict[str, Any], wert: str) -> None:
        antwort = Client().get(f"{NOE}meetings/", headers={"Authorization": wert})
        assert antwort.status_code == 401
        assert antwort["WWW-Authenticate"].startswith('Bearer error="invalid_token"')

    def test_token_in_der_adresse_gilt_nicht(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], ALLE_NOE)
        assert abruf(f"{NOE}meetings/", access_token=token_fuer(user)).status_code == 401


# =============================================================================
# Dienste je Mandant
# =============================================================================


class TestDienst:
    def test_liest_alles_des_eigenen_mandanten(self, welt: dict[str, Any]) -> None:
        key = schluessel()
        raw = dienst_token(dienst(welt["tenant"], key), key)
        sitzungen = abruf(f"{NOE}meetings/", raw)
        assert sitzungen.status_code == 200
        assert namen(sitzungen) == {"Finanzausschuss", "PERSONALSITZUNG", "BAU-GEHEIM"}
        assert namen(abruf(f"{NOE}papers/", raw)) == {"Radweg", "VORLAGE-GEHEIM"}

    def test_nie_ein_anderer_mandant(self, welt: dict[str, Any]) -> None:
        anderer = SessionTenant.objects.create(name="Nachbarstadt", slug="nachbarstadt")
        key = schluessel()
        raw = dienst_token(dienst(anderer, key), key)
        antwort = abruf(f"{NOE}meetings/", raw)
        assert antwort.status_code == 403
        privat(antwort)
        assert abruf("/session/nachbarstadt/api/oparl/nichtoeffentlich/meetings/", raw).status_code == 200

    def test_ohne_bindung_kein_zugriff(self, welt: dict[str, Any]) -> None:
        # Am Formular vorbei gespeichert: Ohne Bindung gilt sitzung:lesen trotzdem nicht
        app = OAuthApplication(
            name="Ungebunden",
            client_type=OAuthApplication.CLIENT_CONFIDENTIAL,
            authorization_grant_type=OAuthApplication.GRANT_CLIENT_CREDENTIALS,
            algorithm=OAuthApplication.NO_ALGORITHM,
            allowed_scopes=[catalog.SITZUNG_LESEN],
            client_secret="s" * 40,
        )
        app.save()
        antwort = abruf(f"{NOE}meetings/", h.access_token(app, None, scope=catalog.SITZUNG_LESEN))
        assert antwort.status_code == 403

    def test_ratsdaten_lesen_reicht_nicht(self, welt: dict[str, Any]) -> None:
        key = schluessel()
        app = dienst(welt["tenant"], key, scopes=[catalog.SITZUNG_LESEN, catalog.RATSDATEN_LESEN])
        antwort = abruf(f"{NOE}meetings/", dienst_token(app, key, scope=catalog.RATSDATEN_LESEN))
        assert antwort.status_code == 403
        assert 'scope="sitzung:lesen"' in antwort["WWW-Authenticate"]

    def test_bindung_verlangt_private_key_jwt(self, welt: dict[str, Any]) -> None:
        app = OAuthApplication(
            name="Mit Secret",
            client_type=OAuthApplication.CLIENT_CONFIDENTIAL,
            authorization_grant_type=OAuthApplication.GRANT_CLIENT_CREDENTIALS,
            algorithm=OAuthApplication.NO_ALGORITHM,
            allowed_scopes=[catalog.SITZUNG_LESEN],
            binding=bindings.compose(bindings.TENANT, str(welt["tenant"].pk)),
            client_secret="s" * 40,
        )
        with pytest.raises(Exception, match="private_key_jwt"):
            app.full_clean(exclude=["client_id"])


class TestBefehl:
    def test_dienst_mit_mandant_und_schluessel_anlegen(self, welt: dict[str, Any], tmp_path: Path) -> None:
        key = schluessel()
        datei = tmp_path / "jwks.json"
        datei.write_text(json.dumps(jwks_von(key)), encoding="utf-8")
        ausgabe = StringIO()
        call_command(
            "oidc_anwendung",
            "anlegen",
            "--name",
            "Fachverfahren",
            "--typ",
            "vertraulich",
            "--grant",
            "client-credentials",
            "--mandant",
            "musterstadt",
            "--jwks",
            str(datei),
            "--scope",
            catalog.SITZUNG_LESEN,
            stdout=ausgabe,
        )
        app = OAuthApplication.objects.get(name="Fachverfahren")
        assert app.binding == f"session-mandant:{welt['tenant'].pk}"
        assert app.uses_private_key_jwt and app.scope_list == [catalog.SITZUNG_LESEN]
        assert "Client-Secret: keins" in ausgabe.getvalue()
        assert "Session-Mandant Stadt Musterstadt" in ausgabe.getvalue()
        raw = dienst_token(app, key)
        assert namen(abruf(f"{NOE}meetings/", raw)) == {"Finanzausschuss", "PERSONALSITZUNG", "BAU-GEHEIM"}

        neu = schluessel("dienst-2")
        datei.write_text(json.dumps(jwks_von(neu)), encoding="utf-8")
        call_command("oidc_anwendung", "schluessel", app.client_id, "--jwks", str(datei), stdout=StringIO())
        dienst_token(OAuthApplication.objects.get(pk=app.pk), neu)

    def test_mandant_unbekannt_oder_ohne_schluessel(self, welt: dict[str, Any], tmp_path: Path) -> None:
        basis = ["oidc_anwendung", "anlegen", "--name", "X", "--typ", "vertraulich", "--grant", "client-credentials"]
        with pytest.raises(CommandError, match="Kurznamen"):
            call_command(*basis, "--mandant", "gibtsnicht", "--scope", catalog.SITZUNG_LESEN, stdout=StringIO())
        with pytest.raises(CommandError, match="--jwks"):
            call_command(*basis, "--mandant", "musterstadt", "--scope", catalog.SITZUNG_LESEN, stdout=StringIO())
        privat_teil = tmp_path / "privat.json"
        privat_teil.write_text(json.dumps({"keys": [json.loads(schluessel().export_private())]}), encoding="utf-8")
        with pytest.raises(CommandError, match="private Teile"):
            call_command(
                *basis,
                "--mandant",
                "musterstadt",
                "--jwks",
                str(privat_teil),
                "--scope",
                catalog.SITZUNG_LESEN,
                stdout=StringIO(),
            )


# =============================================================================
# Prüfprotokoll
# =============================================================================


class TestPruefprotokoll:
    def test_person_anwendung_und_objekte(self, welt: dict[str, Any]) -> None:
        user, session_user = konto(welt["tenant"], ALLE_NOE)
        app = ratsapp()
        abruf(f"{NOE}meeting/{welt['noe_sitzung'].pk}/", token_fuer(user, app=app))
        eintraege = protokoll(welt["tenant"])
        assert len(eintraege) == 1
        eintrag = eintraege[0]
        assert eintrag.user_id == session_user.pk
        assert eintrag.action == "view"
        assert eintrag.changes["anwendung"] == app.client_id
        assert eintrag.changes["handelnd"] == "Person"
        assert set(eintrag.changes["kennungen"]) == {
            kennung("meeting", welt["noe_sitzung"].pk),
            kennung("agendaitem", welt["noe_top"].pk),
            kennung("location", welt["noe_sitzung"].pk),
        }
        assert "PERSONAL" not in json.dumps(eintrag.changes)

    def test_dienst_und_jeder_abruf(self, welt: dict[str, Any]) -> None:
        key = schluessel()
        app = dienst(welt["tenant"], key)
        raw = dienst_token(app, key)
        abruf(f"{NOE}papers/", raw)
        abruf(f"{NOE}papers/", raw)
        abruf(f"{NOE}file/{welt['anlage_vorlage'].pk}/download/", raw)
        eintraege = protokoll(welt["tenant"])
        assert [eintrag.action for eintrag in eintraege] == ["view", "view", "download"]
        assert all(eintrag.user_id is None and eintrag.changes["handelnd"] == "Dienst" for eintrag in eintraege)
        assert all(eintrag.changes["anwendung"] == app.client_id for eintrag in eintraege)
        assert kennung("paper", welt["noe_vorlage"].pk) in eintraege[0].changes["kennungen"]
        assert eintraege[2].changes["kennungen"] == [kennung("file", welt["anlage_vorlage"].pk)]

    def test_dienst_mit_cookie_einer_person_bleibt_dienst(self, welt: dict[str, Any]) -> None:
        key = schluessel()
        raw = dienst_token(dienst(welt["tenant"], key), key)
        person, _ = konto(welt["tenant"], ALLE_NOE)
        browser = Client()
        browser.force_login(person)
        antwort = browser.get(f"{NOE}meeting/{welt['noe_sitzung'].pk}/", headers={"Authorization": f"Bearer {raw}"})
        assert antwort.status_code == 200
        (eintrag,) = protokoll(welt["tenant"])
        assert eintrag.user_id is None
        assert eintrag.on_behalf_of_id is None
        assert eintrag.changes["handelnd"] == "Dienst"

    def test_beratung_mit_nichtoeffentlicher_station(self, welt: dict[str, Any]) -> None:
        # Veröffentlichte Vorlage, beraten in einer nichtöffentlichen Sitzung: Gremium, Rolle und Sitzung sind
        # nichtöffentlich; die Beratung ist gekennzeichnet und steht im Protokoll, auch eingebettet in der Vorlage
        beratung = SessionConsultation.objects.create(
            paper=welt["vorlage"], organization=welt["finanzen"], meeting=welt["noe_sitzung"], role="decision"
        )
        user, _ = konto(welt["tenant"], ALLE_NOE)
        raw = token_fuer(user)
        objekt = abruf(f"{NOE}consultation/{beratung.pk}/", raw).json()
        assert objekt["kommparl:nonPublic"] is True
        assert objekt["meeting"] == kennung("meeting", welt["noe_sitzung"].pk)
        assert objekt["kommparl:publicId"] == f"{SITE}{OEFFENTLICH}consultation/{beratung.pk}/"
        assert pruefe(objekt, "Consultation") == []
        abruf(f"{NOE}consultations/", raw)
        vorlage = abruf(f"{NOE}paper/{welt['vorlage'].pk}/", raw).json()
        assert "kommparl:nonPublic" not in vorlage
        assert vorlage["consultation"][0]["kommparl:nonPublic"] is True
        eintraege = protokoll(welt["tenant"])
        assert [eintrag.changes["abruf"] for eintrag in eintraege] == ["consultation", "Liste consultations", "paper"]
        assert all(kennung("consultation", beratung.pk) in eintrag.changes["kennungen"] for eintrag in eintraege)
        # Öffentlich bleibt nur, dass die Vorlage dort beraten wird
        oeffentlich = Client().get(f"{OEFFENTLICH}consultation/{beratung.pk}/").json()
        assert {"organization", "meeting", "role"}.isdisjoint(oeffentlich)

    def test_ohne_sicht_auf_die_station_wie_oeffentlich(self, welt: dict[str, Any]) -> None:
        beratung = SessionConsultation.objects.create(
            paper=welt["vorlage"], organization=welt["finanzen"], meeting=welt["noe_sitzung"], role="decision"
        )
        user, _ = konto(welt["tenant"], NUR_SEHEN)
        objekt = abruf(f"{NOE}consultation/{beratung.pk}/", token_fuer(user)).json()
        assert "kommparl:nonPublic" not in objekt
        assert {"organization", "meeting", "role"}.isdisjoint(objekt)
        assert protokoll(welt["tenant"]) == []

    def test_oeffentlicher_top_verweist_auf_nichtoeffentliche_beratung(self, welt: dict[str, Any]) -> None:
        beratung = SessionConsultation.objects.create(
            paper=welt["noe_vorlage"],
            organization=welt["finanzen"],
            meeting=welt["sitzung"],
            agenda_item=welt["oeffentlich"],
        )
        # Nur Vorlagen-Recht: Der öffentliche TOP selbst enthält nichts Nichtöffentliches außer dem Verweis
        user, _ = konto(welt["tenant"], {**ALLE_NOE, "can_view_non_public_meetings": False})
        top = abruf(f"{NOE}agendaitem/{welt['oeffentlich'].pk}/", token_fuer(user)).json()
        assert "kommparl:nonPublic" not in top
        assert "kommparl:nonPublicResolutionText" not in top
        assert top["consultation"] == kennung("consultation", beratung.pk)
        (eintrag,) = protokoll(welt["tenant"])
        assert eintrag.changes["kennungen"] == [kennung("consultation", beratung.pk)]

    def test_nur_oeffentliches_ohne_eintrag(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], ALLE_NOE)
        abruf(f"{NOE}organizations/", token_fuer(user))
        abruf(f"{NOE}file/{welt['anlage_oeffentlich'].pk}/download/", token_fuer(user))
        assert protokoll(welt["tenant"]) == []

    def test_ohne_eintrag_keine_inhalte(self, welt: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        def kaputt(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Datenbank weg")

        monkeypatch.setattr(audit, "log_nonpublic_read", kaputt)
        user, _ = konto(welt["tenant"], ALLE_NOE)
        antwort = abruf(f"{NOE}meeting/{welt['noe_sitzung'].pk}/", token_fuer(user))
        assert antwort.status_code == 503
        assert b"PERSONAL" not in antwort.content
        privat(antwort)
        assert pruefe_profil(antwort.json(), "nonpublic", "NonPublicProblem") == []


# =============================================================================
# Öffentliche Ausgabe, Feed und Snapshot
# =============================================================================


OEFFENTLICHE_PFADE = (
    "",
    "bodies/",
    "body/",
    "organizations/",
    "people/",
    "memberships/",
    "meetings/",
    "agendaitems/",
    "papers/",
    "consultations/",
    "files/",
    "legislativeterms/",
)


#: Kopfzeilen, die eine Antwort der Schnittstelle ausmachen
KOPFZEILEN = {
    "Content-Type",
    "Cache-Control",
    "ETag",
    "Vary",
    "Link",
    "Access-Control-Allow-Origin",
    "Access-Control-Expose-Headers",
    "Snapshot-Cursor",
    "Deprecation",
    "Sunset",
    "WWW-Authenticate",
}


class TestOeffentlicheAusgabe:
    def _alle(self, welt: dict[str, Any], **headers: str) -> dict[str, bytes]:
        pfade = [
            *OEFFENTLICHE_PFADE,
            f"meeting/{welt['sitzung'].pk}/",
            f"agendaitem/{welt['oeffentlich'].pk}/",
            f"paper/{welt['vorlage'].pk}/",
            f"file/{welt['anlage_oeffentlich'].pk}/",
            f"meeting/{welt['noe_sitzung'].pk}/",
            "body/kommparl/changes/",
            "body/kommparl/snapshot/",
        ]
        ergebnis = {}
        for pfad in pfade:
            antwort = Client().get(f"{OEFFENTLICH}{pfad}", headers=headers)
            inhalt = b"".join(cast(Any, antwort).streaming_content) if antwort.streaming else antwort.content
            # Der Snapshot nennt den Zeitpunkt seiner Erzeugung; er gehört nicht zum Vergleich
            inhalt = re.sub(rb'"generated": ?"[^"]*"', b'"generated": ""', inhalt)
            # Kopfzeilen der Schnittstelle (ohne die je Anfrage neuen wie Nonce und Kennung der Anfrage)
            kopf = sorted((name, wert) for name, wert in antwort.items() if name in KOPFZEILEN)
            ergebnis[pfad] = f"{antwort.status_code} {kopf}".encode() + inhalt
        return ergebnis

    def test_unveraendert_mit_token_und_schalter(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], ALLE_NOE)
        with override_settings(OPARL_CHANGES_ENABLED=True):
            mit = self._alle(welt)
            mit_token = self._alle(welt, Authorization=f"Bearer {token_fuer(user)}")
            with override_settings(OPARL_NONPUBLIC_ENABLED=False):
                ohne = self._alle(welt)
        # Ein Token ändert an der öffentlichen Ausgabe nichts, Byte für Byte (auch die Kopfzeilen)
        for pfad in mit:
            assert mit_token[pfad] == mit[pfad], (pfad, mit[pfad][:600], mit_token[pfad][:600])
        # Der Schalter ändert nur das System: das Profil in conformsTo und der Verweis auf die nichtöffentliche Ausgabe
        assert {pfad for pfad in mit if mit[pfad] != ohne[pfad]} == {""}
        assert b"GEHEIM" not in b"".join(mit.values())
        assert b"nichtoeffentlich" not in b"".join(wert for pfad, wert in mit.items() if pfad != "")

    def test_system_nennt_profil_und_ausgabe(self, welt: dict[str, Any]) -> None:
        system = Client().get(OEFFENTLICH).json()
        assert "https://schema.kommparl.de/0.1/profile/nonpublic" in system["kommparl:conformsTo"]
        assert system["kommparl:nonPublicSystem"] == NOE_URL
        assert pruefe(system, "System") == []
        with override_settings(OPARL_NONPUBLIC_ENABLED=False):
            system = Client().get(OEFFENTLICH).json()
        assert "kommparl:nonPublicSystem" not in system
        assert "https://schema.kommparl.de/0.1/profile/nonpublic" not in system["kommparl:conformsTo"]

    def test_nichts_nichtoeffentliches_in_feed_und_snapshot(self, welt: dict[str, Any]) -> None:
        basis = f"{SITE}{OEFFENTLICH}"
        body = canonical_id(f"{basis}body/")
        mandant = f"session:{welt['tenant'].pk}"
        with override_settings(OPARL_CHANGES_ENABLED=True):
            ereignis(
                "ris.meeting.changed", body, canonical_id(f"{basis}meeting/{welt['sitzung'].pk}/"), mandant=mandant
            )
            # Auch ein Ereignis, das ein nichtöffentliches Objekt fälschlich als öffentlich meldet, ergibt keinen Eintrag
            ereignis(
                "ris.meeting.changed", body, canonical_id(f"{basis}meeting/{welt['noe_sitzung'].pk}/"), mandant=mandant
            )
            user, _ = konto(welt["tenant"], ALLE_NOE)
            headers = {"Authorization": f"Bearer {token_fuer(user)}"}
            feed = Client().get(f"{OEFFENTLICH}body/kommparl/changes/", headers=headers).json()
            snapshot = Client().get(f"{OEFFENTLICH}body/kommparl/snapshot/", headers=headers)
            zeilen = b"".join(cast(Any, snapshot).streaming_content).decode()
        assert [eintrag["id"] for eintrag in feed["data"]] == [f"{basis}meeting/{welt['sitzung'].pk}/"]
        for geheim in ("GEHEIM", "PERSONAL", "nichtoeffentlich", str(welt["noe_sitzung"].pk), str(welt["geheim"].pk)):
            assert geheim not in json.dumps(feed)
            assert geheim not in zeilen

    def test_feed_und_snapshot_gibt_es_in_der_nichtoeffentlichen_ausgabe_nicht(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], ALLE_NOE)
        body = abruf(f"{NOE}body/", token_fuer(user)).json()
        assert "kommparl:changes" not in body and "kommparl:snapshot" not in body
        assert abruf(f"{NOE}changes/", token_fuer(user)).status_code == 404


# =============================================================================
# Schalter, Metadaten, Konformität
# =============================================================================


class TestSchalterUndMetadaten:
    def test_ausgeschaltet_gibt_es_nichts(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], ALLE_NOE)
        raw = token_fuer(user)
        with override_settings(OPARL_NONPUBLIC_ENABLED=False):
            assert abruf(f"{NOE}meetings/", raw).status_code == 404
            assert Client().get(METADATEN).status_code == 404
        with override_settings(OIDC_PROVIDER_ENABLED=False):
            assert abruf(f"{NOE}meetings/", raw).status_code == 404

    def test_metadaten_nach_rfc_9728(self, welt: dict[str, Any]) -> None:
        antwort = Client().get(METADATEN)
        assert antwort.status_code == 200
        daten = antwort.json()
        assert daten["resource"] == NOE_URL
        assert daten["authorization_servers"] == [SITE]
        assert daten["scopes_supported"] == [catalog.RATSDATEN_NICHTOEFFENTLICH, catalog.SITZUNG_LESEN]
        assert daten["bearer_methods_supported"] == ["header"]
        assert pruefe_profil(daten, "nonpublic", "ProtectedResourceMetadata") == []
        assert nichtoeffentlich.metadata_url(welt["tenant"]) == f"{SITE}{METADATEN}"

    def test_nur_lesend(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], ALLE_NOE)
        antwort = Client().post(f"{NOE}meetings/", headers={"Authorization": f"Bearer {token_fuer(user)}"})
        assert antwort.status_code == 405
        privat(antwort)


class TestKonformitaet:
    def test_objekte_und_listen_gegen_die_schemas(self, welt: dict[str, Any]) -> None:
        user, _ = konto(welt["tenant"], ALLE_NOE)
        raw = token_fuer(user)
        system = abruf(NOE, raw).json()
        assert pruefe(system, "System") == []
        assert system["kommparl:conformsTo"] == [
            "https://schema.kommparl.de/0.1/",
            "https://schema.kommparl.de/0.1/profile/nonpublic",
        ]
        assert system["kommparl:publicId"] == f"{SITE}{OEFFENTLICH}"
        assert pruefe(abruf(f"{NOE}body/", raw).json(), "Body") == []
        for liste, typ in (
            ("meetings", "Meeting"),
            ("agendaitems", "AgendaItem"),
            ("papers", "Paper"),
            ("consultations", "Consultation"),
            ("files", "File"),
            ("organizations", "Organization"),
            ("people", "Person"),
        ):
            assert pruefe_liste(abruf(f"{NOE}{liste}/", raw).json(), typ) == [], liste
        assert pruefe(abruf(f"{NOE}meeting/{welt['noe_sitzung'].pk}/", raw).json(), "Meeting") == []
        assert pruefe(abruf(f"{NOE}location/{welt['noe_sitzung'].pk}/", raw).json(), "Location") == []
        assert pruefe(abruf(f"{NOE}file/{welt['anlage_geheim'].pk}/", raw).json(), "File") == []

    def test_ohne_oeffentliche_ausgabe_keine_public_id(self, welt: dict[str, Any]) -> None:
        tenant = welt["tenant"]
        tenant.oparl_public_since = None
        tenant.save()
        user, _ = konto(tenant, ALLE_NOE)
        sitzung = abruf(f"{NOE}meeting/{welt['sitzung'].pk}/", token_fuer(user)).json()
        assert "kommparl:publicId" not in json.dumps(sitzung)
