# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ordner „Nichtöffentliche Vorgänge“ im Dokumentenspeicher (Issue #873).

Dokumente darin öffnen ausschließlich vereidigte Mitglieder der Organisation – unabhängig von Rollen
(auch Administration), Sichtbarkeit, Freigaben, Federführung oder Mitarbeit. Gäste nie. Geprüft werden alle
Lesewege (Liste, Ordner, Suche, Editor, Download, Export, Versionen, Live-Bearbeitung, Bezugssuche,
Dashboard, Papierkorb, Gastübersicht, Verknüpfungen) und dass Freigaben serverseitig abgelehnt werden.
Aufrufe stehen in der Änderungshistorie.
"""

from __future__ import annotations

import io
from typing import Any, cast
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.faction.models import FactionAuditLog
from apps.work.motions import non_public
from apps.work.motions.models import (
    DocumentFolder,
    FolderGuestShare,
    Motion,
    MotionApproval,
    MotionComment,
    MotionDocument,
    MotionShare,
)
from apps.work.notifications.models import Notification

GEHEIM = "Grundstueck Musterweg Kaufpreis"
ORDNER = DocumentFolder.NON_PUBLIC_NAME
RECHTE = [
    "motions.view",
    "motions.view_drafts",
    "motions.view_former_members",
    "motions.create",
    "motions.edit",
    "motions.edit_all",
    "motions.delete",
    "motions.comment",
    "motions.share",
    "motions.submit_to_ris",
    "motions.approve",
    "guests.manage",
    "organization.edit",
    "faction.view_public",
    "faction.view_non_public",
    "faction.view_audit",
    "tasks.view",
    "tasks.create",
    "dashboard.view",
]


def _pdf(text: str = GEHEIM) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    puffer = io.BytesIO()
    leinwand = canvas.Canvas(puffer, pagesize=A4)
    leinwand.drawString(72, 750, "Nichtoeffentlicher Teil")
    leinwand.drawString(72, 730, f"N 1 {text}")
    leinwand.showPage()
    leinwand.save()
    return puffer.getvalue()


@pytest.fixture(autouse=True)
def _medien(settings: Any, tmp_path: Any) -> None:
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def vereidigt(org: Any, make_member: Any) -> Any:
    member = make_member(org, RECHTE, email="vereidigt@example.org")
    member.is_sworn_in = True
    member.save(update_fields=["is_sworn_in"])
    return member


@pytest.fixture
def vereidigt2(org: Any, make_member: Any) -> Any:
    member = make_member(org, RECHTE, email="vereidigt2@example.org")
    member.is_sworn_in = True
    member.save(update_fields=["is_sworn_in"])
    return member


@pytest.fixture
def nicht_vereidigt(org: Any, make_member: Any) -> Any:
    """Alle Dokumentrechte und Recht auf den nichtöffentlichen Teil – aber nicht vereidigt."""
    return make_member(org, RECHTE, email="nicht-vereidigt@example.org")


@pytest.fixture
def admin_nicht_vereidigt(org: Any, make_member: Any) -> Any:
    return make_member(org, [], email="admin@example.org", is_admin=True)


@pytest.fixture
def gast(org: Any) -> Any:
    konto = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    return MembershipFactory(user=konto, organization=org, is_guest=True, is_sworn_in=True)  # type: ignore[no-untyped-call]


@pytest.fixture
def unterlage(org: Any, vereidigt: Any) -> Motion:
    datei = SimpleUploadedFile("unterlage.pdf", _pdf(), content_type="application/pdf")
    extracted = non_public.extract(_pdf(), "unterlage.pdf")
    assert extracted is not None
    return non_public.store(
        organization=org,
        author=vereidigt,
        uploaded_file=datei,
        file_size=datei.size or 0,
        title=f"Nichtöffentliche Unterlage: {GEHEIM}",
        extracted=extracted,
    )


def _nicht_berechtigt(nicht_vereidigt: Any, admin_nicht_vereidigt: Any, gast: Any) -> list[Any]:
    return [nicht_vereidigt, admin_nicht_vereidigt, gast]


def _url(name: str, org: Any, **kwargs: Any) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug, **kwargs})


# =============================================================================
# Ablage
# =============================================================================


@pytest.mark.django_db
def test_ablage_im_ordner_verschluesselt_ohne_klartextkopie(org: Any, unterlage: Motion) -> None:
    ordner = DocumentFolder.objects.get(organization=org, sworn_in_only=True)
    assert ordner.name == ORDNER and ordner.parent_id is None
    assert unterlage.folder_id == ordner.id
    # Gespeichert als privat (Rückfall auf eine ältere Version), geöffnet über den Ordner
    assert unterlage.visibility == "private"
    assert GEHEIM in (unterlage.content or "")
    # Inhalt nur verschlüsselt gespeichert, keine Suchkopie im Klartext
    gespeichert = Motion.objects.filter(pk=unterlage.pk).values_list("content_encrypted", flat=True).get()
    assert GEHEIM.encode() not in bytes(gespeichert or b"")
    anhang = MotionDocument.objects.get(motion=unterlage)
    assert anhang.text_content == ""
    assert FactionAuditLog.objects.filter(
        organization=org, action="internal_document_stored", object_id=unterlage.pk, is_internal=True
    ).exists()


@pytest.mark.django_db
def test_ein_ordner_je_organisation_und_vorhandener_ordner_bleibt_unberuehrt(org: Any, vereidigt: Any) -> None:
    alt = DocumentFolder.objects.create(organization=org, name=ORDNER, created_by=vereidigt)

    neu = DocumentFolder.non_public_folder(org, created_by=vereidigt)

    assert neu.pk != alt.pk and neu.sworn_in_only and neu.name == f"{ORDNER} (2)"
    alt.refresh_from_db()
    assert alt.sworn_in_only is False
    assert DocumentFolder.non_public_folder(org).pk == neu.pk


# =============================================================================
# Lesewege
# =============================================================================


@pytest.mark.django_db
def test_liste_ordner_und_suche_nur_fuer_vereidigte(
    org: Any, unterlage: Motion, vereidigt: Any, nicht_vereidigt: Any, admin_nicht_vereidigt: Any, client_for: Any
) -> None:
    liste = _url("documents", org)
    ordner = unterlage.folder
    assert ordner is not None

    for person in (nicht_vereidigt, admin_nicht_vereidigt):
        client = client_for(person.user)
        for antwort in (client.get(liste), client.get(liste, {"q": "Musterweg"})):
            text = antwort.content.decode()
            assert antwort.status_code == 200
            assert GEHEIM not in text and ORDNER not in text
        assert client.get(liste, {"ordner": str(ordner.id)}).status_code == 404

    client = client_for(vereidigt.user)
    text = client.get(liste).content.decode()
    assert GEHEIM in text and ORDNER in text
    assert GEHEIM in client.get(liste, {"q": "Musterweg"}).content.decode()
    assert client.get(liste, {"ordner": str(ordner.id)}).status_code == 200


@pytest.mark.django_db
def test_editor_download_export_und_versionen_nur_fuer_vereidigte(
    org: Any,
    unterlage: Motion,
    vereidigt: Any,
    nicht_vereidigt: Any,
    admin_nicht_vereidigt: Any,
    gast: Any,
    client_for: Any,
) -> None:
    anhang = MotionDocument.objects.get(motion=unterlage)
    urls = [
        _url("document_editor", org, motion_id=unterlage.id),
        _url("document_file_download", org, motion_id=unterlage.id, document_id=anhang.id),
        _url("document_export", org, motion_id=unterlage.id) + "?format=pdf",
        _url("document_revisions", org, motion_id=unterlage.id),
    ]
    for person in _nicht_berechtigt(nicht_vereidigt, admin_nicht_vereidigt, gast):
        client = client_for(person.user)
        for url in urls:
            antwort = client.get(url)
            assert antwort.status_code in (302, 403, 404), (person.user.email, url, antwort.status_code)
            assert GEHEIM.encode() not in b"".join(getattr(antwort, "streaming_content", [antwort.content]))
    assert not FactionAuditLog.objects.filter(action="internal_document_access").exists()

    client = client_for(vereidigt.user)
    editor = client.get(urls[0])
    assert editor.status_code == 200 and GEHEIM in editor.content.decode()
    download = client.get(urls[1])
    assert download.status_code == 200 and b"".join(download.streaming_content).startswith(b"%PDF")
    with mock.patch("apps.work.motions.export_service.motion_export_service.export_to_pdf", return_value=b"%PDF-1.4"):
        assert client.get(urls[2]).status_code == 200
    zugriffe = list(
        FactionAuditLog.objects.filter(action="internal_document_access", is_internal=True).values_list(
            "changes", flat=True
        )
    )
    assert sorted(eintrag["zugriff"] for eintrag in zugriffe) == sorted(
        [non_public.ACCESS_OPENED, non_public.ACCESS_DOWNLOADED, non_public.ACCESS_EXPORTED]
    )


@pytest.mark.django_db
def test_live_bearbeitung_kommentare_und_zugriffsstufen(
    org: Any,
    unterlage: Motion,
    vereidigt: Any,
    vereidigt2: Any,
    nicht_vereidigt: Any,
    admin_nicht_vereidigt: Any,
    gast: Any,
    client_for: Any,
) -> None:
    # Freigaben, Federführung und Mitarbeit aus dem Bestand wirken nicht (an save() vorbei angelegt)
    MotionShare.objects.bulk_create(
        [
            MotionShare(
                motion=unterlage, scope="user", user=nicht_vereidigt.user, level="edit", created_by=vereidigt.user
            ),
            MotionShare(motion=unterlage, scope="user", user=gast.user, level="edit", created_by=vereidigt.user),
        ]
    )
    unterlage.contributors.add(nicht_vereidigt)
    for person in _nicht_berechtigt(nicht_vereidigt, admin_nicht_vereidigt, gast):
        assert unterlage.access_level(person) == "none"
        assert unterlage.get_collab_access_level(person) is None
        assert not cast(Any, Motion).visible_to(person).filter(pk=unterlage.pk).exists()
        antwort = client_for(person.user).post(
            _url("document_comment", org, motion_id=unterlage.id), {"content": "Kommentar"}
        )
        assert antwort.status_code in (403, 404)
    assert not MotionComment.objects.filter(motion=unterlage).exists()

    assert unterlage.get_collab_access_level(vereidigt2) in ("view", "comment", "edit")
    assert cast(Any, Motion).visible_to(vereidigt2).filter(pk=unterlage.pk).exists()


@pytest.mark.django_db
def test_vereidigung_entzogen_sperrt_sofort(org: Any, unterlage: Motion, vereidigt2: Any, client_for: Any) -> None:
    url = _url("document_editor", org, motion_id=unterlage.id)
    assert client_for(vereidigt2.user).get(url).status_code == 200
    vereidigt2.is_sworn_in = False
    vereidigt2.save(update_fields=["is_sworn_in"])
    assert client_for(vereidigt2.user).get(url).status_code == 403


@pytest.mark.django_db
def test_dashboard_papierkorb_und_gastuebersicht(
    org: Any,
    vereidigt: Any,
    nicht_vereidigt: Any,
    gast: Any,
    client_for: Any,
) -> None:
    ordner = DocumentFolder.non_public_folder(org, created_by=vereidigt)
    entwurf = Motion.objects.create(
        organization=org, author=vereidigt, title=GEHEIM, visibility="organization", folder=ordner, status="draft"
    )
    geloescht = Motion.objects.create(
        organization=org, author=vereidigt, title=f"{GEHEIM} alt", visibility="organization", folder=ordner
    )
    geloescht.status = "deleted"
    geloescht.save(update_fields=["status"])

    for url in (_url("dashboard", org), _url("document_trash", org)):
        assert GEHEIM not in client_for(nicht_vereidigt.user).get(url).content.decode(), url
    assert GEHEIM in client_for(vereidigt.user).get(_url("dashboard", org)).content.decode()

    gast_client = client_for(gast.user)
    assert GEHEIM not in gast_client.get(_url("guest_documents", org)).content.decode()
    assert gast_client.get(_url("guest_documents", org), {"ordner": str(ordner.id)}).status_code == 404
    assert entwurf.pk


@pytest.mark.django_db
def test_verknuepfungen_zeigen_keine_unterlagen(org: Any, unterlage: Motion, vereidigt: Any, client_for: Any) -> None:
    from apps.work.meetings import selectors as meeting_selectors
    from apps.work.tasks import selectors as task_selectors

    eigenes = Motion.objects.create(organization=org, author=vereidigt, title="Eigener Antrag", visibility="private")
    suche = client_for(vereidigt.user).get(
        _url("document_reference_search", org, motion_id=eigenes.id), {"q": "Musterweg"}
    )
    assert GEHEIM not in suche.content.decode()
    assert task_selectors.find_motion(org, vereidigt, unterlage.id) is None
    assert unterlage not in meeting_selectors.linkable_documents(vereidigt, "Musterweg")
    assert meeting_selectors.find_linkable_document(org, vereidigt, unterlage.id) is None
    aufgabe = client_for(vereidigt.user).get(_url("task_create", org), {"related_motion": str(unterlage.id)})
    assert GEHEIM not in aufgabe.content.decode()


@pytest.mark.django_db
def test_aenderungshistorie_maskiert_fuer_nicht_vereidigte(
    org: Any, unterlage: Motion, vereidigt: Any, nicht_vereidigt: Any, client_for: Any
) -> None:
    client_for(vereidigt.user).get(_url("document_editor", org, motion_id=unterlage.id))
    url = _url("faction_audit", org)

    assert GEHEIM not in client_for(nicht_vereidigt.user).get(url).content.decode()
    assert GEHEIM in client_for(vereidigt.user).get(url).content.decode()


# =============================================================================
# Freigaben und Verschieben werden abgelehnt
# =============================================================================


@pytest.mark.django_db
def test_freigaben_werden_abgelehnt(
    org: Any, unterlage: Motion, vereidigt: Any, vereidigt2: Any, nicht_vereidigt: Any, gast: Any, client_for: Any
) -> None:
    client = client_for(vereidigt.user)
    assert not unterlage.can_share(vereidigt)

    teilen = client.post(
        _url("document_share_update", org, motion_id=unterlage.id),
        {"visibility": "shared", "add_user_email": gast.user.email, "level": "edit"},
    )
    ordner = client.post(
        _url("document_folder_share", org, folder_id=unterlage.folder_id), {"email": gast.user.email, "level": "view"}
    )

    assert teilen.status_code == 403
    assert ordner.status_code == 403
    assert not MotionShare.objects.filter(motion=unterlage).exists()
    assert not FolderGuestShare.objects.filter(folder_id=unterlage.folder_id).exists()
    unterlage.refresh_from_db()
    assert unterlage.visibility == "private"
    with pytest.raises(ValueError):
        MotionShare.objects.create(motion=unterlage, scope="user", user=gast.user, created_by=vereidigt.user)
    with pytest.raises(ValueError):
        FolderGuestShare.objects.create(folder=cast(Any, unterlage.folder), user=gast.user, created_by=vereidigt.user)


@pytest.mark.django_db
def test_gasteinladung_und_zuweisungen_oeffnen_nichts(
    org: Any, unterlage: Motion, vereidigt: Any, nicht_vereidigt: Any, client_for: Any
) -> None:
    from apps.work.organization import selectors as org_selectors

    assert unterlage not in org_selectors.shareable_documents(org, vereidigt)
    assert all(folder.id != unterlage.folder_id for folder, _depth in org_selectors.shareable_folders(org, vereidigt))

    client = client_for(vereidigt.user)
    meta = _url("document_meta", org, motion_id=unterlage.id)
    federfuehrung = client.post(meta, {"action": "set_responsible", "responsible": str(nicht_vereidigt.id)})
    mitarbeit = client.post(meta, {"action": "set_contributors", "contributors": [str(nicht_vereidigt.id)]})
    freigabe = client.post(
        _url("document_approval_request", org, motion_id=unterlage.id),
        {"approver": str(nicht_vereidigt.id), "approval_type": "chair"},
    )

    assert (federfuehrung.status_code, mitarbeit.status_code, freigabe.status_code) == (403, 403, 403)
    unterlage.refresh_from_db()
    assert unterlage.responsible_id == vereidigt.id
    assert not unterlage.contributors.exists()
    assert not MotionApproval.objects.filter(motion=unterlage).exists()


@pytest.mark.django_db
def test_ordner_laesst_sich_weder_aendern_loeschen_noch_unterteilen(
    org: Any, unterlage: Motion, vereidigt: Any, client_for: Any
) -> None:
    ordner = unterlage.folder
    assert ordner is not None
    anderer = DocumentFolder.objects.create(organization=org, name="Ablage", created_by=vereidigt)
    client = client_for(vereidigt.user)

    umbenennen = client.post(_url("document_folder_update", org, folder_id=ordner.id), {"name": "Offen", "parent": ""})
    verschieben = client.post(_url("document_folder_update", org, folder_id=ordner.id), {"parent": str(anderer.id)})
    loeschen = client.post(_url("document_folder_delete", org, folder_id=ordner.id))
    client.post(_url("document_folder_create", org), {"name": "Unterordner", "parent": str(ordner.id)})
    client.post(_url("document_folder_update", org, folder_id=anderer.id), {"parent": str(ordner.id)})

    assert (umbenennen.status_code, verschieben.status_code, loeschen.status_code) == (403, 403, 403)
    ordner.refresh_from_db()
    anderer.refresh_from_db()
    assert ordner.name == ORDNER and ordner.parent_id is None
    assert not DocumentFolder.objects.filter(parent=ordner).exists()
    assert anderer.parent_id is None
    with pytest.raises(ValueError):
        cast(Any, ordner).delete()


@pytest.mark.django_db
def test_dokumente_wandern_weder_heraus_noch_hinein(
    org: Any, unterlage: Motion, vereidigt: Any, client_for: Any
) -> None:
    anderer = DocumentFolder.objects.create(organization=org, name="Ablage", created_by=vereidigt)
    normal = Motion.objects.create(organization=org, author=vereidigt, title="Normal", visibility="organization")
    client = client_for(vereidigt.user)
    verschieben = _url("document_move_to_folder", org)

    client.post(verschieben, {"folder": "", "motion_ids": [str(unterlage.id)]})
    client.post(verschieben, {"folder": str(anderer.id), "motion_ids": [str(unterlage.id)]})
    client.post(verschieben, {"folder": str(unterlage.folder_id), "motion_ids": [str(normal.id)]})
    heraus = client.post(
        _url("document_meta", org, motion_id=unterlage.id), {"action": "set_folder", "folder": str(anderer.id)}
    )
    hinein = client.post(
        _url("document_meta", org, motion_id=normal.id), {"action": "set_folder", "folder": str(unterlage.folder_id)}
    )

    assert (heraus.status_code, hinein.status_code) == (403, 403)
    unterlage.refresh_from_db()
    normal.refresh_from_db()
    assert unterlage.is_sworn_in_only()
    assert normal.folder_id is None


@pytest.mark.django_db
def test_neues_dokument_im_ordner_nur_fuer_vereidigte(
    org: Any, vereidigt: Any, vereidigt2: Any, nicht_vereidigt: Any, client_for: Any
) -> None:
    ordner = DocumentFolder.non_public_folder(org, created_by=vereidigt)
    anlegen = _url("document_create", org)

    client_for(nicht_vereidigt.user).post(anlegen, {"title": "Versuch", "folder": str(ordner.id)})
    client_for(vereidigt.user).post(anlegen, {"title": GEHEIM, "folder": str(ordner.id)})

    assert not Motion.objects.filter(title="Versuch", folder=ordner).exists()
    neu = Motion.objects.get(title=GEHEIM)
    assert neu.folder_id == ordner.id and neu.visibility == "private"
    # Alle Vereidigten sehen es über den Ordner, niemand sonst
    assert neu.can_access(vereidigt2) and cast(Any, Motion).visible_to(vereidigt2).filter(pk=neu.pk).exists()
    assert not neu.can_access(nicht_vereidigt)


@pytest.mark.django_db
def test_einreichung_ausgeschlossen(unterlage: Motion, vereidigt: Any) -> None:
    from apps.work.motions import ris_submission

    erlaubt, grund = ris_submission.can_submit(unterlage, vereidigt)
    assert not erlaubt and "Nichtöffentliche Vorgänge" in grund


@pytest.mark.django_db
def test_keine_nachrichten_und_kein_export_nach_entzug(
    org: Any, unterlage: Motion, vereidigt: Any, vereidigt2: Any, client_for: Any
) -> None:
    from apps.work.notifications.services import NotificationHub
    from apps.work.organization.export_service import DsgvoExportService

    MotionComment.objects.create(motion=unterlage, author=vereidigt2, content="Erste Einschätzung")
    vereidigt2.is_sworn_in = False
    vereidigt2.save(update_fields=["is_sworn_in"])
    kommentar = MotionComment.objects.create(motion=unterlage, author=vereidigt, content="Antwort")

    hub = cast(Any, NotificationHub)
    hub.notify_motion_comment(kommentar, vereidigt)
    hub.notify_motion_assigned(unterlage, vereidigt2, vereidigt)

    assert not Notification.objects.filter(recipient=vereidigt2).exists()
    dienst = DsgvoExportService()
    beteiligt = dienst._collect_document_involvement(vereidigt2, org)
    assert all(GEHEIM not in eintrag["title"] for eintrag in beteiligt)


@pytest.mark.django_db
def test_fruehere_fassungen_in_der_aenderungshistorie(
    org: Any, unterlage: Motion, vereidigt: Any, nicht_vereidigt: Any, client_for: Any
) -> None:
    from apps.work.motions.models import MotionRevision

    fassung = MotionRevision(motion=unterlage, version=1, changed_by=vereidigt)
    cast(Any, fassung).set_content_encrypted(f"<p>{GEHEIM}</p>")
    fassung.save()
    url = _url("document_revision_detail", org, motion_id=unterlage.id, revision_id=fassung.id)

    assert client_for(nicht_vereidigt.user).get(url).status_code == 403
    assert not FactionAuditLog.objects.filter(action="internal_document_access").exists()
    antwort = client_for(vereidigt.user).get(url)
    assert antwort.status_code == 200 and GEHEIM in antwort.content.decode()
    eintrag = FactionAuditLog.objects.get(action="internal_document_access")
    assert eintrag.is_internal and eintrag.changes == {"zugriff": non_public.ACCESS_REVISION}
    assert eintrag.membership == vereidigt


def _datenexport(person: Any, org: Any) -> str:
    import json

    from apps.work.organization.export_service import DsgvoExportService

    return json.dumps(DsgvoExportService().collect_user_data(person.user, person, org), ensure_ascii=False, default=str)


def _spuren_anlegen(unterlage: Motion, vereidigt: Any, vereidigt2: Any, client_for: Any, org: Any) -> None:
    """Vermerke, Änderungshistorie und eine Nachricht der Person ``vereidigt`` zur Unterlage."""
    from django.utils import timezone

    from apps.work.motions.models import MotionChecklistItem, MotionRevision
    from apps.work.notifications.services import NotificationHub

    fassung = MotionRevision(motion=unterlage, version=1, changed_by=vereidigt, change_summary="Erste Fassung")
    cast(Any, fassung).set_content_encrypted("<p>Inhalt</p>")
    fassung.save()
    MotionChecklistItem.objects.create(
        motion=unterlage, title="Prüfen", is_completed=True, completed_by=vereidigt, completed_at=timezone.now()
    )
    MotionComment.objects.create(
        motion=unterlage, author=vereidigt2, content="Erledigt", resolved_by=vereidigt, resolved_at=timezone.now()
    )
    client_for(vereidigt.user).get(_url("document_editor", org, motion_id=unterlage.id))
    kommentar = MotionComment.objects.create(motion=unterlage, author=vereidigt2, content=f"Zu {GEHEIM}")
    cast(Any, NotificationHub).notify_motion_comment(kommentar, vereidigt2)
    assert Notification.objects.filter(recipient=vereidigt, metadata__motion_id=str(unterlage.id)).exists()


@pytest.mark.django_db
def test_datenexport_nach_entzug_ohne_titel_und_auszuege(
    org: Any, unterlage: Motion, vereidigt: Any, vereidigt2: Any, client_for: Any
) -> None:
    _spuren_anlegen(unterlage, vereidigt, vereidigt2, client_for, org)
    # Solange die Person vereidigt ist, enthält ihr Export die Unterlage
    assert GEHEIM in _datenexport(vereidigt, org)

    vereidigt.is_sworn_in = False
    vereidigt.save(update_fields=["is_sworn_in"])
    export = _datenexport(vereidigt, org)

    assert GEHEIM not in export
    # Art und Zeitpunkt bleiben (Auskunft), Titel und Auszüge nicht
    from apps.work.organization.export_service import NON_PUBLIC_CONTENT, NON_PUBLIC_DOCUMENT, DsgvoExportService

    vermerke = DsgvoExportService()._collect_vermerke(vereidigt, org)
    dokumente = [v for v in vermerke if v["area"] == "Dokumente"]
    assert {v["action"] for v in dokumente} >= {
        "Version gespeichert",
        "Anhang hochgeladen",
        "Checklistenpunkt erledigt",
        "Kommentar als erledigt markiert",
    }
    assert all(v["object"] == NON_PUBLIC_DOCUMENT for v in dokumente if v["action"] != "Ordner angelegt")
    historie = [v for v in vermerke if v["action"] == "Änderung protokolliert"]
    assert historie and all(NON_PUBLIC_CONTENT in v["object"] for v in historie)
    nachrichten = DsgvoExportService()._collect_notifications(vereidigt)["notifications"]
    assert nachrichten and all(n["message"] == NON_PUBLIC_DOCUMENT for n in nachrichten)


@pytest.mark.django_db
def test_benachrichtigungen_nach_entzug_ausgeblendet(
    org: Any, unterlage: Motion, vereidigt: Any, vereidigt2: Any, client_for: Any
) -> None:
    from django.core.cache import cache

    from apps.work.notifications.services import NotificationHub

    kommentar = MotionComment.objects.create(motion=unterlage, author=vereidigt2, content=f"Zu {GEHEIM}")
    cast(Any, NotificationHub).notify_motion_comment(kommentar, vereidigt2)
    urls = [_url("notifications", org), _url("notifications_partial", org), _url("notification_latest", org)]
    client = client_for(vereidigt.user)
    assert all(GEHEIM in client.get(url).content.decode() for url in urls)
    assert client.get(_url("notification_count", org)).json()["count"] == 1

    vereidigt.is_sworn_in = False
    vereidigt.save(update_fields=["is_sworn_in"])
    cache.clear()

    for url in urls:
        assert GEHEIM not in client.get(url).content.decode(), url
    assert client.get(_url("notification_count", org)).json()["count"] == 0
    # Gespeichert bleibt die Nachricht (kein Datenverlust); nach erneuter Vereidigung erscheint sie wieder
    assert Notification.objects.filter(recipient=vereidigt).count() == 1
    vereidigt.is_sworn_in = True
    vereidigt.save(update_fields=["is_sworn_in"])
    assert GEHEIM in client.get(urls[0]).content.decode()
