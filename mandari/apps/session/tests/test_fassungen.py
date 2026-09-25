# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fassungen von Vorlagen und Anlagen (Issue #226).

Entlang der Akzeptanzkriterien:
- Nach dem Ersetzen einer Anlage bleibt die vorherige Datei abrufbar – mit derselben
  Sichtbarkeit wie die Anlage und den Schutz-Headern der Anlagen-Downloads.
- Die beschlossene Fassung ist eindeutig gekennzeichnet und unveränderlich (auch ihr Inhalt
  lässt sich nicht per Datenschutz-Löschung entfernen).
- Ein Vergleich zweier Fassungen zeigt die Änderungen am Beschlussvorschlag wortgenau –
  HTML-escaped, ohne Markup aus dem Vorlagentext.
Dazu: automatische Fassung bei jedem Workflow-Übergang, Wiederherstellen als neue Fassung (nur im
Entwurf), Deduplizierung über SHA-256, Löschkonzept, Ö/NÖ und Mandantentrennung, OParl-Dateiname.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionAuditLog,
    SessionConsultation,
    SessionFile,
    SessionFileBlob,
    SessionFileVersion,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPaperVersion,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import file_version_service, paper_version_diff, paper_version_service

pytestmark = pytest.mark.django_db

ALL_PERMS = {f.name[4:] for f in SessionRole._meta.get_fields() if f.name.startswith("can_")}
Version = SessionPaperVersion


# =============================================================================
# Hilfsfunktionen und Testwelt
# =============================================================================


def _client(tenant: SessionTenant, name: str, perms: set[str], *, admin: bool = False) -> Client:
    flags = {f"can_{perm}": perm in perms for perm in ALL_PERMS}
    role = SessionRole.objects.create(tenant=tenant, name=f"rolle-{name}", is_admin=admin, **flags)
    user = cast(User, UserFactory(email=f"{name}@example.org"))  # type: ignore[no-untyped-call]
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    client = Client()
    client.force_login(user)
    return client


def _download(client: Client, url: str) -> tuple[int, bytes, Any]:
    response = cast(Any, client.get(url))
    if response.streaming:
        return response.status_code, b"".join(cast(Iterator[bytes], response.streaming_content)), response
    return response.status_code, bytes(response.content), response


def _upload(name: str, data: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data, content_type="text/plain")


@dataclass
class Welt:
    tenant: SessionTenant
    paper: SessionPaper
    clerk: Client  # Sachbearbeitung: sehen, bearbeiten, freigeben, NÖ
    viewer: Client  # nur öffentliche Vorlagen sehen
    admin: Client

    @property
    def base(self) -> str:
        return f"/session/{self.tenant.slug}"

    def upload(self, client: Client, name: str, data: bytes, *, public: bool = True, **target: Any) -> SessionFile:
        target_type, target_id = next(iter(target.items())) if target else ("paper", self.paper.pk)
        payload: dict[str, Any] = {
            "target_type": target_type,
            "target_id": str(target_id),
            "files": _upload(name, data),
        }
        if public:
            payload["is_public"] = "on"
        response = client.post(f"{self.base}/files/upload/", payload)
        assert response.status_code == 302
        return SessionFile.objects.filter(tenant=self.tenant, name=name).latest("created_at")

    def replace(self, client: Client, session_file: SessionFile, name: str, data: bytes) -> Any:
        return client.post(f"{self.base}/files/{session_file.pk}/replace/", {"file": _upload(name, data)})

    def workflow(self, action: str) -> None:
        response = self.clerk.post(f"{self.base}/papers/{self.paper.pk}/workflow/{action}/")
        assert response.status_code == 302
        self.paper.refresh_from_db()


