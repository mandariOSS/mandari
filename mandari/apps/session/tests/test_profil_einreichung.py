# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Profil Einreichung von kommParl je Session-Mandant (Issue #1228, ``apps/session/api/einreichung.py``).

Geprüft werden der ganze Ablauf (Regeln, Anlage hochladen, einreichen, Bearbeitungsstand, Rücknahme), die
Idempotenz über die Befehle, die Rückmeldung zur Ladung samt Empfangsbestätigung, die Anmeldung (nur Access-Tokens
im Namen einer Person mit Scope ``einreichung:schreiben``), die Sichtbarkeit fremder Einreichungen und die
Konformität jeder Antwort mit den Schemas des Profils aus kommParl.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.oidc.models import OAuthApplication
from apps.accounts.tests import oidc_hilfen as h
from apps.common.tests.factories import MembershipFactory, OrganizationFactory, RoleFactory, UserFactory
from apps.events.models import IdempotencyKey
from apps.session.models import (
    SessionAPIToken,
    SessionApplication,
    SessionAttendance,
    SessionAuditLog,
    SessionFile,
    SessionInvitationDispatch,
    SessionInvitationRecipient,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionSubmissionUpload,
    SessionTenant,
)
from apps.work.motions.ris_submission import connect_with_token
from hub.api.tests.konformitaet import pruefe, pruefe_profil
from hub.commands import canonical_json, content_hash

pytestmark = pytest.mark.django_db

SCOPE = "einreichung:schreiben"
OPARL = "/session/musterstadt/api/oparl/"
SUBMISSIONS = f"{OPARL}body/kommparl/submissions/"
UPLOADS = f"{OPARL}body/kommparl/uploads/"
RULES = f"{OPARL}body/kommparl/submission-rules/"
PDF = b"%PDF-1.4\n% Lageplan Lindenstrasse\n"


@pytest.fixture(autouse=True)
def anbieter() -> Iterator[None]:
    cache.clear()
    with h.eingeschaltet():
        yield


@dataclass
class Welt:
    tenant: SessionTenant
    fraktion: Any
    mitglied: Any
    person: SessionPerson
    meeting: SessionMeeting
    recipient: SessionInvitationRecipient
    app: OAuthApplication
    token: str

    def headers(self, key: str | None = None, token: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token or self.token}"}
        if key:
            headers["Idempotency-Key"] = key
        return headers


def _fraktion(tenant: SessionTenant, name: str, permissions: list[str], email: str) -> tuple[Any, Any]:
    """Fraktion mit Verbindung zur Verwaltung (Einreichungs-Token) und einem Mitglied mit den Rechten."""
    _token, raw = SessionAPIToken.create_token(tenant, name, can_submit_applications=True)
    organization = cast(Any, OrganizationFactory)(name=name)
    role = cast(Any, RoleFactory)(organization=organization, permissions=permissions)
    user = cast(Any, UserFactory)(email=email, first_name="Erika", last_name="Mustermann")
    user.email_verified = True
    user.save()
    member = cast(Any, MembershipFactory)(organization=organization, user=user, roles=[role])
    connect_with_token(organization, raw, member)
    return organization, member


@pytest.fixture
def welt() -> Welt:
    tenant = SessionTenant.objects.create(
        name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now()
    )
    fraktion, mitglied = _fraktion(
        tenant, "Fraktion A", ["motions.submit_to_ris", "meetings.view"], "erika@example.org"
    )
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")
    person = SessionPerson.objects.create(
        tenant=tenant, given_name="Erika", family_name="Mustermann", email="Erika@Example.org"
    )
    SessionOrganizationMembership.objects.create(organization=gremium, person=person, role="member")
    meeting = SessionMeeting.objects.create(
        tenant=tenant,
        name="Sitzung des Hauptausschusses",
        organization=gremium,
        start=timezone.now() + timedelta(days=14),
        is_public=True,
        meeting_state="scheduled",
    )
    dispatch = SessionInvitationDispatch.objects.create(meeting=meeting, subject="Ladung")
    recipient = SessionInvitationRecipient.objects.create(
        dispatch=dispatch,
        person=person,
        name="Erika Mustermann",
        email=person.email,
        status="sent",
        sent_at=timezone.now(),
    )
    app = h.anwendung(name="Fraktionsapp", scopes=["openid", SCOPE])[0]
    token = h.access_token(app, mitglied.user, scope=f"openid {SCOPE}")
    return Welt(tenant, fraktion, mitglied, person, meeting, recipient, app, token)


