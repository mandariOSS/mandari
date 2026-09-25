# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vollständige Protokollierung im Session RIS (Issue #221).

Geprüft wird:
- Lesezugriffe auf Nichtöffentliches (Vorlage, Sitzung, TOP, Niederschrift) mit Nutzer, Zeitpunkt
  und Objekt – ohne Inhalte, je Mandant abschaltbar, innerhalb von zehn Minuten zusammengefasst
- Anmeldung, Abmeldung und Fehlversuche (auch zweiter Faktor) – nie mit Passwort, unbekannte
  Kennungen nur als HMAC im mandantenübergreifenden Sicherheitsprotokoll
- Export als CSV, JSON und ZIP mit SHA-256-Prüfsumme und Schutz gegen Formel-Injektion
- Kettenprüfung in der Oberfläche, Archivpaket vor der fristgerechten Löschung, Prüfbarkeit danach
- Kontrollrechte: nur Revision/Datenschutz, nicht die Administrator-Vollmacht; Mandantentrennung
- Direkte Einträge für Stimmabgaben, Mitzeichnungen, Pauschalen, Rollen- und Rechteänderungen
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts import security_audit
from apps.accounts.models import SecurityAuditLog, User
from apps.common import audit_chain
from apps.common.models import AuditChainHead
from apps.common.tests.factories import DEFAULT_PASSWORD, UserFactory
from apps.session import audit
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionAuditLog,
    SessionCosignature,
    SessionMeeting,
    SessionMonthlyAllowance,
    SessionMonthlyRate,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionPersonMonthlyRate,
    SessionProtocol,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.permissions import SessionPermissionChecker
from apps.session.services import allowance_service, audit_log_service

pytestmark = pytest.mark.django_db

GEHEIM = "GEHEIMER-BETREFF-XYZ"


# =============================================================================
# Hilfen
# =============================================================================


def _tenant(slug: str = "musterstadt", **kwargs: Any) -> SessionTenant:
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


def _eintraege(tenant: SessionTenant, action: str, **filters: Any) -> list[SessionAuditLog]:
    return list(SessionAuditLog.objects.filter(tenant=tenant, action=action, **filters).order_by("created_at"))


def _meldungen(response: Any) -> str:
    return " ".join(str(m) for m in get_messages(response.wsgi_request))


def _body(response: Any) -> bytes:
    return b"".join(response.streaming_content) if response.streaming else bytes(response.content)


def _alle_protokollwerte() -> str:
    """Alle gespeicherten Protokollwerte als Text (für Negativprüfungen: kein Passwort, keine Kennung)."""
    teile = []
    for model in (SessionAuditLog, SecurityAuditLog):
        for zeile in model.objects.values():
            teile.append(json.dumps(zeile, default=str, ensure_ascii=False))
    return "\n".join(teile)


def _vergangen(tage: int) -> Any:
    """Zeitpunkt für die Hash-Kette zurückdrehen (sie setzt created_at unter ihrer Sperre)."""
    return mock.patch("django.utils.timezone.now", return_value=timezone.now() - dt.timedelta(days=tage))


@pytest.fixture
def archiv(tmp_path: Path) -> Iterator[Path]:
    with override_settings(AUDIT_ARCHIVE_ROOT=tmp_path, AUDIT_ARCHIVE_STORAGE=""):
        yield tmp_path


# =============================================================================
# Lesezugriffe
# =============================================================================