@pytest.fixture
def media(settings: Any, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


@pytest.fixture
def welt(media: Path) -> Welt:
    tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
    paper = SessionPaper.objects.create(
        tenant=tenant,
        name="Radweg Hauptstraße",
        main_text="Der Radweg ist marode.",
        resolution_text="Der Rat beschließt den Ausbau des Radwegs.",
        has_financial_impact=False,
        is_public=True,
    )
    clerk = _client(
        tenant,
        "sachbearbeitung",
        {"view_papers", "create_papers", "edit_papers", "approve_papers", "view_non_public_papers", "view_meetings"},
    )
    viewer = _client(tenant, "leser", {"view_papers", "view_meetings"})
    admin = _client(tenant, "admin", set(), admin=True)
    return Welt(tenant=tenant, paper=paper, clerk=clerk, viewer=viewer, admin=admin)


# =============================================================================
# Akzeptanzkriterium 1: Nach dem Ersetzen bleibt die vorherige Datei abrufbar
# =============================================================================


class TestAnlagenFassungen:
    def test_vorherige_datei_bleibt_nach_dem_ersetzen_abrufbar(self, welt: Welt) -> None:
        anlage = welt.upload(welt.clerk, "gutachten.txt", b"Gutachten Fassung eins")
        response = welt.replace(welt.clerk, anlage, "gutachten-v2.txt", b"Gutachten Fassung zwei")
        assert response.status_code == 302
        anlage.refresh_from_db()
        assert anlage.version == 2
        assert anlage.name == "gutachten-v2.txt"

        status, data, _ = _download(welt.clerk, f"{welt.base}/files/{anlage.pk}/download/")
        assert (status, data) == (200, b"Gutachten Fassung zwei")
        status, data, response = _download(welt.clerk, f"{welt.base}/files/{anlage.pk}/fassungen/1/")
        assert (status, data) == (200, b"Gutachten Fassung eins"), "vorherige Fassung nicht mehr abrufbar"
        assert response["Cache-Control"] == "private, no-store"
        assert response["X-Content-Type-Options"] == "nosniff"
        assert "gutachten.txt" in response["Content-Disposition"]

        verlauf = welt.clerk.get(f"{welt.base}/files/{anlage.pk}/fassungen/")
        assert verlauf.status_code == 200
        assert b"Version 1" in verlauf.content
        assert b"gutachten.txt" in verlauf.content

    def test_auch_sitzungsanlagen_behalten_ihre_vorige_fassung(self, welt: Welt) -> None:
        gremium = SessionOrganization.objects.create(tenant=welt.tenant, name="Rat")
        sitzung = SessionMeeting.objects.create(
            tenant=welt.tenant, name="Ratssitzung", organization=gremium, start=timezone.now(), is_public=True
        )
        clerk = _client(welt.tenant, "sitzungsdienst", {"view_meetings", "edit_meetings"})
        anlage = welt.upload(clerk, "teilnehmer.txt", b"Liste alt", meeting=sitzung.pk)
        welt.replace(clerk, anlage, "teilnehmer.txt", b"Liste neu")
        status, data, _ = _download(clerk, f"{welt.base}/files/{anlage.pk}/fassungen/1/")
        assert (status, data) == (200, b"Liste alt")

    def test_nicht_oeffentliche_anlage_auch_im_verlauf_geschuetzt(self, welt: Welt) -> None:
        anlage = welt.upload(welt.clerk, "personalie.txt", b"GEHEIM alt", public=False)
        welt.replace(welt.clerk, anlage, "personalie.txt", b"GEHEIM neu")
        for url in (f"{welt.base}/files/{anlage.pk}/fassungen/", f"{welt.base}/files/{anlage.pk}/fassungen/1/"):
            status, data, _ = _download(welt.viewer, url)
            assert status == 403, f"{url}: NÖ-Verlauf für Leser sichtbar"
            assert b"GEHEIM" not in data

    def test_gleiche_datei_ersetzt_nichts(self, welt: Welt) -> None:
        anlage = welt.upload(welt.clerk, "plan.txt", b"Plan")
        response = welt.replace(welt.clerk, anlage, "plan.txt", b"Plan")
        assert response.status_code == 302
        anlage.refresh_from_db()
        assert anlage.version == 1
        assert SessionFileVersion.objects.filter(session_file=anlage).count() == 1


# =============================================================================
# Speicherkonzept: Deduplizierung, Bestand, Löschen
# =============================================================================


class TestSpeicher:
    def test_gleicher_inhalt_liegt_nur_einmal_im_speicher(self, welt: Welt, media: Path) -> None:
        erste = welt.upload(welt.clerk, "lageplan.txt", b"LAGEPLAN")
        zweite = welt.upload(welt.clerk, "lageplan-kopie.txt", b"LAGEPLAN")
        assert SessionFileBlob.objects.filter(tenant=welt.tenant).count() == 1
        assert erste.file.name == zweite.file.name
        assert len([p for p in media.rglob("*") if p.is_file()]) == 1

    def test_mandanten_teilen_keine_inhalte(self, welt: Welt) -> None:
        andere = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt")
        fremde_vorlage = SessionPaper.objects.create(tenant=andere, name="Fremd")
        welt.upload(welt.clerk, "a.txt", b"GLEICH")
        fremd = SessionFile(tenant=andere, paper=fremde_vorlage, name="b.txt", mime_type="text/plain")
        file_version_service.attach_upload(fremd, _upload("b.txt", b"GLEICH"), user=None)
        sha256 = hashlib.sha256(b"GLEICH").hexdigest()
        assert SessionFileBlob.objects.filter(sha256=sha256).values("tenant").distinct().count() == 2

    def test_bestand_wird_nachtraeglich_erfasst_und_zusammengefuehrt(self, welt: Welt, media: Path) -> None:
        vorhanden = welt.upload(welt.clerk, "neu.txt", b"INHALT")
        alt = SessionFile.objects.create(
            tenant=welt.tenant,
            paper=welt.paper,
            name="altbestand.txt",
            file=_upload("altbestand.txt", b"INHALT"),
            version=3,
        )
        alter_name = alt.file.name
        assert alter_name != vorhanden.file.name
        with transaction.atomic():
            version = file_version_service.ensure_current_version(alt)
        assert version is not None
        assert version.number == 3
        assert version.note == file_version_service.NOTE_BACKFILLED
        alt.refresh_from_db()
        assert alt.file.name == vorhanden.file.name, "Bestand nicht auf vorhandenen Inhalt umgestellt"
        # Erneuter Aufruf erkennt die erfasste Fassung
        assert file_version_service.ensure_current_version(alt) == version

    def test_loeschen_behaelt_inhalte_gesicherter_fassungen(
        self, welt: Welt, media: Path, django_capture_on_commit_callbacks: Any
    ) -> None:
        gesichert = welt.upload(welt.clerk, "gesichert.txt", b"IN DER FASSUNG")
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        welt.replace(welt.clerk, gesichert, "gesichert.txt", b"NUR IM VERLAUF")
        fluechtig = welt.upload(welt.clerk, "fluechtig.txt", b"NIRGENDS GESICHERT")
        entry = Version.objects.get(paper=welt.paper).files.get()

        with django_capture_on_commit_callbacks(execute=True):
            welt.clerk.post(f"{welt.base}/files/{gesichert.pk}/delete/")
            welt.clerk.post(f"{welt.base}/files/{fluechtig.pk}/delete/")

        inhalte = {p.read_bytes() for p in media.rglob("*") if p.is_file()}
        assert inhalte == {b"IN DER FASSUNG"}, "Inhalt der Fassung gelöscht oder Ungesichertes liegen geblieben"
        assert SessionFileVersion.objects.count() == 0
        # Die Fassung bleibt vollständig: nur mit NÖ-Recht, weil die Anlage nicht mehr existiert
        url = f"{welt.base}/papers/{welt.paper.pk}/fassungen/1/anlagen/{entry.pk}/"
        assert _download(welt.clerk, url)[:2] == (200, b"IN DER FASSUNG")
        assert _download(welt.viewer, url)[0] == 403

    def test_mandant_loeschen_raeumt_alles(
        self, welt: Welt, media: Path, django_capture_on_commit_callbacks: Any
    ) -> None:
        welt.upload(welt.clerk, "a.txt", b"A")
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        with django_capture_on_commit_callbacks(execute=True):
            welt.tenant.delete()
        assert not SessionFileBlob.objects.exists()
        assert not [p for p in media.rglob("*") if p.is_file()]


# =============================================================================
# Datenschutz-Löschung
# =============================================================================


class TestDatenschutzLoeschung:
    def test_frueheren_inhalt_endgueltig_loeschen(
        self, welt: Welt, media: Path, django_capture_on_commit_callbacks: Any
    ) -> None:
        anlage = welt.upload(welt.clerk, "brief.txt", b"MIT ADRESSE")
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        welt.replace(welt.clerk, anlage, "brief.txt", b"GESCHWAERZT")
        alt = SessionFileVersion.objects.get(session_file=anlage, number=1)
        url = f"{welt.base}/files/inhalte/{alt.blob_id}/loeschen/"

        assert welt.clerk.post(url, {"reason": "x"}).status_code == 403, "ohne Einstellungsrecht"
        with django_capture_on_commit_callbacks(execute=True):
            response = welt.admin.post(url, {"reason": "Enthielt eine Privatadresse"})
        assert response.status_code == 302
        alt.blob.refresh_from_db()
        assert alt.blob.purged_at is not None
        assert b"MIT ADRESSE" not in {p.read_bytes() for p in media.rglob("*") if p.is_file()}
        assert _download(welt.admin, f"{welt.base}/files/{anlage.pk}/fassungen/1/")[0] == 404
        entry = Version.objects.get(paper=welt.paper).files.get()
        assert _download(welt.admin, f"{welt.base}/papers/{welt.paper.pk}/fassungen/1/anlagen/{entry.pk}/")[0] == 404
        log = SessionAuditLog.objects.get(model_name="SessionFileBlob", action="delete")
        assert log.changes["grund"] == "Enthielt eine Privatadresse"

    def test_loeschen_wird_nur_angeboten_wo_es_zulaessig_ist(self, welt: Welt) -> None:
        anlage = welt.upload(welt.clerk, "plan.txt", b"PLAN ALT")
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        welt.replace(welt.clerk, anlage, "plan.txt", b"PLAN NEU")
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_CONSULTATION, is_resolved=True)
        base = f"{welt.base}/papers/{welt.paper.pk}/fassungen/"
        angebot = "Inhalt endgültig löschen".encode()
        assert angebot in welt.admin.get(f"{base}1/").content, "früherer Inhalt: Löschen fehlt"
        assert angebot not in welt.admin.get(f"{base}2/").content, "beschlossene Fassung: Löschen angeboten"
        assert angebot not in welt.clerk.get(f"{base}1/").content, "ohne Einstellungsrecht: Löschen angeboten"

    def test_aktueller_inhalt_und_beschlossene_fassung_sind_geschuetzt(self, welt: Welt) -> None:
        anlage = welt.upload(welt.clerk, "beschluss.txt", b"BESCHLOSSEN")
        blob = SessionFileVersion.objects.get(session_file=anlage).blob
        with pytest.raises(file_version_service.PurgeRefusedError, match="aktuelle Datei"):
            file_version_service.purge_content(blob, user=None, reason="Test")
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_CONSULTATION, is_resolved=True)
        welt.replace(welt.clerk, anlage, "beschluss.txt", b"SPAETER")
        with pytest.raises(file_version_service.PurgeRefusedError, match="beschlossenen Fassung"):
            file_version_service.purge_content(blob, user=None, reason="Test")
        blob.refresh_from_db()
        assert blob.purged_at is None


