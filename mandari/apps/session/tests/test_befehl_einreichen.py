# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehl ``submission.submit`` (Issue #540): ein Einreichungsweg für Anträge, eine Prüfung im Session-Handler.

Geprüft werden der Handler selbst (Quittung, Wiederholung, Rechte beider Auslöser, Pflichttexte, Zielgremium,
Anlagen, Antragsfassung, Rückrollen) und die beiden API-Wege, die ihn nutzen: Session-API v1 (mit optionalem
``Idempotency-Key``) und der abgekündigte Pfad. Den Weg aus Work prüft
``apps/work/motions/tests/test_einreichung_befehl.py``.
"""

from __future__ import annotations

import hashlib
import io
from datetime import timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, OrganizationFactory, RoleFactory, UserFactory
from apps.events.models import IdempotencyKey
from apps.session import commands
from apps.session.models import SessionAPIToken, SessionApplication, SessionFile, SessionOrganization, SessionTenant
from apps.work.motions.ris_submission import connect_with_token
from hub.commands import Command, CommandError, canonical_json, dispatch

pytestmark = pytest.mark.django_db

TITEL = "Mehr Bänke im Stadtpark"
INHALT: dict[str, Any] = {
    "application_type": "motion",
    "title": f"  {TITEL}  ",
    "resolution_proposal": "Zehn Bänke aufstellen.",
    "justification": "Aufenthaltsqualität.",
    "submitter": {"name": "Erika Mustermann", "email": "Fraktion@Example.org"},
    "co_signers": ["Max Mustermann", "Lea Beispiel"],
    "deadline": "2026-11-15",
}


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")


@pytest.fixture
def token(tenant: SessionTenant) -> tuple[SessionAPIToken, str]:
    return cast(
        tuple[SessionAPIToken, str], SessionAPIToken.create_token(tenant, "Fraktion A", can_submit_applications=True)
    )


@pytest.fixture
def fraktion(tenant: SessionTenant, token: tuple[SessionAPIToken, str]) -> Any:
    """Fraktion, die das Token in Work verbunden hat, mit einem Mitglied, das einreichen darf."""
    organization = cast(Any, OrganizationFactory)()
    role = cast(Any, RoleFactory)(organization=organization, permissions=["motions.submit_to_ris"])
    member = cast(Any, MembershipFactory)(organization=organization, roles=[role])
    connect_with_token(organization, token[1], member)
    organization.member = member
    return organization


def _befehl(tenant: SessionTenant, actor: str, *, key: str = "schluessel-1", **felder: Any) -> Command:
    body = {**INHALT, **felder}
    attachments = body.pop("_dateien", {})
    return Command(
        name="submission.submit",
        body={k: v for k, v in body.items() if v is not None},
        idempotency_key=key,
        tenant_ref=commands.tenant_ref(tenant),
        actor_ref=actor,
        attachments=attachments,
    )


def _abgelehnt(command: Command) -> Any:
    with pytest.raises(CommandError) as fehler:
        dispatch(command)
    return fehler.value.problem


def _datei(inhalt: bytes) -> tuple[dict[str, Any], io.BytesIO]:
    return {"size": len(inhalt), "sha256": hashlib.sha256(inhalt).hexdigest()}, io.BytesIO(inhalt)


# =============================================================================
# Quittung und Wiederholung
# =============================================================================


def test_quittung_mit_eingangsnummer_eingangszeit_und_inhalts_hash(
    tenant: SessionTenant, token: tuple[SessionAPIToken, str]
) -> None:
    befehl = _befehl(tenant, commands.token_actor_ref(token[0]))
    quittung = dispatch(befehl)

    antrag = SessionApplication.objects.get()
    assert quittung.aggregate_id == antrag.pk
    assert quittung.reference == antrag.reference and antrag.reference.startswith("A/")
    assert quittung.received_at == antrag.submitted_at
    assert quittung.content_hash == hashlib.sha256(canonical_json(befehl.body)).hexdigest()
    assert quittung.data == {"status": "submitted"}
    # Gespeichert wie bisher aus Work: ohne Leerraum, E-Mail klein, eine Person je Zeile
    assert antrag.title == TITEL and antrag.submitter_email == "fraktion@example.org"
    assert antrag.co_signers == "Max Mustermann\nLea Beispiel"
    assert str(antrag.deadline) == "2026-11-15" and antrag.submitted_via_token == token[0]
    assert antrag.files.count() == 0  # Antragsfassung nur auf Wunsch


def test_wiederholung_liefert_dieselbe_quittung_ohne_zweiten_antrag(
    tenant: SessionTenant, token: tuple[SessionAPIToken, str]
) -> None:
    actor = commands.token_actor_ref(token[0])
    erste = dispatch(_befehl(tenant, actor))
    zweite = dispatch(_befehl(tenant, actor))
    assert zweite == erste
    assert SessionApplication.objects.count() == 1

    problem = _abgelehnt(_befehl(tenant, actor, title="Anderer Titel"))
    assert (problem.status, problem.kind) == (422, "idempotenzschluessel-wiederverwendet")
    assert SessionApplication.objects.count() == 1


def test_unbekannter_mandant(tenant: SessionTenant, token: tuple[SessionAPIToken, str]) -> None:
    tenant.is_active = False
    tenant.save(update_fields=["is_active"])
    problem = _abgelehnt(_befehl(tenant, commands.token_actor_ref(token[0])))
    assert (problem.status, problem.kind) == (404, "mandant-unbekannt")


# =============================================================================
# Berechtigungen
# =============================================================================


@pytest.mark.parametrize(
    "aenderung",
    [{"can_submit_applications": False}, {"is_active": False}, {"expires_at": timezone.now() - timedelta(days=1)}],
)
def test_token_ohne_einreichungsrecht_oder_abgelaufen(
    tenant: SessionTenant, token: tuple[SessionAPIToken, str], aenderung: dict[str, Any]
) -> None:
    SessionAPIToken.objects.filter(pk=token[0].pk).update(**aenderung)
    problem = _abgelehnt(_befehl(tenant, commands.token_actor_ref(token[0])))
    assert (problem.status, problem.kind) == (403, "keine-berechtigung")
    assert not SessionApplication.objects.exists()


def test_token_eines_anderen_mandanten(tenant: SessionTenant) -> None:
    andere = SessionTenant.objects.create(name="Stadt Anderswo", slug="anderswo")
    fremd, _roh = SessionAPIToken.create_token(andere, "Fremd", can_submit_applications=True)
    problem = _abgelehnt(_befehl(tenant, commands.token_actor_ref(fremd)))
    assert problem.status == 403
    assert not SessionApplication.objects.exists()


def test_token_ordnet_nur_die_eigene_organisation_zu(
    tenant: SessionTenant, token: tuple[SessionAPIToken, str], fraktion: Any
) -> None:
    actor = commands.token_actor_ref(token[0])
    fremde = cast(Any, OrganizationFactory)()
    problem = _abgelehnt(_befehl(tenant, actor, submitting_organization=str(fremde.pk)))
    assert problem.status == 403
    quittung = dispatch(_befehl(tenant, actor, key="schluessel-2"))
    assert SessionApplication.objects.get(pk=cast(Any, quittung.aggregate_id)).submitting_organization == fraktion


def test_work_mitglied_reicht_fuer_die_verbundene_organisation_ein(
    tenant: SessionTenant, token: tuple[SessionAPIToken, str], fraktion: Any
) -> None:
    actor = f"user:{fraktion.member.user_id}"
    quittung = dispatch(_befehl(tenant, actor, submitting_organization=str(fraktion.pk)))

    antrag = SessionApplication.objects.get(pk=cast(Any, quittung.aggregate_id))
    assert antrag.submitting_organization == fraktion and antrag.submitted_via_token == token[0]
    token[0].refresh_from_db()
    assert token[0].usage_count == 1


def test_work_ohne_recht_mitgliedschaft_oder_verbindung_wird_abgelehnt(
    tenant: SessionTenant, token: tuple[SessionAPIToken, str], fraktion: Any
) -> None:
    ohne_recht = cast(Any, MembershipFactory)(organization=fraktion)
    fremder = cast(Any, UserFactory)()
    unverbunden = cast(Any, OrganizationFactory)()
    role = cast(Any, RoleFactory)(organization=unverbunden, permissions=["motions.submit_to_ris"])
    mitglied_unverbunden = cast(Any, MembershipFactory)(organization=unverbunden, roles=[role])
    faelle = [
        (f"user:{ohne_recht.user_id}", str(fraktion.pk), commands.NOT_ALLOWED),
        (f"user:{fremder.pk}", str(fraktion.pk), commands.NOT_ALLOWED),
        (f"user:{fraktion.member.user_id}", None, commands.NOT_ALLOWED),
        (f"user:{mitglied_unverbunden.user_id}", str(unverbunden.pk), commands.NOT_CONNECTED),
        ("system:work", str(fraktion.pk), commands.NOT_ALLOWED),
    ]
    for nummer, (actor, organisation, meldung) in enumerate(faelle):
        problem = _abgelehnt(_befehl(tenant, actor, key=f"k-{nummer}", submitting_organization=organisation))
        assert (problem.status, problem.detail) == (403, meldung), actor

    # Zurückgezogener Token der Verbindung
    SessionAPIToken.objects.filter(pk=token[0].pk).update(is_active=False)
    problem = _abgelehnt(
        _befehl(tenant, f"user:{fraktion.member.user_id}", key="k-x", submitting_organization=str(fraktion.pk))
    )
    assert (problem.status, problem.detail) == (403, commands.TOKEN_INVALID)
    assert not SessionApplication.objects.exists()


# =============================================================================
# Inhalt: Pflichttexte, Zielgremium
# =============================================================================


def test_pflichttexte_duerfen_nicht_nur_leerraum_sein(
    tenant: SessionTenant, token: tuple[SessionAPIToken, str]
) -> None:
    problem = _abgelehnt(
        _befehl(
            tenant,
            commands.token_actor_ref(token[0]),
            title="   ",
            justification="\n",
            submitter={"name": " ", "email": "a@example.org"},
        )
    )
    assert (problem.status, problem.kind) == (422, "validierung")
    assert {e["pointer"] for e in problem.errors} == {"/title", "/justification", "/submitter/name"}
    assert not SessionApplication.objects.exists()


def test_zielgremium_muss_zum_mandanten_gehoeren_und_aktiv_sein(
    tenant: SessionTenant, token: tuple[SessionAPIToken, str]
) -> None:
    actor = commands.token_actor_ref(token[0])
    andere = SessionTenant.objects.create(name="Stadt Anderswo", slug="anderswo")
    fremd = SessionOrganization.objects.create(tenant=andere, name="Rat Anderswo", organization_type="council")
    ruhend = SessionOrganization.objects.create(tenant=tenant, name="Alter Ausschuss", is_active=False)
    for nummer, gremium in enumerate((fremd, ruhend)):
        problem = _abgelehnt(_befehl(tenant, actor, key=f"k-{nummer}", target_organization=str(gremium.pk)))
        assert [e["pointer"] for e in problem.errors] == ["/target_organization"]

    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    quittung = dispatch(_befehl(tenant, actor, key="k-ok", target_organization=str(rat.pk)))
    assert SessionApplication.objects.get(pk=cast(Any, quittung.aggregate_id)).target_organization == rat


def test_meldungen_und_protokoll_nennen_keine_inhalte(
    tenant: SessionTenant, token: tuple[SessionAPIToken, str], caplog: pytest.LogCaptureFixture
) -> None:
    problem = _abgelehnt(
        _befehl(tenant, commands.token_actor_ref(token[0]), title=" ", target_organization=str(tenant.pk))
    )
    text = repr(problem.to_dict()) + caplog.text
    for inhalt in ("Zehn Bänke", "Aufenthaltsqualität", "Erika", "example.org"):
        assert inhalt not in text


# =============================================================================
# Anlagen und Antragsfassung
# =============================================================================


def test_anlagen_werden_geprueft_und_nichtoeffentlich_abgelegt(
    tenant: SessionTenant, token: tuple[SessionAPIToken, str], settings: Any, tmp_path: Any
) -> None:
    settings.MEDIA_ROOT = tmp_path
    settings.SESSION_FILE_SCAN_HOOK = "apps.work.motions.tests.test_dokument_anhaenge.scan_hook_lehnt_virus_ab"
    plan, plan_datei = _datei(b"%PDF-1.4 Lageplan")
    virus, virus_datei = _datei(b"%PDF-1.4 anders")
    quittung = dispatch(
        _befehl(
            tenant,
            commands.token_actor_ref(token[0]),
            attachments=[{"name": "Plan.pdf", **plan}, {"name": "Virus.pdf", **virus}],
            _dateien={plan["sha256"]: plan_datei, virus["sha256"]: virus_datei},
        )
    )

    (datei,) = SessionFile.objects.filter(application_id=quittung.aggregate_id)
    assert datei.name == "Plan.pdf" and datei.is_public is False
    # Nicht Angenommenes steht mit Stelle und Code in der Quittung, ohne Namen
    assert quittung.data["attachments_rejected"] == [{"index": 1, "reason": "virenpruefung"}]
    assert "Virus" not in repr(quittung.to_dict())


def test_anlagen_muessen_zum_inhalt_passen(tenant: SessionTenant, token: tuple[SessionAPIToken, str]) -> None:
    actor = commands.token_actor_ref(token[0])
    plan, plan_datei = _datei(b"%PDF-1.4 Lageplan")
    _anders, anders_datei = _datei(b"%PDF-1.4 untergeschoben")
    faelle = [
        ([{"name": "Plan.pdf", **plan}], {}, "/attachments/0"),
        ([{"name": "Plan.pdf", **plan}], {plan["sha256"]: anders_datei}, "/attachments/0"),
        ([], {plan["sha256"]: plan_datei}, "/attachments"),
    ]
    for nummer, (angaben, dateien, stelle) in enumerate(faelle):
        problem = _abgelehnt(_befehl(tenant, actor, key=f"k-{nummer}", attachments=angaben or None, _dateien=dateien))
        assert [e["pointer"] for e in problem.errors] == [stelle]
    assert not SessionApplication.objects.exists()


def test_scheiternde_ablage_rollt_den_befehl_zurueck(
    tenant: SessionTenant, token: tuple[SessionAPIToken, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.session.services import file_version_service

    def speicher_voll(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("No space left on device: /srv/geheim")

    echt = file_version_service.attach_upload
    monkeypatch.setattr(file_version_service, "attach_upload", speicher_voll)
    plan, plan_datei = _datei(b"%PDF-1.4 Lageplan")
    befehl = _befehl(
        tenant,
        commands.token_actor_ref(token[0]),
        attachments=[{"name": "Plan.pdf", **plan}],
        _dateien={plan["sha256"]: plan_datei},
    )
    problem = _abgelehnt(befehl)
    assert (problem.status, problem.kind, problem.detail) == (
        503,
        "anlagen-nicht-abgelegt",
        commands.ATTACHMENTS_FAILED,
    )
    assert not SessionApplication.objects.exists() and not IdempotencyKey.objects.exists()

    # Dieselbe Wiederholung gelingt, sobald die Ablage wieder geht
    monkeypatch.setattr(file_version_service, "attach_upload", echt)
    quittung = dispatch(befehl)
    assert SessionFile.objects.filter(application_id=quittung.aggregate_id).count() == 1


def test_antragsfassung_auf_wunsch(tenant: SessionTenant, token: tuple[SessionAPIToken, str]) -> None:
    quittung = dispatch(_befehl(tenant, commands.token_actor_ref(token[0]), application_pdf=True))
    (pdf,) = SessionFile.objects.filter(application_id=quittung.aggregate_id)
    assert pdf.is_application_pdf and pdf.is_public is False


# =============================================================================
# API-Wege: Session-API v1 und abgekündigter Pfad
# =============================================================================

API_V1 = "/api/v1/session/musterstadt/applications/submit/"
ALT = "/session/musterstadt/api/session/applications/submit/"
FELDER = {
    "title": TITEL,
    "justification": "Aufenthaltsqualität.",
    "resolution_proposal": "Zehn Bänke aufstellen.",
    "submitter_name": "Fraktion",
    "submitter_email": "fraktion@example.org",
}


def _post(url: str, roh: str, daten: dict[str, Any], **kopf: str) -> Any:
    return Client().post(
        url, daten, content_type="application/json", headers={"Authorization": f"Bearer {roh}", **kopf}
    )


def test_api_v1_quittung_und_idempotenzschluessel(tenant: SessionTenant, token: tuple[SessionAPIToken, str]) -> None:
    erste = _post(API_V1, token[1], FELDER, **{"Idempotency-Key": '"abc-123"'})
    assert erste.status_code == 201, erste.content
    daten = erste.json()
    antrag = SessionApplication.objects.get()
    assert daten["id"] == str(antrag.pk) and daten["reference"] == antrag.reference
    assert daten["status"] == "submitted" and len(daten["content_hash"]) == 64 and daten["received_at"]

    zweite = _post(API_V1, token[1], FELDER, **{"Idempotency-Key": "abc-123"})
    assert zweite.status_code == 201 and zweite.json() == daten
    assert SessionApplication.objects.count() == 1

    anders = _post(API_V1, token[1], {**FELDER, "title": "Anders"}, **{"Idempotency-Key": "abc-123"})
    assert anders.status_code == 422
    assert anders.json()["type"].endswith("/idempotenzschluessel-wiederverwendet")
    # Ohne Schlüssel bleibt jede Anfrage eine neue Einreichung (wie bisher)
    assert _post(API_V1, token[1], FELDER).status_code == 201
    assert SessionApplication.objects.count() == 2


def test_api_v1_prueft_im_handler(tenant: SessionTenant, token: tuple[SessionAPIToken, str]) -> None:
    leer = _post(API_V1, token[1], {**FELDER, "title": "   "})
    assert leer.status_code == 422
    assert leer.json()["errors"] == [
        {"loc": ["body", "payload", "title"], "msg": "darf nicht leer sein", "type": "befehl"}
    ]

    lang = ", ".join(f"Person Nummer {n}" for n in range(30))
    ok = _post(API_V1, token[1], {**FELDER, "co_signers": lang, "target_organization_id": str(tenant.pk)})
    assert ok.status_code == 201, ok.content
    antrag = SessionApplication.objects.get()
    # Unbekanntes Zielgremium bleibt wie bisher unbeachtet; lange Namenszeilen werden an Kommas getrennt
    assert antrag.target_organization is None
    assert antrag.co_signers.splitlines()[:2] == ["Person Nummer 0", "Person Nummer 1"]


def test_alter_pfad_nutzt_den_befehl_im_alten_format(tenant: SessionTenant, token: tuple[SessionAPIToken, str]) -> None:
    antwort = _post(ALT, token[1], {**FELDER, "title": f" {TITEL} ", "financial_impact": "x" * 10001})
    assert antwort.status_code == 201, antwort.content
    antrag = SessionApplication.objects.get()
    assert antwort.json()["application"] == {"id": str(antrag.pk), "reference": antrag.reference, "status": "submitted"}
    # Gekürzt wie bisher, Leerraum wie aus Work entfernt
    assert antrag.title == TITEL and len(antrag.financial_impact) == 10000
    assert antrag.submitted_via_token == token[0]

    leer = _post(ALT, token[1], {**FELDER, "justification": "  "})
    assert leer.status_code == 400
    assert leer.json()["error"] == "Invalid field: justification"
    assert SessionApplication.objects.count() == 1