class TestLesezugriffe:
    def test_noe_vorlage_erzeugt_eintrag_ohne_inhalt(self) -> None:
        tenant = _tenant()
        leser = _nutzer(tenant, "leser", "view_papers", "view_non_public_papers")
        paper = SessionPaper.objects.create(tenant=tenant, reference="V/2026/0042", name=GEHEIM, is_public=False)
        vorher = timezone.now()

        response = _client(leser).get(f"/session/{tenant.slug}/papers/{paper.id}/")

        assert response.status_code == 200
        eintrag = SessionAuditLog.objects.get(tenant=tenant, action="view", object_id=paper.id)
        assert eintrag.user == leser
        assert eintrag.model_name == "SessionPaper"
        assert eintrag.created_at >= vorher
        assert eintrag.object_repr == "Vorlage V/2026/0042"
        assert GEHEIM not in eintrag.object_repr and GEHEIM not in json.dumps(eintrag.changes)
        assert eintrag.seq is not None and eintrag.entry_hash

    def test_oeffentliche_vorlage_ohne_eintrag(self) -> None:
        tenant = _tenant()
        leser = _nutzer(tenant, "leser", "view_papers", "view_non_public_papers")
        paper = SessionPaper.objects.create(tenant=tenant, reference="V/2026/0001", name="Offen", is_public=True)
        assert _client(leser).get(f"/session/{tenant.slug}/papers/{paper.id}/").status_code == 200
        assert not _eintraege(tenant, "view")

    def test_je_mandant_abschaltbar(self) -> None:
        tenant = _tenant(settings={"privacy": {"read_logging": False}})
        leser = _nutzer(tenant, "leser", "view_papers", "view_non_public_papers")
        paper = SessionPaper.objects.create(tenant=tenant, reference="V/1", name=GEHEIM, is_public=False)
        assert _client(leser).get(f"/session/{tenant.slug}/papers/{paper.id}/").status_code == 200
        assert not _eintraege(tenant, "view")

    def test_schalter_in_den_datenschutz_einstellungen(self) -> None:
        tenant = _tenant()
        admin = _nutzer(tenant, "admin", "manage_settings")
        client = _client(admin)
        client.post(f"/session/{tenant.slug}/settings/privacy/", {"persons_years": "0", "audit_years": "0"})
        tenant.refresh_from_db()
        assert tenant.settings["privacy"]["read_logging"] is False
        eintrag = _eintraege(tenant, "update", object_id=tenant.id)[-1]
        assert eintrag.changes["dsgvo_einstellungen"]["lesezugriffe_protokollieren"] == {"alt": True, "neu": False}
        client.post(f"/session/{tenant.slug}/settings/privacy/", {"read_logging": "1"})
        tenant.refresh_from_db()
        assert audit.read_logging_enabled(tenant)

    def test_wiederholte_aufrufe_werden_zusammengefasst(self) -> None:
        tenant = _tenant()
        leser = _nutzer(tenant, "leser", "view_papers", "view_non_public_papers")
        paper = SessionPaper.objects.create(tenant=tenant, reference="V/7", name=GEHEIM, is_public=False)
        client = _client(leser)
        url = f"/session/{tenant.slug}/papers/{paper.id}/"
        for _ in range(3):
            client.get(url)
        assert len(_eintraege(tenant, "view", object_id=paper.id)) == 1

        # Nach Ablauf des Zeitfensters zählt der nächste Aufruf neu
        session = client.session
        session[audit.READ_SESSION_KEY] = {
            key: stamp - audit.READ_DEDUP_SECONDS - 1 for key, stamp in session[audit.READ_SESSION_KEY].items()
        }
        session.save()
        client.get(url)
        assert len(_eintraege(tenant, "view", object_id=paper.id)) == 2

    def test_noe_sitzung_top_und_niederschrift(self) -> None:
        tenant = _tenant()
        org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
        meeting = SessionMeeting.objects.create(
            tenant=tenant, name="Ratssitzung", organization=org, start=timezone.now(), is_public=True
        )
        top = SessionAgendaItem.objects.create(meeting=meeting, number="N1", name=GEHEIM, is_public=False)
        protocol = SessionProtocol.objects.create(meeting=meeting, content="Öffentlich")
        cast(Any, protocol).set_content_encrypted("Geheimer Teil")
        protocol.save()
        berechtigt = _nutzer(
            tenant, "berechtigt", "view_meetings", "view_non_public_meetings", "view_protocols", "edit_meetings"
        )
        ohne_noe = _nutzer(tenant, "ohne-noe", "view_meetings", "view_protocols")
        base = f"/session/{tenant.slug}"

        assert _client(ohne_noe).get(f"{base}/meetings/{meeting.id}/").status_code == 200
        assert not _eintraege(tenant, "view"), "Ohne NÖ-Recht sieht niemand Nichtöffentliches"

        client = _client(berechtigt)
        assert client.get(f"{base}/meetings/{meeting.id}/").status_code == 200
        assert client.get(f"{base}/agenda/{top.id}/edit/").status_code == 200
        assert client.get(f"{base}/meetings/{meeting.id}/protocol/").status_code == 200
        gelesen = {(e.model_name, e.object_id) for e in _eintraege(tenant, "view", user=berechtigt)}
        assert gelesen == {
            ("SessionMeeting", meeting.id),
            ("SessionAgendaItem", top.id),
            ("SessionProtocol", protocol.id),
        }
        assert all(GEHEIM not in e.object_repr for e in _eintraege(tenant, "view"))

    def test_interne_niederschrift_als_pdf(self) -> None:
        tenant = _tenant()
        org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
        meeting = SessionMeeting.objects.create(tenant=tenant, name="S", organization=org, start=timezone.now())
        protocol = SessionProtocol.objects.create(meeting=meeting, content="x")
        leser = _nutzer(tenant, "leser", "view_protocols", "view_meetings", "view_non_public_meetings")
        with mock.patch("apps.session.services.protocol_service.build_protocol_pdf", return_value=b"%PDF-1.4"):
            response = _client(leser).get(
                f"/session/{tenant.slug}/meetings/{meeting.id}/niederschrift.pdf?fassung=intern"
            )
        assert response.status_code == 200
        assert _eintraege(tenant, "download", object_id=protocol.id)

    def test_noe_fassung_und_anlagen_download(self, tmp_path: Path) -> None:
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.session.models import SessionFile
        from apps.session.services import paper_version_service

        tenant = _tenant()
        leser = _nutzer(tenant, "leser", "view_papers", "view_non_public_papers")
        paper = SessionPaper.objects.create(tenant=tenant, reference="V/3", name=GEHEIM, is_public=False)
        version = paper_version_service.save_manually(paper, user=None, note="")
        client = _client(leser)
        base = f"/session/{tenant.slug}/papers/{paper.id}/fassungen"
        assert client.get(f"{base}/{version.number}/").status_code == 200
        ansicht = _eintraege(tenant, "view", object_id=paper.id)
        assert len(ansicht) == 1 and "Fassung" in ansicht[0].changes["umfang"]

        with override_settings(MEDIA_ROOT=str(tmp_path)):
            anlage = SessionFile.objects.create(
                tenant=tenant,
                paper=paper,
                name="anlage.txt",
                file=SimpleUploadedFile("anlage.txt", b"x"),
                is_public=True,
            )
            response = client.get(f"/session/{tenant.slug}/files/{anlage.id}/download/")
            assert response.status_code == 200
            _body(response)
        download = _eintraege(tenant, "download", object_id=anlage.id)
        assert download and download[0].changes == {"nichtoeffentlich": True}, "Anlage einer NÖ-Vorlage"

    def test_protokollfehler_bricht_die_seite_nicht(self) -> None:
        tenant = _tenant()
        leser = _nutzer(tenant, "leser", "view_papers", "view_non_public_papers")
        paper = SessionPaper.objects.create(tenant=tenant, reference="V/9", name=GEHEIM, is_public=False)
        with mock.patch("apps.common.audit_chain.append", side_effect=RuntimeError("Datenbank weg")):
            response = _client(leser).get(f"/session/{tenant.slug}/papers/{paper.id}/")
        assert response.status_code == 200