# =============================================================================
# Automatische Fassungen bei Workflow-Übergängen
# =============================================================================


class TestWorkflowFassungen:
    def test_jeder_uebergang_sichert_eine_fassung(self, welt: Welt) -> None:
        welt.upload(welt.clerk, "anlage.txt", b"ANLAGE")
        welt.workflow("submit")
        welt.workflow("reject")
        welt.workflow("submit")
        welt.workflow("approve")
        versions = list(Version.objects.filter(paper=welt.paper).order_by("number"))
        assert [v.note for v in versions] == [
            "Entwurf → In Prüfung",
            "In Prüfung → Entwurf",
            "Entwurf → In Prüfung",
            "In Prüfung → Freigegeben",
        ]
        assert {v.trigger for v in versions} == {Version.TRIGGER_TRANSITION}
        assert versions[-1].status == "approved"
        assert versions[-1].reference == welt.paper.reference != ""
        assert (
            versions[-1].created_by is not None and versions[-1].created_by.user.email == "sachbearbeitung@example.org"
        )
        assert versions[0].files.get().name == "anlage.txt"

    def test_statuswechsel_ausserhalb_des_freigabelaufs(self, welt: Welt) -> None:
        welt.paper.status = "withdrawn"
        welt.paper.save()
        assert Version.objects.get(paper=welt.paper).note == "Entwurf → Zurückgezogen"
        welt.paper.name = "Nur Betreff geändert"
        welt.paper.save()
        assert Version.objects.filter(paper=welt.paper).count() == 1, "Bearbeiten ohne Übergang sichert nichts"

    def test_manuell_sichern_braucht_bearbeitungsrecht(self, welt: Welt) -> None:
        url = f"{welt.base}/papers/{welt.paper.pk}/fassungen/sichern/"
        assert welt.viewer.post(url, {"note": "x"}).status_code == 403
        assert welt.clerk.post(url, {"note": "Stand nach Rücksprache"}).status_code == 302
        version = Version.objects.get(paper=welt.paper)
        assert (version.trigger, version.note) == (Version.TRIGGER_MANUAL, "Stand nach Rücksprache")
        assert SessionAuditLog.objects.filter(model_name="SessionPaperVersion", action="create").exists()

    def test_fassungen_sind_unveraenderlich(self, welt: Welt) -> None:
        welt.upload(welt.clerk, "a.txt", b"A")
        version = paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        version.resolution_text = "manipuliert"
        with pytest.raises(ValueError, match="unveränderlich"):
            version.save()
        with pytest.raises(ValueError, match="unveränderlich"):
            version.delete()
        entry = version.files.get()
        with pytest.raises(ValueError, match="unveränderlich"):
            entry.save()
        with pytest.raises(ValueError, match="unveränderlich"):
            SessionFileVersion.objects.get().save()