def _antrag(**felder: Any) -> dict[str, Any]:
    return {
        "submissionType": "motion",
        "name": "Sichere Schulwege an der Lindenstraße",
        "proposalText": "Die Verwaltung legt ein Konzept für sichere Schulwege vor.",
        "reasoningText": "Eltern melden gefährliche Situationen.",
        **felder,
    }


def _post(path: str, data: Any, headers: dict[str, str]) -> Any:
    content = data if isinstance(data, bytes) else json.dumps(data).encode()
    return Client().post(path, data=content, content_type="application/json", headers=headers)


def _hochladen(welt: Welt, inhalt: bytes = PDF, name: str = "Lageplan.pdf", token: str | None = None) -> Any:
    return Client().post(
        UPLOADS,
        {"file": SimpleUploadedFile(name, inhalt, content_type="application/pdf")},
        headers=welt.headers(token=token),
    )


def _konform(daten: Any, name: str) -> None:
    probleme = pruefe_profil(daten, "submission", name)
    assert not probleme, probleme


def _digest(content: bytes) -> str:
    return "sha-256=:" + base64.b64encode(hashlib.sha256(content).digest()).decode() + ":"


# =============================================================================
# Ablauf: Regeln, Anlage, Einreichen, Bearbeitungsstand
# =============================================================================