# =============================================================================
# Anmeldungen
# =============================================================================


class TestAnmeldung:
    FALSCH = "Falsches-Geheimwort-987!"

    def test_fehlversuch_eines_session_nutzers_steht_im_mandantenprotokoll(self) -> None:
        tenant = _tenant()
        nutzer = _nutzer(tenant, "rat", "view_meetings")
        Client().post(reverse("accounts:login"), {"email": nutzer.user.email, "password": self.FALSCH})

        eintrag = SessionAuditLog.objects.get(tenant=tenant, action="login_failed")
        assert eintrag.object_id == nutzer.id and eintrag.user is None
        assert eintrag.changes == {"grund": security_audit.REASON_CREDENTIALS}
        assert self.FALSCH not in _alle_protokollwerte()

    def test_fehlversuch_mit_unbekannter_kennung_nur_als_hmac(self) -> None:
        kennung = "niemand@example.org"
        Client().post(reverse("accounts:login"), {"email": kennung, "password": self.FALSCH})

        eintrag = SecurityAuditLog.objects.get(
            event="login_failed", identifier_hash=security_audit.identifier_hash(kennung)
        )
        assert eintrag.user_ref is None
        assert eintrag.identifier_hash != hashlib.sha256(kennung.encode()).hexdigest(), "Nur schlüsselgebunden"
        werte = _alle_protokollwerte()
        assert kennung not in werte and self.FALSCH not in werte
        assert security_audit.identifier_hash(" NIEMAND@example.org ") == eintrag.identifier_hash

    def test_anmeldung_und_abmeldung_im_mandantenprotokoll(self) -> None:
        tenant = _tenant()
        nutzer = _nutzer(tenant, "rat", "view_meetings")
        client = Client()
        response = client.post(reverse("accounts:login"), {"email": nutzer.user.email, "password": DEFAULT_PASSWORD})
        assert response.status_code == 302
        client.post(reverse("accounts:logout"))

        assert [e.user for e in _eintraege(tenant, "login")] == [nutzer]
        assert [e.user for e in _eintraege(tenant, "logout")] == [nutzer]
        assert not SecurityAuditLog.objects.filter(user_ref=nutzer.user.pk).exists(), "Nicht doppelt protokolliert"
        assert DEFAULT_PASSWORD not in _alle_protokollwerte()

    def test_nutzer_ohne_mandant_im_sicherheitsprotokoll(self) -> None:
        user = cast(User, UserFactory(email="fraktion@example.org"))  # type: ignore[no-untyped-call]
        client = Client()
        client.post(reverse("accounts:login"), {"email": user.email, "password": DEFAULT_PASSWORD})
        client.post(reverse("accounts:logout"))
        ereignisse = list(
            SecurityAuditLog.objects.filter(user_ref=user.pk).order_by("seq").values_list("event", "user_ref")
        )
        assert ereignisse == [("login", user.pk), ("logout", user.pk)]
        assert audit_chain.verify(audit_chain.SECURITY, None).ok

    def test_falscher_zweiter_faktor(self) -> None:
        from apps.accounts.tests.test_login_2fa import PASSWORD, code_step, enable_2fa, make_user, password_step

        user = make_user("zwei@example.org")
        enable_2fa(user)
        client = Client()
        password_step(client, user)
        code_step(client, "000000")
        eintrag = SecurityAuditLog.objects.get(event="login_failed", user_ref=user.pk)
        assert eintrag.details == {"grund": security_audit.REASON_SECOND_FACTOR}
        assert PASSWORD not in _alle_protokollwerte()

    def test_protokollfehler_verhindert_keine_anmeldung(self) -> None:
        user = cast(User, UserFactory(email="robust@example.org"))  # type: ignore[no-untyped-call]
        client = Client()
        with mock.patch("apps.accounts.security_audit._write_global", side_effect=RuntimeError("weg")):
            response = client.post(reverse("accounts:login"), {"email": user.email, "password": DEFAULT_PASSWORD})
        assert response.status_code == 302
        assert "_auth_user_id" in client.session


