# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Selbst hochgeladene Anhänge öffnen im Browser wie die Dokumente aus dem Ratsinformationssystem.

Bisher kam jeder Anhang an einem Dokument und an einer Aufgabe als Download; nur Sitzungsunterlagen hatten die Vorschau
(``?vorschau``). Jetzt gilt für alle: Mit ``?vorschau`` gehen PDF und Bilder eingebettet hinaus, alles andere bleibt ein
Download (HTML, Office-Dateien), damit nichts Ausführbares im Ursprung der Anwendung angezeigt wird. Die Links in Work
öffnen die Vorschau in einem neuen Tab.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse

from apps.work.motions.models import Motion, MotionDocument
from apps.work.tasks.models import Task, TaskAttachment

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.4 Lageplan"


@pytest.fixture(autouse=True)
def ablage(tmp_path: Path, settings: Any) -> Path:
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view", "motions.view", "motions.edit", "tasks.view"], email="a@example.org")


def _dokument_url(org: Any, motion: Motion, anhang: MotionDocument) -> str:
    return reverse(
        "work:document_file_download", kwargs={"org_slug": org.slug, "motion_id": motion.id, "document_id": anhang.id}
    )


@pytest.fixture
def dokument(org: Any, autorin: Any) -> tuple[Motion, MotionDocument, MotionDocument]:
    motion = Motion.objects.create(organization=org, author=autorin, title="Radweg", visibility="organization")
    pdf = MotionDocument.objects.create(
        motion=motion, file=ContentFile(PDF, name="Plan.pdf"), filename="Plan.pdf", file_size=len(PDF)
    )
    html = MotionDocument.objects.create(
        motion=motion, file=ContentFile(b"<script>x</script>", name="seite.html"), filename="seite.html", file_size=18
    )
    return motion, pdf, html


def test_dokument_anhang_mit_vorschau_im_browser(
    org: Any, autorin: Any, dokument: tuple[Motion, MotionDocument, MotionDocument], client_for: Any
) -> None:
    motion, pdf, html = dokument
    client = client_for(autorin.user)
    url = _dokument_url(org, motion, pdf)

    vorschau = client.get(f"{url}?vorschau")
    assert vorschau.status_code == 200
    assert vorschau["Content-Disposition"].startswith("inline;")
    assert vorschau["Content-Type"] == "application/pdf"
    assert vorschau["X-Content-Type-Options"] == "nosniff"
    assert b"".join(vorschau.streaming_content) == PDF

    assert client.get(url)["Content-Disposition"].startswith("attachment;")
    # HTML kommt auch mit Vorschau nur als Download und nie als text/html
    gefaehrlich = client.get(f"{_dokument_url(org, motion, html)}?vorschau")
    assert gefaehrlich["Content-Disposition"].startswith("attachment;")
    assert gefaehrlich["Content-Type"] == "application/octet-stream"


def test_dokument_verlinkt_die_vorschau_in_neuem_tab(
    org: Any, autorin: Any, dokument: tuple[Motion, MotionDocument, MotionDocument], client_for: Any
) -> None:
    motion, pdf, _ = dokument
    seite = client_for(autorin.user).get(f"/work/{org.slug}/documents/{motion.id}/").content.decode()
    assert f'{_dokument_url(org, motion, pdf)}?vorschau" target="_blank" rel="noopener"' in seite


def test_aufgaben_anhang_mit_vorschau_im_browser(org: Any, autorin: Any, client_for: Any) -> None:
    task = Task.objects.create(organization=org, title="Aufgabe", created_by=autorin)
    anhang = TaskAttachment.objects.create(
        task=task,
        file=ContentFile(PDF, name="Plan.pdf"),
        filename="Plan.pdf",
        mime_type="application/pdf",
        uploaded_by=autorin,
    )
    url = reverse(
        "work:task_attachment_download",
        kwargs={"org_slug": org.slug, "task_id": task.id, "attachment_id": anhang.id},
    )
    client = client_for(autorin.user)
    assert client.get(f"{url}?vorschau")["Content-Disposition"].startswith("inline;")
    assert client.get(url)["Content-Disposition"].startswith("attachment;")


def test_fehlende_datei_ergibt_404(org: Any, autorin: Any, client_for: Any) -> None:
    motion = Motion.objects.create(organization=org, author=autorin, title="Radweg", visibility="organization")
    anhang = MotionDocument.objects.create(motion=motion, file="motions/documents/fehlt.pdf", filename="fehlt.pdf")
    antwort = client_for(autorin.user).get(f"{_dokument_url(org, motion, anhang)}?vorschau")
    assert antwort.status_code == 404