class TestEinreichen:
    def test_ablauf_mit_anlage(self, welt: Welt) -> None:
        regeln = Client().get(RULES, headers=welt.headers())
        assert regeln.status_code == 200
        _konform(regeln.json(), "SubmissionRules")
        assert regeln.json()["originator"] == [{"id": f"urn:uuid:{welt.fraktion.pk}", "name": "Fraktion A"}]
        assert "question" in regeln.json()["acceptedTypes"] and "majorQuestion" not in regeln.json()["acceptedTypes"]

        hochgeladen = _hochladen(welt)
        assert hochgeladen.status_code == 201, hochgeladen.content
        upload = hochgeladen.json()
        _konform(upload, "SubmissionUpload")
        assert hochgeladen["Location"] == upload["id"]
        assert upload["contentDigest"] == _digest(PDF) and upload["size"] == len(PDF)

        inhalt = json.dumps(_antrag(upload=[upload["id"]], coSigner=["Max Mustermann"])).encode()
        antwort = _post(SUBMISSIONS, inhalt, welt.headers("schluessel-1") | {"Content-Digest": _digest(inhalt)})
        assert antwort.status_code == 201, antwort.content
        einreichung = antwort.json()
        _konform(einreichung, "Submission")
        assert antwort["Location"] == einreichung["id"]
        assert antwort["Cache-Control"] == "no-store"
        assert einreichung["status"] == "submitted"
        assert einreichung["contentDigest"] == _digest(inhalt)
        assert einreichung["originator"] == f"urn:uuid:{welt.fraktion.pk}"
        assert einreichung["contact"] == {"name": "Erika Mustermann", "email": "erika@example.org"}
        assert [a["contentDigest"] for a in einreichung["attachment"]] == [_digest(PDF)]

        antrag = SessionApplication.objects.get()
        assert einreichung["receiptNumber"] == antrag.reference
        assert antrag.submitting_organization_id == welt.fraktion.pk and antrag.co_signers == "Max Mustermann"
        assert SessionFile.objects.filter(application=antrag, is_public=False).count() == 1
        verwendet = SessionSubmissionUpload.objects.get()
        assert verwendet.used_at is not None and not verwendet.file, "Inhalt liegt jetzt in der Ablage der Anlagen"

        abruf = Client().get(einreichung["id"].removeprefix("http://localhost:8000"), headers=welt.headers())
        assert abruf.status_code == 200
        _konform(abruf.json(), "Submission")
        assert abruf.json()["contentDigest"] == einreichung["contentDigest"]
        assert (
            Client()
            .get(
                einreichung["id"].removeprefix("http://localhost:8000"),
                headers=welt.headers() | {"If-None-Match": abruf["ETag"]},
            )
            .status_code
            == 304
        )

        liste = Client().get(SUBMISSIONS, headers=welt.headers())
        assert liste.status_code == 200
        _konform(liste.json(), "SubmissionList")
        assert [e["id"] for e in liste.json()["data"]] == [einreichung["id"]]
        for eintrag in liste.json()["data"]:
            _konform(eintrag, "Submission")

    def test_anlage_nur_fuer_die_eigene_person_und_inhalt_nie_abrufbar(self, welt: Welt) -> None:
        upload = _hochladen(welt).json()
        pfad = upload["id"].removeprefix("http://localhost:8000")
        abruf = Client().get(pfad, headers=welt.headers())
        assert abruf.status_code == 200 and abruf["Content-Type"].startswith("application/json")
        assert PDF not in abruf.content

        _fremd, anderes = _fraktion(welt.tenant, "Fraktion B", ["motions.submit_to_ris"], "bea@example.org")
        fremdes_token = h.access_token(welt.app, anderes.user, scope=SCOPE)
        assert Client().get(pfad, headers=welt.headers(token=fremdes_token)).status_code == 404
        antwort = _post(SUBMISSIONS, _antrag(upload=[upload["id"]]), welt.headers("fremd-1", token=fremdes_token))
        assert antwort.status_code == 422
        assert antwort.json()["errors"][0]["pointer"] == "/upload/0"

    def test_abgelaufene_anlagen_werden_aufgeraeumt(self, welt: Welt) -> None:
        alt = _hochladen(welt).json()
        SessionSubmissionUpload.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        name = str(SessionSubmissionUpload.objects.get().file.name)
        storage = SessionSubmissionUpload._meta.get_field("file").storage
        assert storage.exists(name)
        antwort = _post(SUBMISSIONS, _antrag(upload=[alt["id"]]), welt.headers("k-1"))
        assert antwort.status_code == 422 and antwort.json()["errors"][0]["pointer"] == "/upload/0"
        assert _hochladen(welt).status_code == 201
        assert SessionSubmissionUpload.objects.count() == 1
        assert not storage.exists(name)

    def test_anlage_mit_unerlaubtem_typ(self, welt: Welt) -> None:
        antwort = _hochladen(welt, b"<html><script>alert(1)</script></html>", name="seite.html")
        assert antwort.status_code == 415
        assert antwort["Content-Type"].startswith("application/problem+json")
        assert not SessionSubmissionUpload.objects.exists()

    def test_wiederholung_mit_gleichem_schluessel_ergibt_keinen_zweiten_vorgang(self, welt: Welt) -> None:
        upload = _hochladen(welt).json()
        inhalt = json.dumps(_antrag(upload=[upload["id"]])).encode()
        erste = _post(SUBMISSIONS, inhalt, welt.headers("schluessel-1"))
        zweite = _post(SUBMISSIONS, inhalt, welt.headers("schluessel-1"))
        assert erste.status_code == zweite.status_code == 201
        for feld in ("id", "receiptNumber", "receivedAt", "contentDigest"):
            assert erste.json()[feld] == zweite.json()[feld], feld
        assert SessionApplication.objects.count() == 1
        assert SessionFile.objects.count() == 1

    def test_anderer_inhalt_mit_gleichem_schluessel(self, welt: Welt) -> None:
        assert _post(SUBMISSIONS, _antrag(), welt.headers("schluessel-1")).status_code == 201
        antwort = _post(SUBMISSIONS, _antrag(name="Anderer Titel"), welt.headers("schluessel-1"))
        assert antwort.status_code == 422
        assert antwort.json()["type"].endswith("/idempotenzschluessel-wiederverwendet")
        _konform(antwort.json(), "Problem")
        assert SessionApplication.objects.count() == 1

    def test_ohne_idempotency_key(self, welt: Welt) -> None:
        antwort = _post(SUBMISSIONS, _antrag(), welt.headers())
        assert antwort.status_code == 400
        assert antwort.json()["type"].endswith("/idempotenzschluessel-fehlt")
        assert not SessionApplication.objects.exists()

    def test_content_digest_passt_nicht(self, welt: Welt) -> None:
        antwort = _post(SUBMISSIONS, _antrag(), welt.headers("k-1") | {"Content-Digest": _digest(b"anders")})
        assert antwort.status_code == 400
        assert not SessionApplication.objects.exists()

    def test_inhalt_nach_dem_profil_geprueft(self, welt: Welt) -> None:
        ohne_begruendung = _antrag()
        del ohne_begruendung["reasoningText"]
        antwort = _post(SUBMISSIONS, ohne_begruendung, welt.headers("k-1"))
        assert antwort.status_code == 422
        assert antwort.json()["errors"] == [
            {"pointer": "/reasoningText", "detail": "Pflichtangabe nach der Geschäftsordnung"}
        ]
        antwort = _post(SUBMISSIONS, _antrag(submissionType="majorQuestion"), welt.headers("k-2"))
        assert antwort.status_code == 422
        antwort = _post(SUBMISSIONS, _antrag(unbekannt=1, name=""), welt.headers("k-3"))
        assert antwort.status_code == 422
        assert {fehler["pointer"] for fehler in antwort.json()["errors"]} == {"", "/name"}
        _konform(antwort.json(), "Problem")
        assert not SessionApplication.objects.exists()

    def test_fremde_stelle_ist_verboten(self, welt: Welt) -> None:
        fremd, _anderes = _fraktion(welt.tenant, "Fraktion B", ["motions.submit_to_ris"], "bea@example.org")
        antwort = _post(SUBMISSIONS, _antrag(originator=f"urn:uuid:{fremd.pk}"), welt.headers("k-1"))
        assert antwort.status_code == 403

    def test_ohne_einreichungsrecht(self, welt: Welt) -> None:
        _fraktion_ohne, ohne = _fraktion(welt.tenant, "Fraktion C", ["meetings.view"], "carl@example.org")
        token = h.access_token(welt.app, ohne.user, scope=SCOPE)
        assert Client().get(RULES, headers=welt.headers(token=token)).json()["originator"] == []
        assert _post(SUBMISSIONS, _antrag(), welt.headers("k-1", token=token)).status_code == 403

    def test_pruefprotokoll_nennt_person_und_anwendung(self, welt: Welt) -> None:
        assert _post(SUBMISSIONS, _antrag(), welt.headers("k-1")).status_code == 201
        eintrag = SessionAuditLog.objects.filter(changes__aktion="Einreichung").get()
        assert eintrag.changes["anwendung"] == welt.app.client_id
        assert eintrag.changes["person"] == f"user:{welt.mitglied.user.pk}"
        assert "Lindenstraße" not in json.dumps(eintrag.changes)


