# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anhänge an Dokumenten (Issue #584): hochladen, umbenennen, entfernen – und bei der Einreichung
an die Verwaltung (mandari Session) übergeben.

Früher entstanden Anhänge nur beim Import; die Seitenleiste zeigte sie nur zum Herunterladen,
und bei der Einreichung blieben sie in Work zurück.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.common.tests.factories import UserFactory
from apps.session.models import SessionAPIToken, SessionFile, SessionRole, SessionTenant, SessionUser
from apps.session.services import file_service
from apps.work.motions import ris_submission
from apps.work.motions.models import Motion, MotionDocument

pytestmark = pytest.mark.django_db

EDITOR = ["dashboard.view", "motions.view", "motions.edit", "motions.submit_to_ris", "motions.approve"]
INHALT = "<h2>Beschlussvorschlag</h2><p>Die Verwaltung plant einen Radweg.</p><h2>Begründung</h2><p>Sicherheit.</p>"
HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture(autouse=True)
def ablage(tmp_path: Path, settings: Any) -> Path:
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, EDITOR, email="autorin@example.org")


@pytest.fixture
def antrag(org: Any, autorin: Any) -> Motion:
    motion = Motion.objects.create(
        organization=org, author=autorin, title="Radweg Hauptstraße", visibility="organization", status="approved"
    )
    cast(Any, motion).set_content_encrypted(INHALT)
    motion.save()
    return motion


def _url(org: Any, motion: Motion, suffix: str) -> str:
    return f"/work/{org.slug}/documents/{motion.id}/{suffix}"


def _datei(name: str, inhalt: bytes = b"%PDF-1.4 Lageplan") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, inhalt, content_type="application/x-irrelevant")


# =============================================================================
# Hochladen, umbenennen, entfernen
# =============================================================================


def test_mehrere_dateien_hochladen_zufallsname_und_typ_vom_server(
    org: Any, antrag: Motion, autorin: Any, client_for: Any, ablage: Path
) -> None:
    antwort = client_for(autorin.user).post(
        _url(org, antrag, "upload/"),
        {"file": [_datei("Lageplan Hauptstraße.pdf"), _datei("Kosten.xlsx", b"PK tabelle")]},
        **HTMX,
    )
    assert antwort.status_code == 200
    html = antwort.content.decode()
    assert 'id="motion-attachments"' in html and "2 Anhänge hinzugefügt." in html
    assert "Lageplan Hauptstraße.pdf" in html and "Kosten.xlsx" in html

    lageplan = MotionDocument.objects.get(motion=antrag, filename="Lageplan Hauptstraße.pdf")
    assert lageplan.mime_type == "application/pdf"  # nicht die Angabe des Browsers
    assert lageplan.uploaded_by == autorin and lageplan.file_size == len(b"%PDF-1.4 Lageplan")
    speichername = str(lageplan.file.name)
    gespeichert = Path(speichername)
    assert speichername.startswith("motions/documents/")
    assert "Lageplan" not in gespeichert.name and gespeichert.suffix == ".pdf"
    assert (ablage / speichername).read_bytes() == b"%PDF-1.4 Lageplan"


def test_download_mit_originalnamen(org: Any, antrag: Motion, autorin: Any, client_for: Any) -> None:
    client = client_for(autorin.user)
    client.post(_url(org, antrag, "upload/"), {"file": [_datei("Lageplan.pdf")]}, **HTMX)
    anhang = MotionDocument.objects.get(motion=antrag)
    antwort = client.get(_url(org, antrag, f"files/{anhang.id}/download/"))
    assert antwort.status_code == 200
    assert 'filename="Lageplan.pdf"' in antwort["Content-Disposition"]
    assert b"".join(cast(Any, antwort).streaming_content) == b"%PDF-1.4 Lageplan"


def test_unzulaessige_datei_wird_mit_grund_abgelehnt(org: Any, antrag: Motion, autorin: Any, client_for: Any) -> None:
    antwort = client_for(autorin.user).post(
        _url(org, antrag, "upload/"), {"file": [_datei("seite.html", b"<script>")]}, **HTMX
    )
    html = antwort.content.decode()
    assert "seite.html" in html and "nicht erlaubt" in html
    assert not MotionDocument.objects.filter(motion=antrag).exists()


