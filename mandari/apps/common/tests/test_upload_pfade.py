# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Upload-Pfade des Arbeitsbereichs (Issue #260): Je Pfad wird ein erlaubter Typ
angenommen und ein nicht erlaubter abgelehnt – über die echte Adresse, nicht
über den Validator allein. Die Session-Pfade und die Sitzungsvorbereitung haben
eigene Tests in ihren Apps.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.work.motions.models import Motion, MotionDocument, OrganizationLetterhead

pytestmark = pytest.mark.django_db

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def datei(name: str, inhalt: bytes = b"%PDF-1.4 " + b"x" * 64) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, inhalt)


def _seitentext(response: Any) -> str:
    return str(response.content.decode("utf-8", "replace"))


# ---------------------------------------------------------------- Briefkopf (PDF, 10 MB)


def test_briefkopf_nimmt_pdf_und_lehnt_html_ab(org: Any, make_member: Any, client_for: Any) -> None:
    member = make_member(org, ["organization.edit"], email="orga@example.org")
    client = client_for(member.user)
    url = f"/work/{org.slug}/organization/documents/letterheads/create/"

    abgelehnt = client.post(url, {"name": "Böse", "kind": "pdf", "pdf_file": datei("kopf.html", b"<script>")})
    assert abgelehnt.status_code == 200
    assert "erlaubt" in _seitentext(abgelehnt)
    assert not OrganizationLetterhead.objects.filter(organization=org, name="Böse").exists()

    angenommen = client.post(url, {"name": "Standard", "kind": "pdf", "pdf_file": datei("kopf.pdf")}, follow=True)
    assert angenommen.status_code == 200
    assert OrganizationLetterhead.objects.filter(organization=org, name="Standard").exists()


# ---------------------------------------------------------------- Profilbild (IMAGES, 5 MB)


def test_profilbild_nimmt_png_und_lehnt_svg_ab(org: Any, make_member: Any, client_for: Any) -> None:
    member = make_member(org, ["dashboard.view"], email="person@example.org")
    client = client_for(member.user)
    url = f"/work/{org.slug}/profile/"
    felder = {"action": "update_profile", "first_name": "Kim", "last_name": "Muster", "phone": ""}

    client.post(url, {**felder, "avatar": datei("bild.svg", b"<svg onload=alert(1)>")}, follow=True)
    member.user.refresh_from_db()
    assert not member.user.avatar

    client.post(url, {**felder, "avatar": datei("bild.png", PNG)}, follow=True)
    member.user.refresh_from_db()
    assert member.user.avatar


# ---------------------------------------------------------------- Logo (IMAGES, 5 MB)


def test_logo_nimmt_png_und_lehnt_svg_ab(org: Any, make_member: Any, client_for: Any) -> None:
    member = make_member(org, ["organization.edit", "dashboard.view"], email="orga2@example.org", is_admin=True)
    client = client_for(member.user)
    url = f"/work/{org.slug}/organization/"
    felder = {"action": "update_general", "name": org.name, "description": "", "primary_color": ""}

    client.post(url, {**felder, "logo": datei("logo.svg", b"<svg/>")}, follow=True)
    org.refresh_from_db()
    assert not org.logo

    client.post(url, {**felder, "logo": datei("logo.png", PNG)}, follow=True)
    org.refresh_from_db()
    assert org.logo


# ---------------------------------------------------------------- Aufgaben-Import (DATA, 5 MB)


def test_aufgaben_import_lehnt_ausfuehrbare_datei_ab(org: Any, make_member: Any, client_for: Any) -> None:
    member = make_member(org, ["tasks.create", "tasks.view"], email="aufgaben@example.org")
    client = client_for(member.user)
    url = f"/work/{org.slug}/tasks/import-file/"

    abgelehnt = client.post(url, {"file": datei("aufgaben.exe", b"MZ")})
    assert abgelehnt.status_code == 400
    assert "erlaubt" in abgelehnt.json()["error"]

    csv = client.post(url, {"file": datei("aufgaben.csv", b"title\nErste Aufgabe\n")})
    assert "erlaubt" not in (csv.json().get("error", "") if csv.status_code == 400 else "")


# ---------------------------------------------------------------- Antrags-Import (PDF/DOCX, 25 MB)


def test_antrags_import_ueberspringt_nicht_erlaubte_dateien(org: Any, make_member: Any, client_for: Any) -> None:
    member = make_member(org, ["motions.create", "motions.view"], email="import@example.org")
    client = client_for(member.user)
    url = f"/work/{org.slug}/documents/import/"

    response = client.post(url, {"import_files": [datei("antrag.exe", b"MZ")]}, follow=True)
    text = _seitentext(response)
    assert "übersprungen" in text or "Keine gültigen Dateien" in text
    assert not Motion.objects.filter(organization=org, title__icontains="antrag.exe").exists()


# ---------------------------------------------------------------- Antragsdokument (DOCUMENTS, 50 MB)


def test_antragsdokument_nimmt_pdf_und_lehnt_skript_ab(org: Any, make_member: Any, client_for: Any) -> None:
    member = make_member(org, ["motions.view", "motions.edit", "motions.create"], email="autor@example.org")
    motion = Motion.objects.create(organization=org, author=member, title="Antrag", visibility="organization")
    cast(Any, motion).set_content_encrypted("<p>Text</p>")
    motion.save()
    client = client_for(member.user)
    url = f"/work/{org.slug}/documents/{motion.id}/upload/"

    abgelehnt = client.post(url, {"file": datei("anlage.js", b"alert(1)")}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert abgelehnt.status_code in (200, 400)
    assert not MotionDocument.objects.filter(motion=motion).exists()

    angenommen = client.post(url, {"file": datei("anlage.pdf")}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert angenommen.status_code == 200
    assert MotionDocument.objects.filter(motion=motion).count() == 1