# =============================================================================
# Sichtbarkeit und Rücknahme
# =============================================================================


class TestSichtbarkeitUndRuecknahme:
    def _einreichen(self, welt: Welt, key: str = "k-1") -> dict[str, Any]:
        antwort = _post(SUBMISSIONS, _antrag(), welt.headers(key))
        assert antwort.status_code == 201
        return cast(dict[str, Any], antwort.json())

    def test_fremde_einreichung_ist_nicht_auffindbar(self, welt: Welt) -> None:
        einreichung = self._einreichen(welt)
        _fremd, anderes = _fraktion(welt.tenant, "Fraktion B", ["motions.submit_to_ris"], "bea@example.org")
        token = h.access_token(welt.app, anderes.user, scope=SCOPE)
        pfad = einreichung["id"].removeprefix("http://localhost:8000")
        assert Client().get(pfad, headers=welt.headers(token=token)).status_code == 404
        assert Client().get(SUBMISSIONS, headers=welt.headers(token=token)).json()["data"] == []
        assert _post(f"{pfad}withdraw/", {}, welt.headers("r-1", token=token)).status_code == 404
        assert SessionApplication.objects.get().status == "submitted"

    def test_ruecknahme_erlaubt_und_wiederholt(self, welt: Welt) -> None:
        einreichung = self._einreichen(welt)
        ziel = einreichung["withdraw"].removeprefix("http://localhost:8000")
        antwort = _post(ziel, {}, welt.headers("r-1"))
        assert antwort.status_code == 200, antwort.content
        _konform(antwort.json(), "Submission")
        assert antwort.json()["status"] == "withdrawn"
        assert antwort.json()["statusHistory"][-1] == {"status": "withdrawn"}
        assert SessionApplication.objects.get().status == "withdrawn"
        # Wiederholung mit demselben Schlüssel: dieselbe Antwort, nicht 409
        assert _post(ziel, {}, welt.headers("r-1")).status_code == 200

    def test_ruecknahme_in_unzulaessigem_stand(self, welt: Welt) -> None:
        einreichung = self._einreichen(welt)
        SessionApplication.objects.update(status="converted")
        ziel = einreichung["withdraw"].removeprefix("http://localhost:8000")
        antwort = _post(ziel, b"", welt.headers("r-1"))
        assert antwort.status_code == 409
        _konform(antwort.json(), "Problem")
        assert antwort.json()["type"].endswith("/ruecknahme-nicht-zulaessig")
        assert SessionApplication.objects.get().status == "converted"

    def test_ruecknahme_ohne_schluessel(self, welt: Welt) -> None:
        einreichung = self._einreichen(welt)
        ziel = einreichung["withdraw"].removeprefix("http://localhost:8000")
        assert _post(ziel, {}, welt.headers()).status_code == 400