def test_umbenennen_behaelt_die_endung(org: Any, antrag: Motion, autorin: Any, client_for: Any) -> None:
    client = client_for(autorin.user)
    client.post(_url(org, antrag, "upload/"), {"file": [_datei("scan_0001.pdf")]}, **HTMX)
    anhang = MotionDocument.objects.get(motion=antrag)

    antwort = client.post(_url(org, antrag, f"files/{anhang.id}/rename/"), {"filename": "Lageplan Nord"}, **HTMX)
    assert "Anhang umbenannt." in antwort.content.decode()
    anhang.refresh_from_db()
    assert anhang.filename == "Lageplan Nord.pdf"

    leer = client.post(_url(org, antrag, f"files/{anhang.id}/rename/"), {"filename": "  "}, **HTMX)
    assert "Bitte einen Namen angeben." in leer.content.decode()
    anhang.refresh_from_db()
    assert anhang.filename == "Lageplan Nord.pdf"


def test_entfernen_loescht_eintrag_und_datei(
    org: Any, antrag: Motion, autorin: Any, client_for: Any, ablage: Path, django_capture_on_commit_callbacks: Any
) -> None:
    client = client_for(autorin.user)
    client.post(_url(org, antrag, "upload/"), {"file": [_datei("Lageplan.pdf")]}, **HTMX)
    anhang = MotionDocument.objects.get(motion=antrag)
    pfad = ablage / str(anhang.file.name)
    assert pfad.exists()

    with django_capture_on_commit_callbacks(execute=True):
        antwort = client.post(_url(org, antrag, f"files/{anhang.id}/delete/"), **HTMX)
    assert "Anhang entfernt." in antwort.content.decode()
    assert not MotionDocument.objects.filter(pk=anhang.pk).exists()
    assert not pfad.exists()


def test_status_sperre_betrifft_anhaenge_nicht(org: Any, antrag: Motion, autorin: Any, client_for: Any) -> None:
    antrag.status = "submitted"
    antrag.save(update_fields=["status"])
    antwort = client_for(autorin.user).post(_url(org, antrag, "upload/"), {"file": [_datei("Nachtrag.pdf")]}, **HTMX)
    assert antwort.status_code == 200
    assert MotionDocument.objects.filter(motion=antrag).count() == 1


def test_lesende_duerfen_nicht_hochladen_umbenennen_entfernen(
    org: Any, antrag: Motion, autorin: Any, make_member: Any, client_for: Any
) -> None:
    anhang = MotionDocument.objects.create(motion=antrag, file=_datei("Plan.pdf"), filename="Plan.pdf", file_size=1)
    lesend = client_for(make_member(org, ["dashboard.view", "motions.view"]).user)
    for suffix, daten in (
        ("upload/", {"file": [_datei("x.pdf")]}),
        (f"files/{anhang.id}/rename/", {"filename": "Neu"}),
        (f"files/{anhang.id}/delete/", {}),
    ):
        antwort = lesend.post(_url(org, antrag, suffix), daten, **HTMX)
        assert antwort.status_code in (302, 403, 404), suffix
    anhang.refresh_from_db()
    assert anhang.filename == "Plan.pdf"
    assert MotionDocument.objects.filter(motion=antrag).count() == 1


def test_editor_zeigt_hochladen_nur_mit_bearbeitungsrecht(
    org: Any, antrag: Motion, autorin: Any, make_member: Any, client_for: Any
) -> None:
    seite = client_for(autorin.user).get(f"/work/{org.slug}/documents/{antrag.id}/").content.decode()
    assert "Anhang hinzufügen" in seite and f"/documents/{antrag.id}/upload/" in seite

    leserin = make_member(org, ["dashboard.view", "motions.view"])
    seite = client_for(leserin.user).get(f"/work/{org.slug}/documents/{antrag.id}/").content.decode()
    assert "Anhang hinzufügen" not in seite


# =============================================================================
# Einreichung an mandari Session
# =============================================================================


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")


@pytest.fixture
def verbunden(org: Any, autorin: Any, tenant: SessionTenant) -> Any:
    _token, raw = SessionAPIToken.create_token(tenant, "Fraktion Test", can_submit_applications=True)
    return ris_submission.connect_with_token(org, raw, autorin)


@pytest.fixture
def commit(django_capture_on_commit_callbacks: Any) -> Iterator[Callable[..., Any]]:
    def run() -> Any:
        return django_capture_on_commit_callbacks(execute=True)

    yield run


