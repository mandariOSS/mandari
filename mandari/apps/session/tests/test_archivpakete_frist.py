# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufbewahrungsfrist mit automatischer Löschung für Archivpakete des Protokolls (Issue #366).

Archivpakete entstehen vor der fristgerechten Löschung von Protokolleinträgen (Issue #221). Bisher blieben sie
unbegrenzt liegen. Geprüft wird: Frist je Session-Mandant (Einstellungen → Datenschutz) und für das
Sicherheitsprotokoll, Grundlage ist der Ablagezeitpunkt im Dateinamen, nur Pakete des eigenen Bereichs, Probelauf,
Frist 0 löscht nichts, und jede Löschung steht im Protokoll.
"""

from __future__ import annotations

import datetime as dt
import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.models import SecurityAuditLog, User
from apps.common import audit_archive, audit_chain
from apps.common.tests.factories import UserFactory
from apps.session import audit
from apps.session.models import SessionAuditLog, SessionOrganization, SessionRole, SessionTenant, SessionUser
from apps.session.services import privacy_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def archiv(tmp_path: Path) -> Iterator[Path]:
    with override_settings(AUDIT_ARCHIVE_ROOT=tmp_path, AUDIT_ARCHIVE_STORAGE=""):
        yield tmp_path


def _tenant(slug: str = "musterstadt", **privacy: int) -> SessionTenant:
    return SessionTenant.objects.create(name=f"Stadt {slug}", slug=slug, settings={"privacy": privacy})


def _paket(root: Path, ordner: str, stempel: str, name: str = "protokoll-x-nr00000001-00000003.zip") -> Path:
    """Archivpaket mit dem Namensschema von archive_and_delete ablegen (Inhalt egal)."""
    datei = root / ordner / f"{stempel}-{name}"
    datei.parent.mkdir(parents=True, exist_ok=True)
    datei.write_bytes(b"PK")
    return datei


def _ordner(tenant: SessionTenant) -> str:
    return audit_archive.archive_folder(audit_chain.SESSION, tenant.pk)


# =============================================================================
# Grundfunktion
# =============================================================================


class TestPurgeArchives:
    def test_nur_alte_pakete_des_eigenen_bereichs(self, tmp_path: Path) -> None:
        storage = FileSystemStorage(location=str(tmp_path))
        tenant, fremd = _tenant(), _tenant("nachbarstadt")
        alt = _paket(tmp_path, _ordner(tenant), "20200105-101500")
        jung = _paket(tmp_path, _ordner(tenant), "20250105-101500")
        notiz = tmp_path / _ordner(tenant) / "LIESMICH.txt"
        notiz.write_text("bleibt")
        fremd_alt = _paket(tmp_path, _ordner(fremd), "20190101-000000")
        stichtag = dt.datetime(2021, 1, 1, tzinfo=dt.UTC)

        probe = audit_archive.purge_archives(audit_chain.SESSION, tenant.pk, stichtag, storage=storage, dry_run=True)
        assert [p.name for p in probe] == [f"{_ordner(tenant)}/{alt.name}"]
        assert alt.exists(), "Probelauf löscht nichts"

        geloescht = audit_archive.purge_archives(audit_chain.SESSION, tenant.pk, stichtag, storage=storage)
        assert [p.created_at for p in geloescht] == [dt.datetime(2020, 1, 5, 10, 15, tzinfo=dt.UTC)]
        assert not alt.exists()
        assert jung.exists() and notiz.exists() and fremd_alt.exists()

    def test_ohne_ordner_nichts(self, tmp_path: Path) -> None:
        storage = FileSystemStorage(location=str(tmp_path))
        assert audit_archive.purge_archives(audit_chain.SECURITY, None, timezone.now(), storage=storage) == []

    def test_abgelegte_pakete_tragen_den_ablagezeitpunkt(self, archiv: Path) -> None:
        """Rundlauf: ein echtes Archivpaket wird mit seinem Ablagezeitpunkt erkannt."""
        tenant = _tenant(audit_years=1)
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() - dt.timedelta(days=3 * 365)):
            org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
            for nummer in range(3):
                audit.log_event("update", org, tenant=tenant, changes={"n": nummer})
        vorher = timezone.now().replace(microsecond=0)
        privacy_service.run_privacy_purge(tenant, allow_backfill=True)

        (paket,) = audit_archive.list_archives(audit_chain.SESSION, tenant.pk)
        assert vorher <= paket.created_at <= timezone.now()


# =============================================================================
# Session-Mandant: Frist in den Datenschutz-Einstellungen
# =============================================================================


class TestSessionMandant:
    def test_loeschlauf_loescht_abgelaufene_pakete_und_protokolliert(self, archiv: Path) -> None:
        tenant = _tenant(archive_years=2)
        heute = timezone.now()
        alt = _paket(archiv, _ordner(tenant), f"{heute.year - 3}0101-000000")
        jung = _paket(archiv, _ordner(tenant), f"{heute.year - 1}0101-000000")

        stats = privacy_service.run_privacy_purge(tenant)

        assert stats["archives_deleted"] == [f"{_ordner(tenant)}/{alt.name}"]
        assert not alt.exists() and jung.exists()
        (lauf,) = [
            e for e in SessionAuditLog.objects.filter(tenant=tenant, action="delete") if "dsgvo_loeschlauf" in e.changes
        ]
        assert lauf.changes["dsgvo_loeschlauf"]["archivpakete_geloescht"] == [f"{_ordner(tenant)}/{alt.name}"]
        assert lauf.changes["dsgvo_loeschlauf"]["fristen"]["archiv_jahre"] == 2

    def test_frist_null_loescht_nichts(self, archiv: Path) -> None:
        tenant = _tenant()
        alt = _paket(archiv, _ordner(tenant), "20000101-000000")
        stats = privacy_service.run_privacy_purge(tenant)
        assert alt.exists()
        assert stats["archives_deleted"] == []
        assert "Archivpakete (Frist deaktiviert)" in stats["skipped"]

    def test_probelauf_zaehlt_nur(self, archiv: Path) -> None:
        tenant = _tenant(archive_years=1)
        alt = _paket(archiv, _ordner(tenant), "20000101-000000")
        out = io.StringIO()
        call_command("session_privacy_purge", tenant=tenant.slug, dry_run=True, stdout=out)
        assert alt.exists()
        assert "1 Archivpaket(e) nach Frist gelöscht" in out.getvalue()

    def test_speicherfehler_bricht_den_lauf_nicht_ab(self, archiv: Path) -> None:
        tenant = _tenant(archive_years=1)
        with mock.patch.object(audit_archive, "purge_archives", side_effect=OSError("weg")):
            stats = privacy_service.run_privacy_purge(tenant)
        assert any(grund.startswith("Archivpakete (Löschung ausgesetzt") for grund in stats["skipped"])

    def test_frist_in_den_einstellungen(self, archiv: Path) -> None:
        tenant = _tenant()
        rolle = SessionRole.objects.create(tenant=tenant, name="Verwaltung", can_manage_settings=True)
        user = cast(User, UserFactory(email="verwaltung@example.org"))  # type: ignore[no-untyped-call]
        nutzer = SessionUser.objects.create(user=user, tenant=tenant)
        nutzer.roles.add(rolle)
        client = Client()
        client.force_login(user)

        response = client.post(f"/session/{tenant.slug}/settings/privacy/", {"archive_years": "10"})
        assert response.status_code == 302
        tenant.refresh_from_db()
        assert privacy_service.get_privacy_settings(tenant)["archive_years"] == 10
        eintrag = SessionAuditLog.objects.filter(tenant=tenant, action="update").latest("created_at")
        assert eintrag.changes["dsgvo_einstellungen"]["archiv_jahre"] == {"alt": 0, "neu": 10}

        seite = client.get(f"/session/{tenant.slug}/settings/privacy/")
        assert 'name="archive_years"' in seite.content.decode()

        # Ungültige Werte werden begrenzt
        client.post(f"/session/{tenant.slug}/settings/privacy/", {"archive_years": "-3"})
        tenant.refresh_from_db()
        assert privacy_service.get_privacy_settings(tenant)["archive_years"] == 0


# =============================================================================
# Sicherheitsprotokoll
# =============================================================================


class TestSicherheitsprotokoll:
    def _betrieb(self) -> list[Any]:
        return [
            e.details
            for e in SecurityAuditLog.objects.filter(event="betrieb").order_by("seq")
            if e.details.get("aktion") == "archivpakete_geloescht"
        ]

    def test_frist_der_archivpakete(self, archiv: Path) -> None:
        ordner = audit_archive.archive_folder(audit_chain.SECURITY, None)
        alt = _paket(archiv, ordner, "20000101-000000", "protokoll-sicherheitsprotokoll-nr00000001-00000009.zip")
        jung = _paket(archiv, ordner, f"{timezone.now():%Y%m%d}-000000")
        out = io.StringIO()

        call_command("purge_security_audit_log", archive_days=30, stdout=out)

        assert not alt.exists() and jung.exists()
        assert "Archivpakete: 1 nach 30 Tagen gelöscht" in out.getvalue()
        (eingriff,) = self._betrieb()
        assert eingriff["pakete"] == [f"{ordner}/{alt.name}"]
        assert eingriff["frist_tage"] == 30
        assert audit_chain.verify(audit_chain.SECURITY, None).ok

    def test_standard_loescht_keine_pakete(self, archiv: Path) -> None:
        ordner = audit_archive.archive_folder(audit_chain.SECURITY, None)
        alt = _paket(archiv, ordner, "20000101-000000")
        call_command("purge_security_audit_log", stdout=io.StringIO())
        assert alt.exists()
        assert self._betrieb() == []

    @override_settings(SECURITY_AUDIT_ARCHIVE_RETENTION_DAYS=30)
    def test_probelauf_und_einstellung(self, archiv: Path) -> None:
        ordner = audit_archive.archive_folder(audit_chain.SECURITY, None)
        alt = _paket(archiv, ordner, "20000101-000000")
        out = io.StringIO()
        call_command("purge_security_audit_log", dry_run=True, stdout=out)
        assert alt.exists()
        assert "[DRY-RUN] Archivpakete: 1 älter als 30 Tage" in out.getvalue()
        call_command("purge_security_audit_log", stdout=io.StringIO())
        assert not alt.exists()


def test_archivspeicher_aus_storages(tmp_path: Path) -> None:
    """Mit AUDIT_ARCHIVE_STORAGE gilt die Frist im konfigurierten Speicher (etwa S3)."""
    speicher = {
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        "archiv": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": str(tmp_path)}},
    }
    with override_settings(STORAGES=speicher, AUDIT_ARCHIVE_STORAGE="archiv"):
        from django.core.files.storage import storages

        ziel = audit_archive.archive_storage()
        ziel.save("security/20000101-000000-protokoll-a.zip", ContentFile(b"PK"))
        geloescht = audit_archive.purge_archives(audit_chain.SECURITY, None, timezone.now())
        assert [p.name for p in geloescht] == ["security/20000101-000000-protokoll-a.zip"]
        assert not storages["archiv"].exists("security/20000101-000000-protokoll-a.zip")