# =============================================================================
# Rückmeldung zur Ladung und Empfangsbestätigung
# =============================================================================


class TestRueckmeldung:
    def _pfad(self, welt: Welt, art: str) -> str:
        return reverse(f"session:oparl_kommparl_{art}", kwargs={"tenant_slug": "musterstadt", "pk": welt.meeting.pk})

    def test_absage_mit_vertretung_und_grund(self, welt: Welt) -> None:
        pfad = self._pfad(welt, "attendance")
        stand = Client().get(pfad, headers=welt.headers())
        assert stand.status_code == 200
        _konform(stand.json(), "AttendanceResponse")
        assert stand.json()["response"] == "pending" and stand.json()["acceptsResponses"] is True

        anfrage = {"response": "notAttending", "substituteRequested": True, "reason": "VERTRAULICHER-GRUND"}
        antwort = _post(pfad, anfrage, welt.headers("m-1"))
        assert antwort.status_code == 200, antwort.content
        _konform(antwort.json(), "AttendanceResponse")
        assert antwort.json()["response"] == "notAttending" and antwort.json()["substituteRequested"] is True
        assert "acknowledgedAt" in antwort.json(), "Die Rückmeldung bestätigt den Empfang"
        assert b"VERTRAULICHER-GRUND" not in antwort.content

        anwesenheit = SessionAttendance.objects.get(meeting=welt.meeting, person=welt.person)
        assert anwesenheit.status == "declined" and anwesenheit.response_source == "portal"
        assert cast(Any, anwesenheit).get_response_reason_decrypted() == "VERTRAULICHER-GRUND"
        schluessel = IdempotencyKey.objects.get(label="attendance.respond")
        assert b"VERTRAULICHER-GRUND" not in json.dumps(schluessel.response).encode()
        # Personenbezogen: Hash mit Schlüssel der Installation, nicht nachrechenbar
        body = {
            "meeting": str(welt.meeting.pk),
            "person": str(welt.person.pk),
            "response": "declined",
            "source": "portal",
            "substitute_requested": True,
            "reason": "VERTRAULICHER-GRUND",
        }
        ungeschluesselt = content_hash(
            {"command": "attendance.respond", "version": 1, "content_hash": content_hash(body)}
        )
        assert schluessel.request_hash != ungeschluesselt
        assert schluessel.response["content_hash"] != hashlib.sha256(canonical_json(body)).hexdigest()

    def test_zusage_und_wiederholung(self, welt: Welt) -> None:
        pfad = self._pfad(welt, "attendance")
        erste = _post(pfad, {"response": "attending"}, welt.headers("m-1"))
        zweite = _post(pfad, {"response": "attending"}, welt.headers("m-1"))
        assert erste.status_code == zweite.status_code == 200
        assert erste.json()["response"] == "attending"
        assert IdempotencyKey.objects.filter(label="attendance.respond").count() == 1
        mit_grund = _post(pfad, {"response": "attending", "reason": "x"}, welt.headers("m-2"))
        assert mit_grund.status_code == 422

    def test_nach_sitzungsbeginn(self, welt: Welt) -> None:
        SessionMeeting.objects.filter(pk=welt.meeting.pk).update(start=timezone.now() - timedelta(hours=1))
        antwort = _post(self._pfad(welt, "attendance"), {"response": "attending"}, welt.headers("m-1"))
        assert antwort.status_code == 409

    def test_nicht_geladen(self, welt: Welt) -> None:
        SessionInvitationRecipient.objects.all().delete()
        assert Client().get(self._pfad(welt, "attendance"), headers=welt.headers()).status_code == 404
        assert _post(self._pfad(welt, "invitation_receipt"), {}, welt.headers("e-1")).status_code == 404

    def test_empfangsbestaetigung(self, welt: Welt) -> None:
        pfad = self._pfad(welt, "invitation_receipt")
        assert Client().get(pfad, headers=welt.headers()).status_code == 405, "Ein Abruf bestätigt nichts"
        welt.recipient.refresh_from_db()
        assert welt.recipient.acknowledged_at is None

        antwort = _post(pfad, b"", welt.headers("e-1"))
        assert antwort.status_code == 200, antwort.content
        _konform(antwort.json(), "InvitationReceipt")
        welt.recipient.refresh_from_db()
        assert welt.recipient.acknowledged_at is not None and welt.recipient.acknowledged_via == "portal"
        erste = antwort.json()["acknowledgedAt"]
        assert _post(pfad, {}, welt.headers("e-2")).json()["acknowledgedAt"] == erste

    def test_nicht_zugestellte_ladung(self, welt: Welt) -> None:
        SessionInvitationRecipient.objects.update(status="failed")
        assert _post(self._pfad(welt, "invitation_receipt"), {}, welt.headers("e-1")).status_code == 409