# =============================================================================
# Akzeptanzkriterium 2: beschlossene Fassung eindeutig und unveränderlich
# =============================================================================


class TestBeschlosseneFassung:
    def _beratung(self, welt: Welt, *, authoritative: bool, role: str = "preliminary") -> SessionAgendaItem:
        gremium = SessionOrganization.objects.create(tenant=welt.tenant, name=f"Gremium {role}")
        sitzung = SessionMeeting.objects.create(
            tenant=welt.tenant, name="Sitzung", organization=gremium, start=timezone.now(), is_public=True
        )
        top = SessionAgendaItem.objects.create(meeting=sitzung, number="5", name="Radweg", paper=welt.paper)
        SessionConsultation.objects.create(
            paper=welt.paper, organization=gremium, role=role, authoritative=authoritative, agenda_item=top
        )
        return top

    def test_angenommen_in_der_entscheidenden_beratung_kennzeichnet_die_fassung(self, welt: Welt) -> None:
        vorberatung = self._beratung(welt, authoritative=False)
        entscheidung = self._beratung(welt, authoritative=True, role="decision")
        vorberatung.vote_result = "approved"
        vorberatung.save()
        entscheidung.vote_result = "deferred"
        entscheidung.save()
        assert not Version.objects.filter(paper=welt.paper, is_resolved=True).exists()

        entscheidung.vote_result = "approved"
        entscheidung.save()
        beschlossen = Version.objects.get(paper=welt.paper, is_resolved=True)
        assert beschlossen.trigger == Version.TRIGGER_CONSULTATION
        assert beschlossen.agenda_item == entscheidung
        assert beschlossen.note.startswith("Angenommen")
        assert Version.objects.filter(paper=welt.paper).count() == 3  # je Beratungsergebnis eine Fassung

        # Eine spätere zweite Annahme ändert die Kennzeichnung nicht
        entscheidung.vote_result = "rejected"
        entscheidung.save()
        entscheidung.vote_result = "approved"
        entscheidung.save()
        assert list(Version.objects.filter(paper=welt.paper, is_resolved=True)) == [beschlossen]

        detail = welt.viewer.get(f"{welt.base}/papers/{welt.paper.pk}/")
        assert b"Beschlossene Fassung" in detail.content

    def test_top_ohne_beratungsfolge_beschliesst_direkt(self, welt: Welt) -> None:
        gremium = SessionOrganization.objects.create(tenant=welt.tenant, name="Rat")
        sitzung = SessionMeeting.objects.create(
            tenant=welt.tenant, name="Rat", organization=gremium, start=timezone.now(), is_public=True
        )
        top = SessionAgendaItem.objects.create(meeting=sitzung, number="3", name="Radweg", paper=welt.paper)
        top.vote_result = "approved"
        top.save()
        assert Version.objects.get(paper=welt.paper).is_resolved

    def test_fassung_zeigt_den_gespeicherten_stand(self, welt: Welt) -> None:
        gremium = SessionOrganization.objects.create(tenant=welt.tenant, name="Rat")
        sitzung = SessionMeeting.objects.create(
            tenant=welt.tenant, name="Rat", organization=gremium, start=timezone.now(), is_public=True
        )
        top = SessionAgendaItem.objects.create(meeting=sitzung, number="3", name="Radweg", paper=welt.paper)
        # Das Objekt am TOP ist veraltet; gesichert wird der Stand aus der Datenbank
        SessionPaper.objects.filter(pk=welt.paper.pk).update(status="scheduled", name="Aktueller Betreff")
        top.vote_result = "approved"
        top.save()
        version = Version.objects.get(paper=welt.paper)
        assert (version.status, version.name) == ("scheduled", "Aktueller Betreff")

    def test_datenbank_erlaubt_nur_eine_beschlossene_fassung(self, welt: Welt) -> None:
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_CONSULTATION, is_resolved=True)
        with pytest.raises(IntegrityError), transaction.atomic():
            paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_CONSULTATION, is_resolved=True)