def _verwaltung(tenant: SessionTenant, **rechte: bool) -> Client:
    rolle = SessionRole.objects.create(tenant=tenant, name=f"Rolle {SessionRole.objects.count()}", **rechte)
    user = cast(Any, UserFactory)()
    SessionUser.objects.create(user=user, tenant=tenant).roles.add(rolle)
    client = Client()
    client.force_login(user)
    return client


def test_anhaenge_gehen_bei_der_einreichung_mit(
    org: Any, antrag: Motion, autorin: Any, verbunden: Any, tenant: SessionTenant, client_for: Any, commit: Any
) -> None:
    fraktion = client_for(autorin.user)
    fraktion.post(
        _url(org, antrag, "upload/"),
        {"file": [_datei("Lageplan.pdf"), _datei("Foto.webp", b"RIFF bild")]},
        **HTMX,
    )

    vorschau = fraktion.get(_url(org, antrag, "submit-ris/")).content.decode()
    assert "Lageplan.pdf" in vorschau
    assert "Foto.webp" in vorschau and "wird nicht übermittelt" in vorschau

    with commit():
        antwort = fraktion.post(
            _url(org, antrag, "submit-ris/"),
            {**ris_submission.build_prefill(antrag), "application_type": "motion", "confirm": "on"},
        )
    assert antwort.status_code == 302
    antrag.refresh_from_db()
    application = antrag.session_application
    assert application is not None

    (datei,) = SessionFile.objects.filter(application=application)
    assert datei.name == "Lageplan.pdf" and datei.is_public is False and datei.tenant == tenant
    assert datei.mime_type == "application/pdf" and datei.versions.count() == 1
    with datei.file.open("rb") as handle:
        assert handle.read() == b"%PDF-1.4 Lageplan"

    # Verwaltung mit Antragsrecht sieht und lädt den Anhang, ohne Antragsrecht nicht
    verwaltung = _verwaltung(tenant, can_view_applications=True)
    detail = verwaltung.get(f"/session/{tenant.slug}/applications/{application.id}/").content.decode()
    assert "Lageplan.pdf" in detail
    download = verwaltung.get(f"/session/{tenant.slug}/files/{datei.id}/download/")
    assert download.status_code == 200
    ohne = _verwaltung(tenant, can_view_applications=False, can_view_papers=True, can_view_non_public_papers=True)
    assert ohne.get(f"/session/{tenant.slug}/files/{datei.id}/download/").status_code == 403


def test_umwandlung_haengt_anhaenge_nichtoeffentlich_an_die_vorlage(
    org: Any, antrag: Motion, autorin: Any, verbunden: Any, tenant: SessionTenant, commit: Any
) -> None:
    from apps.session.services.application_service import convert_to_paper

    MotionDocument.objects.create(
        motion=antrag, file=_datei("Plan.pdf"), filename="Plan.pdf", mime_type="application/pdf", file_size=17
    )
    with commit():
        application = ris_submission.submit_motion(antrag, autorin, ris_submission.build_prefill(antrag))
        paper, _neu = convert_to_paper(application)

    (datei,) = SessionFile.objects.filter(application=application)
    assert datei.paper == paper and datei.is_public is False
    assert list(paper.files.all()) == [datei]
    # Öffentlich nur mit NÖ-Recht für Vorlagen, wie jede nichtöffentliche Anlage
    assert not file_service.file_visible({"view_papers"}, datei)
    assert file_service.file_visible({"view_papers", "view_non_public_papers"}, datei)


def test_antrags_anhang_laesst_sich_vor_der_umwandlung_nicht_veroeffentlichen(
    org: Any, antrag: Motion, autorin: Any, verbunden: Any, tenant: SessionTenant, commit: Any
) -> None:
    MotionDocument.objects.create(motion=antrag, file=_datei("Plan.pdf"), filename="Plan.pdf", file_size=17)
    with commit():
        application = ris_submission.submit_motion(antrag, autorin, ris_submission.build_prefill(antrag))
    datei = SessionFile.objects.get(application=application)

    verwaltung = _verwaltung(tenant, can_view_applications=True, can_process_applications=True, can_edit_meetings=True)
    verwaltung.post(f"/session/{tenant.slug}/files/{datei.id}/update/", {"name": "Plan.pdf", "is_public": "on"})
    datei.refresh_from_db()
    assert datei.is_public is False
    assert file_service.is_non_public(datei)
    assert not file_service.file_visible({"view_papers", "view_meetings"}, datei)