# =============================================================================
# Anmeldung
# =============================================================================


class TestAnmeldung:
    def test_ohne_token(self, welt: Welt) -> None:
        antwort = Client().get(RULES)
        assert antwort.status_code == 401
        assert antwort["WWW-Authenticate"].startswith('Bearer realm="mandari"')
        _konform(antwort.json(), "Problem")

    def test_fehlender_scope(self, welt: Welt) -> None:
        token = h.access_token(welt.app, welt.mitglied.user, scope="openid")
        antwort = _post(SUBMISSIONS, _antrag(), welt.headers("k-1", token=token))
        assert antwort.status_code == 403
        assert 'error="insufficient_scope"' in antwort["WWW-Authenticate"]
        assert not SessionApplication.objects.exists()

    def test_client_credentials(self, welt: Welt) -> None:
        dienst = h.anwendung(name="Dienst", ablauf=OAuthApplication.GRANT_CLIENT_CREDENTIALS, scopes=[SCOPE])[0]
        token = h.access_token(dienst, None, scope=SCOPE)
        assert Client().get(RULES, headers=welt.headers(token=token)).status_code == 403
        assert _post(SUBMISSIONS, _antrag(), welt.headers("k-1", token=token)).status_code == 403
        assert not SessionApplication.objects.exists()

    def test_api_token_der_session_api(self, welt: Welt) -> None:
        _token, raw = SessionAPIToken.create_token(welt.tenant, "Integration", can_submit_applications=True)
        antwort = _post(SUBMISSIONS, _antrag(), welt.headers("k-1", token=raw))
        assert antwort.status_code == 401
        assert 'error="invalid_token"' in antwort["WWW-Authenticate"]
        assert not SessionApplication.objects.exists()

    def test_bearer_ohne_csrf(self, welt: Welt) -> None:
        """Nur Bearer-Tokens, keine Cookies: Die Endpunkte brauchen kein CSRF-Token."""
        client = Client(enforce_csrf_checks=True)
        antwort = client.post(
            SUBMISSIONS, data=json.dumps(_antrag()), content_type="application/json", headers=welt.headers("k-1")
        )
        assert antwort.status_code == 201

    def test_sitzung_zaehlt_nicht(self, welt: Welt) -> None:
        client = Client()
        client.force_login(welt.mitglied.user)
        assert client.get(RULES).status_code == 401

    def test_ohne_anbieter_gibt_es_das_profil_nicht(self, welt: Welt) -> None:
        with override_settings(OIDC_PROVIDER_ENABLED=False):
            assert Client().get(RULES, headers=welt.headers()).status_code == 404


