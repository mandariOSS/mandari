# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vier-Augen-Prinzip und Vertretungen (Issue #222).

Keine Rechteausweitung durch die Hintertür: Selbstfreigabe ist verboten, eine Vertretung wirkt nur
im Zeitraum, nur im eigenen Mandanten, nur mit den Rechten der vertretenen Person und nicht als
Kette. Jede Handlung aus einer Vertretung steht mit „in Vertretung für …“ im Audit-Log.
"""

from __future__ import annotations

import importlib
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.session import audit
from apps.session.models import (
    SessionAgendaItem,
    SessionAuditLog,
    SessionCosignature,
    SessionDelegation,
    SessionFile,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionProtocol,
    SessionResolutionForwarding,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.permissions import SessionPermissionChecker
from apps.session.services import allowance_service, cosign_service, delegation_service, four_eyes_service
from apps.session.services.four_eyes_service import PROCESS_PAPER, ApprovalError

pytestmark = pytest.mark.django_db

HEUTE = timezone.localdate()


# =============================================================================
# Hilfen
# =============================================================================


def _tenant(slug: str = "nord", **kwargs: Any) -> SessionTenant:
    return SessionTenant.objects.create(name=f"Stadt {slug}", slug=slug, **kwargs)


def _nutzer(tenant: SessionTenant, name: str, *perms: str, admin: bool = False) -> SessionUser:
    flags = {f"can_{perm}": True for perm in perms}
    role = SessionRole.objects.create(tenant=tenant, name=f"rolle-{name}", is_admin=admin, **flags)
    user = cast(User, UserFactory(email=f"{name}@example.org"))  # type: ignore[no-untyped-call]
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    return session_user


def _client(session_user: SessionUser) -> Client:
    client = Client()
    client.force_login(session_user.user)
    return client


def _vertretung(principal: SessionUser, deputy: SessionUser, **kwargs: Any) -> SessionDelegation:
    kwargs.setdefault("start_date", HEUTE)
    kwargs.setdefault("end_date", HEUTE + timedelta(days=7))
    return SessionDelegation.objects.create(tenant=principal.tenant, principal=principal, deputy=deputy, **kwargs)


def _vorlage(tenant: SessionTenant, creator: SessionUser | None, **kwargs: Any) -> SessionPaper:
    kwargs.setdefault("name", "Vorlage Spielplatz")
    kwargs.setdefault("status", "review")
    return SessionPaper.objects.create(tenant=tenant, created_by=creator, **kwargs)


def _aktion(client: Client, paper: SessionPaper, action: str, **data: str) -> Any:
    return client.post(f"/session/{paper.tenant.slug}/papers/{paper.id}/workflow/{action}/", data)


def _meldungen(response: Any) -> str:
    return " ".join(str(m) for m in get_messages(response.wsgi_request))


def _frisch(session_user: SessionUser) -> SessionUser:
    """Neu geladen – ohne zwischengespeicherte Vertretungen."""
    return SessionUser.objects.select_related("tenant", "user").get(pk=session_user.pk)


class _Anfrage:
    """Laufender Request für die Signal-Hooks (wie ihn die Middleware setzt)."""

    def __init__(self, session_user: SessionUser) -> None:
        self.request = SimpleNamespace(session_user=session_user, META={})

    def __enter__(self) -> None:
        cast(Any, audit).set_current_request(self.request)

    def __exit__(self, *args: object) -> None:
        cast(Any, audit).clear_current_request()


# =============================================================================
# Standardwerte
# =============================================================================


class TestStandardwerte:
    def test_neuer_mandant_vier_augen_nur_fuer_finanzielle_vorgaenge(self) -> None:
        tenant = _tenant()
        assert tenant.four_eyes_papers == "off"
        assert tenant.four_eyes_protocols is False
        assert tenant.four_eyes_allowances is True
        assert tenant.four_eyes_forwardings is False
        assert four_eyes_service.required(tenant, four_eyes_service.PROCESS_ALLOWANCE)
        assert not four_eyes_service.required(tenant, PROCESS_PAPER, SessionPaper(has_financial_impact=True))

    def test_migration_setzt_standardwerte_fuer_bestehende_mandanten(self) -> None:
        migration = importlib.import_module("apps.session.migrations.0029_vier_augen_vertretung").Migration
        defaults = {
            op.name: op.field.default
            for op in migration.operations
            if getattr(op, "model_name", "") == "sessiontenant" and op.name.startswith("four_eyes_")
        }
        assert defaults == {
            "four_eyes_papers": "off",
            "four_eyes_protocols": False,
            "four_eyes_allowances": True,
            "four_eyes_forwardings": False,
        }


# =============================================================================
# Vorlagenfreigabe
# =============================================================================


class TestVorlagenfreigabe:
    def test_selbst_erstellte_vorlage_ist_nicht_freigebbar(self) -> None:
        tenant = _tenant(four_eyes_papers="always")
        autorin = _nutzer(tenant, "autorin", "approve_papers", "edit_papers")
        paper = _vorlage(tenant, autorin)

        with pytest.raises(ApprovalError, match="Sie haben diese Vorlage erstellt"):
            four_eyes_service.authorize(PROCESS_PAPER, paper, autorin)

        response = _aktion(_client(autorin), paper, "approve")
        paper.refresh_from_db()
        assert paper.status == "review" and paper.approved_by is None
        assert "Vier-Augen-Prinzip" in _meldungen(response)

    def test_andere_person_gibt_frei(self) -> None:
        tenant = _tenant(four_eyes_papers="always")
        paper = _vorlage(tenant, _nutzer(tenant, "autorin", "approve_papers"))
        pruefer = _nutzer(tenant, "pruefer", "approve_papers")

        _aktion(_client(pruefer), paper, "approve")
        paper.refresh_from_db()
        assert paper.status == "approved"
        assert paper.approved_by == pruefer and paper.approved_on_behalf_of is None

    def test_letzte_inhaltliche_bearbeitung_sperrt_die_freigabe(self) -> None:
        tenant = _tenant(four_eyes_papers="always")
        autorin = _nutzer(tenant, "autorin", "approve_papers")
        bearbeiter = _nutzer(tenant, "bearbeiter", "approve_papers", "edit_papers")
        dritte = _nutzer(tenant, "dritte", "approve_papers")
        paper = _vorlage(tenant, autorin)

        response = _client(bearbeiter).post(
            f"/session/{tenant.slug}/papers/{paper.id}/edit/",
            {"name": "Vorlage Spielplatz (neu)", "paper_type": "proposal", "status": "review", "main_text": "neu"},
        )
        assert response.status_code == 302
        paper.refresh_from_db()
        assert paper.content_edited_by == bearbeiter

        with pytest.raises(ApprovalError, match="zuletzt inhaltlich bearbeitet"):
            four_eyes_service.authorize(PROCESS_PAPER, paper, bearbeiter)
        with pytest.raises(ApprovalError, match="erstellt"):
            four_eyes_service.authorize(PROCESS_PAPER, paper, autorin)
        assert four_eyes_service.authorize(PROCESS_PAPER, paper, dritte) is None

    def test_speichern_ohne_inhaltsaenderung_zaehlt_nicht(self) -> None:
        tenant = _tenant(four_eyes_papers="always")
        paper = _vorlage(tenant, _nutzer(tenant, "autorin"))
        sachbearbeitung = _nutzer(tenant, "sb", "edit_papers")
        with _Anfrage(sachbearbeitung):
            paper.deadline = HEUTE
            paper.save()
        paper.refresh_from_db()
        assert paper.content_edited_by is None

    def test_anlage_zaehlt_als_inhaltliche_bearbeitung(self, settings: Any, tmp_path: Any) -> None:
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = _tenant(four_eyes_papers="always")
        paper = _vorlage(tenant, _nutzer(tenant, "autorin"))
        anlagen = _nutzer(tenant, "anlagen", "edit_papers", "approve_papers")
        with _Anfrage(anlagen):
            SessionFile.objects.create(
                tenant=tenant, paper=paper, name="plan.txt", file=SimpleUploadedFile("plan.txt", b"x")
            )
        paper.refresh_from_db()
        assert paper.content_edited_by == anlagen
        with pytest.raises(ApprovalError):
            four_eyes_service.authorize(PROCESS_PAPER, paper, anlagen)

    def test_ohne_vier_augen_bleibt_der_ablauf_unveraendert(self) -> None:
        tenant = _tenant()
        autorin = _nutzer(tenant, "autorin", "approve_papers")
        paper = _vorlage(tenant, autorin)
        _aktion(_client(autorin), paper, "approve")
        paper.refresh_from_db()
        assert paper.status == "approved"

    def test_nur_bei_finanziellen_auswirkungen(self) -> None:
        tenant = _tenant(four_eyes_papers="financial")
        autorin = _nutzer(tenant, "autorin", "approve_papers")
        ohne = _vorlage(tenant, autorin, has_financial_impact=False)
        mit = _vorlage(tenant, autorin, has_financial_impact=True)
        assert four_eyes_service.authorize(PROCESS_PAPER, ohne, autorin) is None
        with pytest.raises(ApprovalError):
            four_eyes_service.authorize(PROCESS_PAPER, mit, autorin)

    def test_oberflaeche_zeigt_hinweis_statt_freigabeknopf(self) -> None:
        tenant = _tenant(four_eyes_papers="always")
        autorin = _nutzer(tenant, "autorin", "approve_papers")
        paper = _vorlage(tenant, autorin)
        client = _client(autorin)

        detail = client.get(f"/session/{tenant.slug}/papers/{paper.id}/")
        assert b"vier-augen-hinweis" in detail.content
        assert b"/workflow/approve/" not in detail.content
        liste = client.get(f"/session/{tenant.slug}/papers/review/")
        assert b"Sie haben diese Vorlage erstellt" in liste.content
        assert b"/workflow/approve/" not in liste.content


# =============================================================================
# Vertretung
# =============================================================================


class TestVertretung:
    def _welt(self, **tenant_kwargs: Any) -> tuple[SessionTenant, SessionUser, SessionUser, SessionPaper]:
        tenant = _tenant(**tenant_kwargs)
        leitung = _nutzer(tenant, "leitung", "approve_papers", "view_non_public_papers")
        vertretung = _nutzer(tenant, "vertretung", "edit_papers")
        paper = _vorlage(tenant, _nutzer(tenant, "sachbearbeitung", "edit_papers"))
        return tenant, leitung, vertretung, paper

    def test_freigabe_in_vertretung_wird_vermerkt(self) -> None:
        tenant, leitung, vertretung, paper = self._welt(four_eyes_papers="always")
        _vertretung(leitung, vertretung)

        response = _aktion(_client(vertretung), paper, "approve")
        assert "in Vertretung für leitung@example.org" in _meldungen(response)
        paper.refresh_from_db()
        assert paper.status == "approved"
        assert paper.approved_by == vertretung and paper.approved_on_behalf_of == leitung

        eintrag = SessionAuditLog.objects.get(object_id=paper.id, action="approve")
        assert eintrag.user == vertretung and eintrag.on_behalf_of == leitung

        # Das Protokoll sieht die Revision; die Administrator-Vollmacht umfasst es nicht (Issue #221)
        revision = _nutzer(tenant, "revision", "view_audit_log")
        protokoll = _client(revision).get(f"/session/{tenant.slug}/audit/?object={paper.id}")
        assert "in Vertretung für leitung@example.org".encode() in protokoll.content

    @pytest.mark.parametrize(
        "zeitraum",
        [
            {"start_date": HEUTE - timedelta(days=10), "end_date": HEUTE - timedelta(days=1)},
            {"start_date": HEUTE + timedelta(days=1), "end_date": HEUTE + timedelta(days=5)},
            {"revoked_at": timezone.now()},
        ],
        ids=["vergangen", "geplant", "aufgehoben"],
    )
    def test_vertretung_wirkt_nur_im_zeitraum(self, zeitraum: dict[str, Any]) -> None:
        _tenant_, leitung, vertretung, paper = self._welt()
        _vertretung(leitung, vertretung, **zeitraum)

        assert "approve_papers" not in SessionPermissionChecker(_frisch(vertretung)).permissions
        response = _aktion(_client(vertretung), paper, "approve")
        assert response.status_code == 403
        paper.refresh_from_db()
        assert paper.status == "review"

    def test_nie_mehr_als_die_vertretene_person(self) -> None:
        tenant = _tenant()
        leitung = _nutzer(tenant, "leitung", "edit_papers")  # ohne Freigaberecht
        vertretung = _nutzer(tenant, "vertretung")
        paper = _vorlage(tenant, None)
        _vertretung(leitung, vertretung)

        assert "approve_papers" not in SessionPermissionChecker(_frisch(vertretung)).permissions
        assert _aktion(_client(vertretung), paper, "approve").status_code == 403

    def test_fremder_mandant_ist_wirkungslos(self) -> None:
        tenant, _leitung, vertretung, paper = self._welt()
        fremd = _tenant("fremd")
        fremde_leitung = _nutzer(fremd, "fremde-leitung", "approve_papers", admin=True)
        # Unmittelbar in der Datenbank angelegt (an allen Prüfungen vorbei): wirkt trotzdem nicht
        SessionDelegation.objects.create(
            tenant=tenant, principal=fremde_leitung, deputy=vertretung, start_date=HEUTE, end_date=HEUTE
        )
        SessionDelegation.objects.create(
            tenant=fremd, principal=fremde_leitung, deputy=vertretung, start_date=HEUTE, end_date=HEUTE
        )

        assert delegation_service.incoming(_frisch(vertretung)) == []
        assert _aktion(_client(vertretung), paper, "approve").status_code == 403
        with pytest.raises(delegation_service.DelegationError, match="eigenen Mandanten"):
            delegation_service.create(
                tenant,
                principal=fremde_leitung,
                deputy=vertretung,
                start_date=HEUTE,
                end_date=HEUTE,
                scopes=["approvals"],
                created_by=None,
            )

    def test_vier_augen_gilt_auch_in_vertretung(self) -> None:
        tenant, leitung, vertretung, _paper = self._welt(four_eyes_papers="always")
        _vertretung(leitung, vertretung)
        von_leitung = _vorlage(tenant, leitung)
        von_vertretung = _vorlage(tenant, vertretung)

        response = _aktion(_client(vertretung), von_leitung, "approve")
        assert "vertretene Person (leitung@example.org) hat diese Vorlage erstellt" in _meldungen(response)
        response = _aktion(_client(vertretung), von_vertretung, "approve")
        assert "Sie haben diese Vorlage erstellt" in _meldungen(response)
        for paper in (von_leitung, von_vertretung):
            paper.refresh_from_db()
            assert paper.status == "review"

    def test_keine_kettenvertretung(self) -> None:
        tenant, leitung, vertretung, paper = self._welt()
        kette = _nutzer(tenant, "kette")
        _vertretung(leitung, vertretung)
        _vertretung(vertretung, kette)

        assert "approve_papers" in SessionPermissionChecker(_frisch(vertretung)).permissions
        assert "approve_papers" not in SessionPermissionChecker(_frisch(kette)).permissions
        assert _aktion(_client(kette), paper, "approve").status_code == 403

    def test_nichtoeffentliches_nur_mit_sichtrecht_der_vertretenen_person(self) -> None:
        tenant = _tenant()
        leitung = _nutzer(tenant, "leitung", "approve_papers")  # sieht keine NÖ-Vorlagen
        vertretung = _nutzer(tenant, "vertretung", "view_non_public_papers")
        paper = _vorlage(tenant, None, is_public=False)
        _vertretung(leitung, vertretung)

        decision = four_eyes_service.evaluate(PROCESS_PAPER, paper, _frisch(vertretung))
        assert not decision.allowed
        _aktion(_client(vertretung), paper, "approve")
        paper.refresh_from_db()
        assert paper.status == "review"

    def test_eigenes_recht_geht_vor(self) -> None:
        tenant, leitung, _vertretung_, paper = self._welt()
        pruefer = _nutzer(tenant, "pruefer", "approve_papers")
        _vertretung(leitung, pruefer)
        _aktion(_client(pruefer), paper, "approve")
        paper.refresh_from_db()
        assert paper.approved_by == pruefer and paper.approved_on_behalf_of is None

    def test_zurueckweisung_in_vertretung_im_audit(self) -> None:
        _tenant_, leitung, vertretung, paper = self._welt()
        _vertretung(leitung, vertretung)
        _aktion(_client(vertretung), paper, "reject", comment="Bitte Kosten ergänzen")
        paper.refresh_from_db()
        assert paper.status == "draft"
        eintrag = SessionAuditLog.objects.filter(object_id=paper.id, changes__has_key="zurueckweisungs_kommentar").get()
        assert eintrag.on_behalf_of == leitung

    def test_ohne_vertretung_keine_zusaetzliche_abfrage(self, django_assert_num_queries: Any) -> None:
        tenant = _tenant()
        nutzer = _nutzer(tenant, "nutzer")
        annotiert = delegation_service.annotate_active(SessionUser.objects.filter(pk=nutzer.pk)).get()
        with django_assert_num_queries(0):
            assert delegation_service.incoming(annotiert) == []

        leitung = _nutzer(tenant, "leitung", "approve_papers")
        _vertretung(leitung, nutzer)
        annotiert = delegation_service.annotate_active(SessionUser.objects.filter(pk=nutzer.pk)).get()
        assert [d.principal for d in delegation_service.incoming(annotiert)] == [leitung]

    def test_umfang_ohne_freigaben_gibt_keine_freigaberechte(self) -> None:
        _tenant_, leitung, vertretung, paper = self._welt()
        _vertretung(leitung, vertretung, scope_approvals=False)
        assert _aktion(_client(vertretung), paper, "approve").status_code == 403


# =============================================================================
# Niederschrift und Beschlussauszug
# =============================================================================


def _sitzung(tenant: SessionTenant) -> SessionMeeting:
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")
    return SessionMeeting.objects.create(tenant=tenant, name="Sitzung", organization=gremium, start=timezone.now())


class TestNiederschrift:
    def _welt(self) -> tuple[SessionTenant, SessionMeeting, SessionProtocol, SessionUser]:
        tenant = _tenant(four_eyes_protocols=True, four_eyes_forwardings=True)
        meeting = _sitzung(tenant)
        protokoll = _nutzer(tenant, "protokoll", "approve_protocols", "edit_protocols", "edit_meetings")
        protocol = SessionProtocol.objects.create(meeting=meeting, created_by=protokoll, status="review")
        return tenant, meeting, protocol, protokoll

    def _genehmigen(self, client: Client, meeting: SessionMeeting) -> Any:
        return client.post(f"/session/{meeting.tenant.slug}/meetings/{meeting.id}/protocol/approve/")

    def test_eigene_niederschrift_nicht_genehmigen(self) -> None:
        tenant, meeting, protocol, protokoll = self._welt()
        response = self._genehmigen(_client(protokoll), meeting)
        assert "Sie haben diese Niederschrift erstellt" in _meldungen(response)
        protocol.refresh_from_db()
        assert protocol.status == "review"
        detail = _client(protokoll).get(f"/session/{tenant.slug}/meetings/{meeting.id}/protocol/")
        assert b"vier-augen-hinweis" in detail.content
        assert b"/protocol/approve/" not in detail.content

        self._genehmigen(_client(_nutzer(tenant, "vorsitz", "approve_protocols")), meeting)
        protocol.refresh_from_db()
        assert protocol.status == "approved"

    def test_bearbeitung_der_niederschrift_sperrt(self) -> None:
        tenant, meeting, protocol, _protokoll = self._welt()
        redaktion = _nutzer(tenant, "redaktion", "approve_protocols", "edit_protocols")
        _client(redaktion).post(
            f"/session/{tenant.slug}/meetings/{meeting.id}/protocol/edit/", {"content": "Neuer Text"}
        )
        protocol.refresh_from_db()
        assert protocol.content_edited_by == redaktion
        self._genehmigen(_client(redaktion), meeting)
        protocol.refresh_from_db()
        assert protocol.status == "review"

    def test_speichern_ohne_aenderung_zaehlt_nicht_als_bearbeitung(self) -> None:
        tenant, meeting, protocol, _protokoll = self._welt()
        protocol.content = "Allgemeiner Teil"
        cast(Any, protocol).set_content_encrypted("Vertraulicher Teil")
        protocol.save()
        item = SessionAgendaItem.objects.create(meeting=meeting, number="N1", name="NÖ-TOP", is_public=False)
        cast(Any, item).set_protocol_note_encrypted("geheime Notiz")
        item.save()
        leser = _nutzer(tenant, "leser", "edit_protocols", "approve_protocols", "view_non_public_meetings")

        _client(leser).post(
            f"/session/{tenant.slug}/meetings/{meeting.id}/protocol/edit/",
            {
                "content": "Allgemeiner Teil",
                "content_np": "Vertraulicher Teil",
                f"protocol_note_{item.pk}": "",
                f"protocol_note_np_{item.pk}": "geheime Notiz",
            },
        )
        protocol.refresh_from_db()
        assert protocol.content_edited_by is None
        assert cast(Any, protocol).get_content_decrypted() == "Vertraulicher Teil"

        # Eine Änderung nur im nichtöffentlichen Teil eines TOP zählt dagegen
        _client(leser).post(
            f"/session/{tenant.slug}/meetings/{meeting.id}/protocol/edit/",
            {
                "content": "Allgemeiner Teil",
                "content_np": "Vertraulicher Teil",
                f"protocol_note_{item.pk}": "",
                f"protocol_note_np_{item.pk}": "geänderte Notiz",
            },
        )
        protocol.refresh_from_db()
        assert protocol.content_edited_by == leser

    def test_genehmigung_in_vertretung(self) -> None:
        tenant, meeting, protocol, _protokoll = self._welt()
        vorsitz = _nutzer(tenant, "vorsitz", "approve_protocols")
        vertretung = _nutzer(tenant, "vertretung")
        _vertretung(vorsitz, vertretung)
        response = self._genehmigen(_client(vertretung), meeting)
        assert "in Vertretung für vorsitz@example.org" in _meldungen(response)
        protocol.refresh_from_db()
        assert protocol.status == "approved" and protocol.approved_on_behalf_of == vorsitz
        assert SessionAuditLog.objects.get(object_id=protocol.id, action="approve").on_behalf_of == vorsitz

    def test_beschlussauszug_nicht_von_der_protokollfuehrung(self) -> None:
        tenant, meeting, _protocol, protokoll = self._welt()
        item = SessionAgendaItem.objects.create(meeting=meeting, number="1", name="TOP", vote_result="approved")
        url = f"/session/{tenant.slug}/agenda/{item.id}/forwarding/add/"

        response = _client(protokoll).post(url, {"recipient": "Bauamt"})
        assert "Den Beschlussauszug übergibt eine andere Person" in _meldungen(response)
        assert not SessionResolutionForwarding.objects.exists()

        _client(_nutzer(tenant, "sitzungsdienst", "edit_meetings")).post(url, {"recipient": "Bauamt"})
        assert SessionResolutionForwarding.objects.filter(agenda_item=item).count() == 1


# =============================================================================
# Sitzungsgeld und Pauschalen
# =============================================================================


class _Posten:
    def __init__(self, created_by_id: Any) -> None:
        self.created_by_id = created_by_id
        self.status = "pending"
        self.approved_by: Any = None
        self.approved_at: Any = None

    def save(self, update_fields: list[str] | None = None) -> None:
        pass


class TestSitzungsgeld:
    def test_abschaltbar_je_mandant(self) -> None:
        tenant = _tenant(four_eyes_allowances=False)
        assert not four_eyes_service.required(tenant, four_eyes_service.PROCESS_ALLOWANCE)
        approver = SimpleNamespace(pk=7)
        ergebnis = allowance_service.approve_allowances([_Posten(7)], approver, four_eyes=False)
        assert ergebnis == {"approved": 1, "blocked_four_eyes": 0}
        ergebnis = allowance_service.approve_monthly_allowances([_Posten(7)], approver, four_eyes=True)
        assert ergebnis == {"approved": 0, "blocked_four_eyes": 1}


# =============================================================================
# Arbeitsvorrat und Benachrichtigungen
# =============================================================================


class TestArbeitsvorrat:
    def test_mitzeichnung_in_vertretung(self) -> None:
        tenant = _tenant()
        amt = SessionOrganization.objects.create(tenant=tenant, name="Kämmerei", organization_type="department")
        kaemmerin = _nutzer(tenant, "kaemmerin")
        kaemmerin.departments.add(amt)
        vertretung = _nutzer(tenant, "vertretung")
        paper = _vorlage(tenant, None)
        station = SessionCosignature.objects.create(paper=paper, department=amt, order=1)

        assert not cosign_service.can_decide(_frisch(vertretung), station)
        _vertretung(kaemmerin, vertretung, scope_worklist=False)
        assert list(cosign_service.my_pending_cosignatures(_frisch(vertretung))) == []

        SessionDelegation.objects.update(scope_worklist=True)
        deputy = _frisch(vertretung)
        assert list(cosign_service.my_pending_cosignatures(deputy)) == [station]
        assert cosign_service.acting_for(deputy, station) == kaemmerin

        liste = _client(vertretung).get(f"/session/{tenant.slug}/cosignatures/")
        assert b"meine-vertretungen" in liste.content
        assert "in Vertretung für kaemmerin@example.org".encode() in liste.content

        response = _client(vertretung).post(f"/session/{tenant.slug}/cosignatures/{station.id}/sign/")
        assert "in Vertretung für kaemmerin@example.org" in _meldungen(response)
        station.refresh_from_db()
        assert station.status == "signed" and station.decided_by == vertretung
        eintrag = SessionAuditLog.objects.filter(object_id=paper.id, changes__has_key="mitzeichnung").get()
        assert eintrag.on_behalf_of == kaemmerin


class TestBenachrichtigungen:
    def test_freigabe_aufforderung_in_kopie_an_die_vertretung(self) -> None:
        tenant = _tenant()
        _nutzer(tenant, "leitung", "approve_papers")
        leitung = SessionUser.objects.get(user__email="leitung@example.org")
        vertretung = _nutzer(tenant, "vertretung")
        ohne_umfang = _nutzer(tenant, "ohne-umfang")
        _vertretung(leitung, vertretung)
        _vertretung(leitung, ohne_umfang, scope_notifications=False)
        autorin = _nutzer(tenant, "autorin", "edit_papers")
        paper = _vorlage(tenant, autorin, status="draft", has_financial_impact=False)

        mail.outbox = []
        _aktion(_client(autorin), paper, "submit")
        empfaenger = {address for message in mail.outbox for address in message.to}
        assert {"leitung@example.org", "vertretung@example.org"} <= empfaenger
        assert "ohne-umfang@example.org" not in empfaenger

    def test_nichtoeffentliche_vorlage_nur_an_vertretung_mit_sichtrecht(self) -> None:
        tenant = _tenant()
        leitung = _nutzer(tenant, "leitung", "approve_papers", "view_non_public_papers")
        vertretung = _nutzer(tenant, "vertretung")
        _vertretung(leitung, vertretung)
        autorin = _nutzer(tenant, "autorin", "edit_papers", "view_non_public_papers")
        paper = _vorlage(tenant, autorin, status="draft", has_financial_impact=False, is_public=False)

        mail.outbox = []
        _aktion(_client(autorin), paper, "submit")
        empfaenger = {address for message in mail.outbox for address in message.to}
        assert "leitung@example.org" in empfaenger
        assert "vertretung@example.org" not in empfaenger

    def test_zurueckweisung_geht_an_die_vertretung_der_autorin(self) -> None:
        tenant = _tenant()
        autorin = _nutzer(tenant, "autorin", "edit_papers")
        vertretung = _nutzer(tenant, "vertretung")
        _vertretung(autorin, vertretung)
        paper = _vorlage(tenant, autorin)

        mail.outbox = []
        _aktion(_client(_nutzer(tenant, "pruefer", "approve_papers")), paper, "reject", comment="bitte ergänzen")
        assert mail.outbox and set(mail.outbox[-1].to) == {"autorin@example.org", "vertretung@example.org"}


# =============================================================================
# Einstellungen und Übersicht
# =============================================================================


class TestEinstellungen:
    def test_vier_augen_speichern(self) -> None:
        tenant = _tenant()
        admin = _nutzer(tenant, "admin", "manage_settings")
        response = _client(admin).post(
            f"/session/{tenant.slug}/settings/four-eyes/",
            {"four_eyes_papers": "always", "four_eyes_protocols": "on", "four_eyes_forwardings": "on"},
        )
        assert response.status_code == 302
        tenant.refresh_from_db()
        assert (tenant.four_eyes_papers, tenant.four_eyes_protocols, tenant.four_eyes_forwardings) == (
            "always",
            True,
            True,
        )
        assert tenant.four_eyes_allowances is False
        eintrag = SessionAuditLog.objects.get(object_id=tenant.id, action="update")
        assert eintrag.changes["four_eyes_allowances"] == {"alt": True, "neu": False}

        seite = _client(admin).get(f"/session/{tenant.slug}/settings/four-eyes/")
        assert seite.status_code == 200 and b"vier-augen-einstellungen" in seite.content

    def test_vertretung_eintragen_uebersicht_und_aufheben(self) -> None:
        tenant = _tenant()
        # Freigaberechte überträgt per Vertretung nur, wer sie selbst vergeben darf (hier: Administrator)
        admin = _nutzer(tenant, "admin", "manage_users", admin=True)
        leitung = _nutzer(tenant, "leitung", "approve_papers")
        vertretung = _nutzer(tenant, "vertretung")
        client = _client(admin)

        client.post(
            f"/session/{tenant.slug}/settings/delegations/create/",
            {
                "principal": str(leitung.id),
                "deputy": str(vertretung.id),
                "start_date": HEUTE.isoformat(),
                "end_date": (HEUTE + timedelta(days=14)).isoformat(),
                "scopes": ["approvals", "notifications"],
            },
        )
        delegation = SessionDelegation.objects.get()
        assert (delegation.scope_approvals, delegation.scope_worklist, delegation.scope_notifications) == (
            True,
            False,
            True,
        )
        assert delegation.created_by == admin
        assert SessionAuditLog.objects.filter(object_id=delegation.id, action="create").exists()
        assert "approve_papers" in SessionPermissionChecker(_frisch(vertretung)).permissions

        seite = client.get(f"/session/{tenant.slug}/settings/delegations/")
        assert seite.status_code == 200
        assert seite.context["overview"]["active"] == [delegation]

        client.post(f"/session/{tenant.slug}/settings/delegations/{delegation.id}/revoke/")
        delegation.refresh_from_db()
        assert delegation.revoked_at is not None and delegation.revoked_by == admin
        assert "approve_papers" not in SessionPermissionChecker(_frisch(vertretung)).permissions
        assert client.get(f"/session/{tenant.slug}/settings/delegations/").context["overview"]["past"] == [delegation]

    @pytest.mark.parametrize(
        ("daten", "meldung"),
        [
            ({"deputy": "leitung"}, "nicht selbst vertreten"),
            ({"start_date": 5, "end_date": 2}, "vor ihrem Beginn"),
            ({"start_date": -10, "end_date": -2}, "Vergangenheit"),
            ({"end_date": 400}, "höchstens ein Jahr"),
            ({"scopes": []}, "mindestens einen Umfang"),
        ],
        ids=["selbst", "ende-vor-beginn", "vergangen", "zu-lang", "ohne-umfang"],
    )
    def test_ungueltige_vertretung(self, daten: dict[str, Any], meldung: str) -> None:
        tenant = _tenant()
        admin = _nutzer(tenant, "admin", "manage_users")
        nutzer = {"leitung": _nutzer(tenant, "leitung"), "vertretung": _nutzer(tenant, "vertretung")}
        post = {
            "principal": str(nutzer["leitung"].id),
            "deputy": str(nutzer[daten.get("deputy", "vertretung")].id),
            "start_date": (HEUTE + timedelta(days=daten.get("start_date", 0))).isoformat(),
            "end_date": (HEUTE + timedelta(days=daten.get("end_date", 7))).isoformat(),
            "scopes": daten.get("scopes", ["approvals"]),
        }
        response = _client(admin).post(f"/session/{tenant.slug}/settings/delegations/create/", post)
        assert meldung in _meldungen(response)
        assert not SessionDelegation.objects.exists()

    def test_fremde_nutzer_und_vertretungen_bleiben_unerreichbar(self) -> None:
        tenant = _tenant()
        fremd = _tenant("fremd")
        admin = _nutzer(tenant, "admin", "manage_users")
        eigene = _nutzer(tenant, "eigene")
        fremde = [_nutzer(fremd, "fremd-a"), _nutzer(fremd, "fremd-b")]
        fremde_vertretung = _vertretung(fremde[0], fremde[1])
        client = _client(admin)

        client.post(
            f"/session/{tenant.slug}/settings/delegations/create/",
            {
                "principal": str(fremde[0].id),
                "deputy": str(eigene.id),
                "start_date": HEUTE.isoformat(),
                "end_date": HEUTE.isoformat(),
                "scopes": ["approvals"],
            },
        )
        assert SessionDelegation.objects.count() == 1
        seite = client.get(f"/session/{tenant.slug}/settings/delegations/")
        assert b"fremd-a@example.org" not in seite.content
        antwort = client.post(f"/session/{tenant.slug}/settings/delegations/{fremde_vertretung.id}/revoke/")
        assert antwort.status_code == 404
        fremde_vertretung.refresh_from_db()
        assert fremde_vertretung.revoked_at is None

    def test_hinweis_wenn_die_vertretung_selbst_abwesend_ist(self) -> None:
        tenant = _tenant()
        admin = _nutzer(tenant, "admin", "manage_users")
        leitung, vertretung, dritte = (
            _nutzer(tenant, "leitung"),
            _nutzer(tenant, "vertretung"),
            _nutzer(tenant, "dritte"),
        )
        _vertretung(vertretung, dritte)
        response = _client(admin).post(
            f"/session/{tenant.slug}/settings/delegations/create/",
            {
                "principal": str(leitung.id),
                "deputy": str(vertretung.id),
                "start_date": HEUTE.isoformat(),
                "end_date": HEUTE.isoformat(),
                "scopes": ["approvals"],
            },
        )
        assert "nicht weitergereicht" in _meldungen(response)