# =============================================================================
# Export
# =============================================================================


class TestExport:
    def _welt(self) -> tuple[SessionTenant, SessionUser]:
        tenant = _tenant()
        revision = _nutzer(tenant, "revision", "view_audit_log", "export_audit_log")
        # Wert mit Formel-Anfang (z. B. ein böswillig benanntes Gremium)
        SessionOrganization.objects.create(tenant=tenant, name='=HYPERLINK("https://example.org","klick")')
        fremd = _tenant("fremdstadt")
        SessionOrganization.objects.create(tenant=fremd, name="FREMD-GREMIUM-ABC")
        return tenant, revision

    def _export(self, client: Client, tenant: SessionTenant, fmt: str, **data: str) -> Any:
        return client.post(f"/session/{tenant.slug}/audit/export/", {"format": fmt, **data})

    def test_csv_mit_pruefsumme_und_formelschutz(self) -> None:
        tenant, revision = self._welt()
        response = self._export(_client(revision), tenant, "csv")
        assert response.status_code == 200
        inhalt = _body(response)
        assert response["X-Checksum-SHA256"] == hashlib.sha256(inhalt).hexdigest()
        text = inhalt.decode("utf-8-sig")
        zeilen = list(csv.reader(io.StringIO(text), delimiter=";"))
        kopf = zeilen[0]
        assert {"seq", "created_at", "action", "object_repr", "entry_hash", "anzeige.benutzer"} <= set(kopf)
        werte = [zelle for zeile in zeilen[1:] for zelle in zeile]
        assert any(zelle.startswith("'=HYPERLINK") for zelle in werte)
        assert not any(zelle.startswith(("=", "+", "-", "@")) for zelle in werte)
        assert "FREMD-GREMIUM-ABC" not in text

        export = _eintraege(tenant, "audit_export")
        assert len(export) == 1 and export[0].changes["sha256"] == response["X-Checksum-SHA256"]
        assert export[0].user == revision

    def test_json_mit_pruefsumme_im_umschlag_und_nachrechenbaren_hashes(self) -> None:
        tenant, revision = self._welt()
        response = self._export(_client(revision), tenant, "json")
        inhalt = _body(response)
        assert response["X-Checksum-SHA256"] == hashlib.sha256(inhalt).hexdigest()
        dokument = json.loads(inhalt)
        assert dokument["mandant"]["slug"] == tenant.slug
        assert dokument["anzahl"] == len(dokument["eintraege"]) > 0
        pruefsumme = hashlib.sha256(audit_chain.canonical_json(dokument["eintraege"]).encode()).hexdigest()
        assert dokument["pruefsumme"]["wert"] == pruefsumme
        for eintrag in dokument["eintraege"]:
            assert eintrag["tenant_id"] == str(tenant.pk)
            gehasht = {k: v for k, v in eintrag.items() if k not in ("entry_hash", "anzeige")}
            assert audit_chain.hash_payload(gehasht) == eintrag["entry_hash"]
        assert "FREMD-GREMIUM-ABC" not in inhalt.decode()

    def test_zip_mit_begleitdatei(self) -> None:
        tenant, revision = self._welt()
        response = self._export(_client(revision), tenant, "zip")
        inhalt = _body(response)
        assert response["X-Checksum-SHA256"] == hashlib.sha256(inhalt).hexdigest()
        with zipfile.ZipFile(io.BytesIO(inhalt)) as paket:
            namen = set(paket.namelist())
            assert "SHA256SUMS" in namen and "kette.json" in namen
            for zeile in paket.read("SHA256SUMS").decode().splitlines():
                summe, name = zeile.split("  ", 1)
                assert hashlib.sha256(paket.read(name)).hexdigest() == summe

    def test_zeitraum(self) -> None:
        tenant, revision = self._welt()
        org = SessionOrganization.objects.get(tenant=tenant)
        with _vergangen(40):
            audit.log_event("update", org, tenant=tenant, changes={"alt": "ALT-EINTRAG"})
        heute = timezone.localdate()
        response = self._export(
            _client(revision),
            tenant,
            "json",
            **{"from": (heute - dt.timedelta(days=1)).isoformat(), "to": heute.isoformat()},
        )
        text = _body(response).decode()
        assert "ALT-EINTRAG" not in text
        alles = _body(self._export(_client(revision), tenant, "json")).decode()
        assert "ALT-EINTRAG" in alles

    def test_obergrenze(self) -> None:
        tenant, revision = self._welt()
        with override_settings(AUDIT_EXPORT_MAX_ROWS=1):
            response = self._export(_client(revision), tenant, "csv")
        assert response.status_code == 302
        assert "nicht exportieren" in _meldungen(response)
        assert not _eintraege(tenant, "audit_export")

    def test_befehl_fuer_grosse_zeitraeume(self, tmp_path: Path) -> None:
        tenant, _revision = self._welt()
        out = io.StringIO()
        call_command("export_audit_log", tenant=tenant.slug, format="zip", output=str(tmp_path), stdout=out)
        datei = next(tmp_path.glob("*.zip"))
        assert hashlib.sha256(datei.read_bytes()).hexdigest() in out.getvalue()
        assert _eintraege(tenant, "audit_export")[0].changes["weg"] == "Kommandozeile"