# =============================================================================
# Auffinden: Body und Sitzung der OParl-Ausgabe
# =============================================================================


class TestAuffinden:
    def test_body_und_sitzung_nennen_die_adressen(self, welt: Welt) -> None:
        body = Client().get(f"{OPARL}body/").json()
        assert body["kommparl:submission"].endswith(SUBMISSIONS)
        assert body["kommparl:submissionUpload"].endswith(UPLOADS)
        assert body["kommparl:submissionRules"].endswith(RULES)
        assert not pruefe(body, "Body")
        system = Client().get(OPARL).json()
        assert "https://schema.kommparl.de/0.1/profile/submission" in system["kommparl:conformsTo"]

        sitzung = Client().get(f"{OPARL}meeting/{welt.meeting.pk}/").json()
        assert sitzung["kommparl:attendanceResponse"].endswith(f"meeting/{welt.meeting.pk}/kommparl/attendance/")
        assert sitzung["kommparl:invitationReceipt"].endswith(f"meeting/{welt.meeting.pk}/kommparl/invitation-receipt/")
        assert not pruefe(sitzung, "Meeting")

    def test_ohne_anbieter_keine_adressen(self, welt: Welt) -> None:
        with override_settings(OIDC_PROVIDER_ENABLED=False):
            body = Client().get(f"{OPARL}body/").json()
            system = Client().get(OPARL).json()
        assert not any(key.startswith("kommparl:submission") for key in body)
        assert "https://schema.kommparl.de/0.1/profile/submission" not in system["kommparl:conformsTo"]

    def test_kein_profil_beim_aggregator(self, welt: Welt) -> None:
        assert uuid.UUID(str(welt.meeting.pk))
        system = Client().get("/oparl/v1/system").json()
        assert "https://schema.kommparl.de/0.1/profile/submission" not in system.get("kommparl:conformsTo", [])