# =============================================================================
# Akzeptanzkriterium 3: Vergleich zeigt Änderungen am Beschlussvorschlag
# =============================================================================


class TestVergleich:
    def test_vergleich_zeigt_aenderung_am_beschlussvorschlag(self, welt: Welt) -> None:
        welt.workflow("submit")  # Stand des Amts
        welt.paper.resolution_text = "Der Rat beschließt den Ausbau und die Beleuchtung des Radwegs."
        welt.paper.save()
        welt.workflow("approve")  # Stand der Freigabe
        response = welt.viewer.get(f"{welt.base}/papers/{welt.paper.pk}/fassungen/vergleich/?a=1&b=2")
        assert response.status_code == 200
        html = response.content.decode()
        assert "<ins>und die Beleuchtung </ins>" in html
        assert "Beschlussvorschlag" in html

    def test_wortvergleich_escaped_jedes_textstueck(self) -> None:
        html = str(paper_version_diff.word_diff("Alt <b>fett</b>", 'Neu <script>alert("x")</script>'))
        assert "<script>" not in html and "<b>" not in html
        assert html == (
            "<del>Alt &lt;b&gt;fett&lt;/b&gt;</del><ins>Neu &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;</ins>"
        )

    def test_wortvergleich_markiert_nur_geaenderte_woerter(self) -> None:
        html = str(
            paper_version_diff.word_diff(
                "Der Rat beschließt A.\nZweiter Absatz.", "Der Rat beschließt B.\nZweiter Absatz."
            )
        )
        assert html == "Der Rat beschließt <del>A.</del><ins>B.</ins>\nZweiter Absatz."

    def test_xss_im_vorlagentext_wird_nicht_ausgefuehrt(self, welt: Welt) -> None:
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        welt.paper.resolution_text = '<img src=x onerror="alert(1)">'
        welt.paper.save()
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        html = welt.viewer.get(f"{welt.base}/papers/{welt.paper.pk}/fassungen/vergleich/?a=1&b=2").content.decode()
        assert "<img src=x" not in html
        assert "<ins>&lt;img src=x onerror=&quot;alert(1)&quot;&gt;</ins>" in html

    def test_anlagen_ueber_metadaten(self, welt: Welt) -> None:
        bleibt = welt.upload(welt.clerk, "bleibt.txt", b"BLEIBT")
        ersetzt = welt.upload(welt.clerk, "plan.txt", b"PLAN ALT")
        weg = welt.upload(welt.clerk, "weg.txt", b"WEG")
        alt = paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        welt.replace(welt.clerk, ersetzt, "plan.txt", b"PLAN NEU, LAENGER")
        weg.delete()
        welt.upload(welt.clerk, "neu.txt", b"NEU")
        welt.clerk.post(f"{welt.base}/files/{bleibt.pk}/update/", {"name": "bleibt-umbenannt.txt", "is_public": "on"})
        neu = paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)

        vergleich = paper_version_diff.compare(alt, neu, older_entries=alt.files.all(), newer_entries=neu.files.all())
        arten = {item.name: (item.kind, item.changes) for item in vergleich.files}
        assert arten["neu.txt"][0] == "added"
        assert arten["weg.txt"][0] == "removed"
        assert arten["plan.txt"][0] == "changed" and arten["plan.txt"][1][0].startswith("ersetzt")
        assert arten["bleibt-umbenannt.txt"] == ("changed", ["umbenannt (vorher „bleibt.txt“)"])

    def test_nicht_oeffentliche_anlagen_fehlen_im_vergleich_fuer_leser(self, welt: Welt) -> None:
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        welt.upload(welt.clerk, "GEHEIM-gutachten.txt", b"GEHEIM", public=False)
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        url = f"{welt.base}/papers/{welt.paper.pk}/fassungen/vergleich/?a=1&b=2"
        assert b"GEHEIM" not in welt.viewer.get(url).content
        assert b"GEHEIM-gutachten.txt" in welt.clerk.get(url).content