# =============================================================================
# Rechte und Mandantentrennung
# =============================================================================


class TestRechte:
    @pytest.mark.parametrize(
        ("perms", "admin", "ansicht", "export"),
        [
            ((), False, 403, 403),
            (("view_meetings", "manage_users", "manage_settings"), False, 403, 403),
            ((), True, 403, 403),  # Administrator-Vollmacht umfasst die Kontrollrechte nicht
            (("view_audit_log",), False, 200, 403),
            (("view_audit_log", "export_audit_log"), False, 200, 200),
        ],
        ids=["ohne", "verwaltung", "admin", "nur-ansicht", "revision"],
    )
    def test_ansicht_export_und_pruefung(self, perms: tuple[str, ...], admin: bool, ansicht: int, export: int) -> None:
        tenant = _tenant()
        client = _client(_nutzer(tenant, "person", *perms, admin=admin))
        base = f"/session/{tenant.slug}/audit"
        assert client.get(f"{base}/").status_code == ansicht
        response = client.post(f"{base}/export/", {"format": "json"})
        assert response.status_code == export
        pruefung = client.post(f"{base}/pruefen/")
        assert pruefung.status_code == (302 if export == 200 else 403)

    def test_fremder_mandant(self) -> None:
        tenant = _tenant()
        fremd = _tenant("fremdstadt")
        revision = _client(_nutzer(tenant, "revision", "view_audit_log", "export_audit_log"))
        assert revision.get(f"/session/{fremd.slug}/audit/").status_code == 403
        assert revision.post(f"/session/{fremd.slug}/audit/export/", {"format": "json"}).status_code == 403
        assert revision.post(f"/session/{fremd.slug}/audit/pruefen/").status_code == 403

    def test_ansicht_zeigt_nur_eigene_eintraege_und_wird_protokolliert(self) -> None:
        tenant = _tenant()
        fremd = _tenant("fremdstadt")
        SessionOrganization.objects.create(tenant=fremd, name="FREMD-GREMIUM-ABC")
        revision = _nutzer(tenant, "revision", "view_audit_log")
        response = _client(revision).get(f"/session/{tenant.slug}/audit/", {"action": "login"})
        assert response.status_code == 200
        assert b"FREMD-GREMIUM-ABC" not in response.content
        einsicht = _eintraege(tenant, "audit_view")
        assert len(einsicht) == 1 and einsicht[0].user == revision
        assert einsicht[0].changes == {"filter": {"action": "login"}}
        assert not SessionAuditLog.objects.filter(tenant=fremd, action="audit_view").exists()

    def test_standardrollen(self) -> None:
        tenant = _tenant()
        rollen = SessionRole.create_default_roles(tenant)
        for key in ("revision", "privacy"):
            rolle = rollen[key]
            assert rolle.can_view_audit_log and rolle.can_export_audit_log and not rolle.is_admin
            assert not rolle.can_view_non_public_meetings and not rolle.can_view_papers
        assert not rollen["admin"].can_view_audit_log

        admin = SessionUser.objects.create(user=cast(User, UserFactory(email="a@example.org")), tenant=tenant)  # type: ignore[no-untyped-call]
        admin.roles.add(rollen["admin"])
        rechte = SessionPermissionChecker(admin).permissions
        assert "manage_users" in rechte and not ({"view_audit_log", "export_audit_log"} & rechte)
        admin.roles.add(rollen["revision"])
        assert {"view_audit_log", "export_audit_log"} <= SessionPermissionChecker(admin).permissions

    def test_kontrollrechte_verlangen_zweiten_faktor(self) -> None:
        from apps.accounts.two_factor_policy import two_factor_reasons

        tenant = _tenant()
        revision = _nutzer(tenant, "revision", "view_audit_log", "export_audit_log")
        with override_settings(TWO_FACTOR_ENFORCEMENT=True):
            assert two_factor_reasons(revision.user)


