# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sofort-Fixes zur Freigabe (Issue #582, Entscheidungen „1 A, 2 B, 3 A“ vom 09.10.2026).

1. Ordner-Freigaben an Gäste erfassen keine privaten Dokumente der freigebenden Person mehr: privat bleibt
   privat. Einzeln lassen sich private Dokumente weiter freigeben.
2. Gäste exportieren ab der Stufe „Lesen“ (PDF, DOCX) und laden Anhänge herunter – je Freigabe abschaltbar
   („Herunterladen erlauben“, Standard an). Downloads von Gästen stehen in der Änderungshistorie.
3. Entzug einer Freigabe, Mitgliedschaft oder von Rechten wirkt sofort in geöffneten Bearbeitungen: Die
   Live-Verbindung wird getrennt oder herabgestuft, auch wenn die Benachrichtigung verloren geht (Nachprüfung
   beim Speichern).
"""

from __future__ import annotations

import asyncio
import base64
import re
from typing import Any, cast

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.tenants.models import Membership
from apps.work.faction.models import FactionAuditLog
from apps.work.motions import consumers, export_service
from apps.work.motions.consumers import DocumentCollaborationConsumer
from apps.work.motions.models import DocumentFolder, FolderGuestShare, Motion, MotionDocument, MotionShare
from apps.work.organization import selectors, services

RECHTE = ["motions.view", "motions.view_drafts", "motions.create", "motions.edit", "motions.comment"]
PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
DOCX_BYTES = b"PK\x03\x04docx"


def _gast(org: Any, email: str = "gast@example.org") -> Any:
    konto = UserFactory(email=email)  # type: ignore[no-untyped-call]
    return MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]


@pytest.fixture
def freigebende(org: Any, make_member: Any) -> Any:
    return make_member(
        org, [*RECHTE, "motions.share", "guests.manage", "members.view"], email="freigebende@example.org"
    )


@pytest.fixture
def kollegin(org: Any, make_member: Any) -> Any:
    return make_member(org, RECHTE, email="kollegin@example.org")


@pytest.fixture
def export_ohne_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export_service.motion_export_service, "export_to_pdf", lambda motion: PDF_BYTES)
    monkeypatch.setattr(export_service.motion_export_service, "export_to_docx", lambda motion: DOCX_BYTES)


def _url(name: str, org: Any, **kwargs: Any) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug, **kwargs})


def _gast_downloads(org: Any) -> list[dict[str, Any]]:
    return list(
        FactionAuditLog.objects.filter(organization=org, action="guest_download")
        .order_by("created_at")
        .values_list("changes", flat=True)
    )


# =============================================================================
# 1. Ordner-Freigaben: privat bleibt privat
# =============================================================================


@pytest.mark.django_db
def test_ordnerfreigabe_erfasst_private_dokumente_der_freigebenden_person_nicht(
    org: Any, freigebende: Any, client_for: Any
) -> None:
    ordner = DocumentFolder.objects.create(organization=org, name="AG Haushalt", created_by=freigebende)
    gast = _gast(org)
    FolderGuestShare.objects.create(folder=ordner, user=gast.user, level="edit", created_by=freigebende.user)
    privat = Motion.objects.create(
        organization=org, author=freigebende, title="Privater Entwurf", visibility="private", folder=ordner
    )
    geteilt = Motion.objects.create(
        organization=org, author=freigebende, title="Geteilter Entwurf", visibility="shared", folder=ordner
    )
    Motion.objects.create(
        organization=org, author=freigebende, title="Für alle", visibility="organization", folder=ordner
    )

    assert privat.access_level(gast) == "none"
    assert cast(Any, privat).guest_share_since(gast) is None
    assert geteilt.can_edit(gast)
    sichtbar = set(cast(Any, Motion).visible_to(gast).values_list("title", flat=True))
    assert sichtbar == {"Geteilter Entwurf", "Für alle"}

    client = client_for(gast.user)
    uebersicht = client.get(_url("guest_documents", org), {"ordner": str(ordner.id)}).content.decode()
    assert "Für alle" in uebersicht
    assert "Privater Entwurf" not in uebersicht
    assert client.get(_url("document_editor", org, motion_id=privat.id)).status_code in (403, 404)

    # „Was sieht dieser Gast?“ zählt nur, was die Freigabe erfasst
    (freigabe,) = selectors.guest_folder_shares(org, gast.user)
    assert cast(Any, freigabe).document_count == 2


@pytest.mark.django_db
def test_privates_dokument_bleibt_einzeln_freigebbar(org: Any, freigebende: Any) -> None:
    ordner = DocumentFolder.objects.create(organization=org, name="AG Haushalt", created_by=freigebende)
    gast = _gast(org)
    FolderGuestShare.objects.create(folder=ordner, user=gast.user, level="view", created_by=freigebende.user)
    privat = Motion.objects.create(
        organization=org, author=freigebende, title="Privater Entwurf", visibility="private", folder=ordner
    )
    MotionShare.objects.create(
        motion=privat, scope="user", user=gast.user, level="comment", created_by=freigebende.user
    )

    assert privat.access_level(gast) == "comment"
    assert "Privater Entwurf" in set(cast(Any, Motion).visible_to(gast).values_list("title", flat=True))


# =============================================================================
# 2. Herunterladen: ab Lesen, je Freigabe abschaltbar, im Prüfprotokoll
# =============================================================================


@pytest.mark.django_db
@pytest.mark.usefixtures("export_ohne_renderer")
def test_gast_mit_lesen_exportiert_und_steht_in_der_aenderungshistorie(
    org: Any, freigebende: Any, client_for: Any
) -> None:
    dokument = Motion.objects.create(organization=org, author=freigebende, title="Antrag", visibility="private")
    gast = _gast(org)
    MotionShare.objects.create(motion=dokument, scope="user", user=gast.user, level="view", created_by=freigebende.user)
    client = client_for(gast.user)

    assert client.get(_url("document_export", org, motion_id=dokument.id), {"format": "pdf"}).status_code == 200
    assert client.get(_url("document_export", org, motion_id=dokument.id), {"format": "docx"}).status_code == 200

    assert _gast_downloads(org) == [{"art": "PDF-Export"}, {"art": "DOCX-Export"}]
    eintrag = FactionAuditLog.objects.filter(organization=org, action="guest_download").first()
    assert eintrag is not None
    assert eintrag.object_id == dokument.id and eintrag.membership_id == gast.id


@pytest.mark.django_db
@pytest.mark.usefixtures("export_ohne_renderer")
def test_exporte_von_mitgliedern_bleiben_ohne_eintrag(org: Any, freigebende: Any, client_for: Any) -> None:
    dokument = Motion.objects.create(organization=org, author=freigebende, title="Antrag", visibility="private")

    antwort = client_for(freigebende.user).get(_url("document_export", org, motion_id=dokument.id), {"format": "pdf"})

    assert antwort.status_code == 200
    assert _gast_downloads(org) == []


@pytest.mark.django_db
@pytest.mark.usefixtures("export_ohne_renderer")
def test_herunterladen_aus_sperrt_export_und_anhaenge(
    org: Any, freigebende: Any, client_for: Any, tmp_path: Any
) -> None:
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        dokument = Motion.objects.create(organization=org, author=freigebende, title="Antrag", visibility="private")
        anhang = MotionDocument.objects.create(
            motion=dokument,
            file=SimpleUploadedFile("anlage.pdf", PDF_BYTES, content_type="application/pdf"),
            filename="anlage.pdf",
            mime_type="application/pdf",
            file_size=len(PDF_BYTES),
            uploaded_by=freigebende,
        )
        gast = _gast(org)
        freigabe = MotionShare.objects.create(
            motion=dokument, scope="user", user=gast.user, level="edit", created_by=freigebende.user
        )
        client = client_for(gast.user)
        export_url = _url("document_export", org, motion_id=dokument.id)
        anhang_url = _url("document_file_download", org, motion_id=dokument.id, document_id=anhang.id)

        # Standard (auch für bestehende Freigaben): Herunterladen erlaubt
        assert freigabe.allow_download is True
        editor = client.get(_url("document_editor", org, motion_id=dokument.id)).content.decode()
        assert export_url in editor and anhang_url in editor
        antwort = client.get(anhang_url)
        assert antwort.status_code == 200
        # Vollständig gelesen schließt der Test-Client die Datei selbst (kein close(): das beendete die Verbindung)
        assert b"".join(antwort.streaming_content) == PDF_BYTES
        assert _gast_downloads(org) == [{"art": "Anhang", "anhang_id": str(anhang.id)}]

        freigabe.allow_download = False
        cast(Any, freigabe).save()

        assert client.get(export_url, {"format": "pdf"}).status_code == 403
        assert client.get(export_url, {"format": "docx"}).status_code == 403
        assert client.get(anhang_url).status_code == 403
        editor = client.get(_url("document_editor", org, motion_id=dokument.id)).content.decode()
        assert export_url not in editor and anhang_url not in editor
        assert "anlage.pdf" in editor  # der Anhang bleibt sichtbar, nur ohne Download
        assert len(_gast_downloads(org)) == 1


@pytest.mark.django_db
def test_neuer_editor_ohne_herunterladen_ohne_export_im_menue(
    org: Any, freigebende: Any, client_for: Any, tmp_path: Any
) -> None:
    """Neuer Antragseditor (#856): Menü „Datei“ ohne Vorschau und Word-Export, Anhänge ohne Link."""
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        dokument = Motion.objects.create(organization=org, author=freigebende, title="Antrag", visibility="private")
        anhang = MotionDocument.objects.create(
            motion=dokument,
            file=SimpleUploadedFile("anlage.pdf", PDF_BYTES, content_type="application/pdf"),
            filename="anlage.pdf",
            mime_type="application/pdf",
            file_size=len(PDF_BYTES),
            uploaded_by=freigebende,
        )
        gast = _gast(org)
        freigabe = MotionShare.objects.create(
            motion=dokument, scope="user", user=gast.user, level="view", created_by=freigebende.user
        )
        client = client_for(gast.user)
        editor_url = _url("document_editor", org, motion_id=dokument.id)
        export_url = _url("document_export", org, motion_id=dokument.id)
        anhang_url = _url("document_file_download", org, motion_id=dokument.id, document_id=anhang.id)

        antwort = client.get(editor_url)
        assert [t.name for t in antwort.templates][0] == "work/motions/editor_neu.html"
        seite = antwort.content.decode()
        assert export_url in seite and "Herunterladen als Word" in seite and anhang_url in seite

        freigabe.allow_download = False
        cast(Any, freigabe).save()

        seite = client.get(editor_url).content.decode()
        assert export_url not in seite and "Herunterladen als Word" not in seite and anhang_url not in seite
        assert "anlage.pdf" in seite


@pytest.mark.django_db
@pytest.mark.usefixtures("export_ohne_renderer")
def test_weitestgehende_freigabe_entscheidet_ueber_das_herunterladen(
    org: Any, freigebende: Any, client_for: Any
) -> None:
    ordner = DocumentFolder.objects.create(organization=org, name="Extern", created_by=freigebende)
    gast = _gast(org)
    FolderGuestShare.objects.create(
        folder=ordner, user=gast.user, level="view", created_by=freigebende.user, allow_download=False
    )
    dokument = Motion.objects.create(
        organization=org, author=freigebende, title="Für alle", visibility="organization", folder=ordner
    )
    export_url = _url("document_export", org, motion_id=dokument.id)
    client = client_for(gast.user)

    assert dokument.can_access(gast) and not dokument.can_download(gast)
    assert client.get(export_url).status_code == 403

    direkt = MotionShare.objects.create(
        motion=dokument, scope="user", user=gast.user, level="view", created_by=freigebende.user
    )
    assert client.get(export_url).status_code == 200

    direkt.allow_download = False
    cast(Any, direkt).save()
    assert client.get(export_url).status_code == 403


@pytest.mark.django_db
def test_schalter_je_freigabe_umschalten(org: Any, freigebende: Any, kollegin: Any, client_for: Any) -> None:
    ordner = DocumentFolder.objects.create(organization=org, name="Extern", created_by=freigebende)
    dokument = Motion.objects.create(organization=org, author=freigebende, title="Antrag", visibility="private")
    gast = _gast(org)
    dokument_freigabe = MotionShare.objects.create(
        motion=dokument, scope="user", user=gast.user, level="view", created_by=freigebende.user
    )
    ordner_freigabe = FolderGuestShare.objects.create(
        folder=ordner, user=gast.user, level="view", created_by=freigebende.user
    )
    dokument_url = _url("document_share_download", org, share_id=dokument_freigabe.id)
    ordner_url = _url("document_folder_share_download", org, share_id=ordner_freigabe.id)

    # Ohne Freigabe- oder Gastverwaltungsrecht: nichts ändert sich
    client_for(kollegin.user).post(dokument_url, {"allow_download": "0"})
    client_for(kollegin.user).post(ordner_url, {"allow_download": "0"})
    dokument_freigabe.refresh_from_db()
    ordner_freigabe.refresh_from_db()
    assert dokument_freigabe.allow_download and ordner_freigabe.allow_download

    client = client_for(freigebende.user)
    assert client.post(dokument_url, {"allow_download": "0"}).status_code == 204
    assert client.post(ordner_url, {"allow_download": "0"}).status_code == 204
    dokument_freigabe.refresh_from_db()
    ordner_freigabe.refresh_from_db()
    assert not dokument_freigabe.allow_download and not ordner_freigabe.allow_download

    # „Was sieht dieser Gast?“ zeigt den Stand und schaltet zurück
    seite = client.get(_url("member_detail", org, member_id=gast.id)).content.decode()
    assert dokument_url in seite and ordner_url in seite
    assert "Herunterladen (PDF, Word, Anhänge): aus" in seite
    client.post(dokument_url, {"allow_download": "1"})
    dokument_freigabe.refresh_from_db()
    assert dokument_freigabe.allow_download


@pytest.mark.django_db
def test_teilen_dialog_und_einladung_setzen_den_schalter(org: Any, freigebende: Any, client_for: Any) -> None:
    dokument = Motion.objects.create(organization=org, author=freigebende, title="Antrag", visibility="shared")
    gast = _gast(org)
    update_url = _url("document_share_update", org, motion_id=dokument.id)
    client = client_for(freigebende.user)

    client.post(update_url, {"visibility": "shared", "add_user_email": gast.user.email, "level": "view"})
    assert MotionShare.objects.get(motion=dokument, user=gast.user).allow_download is True

    client.post(
        update_url,
        {"visibility": "shared", "add_user_email": gast.user.email, "level": "view", "allow_download": ["0", "1"]},
    )
    assert MotionShare.objects.get(motion=dokument, user=gast.user).allow_download is True
    client.post(
        update_url,
        {"visibility": "shared", "add_user_email": gast.user.email, "level": "view", "allow_download": "0"},
    )
    assert MotionShare.objects.get(motion=dokument, user=gast.user).allow_download is False

    # Gast-Einladung: der Schalter gilt für alle Freigaben der Einladung
    ordner = DocumentFolder.objects.create(organization=org, name="Extern", created_by=freigebende)
    eigenes = Motion.objects.create(organization=org, author=freigebende, title="Eigenes", visibility="private")
    services.invite_guest(
        org,
        freigebende,
        email="neu@example.org",
        note="",
        share_level="view",
        document_ids=[str(eigenes.id)],
        folder_ids=[str(ordner.id)],
        allow_download=False,
    )
    assert MotionShare.objects.get(motion=eigenes, user__email="neu@example.org").allow_download is False
    assert FolderGuestShare.objects.get(folder=ordner, user__email="neu@example.org").allow_download is False


@pytest.mark.django_db
@pytest.mark.usefixtures("export_ohne_renderer")
def test_aenderungshistorie_zeigt_titel_und_anhang_nur_wer_das_dokument_sieht(
    org: Any, make_member: Any, client_for: Any, tmp_path: Any
) -> None:
    """
    Gast lädt ein privates, einzeln freigegebenes Dokument herunter: Titel und Dateiname stehen nie in der
    Hash-Kette, und in der Änderungshistorie sieht sie nur, wer das Dokument selbst sieht.
    """
    titel = "Entwurf Kandidatenliste"
    dateiname = "kandidaten-intern.pdf"
    autorin = make_member(
        org, [*RECHTE, "motions.share", "faction.view_public", "faction.view_audit"], email="autorin@example.org"
    )
    vorsitz = make_member(
        org,
        ["faction.view_public", "faction.view_audit", "motions.view", "motions.view_drafts"],
        email="vorsitz@example.org",
    )
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        dokument = Motion.objects.create(organization=org, author=autorin, title=titel, visibility="private")
        anhang = MotionDocument.objects.create(
            motion=dokument,
            file=SimpleUploadedFile(dateiname, PDF_BYTES, content_type="application/pdf"),
            filename=dateiname,
            mime_type="application/pdf",
            file_size=len(PDF_BYTES),
            uploaded_by=autorin,
        )
        gast = _gast(org)
        MotionShare.objects.create(motion=dokument, scope="user", user=gast.user, level="view", created_by=autorin.user)
        client = client_for(gast.user)
        export_url = _url("document_export", org, motion_id=dokument.id)
        assert client.get(export_url, {"format": "pdf"}).status_code == 200
        antwort = client.get(_url("document_file_download", org, motion_id=dokument.id, document_id=anhang.id))
        assert b"".join(antwort.streaming_content) == PDF_BYTES

    eintraege = list(FactionAuditLog.objects.filter(organization=org, action="guest_download"))
    assert len(eintraege) == 2
    for eintrag in eintraege:
        gespeichert = f"{eintrag.object_repr} {eintrag.changes}"
        assert titel not in gespeichert and dateiname not in gespeichert
        assert eintrag.object_id == dokument.id

    historie_url = _url("faction_audit", org)
    antwort = client_for(vorsitz.user).get(historie_url)
    assert antwort.status_code == 200
    seite = antwort.content.decode()
    assert titel not in seite and dateiname not in seite
    assert seite.count("Gesperrte Information") == 2

    seite = client_for(autorin.user).get(historie_url).content.decode()
    assert titel in seite and dateiname in seite
    assert "PDF-Export" in seite and "Gesperrte Information" not in seite


@pytest.mark.django_db
def test_stufenaenderung_laesst_abgeschaltetes_herunterladen_aus(org: Any, freigebende: Any, client_for: Any) -> None:
    """Schalter aus, danach Stufe ändern (Teilen-Dialog, Ordner-Freigabe ohne Feld): Der Schalter bleibt aus."""
    dokument = Motion.objects.create(organization=org, author=freigebende, title="Antrag", visibility="shared")
    ordner = DocumentFolder.objects.create(organization=org, name="Extern", created_by=freigebende)
    gast = _gast(org)
    freigabe = MotionShare.objects.create(
        motion=dokument, scope="user", user=gast.user, level="view", created_by=freigebende.user, allow_download=False
    )
    ordner_freigabe = FolderGuestShare.objects.create(
        folder=ordner, user=gast.user, level="view", created_by=freigebende.user, allow_download=False
    )
    update_url = _url("document_share_update", org, motion_id=dokument.id)
    ordner_url = _url("document_folder_share", org, folder_id=ordner.id)
    client = client_for(freigebende.user)

    client.post(update_url, {"visibility": "shared", "add_user_email": gast.user.email, "level": "comment"})
    client.post(ordner_url, {"email": gast.user.email, "level": "edit"})
    freigabe.refresh_from_db()
    ordner_freigabe.refresh_from_db()
    assert (freigabe.level, freigabe.allow_download) == ("comment", False)
    assert (ordner_freigabe.level, ordner_freigabe.allow_download) == ("edit", False)

    # Ausdrücklich gewählt: Der Schalter folgt der Angabe
    client.post(
        update_url,
        {"visibility": "shared", "add_user_email": gast.user.email, "level": "comment", "allow_download": "1"},
    )
    client.post(ordner_url, {"email": gast.user.email, "level": "edit", "allow_download": "1"})
    freigabe.refresh_from_db()
    ordner_freigabe.refresh_from_db()
    assert freigabe.allow_download and ordner_freigabe.allow_download

    # Der Dialog sendet das Feld nur, wenn jemand den Schalter bedient hat (vorher gesperrt, Kästchen ohne Namen)
    seite = client.get(_url("documents", org)).content.decode()
    feld = re.search(r'<input type="hidden" name="allow_download"[^>]*>', seite)
    assert feld is not None and re.search(r"\sdisabled[\s>]", feld.group(0))
    assert not re.search(r'<input type="checkbox"[^>]*name="allow_download"', seite)
    assert "Gilt nur für Gäste" in seite


# =============================================================================
# 3. Entzug wirkt sofort in geöffneten Bearbeitungen
# =============================================================================


def _communicator(user: Any, motion: Motion) -> WebsocketCommunicator:
    communicator = WebsocketCommunicator(DocumentCollaborationConsumer.as_asgi(), f"/ws/documents/{motion.id}/")
    communicator.scope["user"] = user
    communicator.scope["url_route"] = {"kwargs": {"document_id": str(motion.id)}}
    return communicator


async def _verbinden(user: Any, motion: Motion, stufe: str) -> WebsocketCommunicator:
    communicator = _communicator(user, motion)
    connected, _ = await communicator.connect()
    assert connected
    begruessung = await communicator.receive_json_from()
    assert begruessung["type"] == "connected"
    assert begruessung["user"]["access_level"] == stufe
    assert (await communicator.receive_json_from())["type"] == "yjs_state"
    return communicator


def _yjs_save(html: str) -> dict[str, str]:
    return {"type": "yjs_save", "data": base64.b64encode(b"neuer-zustand").decode("ascii"), "html": html}


async def _wird_getrennt(communicator: WebsocketCommunicator) -> None:
    assert await communicator.receive_json_from(timeout=5) == {"type": "reload", "reason": "access_revoked"}
    assert await communicator.receive_output(timeout=5) == {"type": "websocket.close", "code": 4403}
    # Was noch eintrifft, nimmt die Verbindung nicht mehr an
    await communicator.send_json_to(_yjs_save("<p>Nach dem Entzug</p>"))
    assert await communicator.receive_nothing(timeout=0.3)


def _dokument(org: Any, autorin: Any, visibility: str, **extra: Any) -> Motion:
    motion = Motion.objects.create(organization=org, author=autorin, title="Antrag", visibility=visibility, **extra)
    cast(Any, motion).set_content_encrypted("<p>Stand</p>")
    motion.save()
    return motion


def _inhalt(motion: Motion) -> str:
    motion.refresh_from_db()
    return str(cast(Any, motion).get_content_decrypted())


@pytest.mark.django_db(transaction=True)
def test_entzug_einer_persoenlichen_freigabe_trennt_die_offene_bearbeitung(
    org: Any, freigebende: Any, kollegin: Any
) -> None:
    dokument = _dokument(org, freigebende, "shared")
    freigabe = MotionShare.objects.create(
        motion=dokument, scope="user", user=kollegin.user, level="edit", created_by=freigebende.user
    )

    async def lauf() -> None:
        autorin = await _verbinden(freigebende.user, dokument, "edit")
        verbindung = await _verbinden(kollegin.user, dokument, "edit")

        await database_sync_to_async(freigabe.delete)()

        await _wird_getrennt(verbindung)
        # Die Autorin arbeitet ungestört weiter
        assert await autorin.receive_nothing(timeout=0.5)
        await verbindung.disconnect()
        await autorin.disconnect()

    asyncio.run(lauf())
    assert _inhalt(dokument) == "<p>Stand</p>"
    assert dokument.get_yjs_state() is None


@pytest.mark.django_db(transaction=True)
def test_herabgestufte_freigabe_speichert_nicht_mehr(org: Any, freigebende: Any, kollegin: Any) -> None:
    dokument = _dokument(org, freigebende, "shared")
    freigabe = MotionShare.objects.create(
        motion=dokument, scope="user", user=kollegin.user, level="edit", created_by=freigebende.user
    )

    async def lauf() -> None:
        verbindung = await _verbinden(kollegin.user, dokument, "edit")

        freigabe.level = "view"
        await database_sync_to_async(freigabe.save)()

        assert await verbindung.receive_json_from(timeout=5) == {"type": "reload", "reason": "access_changed"}
        await verbindung.send_json_to(_yjs_save("<p>Nach der Herabstufung</p>"))
        assert await verbindung.receive_nothing(timeout=0.5)
        await verbindung.disconnect()

    asyncio.run(lauf())
    assert _inhalt(dokument) == "<p>Stand</p>"
    assert dokument.get_yjs_state() is None


@pytest.mark.django_db(transaction=True)
def test_entzug_einer_ordnerfreigabe_trennt_den_gast(org: Any, freigebende: Any) -> None:
    ordner = DocumentFolder.objects.create(organization=org, name="Extern", created_by=freigebende)
    gast = _gast(org)
    freigabe = FolderGuestShare.objects.create(folder=ordner, user=gast.user, level="edit", created_by=freigebende.user)
    dokument = _dokument(org, freigebende, "organization", folder=ordner)

    async def lauf() -> None:
        verbindung = await _verbinden(gast.user, dokument, "edit")
        await database_sync_to_async(freigabe.delete)()
        await _wird_getrennt(verbindung)
        await verbindung.disconnect()

    asyncio.run(lauf())
    assert _inhalt(dokument) == "<p>Stand</p>"


@pytest.mark.django_db(transaction=True)
def test_deaktivierte_mitgliedschaft_trennt_sofort(org: Any, freigebende: Any, kollegin: Any) -> None:
    dokument = _dokument(org, freigebende, "organization")

    async def lauf() -> None:
        verbindung = await _verbinden(kollegin.user, dokument, "comment")
        kollegin.is_active = False
        await database_sync_to_async(kollegin.save)()
        await _wird_getrennt(verbindung)
        await verbindung.disconnect()

    asyncio.run(lauf())


@pytest.mark.django_db(transaction=True)
def test_entzogene_rolle_trennt_sofort(org: Any, freigebende: Any, kollegin: Any) -> None:
    dokument = _dokument(org, freigebende, "organization")

    async def lauf() -> None:
        verbindung = await _verbinden(kollegin.user, dokument, "comment")
        await database_sync_to_async(kollegin.roles.clear)()
        await _wird_getrennt(verbindung)
        await verbindung.disconnect()

    asyncio.run(lauf())


@pytest.mark.django_db(transaction=True)
def test_privat_gestelltes_dokument_trennt_freigegebene(org: Any, freigebende: Any, kollegin: Any) -> None:
    dokument = _dokument(org, freigebende, "shared")
    MotionShare.objects.create(
        motion=dokument, scope="user", user=kollegin.user, level="edit", created_by=freigebende.user
    )

    async def lauf() -> None:
        verbindung = await _verbinden(kollegin.user, dokument, "edit")
        dokument.visibility = "private"
        await database_sync_to_async(dokument.save)()
        await _wird_getrennt(verbindung)
        await verbindung.disconnect()

    asyncio.run(lauf())


@pytest.mark.django_db(transaction=True)
def test_nachpruefung_beim_speichern_greift_ohne_benachrichtigung(
    org: Any, freigebende: Any, kollegin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Geht die Benachrichtigung verloren (hier: Änderung ohne Signale), prüft das Speichern selbst nach."""
    monkeypatch.setattr(consumers, "ACCESS_RECHECK_INTERVAL_SECONDS", 0)
    dokument = _dokument(org, freigebende, "shared")
    freigabe = MotionShare.objects.create(
        motion=dokument, scope="user", user=kollegin.user, level="edit", created_by=freigebende.user
    )

    async def lauf() -> None:
        verbindung = await _verbinden(kollegin.user, dokument, "edit")
        await database_sync_to_async(MotionShare.objects.filter(pk=freigabe.pk).update)(level="view")
        assert await verbindung.receive_nothing(timeout=0.3)

        await verbindung.send_json_to(_yjs_save("<p>Ohne Recht</p>"))
        assert await verbindung.receive_json_from(timeout=5) == {"type": "reload", "reason": "access_changed"}
        assert await verbindung.receive_nothing(timeout=0.3)
        await verbindung.disconnect()

    asyncio.run(lauf())
    assert _inhalt(dokument) == "<p>Stand</p>"
    assert dokument.get_yjs_state() is None


@pytest.mark.django_db(transaction=True)
def test_geloeschte_rolle_trennt_sofort(org: Any, freigebende: Any, kollegin: Any) -> None:
    """Beim Löschen einer Rolle verschwinden die Zuordnungen per Kaskade ohne m2m_changed."""
    dokument = _dokument(org, freigebende, "organization")
    rolle = kollegin.roles.get()

    async def lauf() -> None:
        verbindung = await _verbinden(kollegin.user, dokument, "comment")
        await database_sync_to_async(rolle.delete)()
        await _wird_getrennt(verbindung)
        await verbindung.disconnect()

    asyncio.run(lauf())


@pytest.mark.django_db(transaction=True)
def test_deaktiviertes_konto_trennt_sofort(org: Any, freigebende: Any, kollegin: Any) -> None:
    """Das Konto wird deaktiviert, die Mitgliedschaft bleibt aktiv."""
    dokument = _dokument(org, freigebende, "organization")
    konto = kollegin.user

    async def lauf() -> None:
        verbindung = await _verbinden(konto, dokument, "comment")
        konto.is_active = False
        await database_sync_to_async(konto.save)()
        await _wird_getrennt(verbindung)
        await verbindung.disconnect()

    asyncio.run(lauf())


@pytest.mark.django_db(transaction=True)
def test_nachpruefung_greift_auch_bei_lesenden_ohne_benachrichtigung(
    org: Any, freigebende: Any, kollegin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lesende erhalten nach einem Entzug ohne Benachrichtigung keine Live-Änderungen mehr."""
    monkeypatch.setattr(consumers, "ACCESS_RECHECK_INTERVAL_SECONDS", 0)
    dokument = _dokument(org, freigebende, "organization")
    aenderung = {"type": "yjs_sync", "data": base64.b64encode(b"\x00\x02aenderung").decode("ascii")}

    async def lauf() -> None:
        autorin = await _verbinden(freigebende.user, dokument, "edit")
        lesende = await _verbinden(kollegin.user, dokument, "comment")
        await database_sync_to_async(Membership.objects.filter(pk=kollegin.pk).update)(is_active=False)
        assert await lesende.receive_nothing(timeout=0.3)

        await autorin.send_json_to(aenderung)
        await _wird_getrennt(lesende)
        await lesende.disconnect()
        await autorin.disconnect()

    asyncio.run(lauf())