# =============================================================================
# Wiederherstellen als neue Fassung
# =============================================================================


class TestWiederherstellen:
    def test_wiederherstellen_legt_neue_fassung_an(self, welt: Welt) -> None:
        anlage = welt.upload(welt.clerk, "plan.txt", b"PLAN EINS")
        weg = welt.upload(welt.clerk, "anhang.txt", b"ANHANG", public=True)
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        welt.paper.resolution_text = "Ganz anders."
        welt.paper.save()
        welt.replace(welt.clerk, anlage, "plan.txt", b"PLAN ZWEI")
        weg_id = weg.pk
        weg.delete()

        response = welt.clerk.post(f"{welt.base}/papers/{welt.paper.pk}/fassungen/1/wiederherstellen/")
        assert response.status_code == 302
        welt.paper.refresh_from_db()
        assert welt.paper.resolution_text == "Der Rat beschließt den Ausbau des Radwegs."
        triggers = list(Version.objects.filter(paper=welt.paper).order_by("number").values_list("trigger", flat=True))
        assert triggers == [Version.TRIGGER_MANUAL, Version.TRIGGER_BACKUP, Version.TRIGGER_RESTORE]
        restored = Version.objects.get(paper=welt.paper, number=3)
        assert restored.restored_from is not None and restored.restored_from.number == 1
        assert Version.objects.get(number=2, paper=welt.paper).resolution_text == "Ganz anders."

        anlage.refresh_from_db()
        assert anlage.version == 3
        assert _download(welt.clerk, f"{welt.base}/files/{anlage.pk}/download/")[1] == b"PLAN EINS"
        wieder = SessionFile.objects.get(pk=weg_id)
        assert not wieder.is_public, "wieder angelegte Anlage muss nichtöffentlich zurückkommen"
        assert SessionFileBlob.objects.filter(tenant=welt.tenant).count() == 3, "Wiederherstellen hat Inhalte kopiert"

    def test_neue_fassung_zaehlt_als_inhaltliche_bearbeitung(self, welt: Welt) -> None:
        """Vier-Augen-Prinzip (#222): Wer ersetzt oder wiederherstellt, hat den Stand zuletzt bearbeitet."""
        kollegin = _client(welt.tenant, "kollegin", {"view_papers", "edit_papers", "view_non_public_papers"})
        anlage = welt.upload(welt.clerk, "plan.txt", b"PLAN EINS")
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)

        welt.replace(kollegin, anlage, "plan.txt", b"PLAN ZWEI")
        welt.paper.refresh_from_db()
        assert welt.paper.content_edited_by is not None
        assert welt.paper.content_edited_by.user.email == "kollegin@example.org"

        welt.clerk.post(f"{welt.base}/papers/{welt.paper.pk}/fassungen/1/wiederherstellen/")
        welt.paper.refresh_from_db()
        assert welt.paper.content_edited_by is not None
        assert welt.paper.content_edited_by.user.email == "sachbearbeitung@example.org"

    def test_nachtraegliches_erfassen_ist_keine_bearbeitung(self, welt: Welt) -> None:
        SessionFile.objects.create(
            tenant=welt.tenant, paper=welt.paper, name="alt.txt", file=_upload("alt.txt", b"ALT"), version=2
        )
        welt.workflow("submit")  # sichert die Fassung und erfasst dabei den Bestand
        welt.paper.refresh_from_db()
        assert Version.objects.get(paper=welt.paper).files.get().file_version_number == 2
        assert welt.paper.content_edited_by is None

    def test_nur_im_entwurf(self, welt: Welt) -> None:
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        welt.paper.status = "approved"
        welt.paper.name = "Freigegebener Betreff"
        welt.paper.save()
        response = welt.clerk.post(f"{welt.base}/papers/{welt.paper.pk}/fassungen/1/wiederherstellen/")
        assert response.status_code == 302
        welt.paper.refresh_from_db()
        assert welt.paper.name == "Freigegebener Betreff"
        assert not Version.objects.filter(paper=welt.paper, trigger=Version.TRIGGER_RESTORE).exists()

    def test_braucht_bearbeitungsrecht(self, welt: Welt) -> None:
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        response = welt.viewer.post(f"{welt.base}/papers/{welt.paper.pk}/fassungen/1/wiederherstellen/")
        assert response.status_code == 403