# =============================================================================
# Kettenprüfung und Archivpaket
# =============================================================================


class TestKetteUndArchiv:
    def test_pruefung_in_der_oberflaeche(self) -> None:
        tenant = _tenant()
        revision = _nutzer(tenant, "revision", "view_audit_log", "export_audit_log")
        client = _client(revision)
        response = client.post(f"/session/{tenant.slug}/audit/pruefen/")
        assert "intakt" in _meldungen(response)
        pruefung = _eintraege(tenant, "audit_verify")[-1]
        assert pruefung.changes["intakt"] is True and pruefung.user == revision

        ziel = SessionAuditLog.objects.filter(tenant=tenant).order_by("seq").first()
        assert ziel is not None
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE session_audit_logs SET object_repr = %s WHERE id = %s",
                ["verfälscht", SessionAuditLog._meta.pk.get_db_prep_value(ziel.pk, connection)],
            )
        response = client.post(f"/session/{tenant.slug}/audit/pruefen/")
        assert "Befund" in _meldungen(response)
        assert _eintraege(tenant, "audit_verify")[-1].changes["intakt"] is False
        # Die Ansicht zeigt das Ergebnis der letzten Prüfung
        seite = client.get(f"/session/{tenant.slug}/audit/")
        assert b"Befund" in seite.content

    def _alt_und_jung(self, tenant: SessionTenant) -> tuple[list[Any], list[Any]]:
        with _vergangen(3 * 365):
            org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
            alt = [audit.log_event("update", org, tenant=tenant, changes={"n": n}) for n in range(3)]
        jung = [audit.log_event("update", org, tenant=tenant, changes={"jung": n}) for n in range(2)]
        return alt, jung

    def test_archivpaket_vor_der_loeschung_und_kette_bleibt_pruefbar(self, archiv: Path) -> None:
        tenant = _tenant()
        alt, jung = self._alt_und_jung(tenant)
        SessionTenant.objects.filter(pk=tenant.pk).update(settings={"privacy": {"audit_years": 1}})
        tenant.refresh_from_db()

        out = io.StringIO()
        call_command("session_privacy_purge", tenant=tenant.slug, stdout=out)

        assert not SessionAuditLog.objects.filter(pk__in=[e.pk for e in alt]).exists()
        assert SessionAuditLog.objects.filter(pk__in=[e.pk for e in jung]).count() == len(jung)
        pakete = list(archiv.rglob("*.zip"))
        assert len(pakete) == 1
        with zipfile.ZipFile(pakete[0]) as paket:
            for zeile in paket.read("SHA256SUMS").decode().splitlines():
                summe, name = zeile.split("  ", 1)
                assert hashlib.sha256(paket.read(name)).hexdigest() == summe
            kette = json.loads(paket.read("kette.json"))
            json_name = next(n for n in paket.namelist() if n.endswith(".json") and n != "kette.json")
            inhalt = json.loads(paket.read(json_name))
        assert {e["id"] for e in inhalt["eintraege"]} >= {str(e.pk) for e in alt}
        kopf = AuditChainHead.objects.get(scope=f"session:{tenant.pk}")
        assert kopf.anchor_seq == kette["bis_nr"] and kopf.anchor_hash == kette["anker_nachher"]
        assert kette["anker_vorher"] == audit_chain.GENESIS_HASH

        ergebnis = audit_log_service.verify_tenant(tenant)
        assert ergebnis.ok, ergebnis.errors
        archivierung = _eintraege(tenant, "audit_archive")
        assert len(archivierung) == 1 and archivierung[0].changes["anzahl"] == kette["bis_nr"]
        assert "Archivpaket" in out.getvalue()
        call_command("verify_audit_chain", tenant=tenant.slug, stdout=io.StringIO())

        # Weitere Einträge und ein zweiter Lauf: Kette bleibt ab dem Anker prüfbar
        audit.log_event("update", tenant, tenant=tenant)
        call_command("session_privacy_purge", tenant=tenant.slug, stdout=io.StringIO())
        assert audit_log_service.verify_tenant(tenant).ok

    def test_manipulierte_kette_wird_nicht_geloescht(self, archiv: Path) -> None:
        tenant = _tenant()
        alt, _jung = self._alt_und_jung(tenant)
        SessionTenant.objects.filter(pk=tenant.pk).update(settings={"privacy": {"audit_years": 1}})
        tenant.refresh_from_db()
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE session_audit_logs SET object_repr = %s WHERE id = %s",
                ["verfälscht", SessionAuditLog._meta.pk.get_db_prep_value(alt[1].pk, connection)],
            )
        from apps.session.services import privacy_service

        stats = privacy_service.run_privacy_purge(tenant, allow_backfill=True)
        assert stats["audit_deleted"] == 0
        assert any("Löschung ausgesetzt" in grund for grund in stats["skipped"])
        assert SessionAuditLog.objects.filter(pk__in=[e.pk for e in alt]).count() == len(alt)
        assert not list(archiv.rglob("*.zip"))

    def test_probelauf_zaehlt_nur(self, archiv: Path) -> None:
        tenant = _tenant()
        alt, _jung = self._alt_und_jung(tenant)
        SessionTenant.objects.filter(pk=tenant.pk).update(settings={"privacy": {"audit_years": 1}})
        tenant.refresh_from_db()
        from apps.session.services import privacy_service

        stats = privacy_service.run_privacy_purge(tenant, dry_run=True)
        assert stats["audit_deleted"] >= len(alt)
        assert SessionAuditLog.objects.filter(pk__in=[e.pk for e in alt]).count() == len(alt)
        assert not list(archiv.rglob("*.zip"))

    def test_sicherheitsprotokoll_frist(self, archiv: Path) -> None:
        SecurityAuditLog.objects.all()._raw_delete(connection.alias)  # eigene, leere Kette für diesen Test
        AuditChainHead.objects.filter(scope=audit_chain.SECURITY.scope_key(None)).delete()
        with _vergangen(400):
            alt = SecurityAuditLog.objects.create(event="login", ip_address="192.0.2.10")
        jung = SecurityAuditLog.objects.create(event="logout", ip_address="192.0.2.10")
        out = io.StringIO()
        call_command("purge_security_audit_log", days=365, stdout=out)
        assert not SecurityAuditLog.objects.filter(pk=alt.pk).exists()
        assert SecurityAuditLog.objects.filter(pk=jung.pk).exists()
        assert list(archiv.rglob("*.zip"))
        assert audit_chain.verify(audit_chain.SECURITY, None).ok


