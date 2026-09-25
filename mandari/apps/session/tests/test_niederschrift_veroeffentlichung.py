# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Niederschrift über OParl und im Bürgerportal (Issue #318).

- Veröffentlichen erzeugt eine Datei mit dem öffentlichen Teil (PDF und Text); die Session-OParl-API
  liefert sie als ``resultsProtocol`` der Sitzung, das Bürgerportal zeigt sie auf der Sitzungsseite.
- Der nichtöffentliche Teil erscheint in keiner öffentlichen Ausgabe: weder im PDF noch im Text,
  im Dateinamen, in den Metadaten oder im JSON – auch nicht mit NÖ-TOP in öffentlicher Sitzung,
  NÖ-Unterpunkt, NÖ-Berichtigung oder in einer NÖ-Sitzung. Verschlüsselte Felder werden dabei nie
  entschlüsselt.
- Rücknahme, Berichtigung und ein nachträglich nichtöffentlicher TOP nehmen die alte Fassung sofort
  aus dem Bürgerportal zurück.
- Der Beschlussauszug gibt den gesperrten Stand samt Berichtigungen wieder und hält die NÖ-Regeln ein.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import Client

from apps.session.models import (
    SessionAgendaItem,
    SessionFile,
    SessionMeeting,
    SessionOParlTombstone,
    SessionPaper,
    SessionProtocol,
    SessionProtocolCorrection,
)
from apps.session.services import protocol_correction_service, protocol_publication
from apps.session.tests._niederschrift import GEHEIM, Welt, base, client, pdf_text, welt
from insight_core.models import OParlBody, OParlFile, OParlMeeting, OParlSource
from insight_sync.session_mirror import SessionMirror

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media(settings: Any, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


def _protokoll(w: Welt) -> SessionProtocol:
    return SessionProtocol.objects.select_related("meeting__tenant", "public_file").get(pk=w.protokoll.pk)


def _veroeffentlichen(w: Welt) -> SessionFile:
    client(w.genehmiger).post(f"{base(w)}/meetings/{w.sitzung.pk}/protocol/publish/")
    protokoll = _protokoll(w)
    assert protokoll.status == "published"
    assert protokoll.public_file is not None
    return protokoll.public_file


def _oparl(w: Welt, path: str) -> Any:
    return Client().get(f"/session/{w.tenant.slug}/api/oparl/{path}")


def _noe_welt(slug: str = "nord") -> Welt:
    """Genehmigte Niederschrift mit allen Arten nichtöffentlicher Inhalte."""
    w = welt(slug, status="review")
    top = SessionAgendaItem.objects.get(pk=w.top.pk)
    cast(Any, top).set_protocol_note_encrypted("GEHEIMVERSCHL am öffentlichen TOP")
    cast(Any, top).set_resolution_text_encrypted("GEHEIMVERSCHL Beschlusszusatz")
    top.save()
    # Altbestand vor den Tagesordnungsregeln: NÖ-Unterpunkt unter öffentlichem TOP
    SessionAgendaItem.objects.create(
        meeting=w.sitzung,
        parent=w.top,
        number="1.1",
        order=3,
        name="GEHEIMUNTERPUNKT",
        is_public=False,
        resolution_text="GEHEIMBESCHLUSS Unterpunkt",
    )
    protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
    protokoll.status = "approved"
    protokoll.save()
    # Berichtigung im NÖ-Teil (Grund verschlüsselt)
    protocol_correction_service.propose(
        protokoll,
        target=SessionProtocolCorrection.TARGET_ITEM,
        item=SessionAgendaItem.objects.get(pk=w.top_noe.pk),
        data={"protocol_note": "GEHEIMNOTIZ berichtigt"},
        reason="GEHEIMGRUND",
        user=w.genehmiger,
        can_view_np=True,
    )
    return w


def _keine_geheimnisse(text: str, wo: str) -> None:
    for marker in GEHEIM:
        assert marker not in text, f"{marker} in {wo}"


# =============================================================================
# OParl und Nichtöffentliches
# =============================================================================


class TestOeffentlicheFassung:
    def test_resultsprotocol_am_meeting(self) -> None:
        w = welt()
        datei = _veroeffentlichen(w)
        meeting = _oparl(w, f"meeting/{w.sitzung.pk}/").json()
        assert meeting["resultsProtocol"]["id"].endswith(f"/file/{datei.pk}/")
        assert meeting["resultsProtocol"]["fileName"] == "Niederschrift (öffentlicher Teil).pdf"
        assert meeting["resultsProtocol"]["mimeType"] == "application/pdf"
        assert all(f["id"] != meeting["resultsProtocol"]["id"] for f in meeting.get("auxiliaryFile", []))
        objekt = _oparl(w, f"file/{datei.pk}/").json()
        assert "Radweg Hauptstraße" in objekt["text"] and objekt["meeting"][0].endswith(f"/meeting/{w.sitzung.pk}/")
        download = _oparl(w, f"file/{datei.pk}/download/")
        assert download.status_code == 200 and download["Content-Type"] == "application/pdf"
        pdf = pdf_text(b"".join(cast(Any, download).streaming_content))
        assert "Radweg Hauptstraße" in pdf and "Öffentliche Fassung" in pdf

    def test_nichts_nichtoeffentliches_in_oeffentlichen_ausgaben(self) -> None:
        w = _noe_welt()
        datei = _veroeffentlichen(w)
        ausgaben = {
            "text_content": datei.text_content,
            "Dateiname": f"{datei.name} {datei.file.name}",
            "PDF": pdf_text(datei.file.read()),
            "OParl Meeting": _oparl(w, f"meeting/{w.sitzung.pk}/").content.decode(),
            "OParl Datei": _oparl(w, f"file/{datei.pk}/").content.decode(),
            "OParl Dateiliste": _oparl(w, "files/").content.decode(),
            "OParl Meetingliste": _oparl(w, "meetings/").content.decode(),
        }
        for wo, text in ausgaben.items():
            _keine_geheimnisse(text, wo)
        assert "Radweg Hauptstraße" in ausgaben["text_content"]
        assert "Der Ausschuss beschließt den Radweg." in ausgaben["PDF"]
        assert "Allgemeiner Teil der Sitzung." in ausgaben["text_content"]

    def test_verschluesselte_felder_werden_nie_entschluesselt(self) -> None:
        w = _noe_welt()
        _veroeffentlichen(w)
        with mock.patch("apps.common.encryption.TenantEncryption.decrypt", side_effect=AssertionError("entschlüsselt")):
            neu = protocol_publication.publish(_protokoll(w), force=True)
            assert neu is not None
            text = protocol_publication.public_text(_protokoll(w))
        _keine_geheimnisse(text, "Text")

    def test_noe_sitzung_hat_keine_oeffentliche_fassung(self) -> None:
        w = welt(status="approved")
        SessionMeeting.objects.filter(pk=w.sitzung.pk).update(is_public=False)
        response = client(w.genehmiger).post(f"{base(w)}/meetings/{w.sitzung.pk}/protocol/publish/")
        assert response.status_code == 302
        protokoll = _protokoll(w)
        assert protokoll.status == "published" and protokoll.public_file is None
        assert not SessionFile.objects.filter(meeting=w.sitzung).exists()
        assert _oparl(w, f"meeting/{w.sitzung.pk}/").status_code == 404
        _keine_geheimnisse(_oparl(w, "files/").content.decode(), "Dateiliste")

    def test_datei_nur_solange_veroeffentlicht(self) -> None:
        w = welt()
        datei = _veroeffentlichen(w)
        SessionProtocol.objects.filter(pk=w.protokoll.pk).update(status="approved")
        assert _oparl(w, f"file/{datei.pk}/download/").status_code == 404
        assert "resultsProtocol" not in _oparl(w, f"meeting/{w.sitzung.pk}/").json()


# =============================================================================
# Rücknahme und Aktualisierung im Bürgerportal
# =============================================================================


def _spiegel(w: Welt) -> tuple[OParlBody, Any]:
    """Bürgerportal-Spiegel der Session-OParl-API (wie Ingestor bzw. sync_session_insight)."""
    basis = f"http://testserver/session/{w.tenant.slug}/api/oparl/"
    source = OParlSource.objects.create(name=f"{w.tenant.name} (Session)", url=basis)
    body = OParlBody.objects.create(external_id=basis + "body/", source=source, name=w.tenant.name, slug=w.tenant.slug)
    return body, cast(Any, SessionMirror)(source, fetch=lambda url: Client().get(url).json())


def _spiegeln(w: Welt) -> OParlMeeting:
    body, mirror = _spiegel(w)
    mirror._upsert_meeting(body, _oparl(w, f"meeting/{w.sitzung.pk}/").json())
    return OParlMeeting.objects.get(external_id__endswith=f"/meeting/{w.sitzung.pk}/")


def _gespiegelte_datei(datei: SessionFile) -> OParlFile:
    return OParlFile.objects.get(external_id__endswith=f"/file/{datei.pk}/")


class TestBuergerportal:
    def test_sitzungsseite_zeigt_niederschrift(self) -> None:
        w = welt()
        datei = _veroeffentlichen(w)
        meeting = _spiegeln(w)
        gespiegelt = _gespiegelte_datei(datei)
        assert gespiegelt.meeting_id == meeting.pk
        seite = Client().get(f"/insight/termine/{meeting.pk}/").content.decode()
        assert "oeffentliche-niederschrift" in seite and f"/dokumente/{gespiegelt.pk}/preview/" in seite
        gespiegelt.mark_deleted()
        assert "oeffentliche-niederschrift" not in Client().get(f"/insight/termine/{meeting.pk}/").content.decode()

    def test_ruecknahme_sofort(self, django_capture_on_commit_callbacks: Any) -> None:
        w = welt()
        datei = _veroeffentlichen(w)
        _spiegeln(w)
        with django_capture_on_commit_callbacks(execute=True):
            client(w.genehmiger).post(f"{base(w)}/meetings/{w.sitzung.pk}/protocol/unpublish/")
        assert _gespiegelte_datei(datei).deleted is True
        assert SessionOParlTombstone.objects.filter(oparl_type="file", object_id=datei.pk).exists()
        assert _oparl(w, f"file/{datei.pk}/").json()["deleted"] is True
        assert "resultsProtocol" not in _oparl(w, f"meeting/{w.sitzung.pk}/").json()

    def test_top_wird_nachtraeglich_nichtoeffentlich(self, django_capture_on_commit_callbacks: Any) -> None:
        w = welt()
        alt = _veroeffentlichen(w)
        _spiegeln(w)
        with django_capture_on_commit_callbacks(execute=True):
            item = SessionAgendaItem.objects.get(pk=w.top.pk)
            item.is_public = False
            item.save()
        assert not SessionFile.objects.filter(pk=alt.pk).exists()
        assert _gespiegelte_datei(alt).deleted is True
        neu = _protokoll(w).public_file
        assert neu is not None and neu.pk != alt.pk
        assert "Radweg" not in neu.text_content
        assert _oparl(w, f"meeting/{w.sitzung.pk}/").json()["resultsProtocol"]["id"].endswith(f"/file/{neu.pk}/")

    def test_sitzung_wird_nichtoeffentlich(self, django_capture_on_commit_callbacks: Any) -> None:
        w = welt()
        alt = _veroeffentlichen(w)
        _spiegeln(w)
        with django_capture_on_commit_callbacks(execute=True):
            sitzung = SessionMeeting.objects.get(pk=w.sitzung.pk)
            sitzung.is_public = False
            sitzung.save()
        assert _gespiegelte_datei(alt).deleted is True
        assert _protokoll(w).public_file is None

    def test_berichtigung_erneuert_die_fassung(self, django_capture_on_commit_callbacks: Any) -> None:
        w = welt()
        alt = _veroeffentlichen(w)
        _spiegeln(w)
        with django_capture_on_commit_callbacks(execute=True):
            client(w.genehmiger).post(
                f"{base(w)}/meetings/{w.sitzung.pk}/protocol/berichtigung/",
                {"top": str(w.top.pk), "vote_result": "rejected", "reason": "Zählfehler"},
            )
        neu = _protokoll(w).public_file
        assert neu is not None and neu.pk != alt.pk
        assert "Berichtigung vom" in neu.text_content and "(Zählfehler)" in neu.text_content
        assert "Berichtigung vom" in pdf_text(neu.file.read())
        assert _gespiegelte_datei(alt).deleted is True

    def test_unveraenderter_inhalt_behaelt_die_datei(self, django_capture_on_commit_callbacks: Any) -> None:
        w = welt()
        datei = _veroeffentlichen(w)
        with django_capture_on_commit_callbacks(execute=True):
            item = SessionAgendaItem.objects.get(pk=w.top.pk)
            item.implementation_status = "done"
            item.save()
            protocol_publication.refresh_meeting(w.sitzung.pk)
        assert _protokoll(w).public_file == datei


# =============================================================================
# Befehl und Beschlussauszug
# =============================================================================


class TestBefehl:
    def test_erzeugt_fehlende_fassungen_einmalig(self) -> None:
        w = welt(status="published")
        noe = welt("sued", status="published")
        SessionMeeting.objects.filter(pk=noe.sitzung.pk).update(is_public=False)
        out = StringIO()
        call_command("session_publish_protocols", "--dry-run", stdout=out)
        assert "würde erzeugen" in out.getvalue() and SessionFile.objects.count() == 0
        call_command("session_publish_protocols", stdout=StringIO())
        assert _protokoll(w).public_file is not None
        assert _protokoll(noe).public_file is None
        out = StringIO()
        call_command("session_publish_protocols", stdout=out)
        assert "0 öffentliche Fassung(en) erzeugt" in out.getvalue()


class TestBeschlussauszug:
    def test_gesperrter_stand_mit_berichtigung(self) -> None:
        w = welt()
        protocol_correction_service.propose(
            _protokoll(w),
            target=SessionProtocolCorrection.TARGET_ITEM,
            item=SessionAgendaItem.objects.get(pk=w.top.pk),
            data={"votes_no": "0"},
            reason="Zählfehler",
            user=w.genehmiger,
            can_view_np=True,
        )
        response = client(w.leser).get(f"{base(w)}/agenda/{w.top.pk}/beschlussauszug.pdf")
        text = pdf_text(response.content)
        assert "Stand der genehmigten Niederschrift" in text
        assert "Berichtigung vom" in text and "Zählfehler" in text

    def test_vorlaeufiger_stand_und_noe_regeln(self) -> None:
        w = welt(status="review")
        geheim = SessionPaper.objects.create(tenant=w.tenant, name="GEHEIMTOP Vorlage", is_public=False)
        SessionAgendaItem.objects.filter(pk=w.top.pk).update(paper=geheim)
        leser = client(w.leser)
        text = pdf_text(leser.get(f"{base(w)}/agenda/{w.top.pk}/beschlussauszug.pdf").content)
        assert "Vorläufiger Stand" in text
        _keine_geheimnisse(text, "Beschlussauszug")
        assert leser.get(f"{base(w)}/agenda/{w.top_noe.pk}/beschlussauszug.pdf").status_code == 404


def test_oeffentliche_niederschrift_im_json_nur_einmal() -> None:
    """resultsProtocol ersetzt die Anlage – dieselbe Datei steht nicht doppelt am Meeting."""
    w = welt()
    datei = _veroeffentlichen(w)
    dump = json.dumps(_oparl(w, f"meeting/{w.sitzung.pk}/").json())
    assert dump.count(f"/file/{datei.pk}/download/?download=1") == 1