# =============================================================================
# Sichtbarkeit, Mandantentrennung, Portal
# =============================================================================


class TestSichtbarkeit:
    def test_noe_vorlage_und_noe_stand_bleiben_verborgen(self, welt: Welt) -> None:
        welt.paper.is_public = False
        welt.paper.save()
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        base = f"{welt.base}/papers/{welt.paper.pk}/fassungen/"
        assert welt.viewer.get(base).status_code == 404
        # Später öffentlich: Der damals nichtöffentliche Stand bleibt für Leser verborgen
        welt.paper.is_public = True
        welt.paper.save()
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        liste = welt.viewer.get(base)
        assert liste.status_code == 200
        assert b'data-fassung="1"' not in liste.content and b'data-fassung="2"' in liste.content
        assert welt.viewer.get(f"{base}1/").status_code == 404
        assert welt.clerk.get(f"{base}1/").status_code == 200

    def test_noe_anlage_einer_fassung(self, welt: Welt) -> None:
        welt.upload(welt.clerk, "GEHEIM.txt", b"GEHEIM", public=False)
        version = paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        entry = version.files.get()
        detail = welt.viewer.get(f"{welt.base}/papers/{welt.paper.pk}/fassungen/1/")
        assert b"GEHEIM" not in detail.content
        url = f"{welt.base}/papers/{welt.paper.pk}/fassungen/1/anlagen/{entry.pk}/"
        assert _download(welt.viewer, url)[0] == 403
        status, data, response = _download(welt.clerk, url)
        assert (status, data) == (200, b"GEHEIM")
        assert response["Cache-Control"] == "private, no-store"

    def test_anlage_spaeter_nichtoeffentlich_gilt_auch_fuer_alte_fassungen(self, welt: Welt) -> None:
        anlage = welt.upload(welt.clerk, "gutachten.txt", b"GUTACHTEN")
        version = paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        anlage.is_public = False
        anlage.save()
        url = f"{welt.base}/papers/{welt.paper.pk}/fassungen/1/anlagen/{version.files.get().pk}/"
        assert _download(welt.viewer, url)[0] == 403

    def test_fremder_mandant(self, welt: Welt) -> None:
        andere = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt")
        fremd = SessionPaper.objects.create(tenant=andere, name="FREMD", is_public=True)
        paper_version_service.snapshot(fremd, trigger=Version.TRIGGER_MANUAL)
        for path in ("fassungen/", "fassungen/1/", "fassungen/vergleich/?a=1&b=1"):
            assert welt.admin.get(f"{welt.base}/papers/{fremd.pk}/{path}").status_code == 404

    def test_oparl_nennt_nie_den_speichernamen(self, welt: Welt) -> None:
        welt.paper.status = "approved"
        welt.paper.save()
        welt.upload(welt.clerk, "GEHEIM-kuendigung.txt", b"GLEICHER INHALT", public=False)
        welt.upload(welt.clerk, "Lageplan.txt", b"GLEICHER INHALT", public=True)
        response = Client().get(f"{welt.base}/api/oparl/papers/")
        assert response.status_code == 200
        assert b"Lageplan.txt" in response.content
        assert b"GEHEIM" not in response.content, "Speichername einer NÖ-Anlage über OParl sichtbar"

    def test_detailseite_zeigt_fassungen(self, welt: Welt) -> None:
        paper_version_service.snapshot(welt.paper, trigger=Version.TRIGGER_MANUAL)
        response = welt.viewer.get(f"{welt.base}/papers/{welt.paper.pk}/")
        assert b'data-testid="fassungen"' in response.content
        assert b"Fassung 1" in response.content