# =============================================================================
# Direkte Einträge: Stimmen, Mitzeichnungen, Pauschalen, Rollen und Rechte
# =============================================================================


class TestDirekteEintraege:
    def test_stimmabgabe(self) -> None:
        tenant = _tenant()
        org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
        meeting = SessionMeeting.objects.create(tenant=tenant, name="Rat", organization=org, start=timezone.now())
        ja = SessionPerson.objects.create(tenant=tenant, given_name="Jana", family_name="Ja")
        nein = SessionPerson.objects.create(tenant=tenant, given_name="Nora", family_name="Nein")
        for person in (ja, nein):
            SessionAttendance.objects.create(meeting=meeting, person=person, status="present")
        top = SessionAgendaItem.objects.create(meeting=meeting, number="1", name="Beschluss")
        client = _client(_nutzer(tenant, "protokoll", "edit_protocols", "view_meetings"))
        url = f"/session/{tenant.slug}/agenda/{top.id}/voting/"

        client.post(
            url,
            {"voting_method": "roll_call", "vote_result": "approved", f"vote_{ja.id}": "yes", f"vote_{nein.id}": "no"},
        )
        client.post(
            url,
            {"voting_method": "roll_call", "vote_result": "approved", f"vote_{ja.id}": "yes", f"vote_{nein.id}": "yes"},
        )

        erste, zweite = _eintraege(tenant, "vote", object_id=top.id)
        assert {s["neu"] for s in erste.changes["stimmen"]} == {"Ja", "Nein"}
        assert zweite.changes["stimmen"] == [{"person": nein.display_name, "alt": "Nein", "neu": "Ja"}]

    def test_mitzeichnung(self) -> None:
        tenant = _tenant()
        amt = SessionOrganization.objects.create(tenant=tenant, name="Kämmerei", organization_type="department")
        kaemmerin = _nutzer(tenant, "kaemmerin", "view_papers")
        kaemmerin.departments.add(amt)
        paper = SessionPaper.objects.create(tenant=tenant, reference="V/5", name="Haushalt", status="review")
        station = SessionCosignature.objects.create(paper=paper, department=amt, order=1)
        _client(kaemmerin).post(f"/session/{tenant.slug}/cosignatures/{station.id}/sign/")
        eintrag = _eintraege(tenant, "cosign", object_id=paper.id)
        assert len(eintrag) == 1 and eintrag[0].changes["mitzeichnung"] == "Kämmerei"
        assert eintrag[0].user == kaemmerin

    def test_monatspauschalen_je_posten(self) -> None:
        tenant = _tenant()
        rate = SessionMonthlyRate.objects.create(tenant=tenant, name="Aufwandsentschädigung", amount="300.00")
        person = SessionPerson.objects.create(tenant=tenant, given_name="Paula", family_name="Pauschal")
        SessionPersonMonthlyRate.objects.create(person=person, rate=rate)
        erzeuger = _nutzer(tenant, "kasse", "manage_allowances")
        heute = timezone.localdate()
        allowance_service.generate_monthly_allowances(tenant, heute.year, heute.month, created_by=erzeuger)
        posten = SessionMonthlyAllowance.objects.get(tenant=tenant)
        assert _eintraege(tenant, "allowance_created", object_id=posten.id)

        genehmiger = _nutzer(tenant, "leitung", "manage_allowances")
        allowance_service.approve_monthly_allowances([posten], genehmiger)
        genehmigt = _eintraege(tenant, "allowance_approved", object_id=posten.id)
        assert len(genehmigt) == 1 and genehmigt[0].changes["status"] == {"alt": "pending", "neu": "approved"}

    def test_rollenzuweisung(self) -> None:
        tenant = _tenant()
        verwaltung = _nutzer(tenant, "verwaltung", "manage_users")
        ziel = _nutzer(tenant, "ziel", "view_meetings")
        revision = SessionRole.objects.create(tenant=tenant, name="Revision", can_view_audit_log=True)
        alt = ziel.roles.get()
        _client(verwaltung).post(
            f"/session/{tenant.slug}/settings/users/{ziel.id}/roles/", {"roles": [str(revision.id)]}
        )
        eintrag = _eintraege(tenant, "roles_changed", object_id=ziel.id)
        assert len(eintrag) == 1
        assert eintrag[0].changes == {"hinzugefuegt": ["Revision"], "entzogen": [alt.name]}
        assert eintrag[0].user == verwaltung

    def test_rechteaenderung_einer_rolle(self) -> None:
        tenant = _tenant()
        verwaltung = _nutzer(tenant, "verwaltung", "manage_users")
        rolle = SessionRole.objects.create(tenant=tenant, name="Sitzungsdienst", can_view_meetings=True)
        _client(verwaltung).post(
            f"/session/{tenant.slug}/settings/roles/save/",
            {"role_id": str(rolle.id), "name": "Sitzungsdienst", "can_edit_meetings": "1", "can_view_audit_log": "1"},
        )
        eintrag = _eintraege(tenant, "permissions_changed", object_id=rolle.id)
        assert len(eintrag) == 1
        assert eintrag[0].changes["erteilt"] == ["edit_meetings", "view_audit_log"]
        assert "view_meetings" in eintrag[0].changes["entzogen"]


def test_zeitmessung_der_zusammenfassung_nutzt_die_session() -> None:
    """Der Merker lebt in der Login-Session – kein zusätzlicher Datenbankzugriff je Aufruf."""
    request = mock.Mock(session={})
    tenant = mock.Mock(pk="t")
    jetzt = time.time()
    audit._remember_read(request, f"{tenant.pk}:x", jetzt)
    assert audit._read_seen(request, f"{tenant.pk}:x", jetzt + 1)
    assert not audit._read_seen(request, f"{tenant.pk}:x", jetzt + audit.READ_DEDUP_SECONDS + 1)
