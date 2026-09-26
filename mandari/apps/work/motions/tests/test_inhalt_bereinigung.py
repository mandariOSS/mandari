# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokumentinhalt wird serverseitig auf die Positivliste des Editors reduziert.

Gespeichert wird nur bereinigtes HTML (Editor per POST, Live-Kollaboration, Vorlagen), und jede
Ausgabe als HTML bereinigt erneut – auch Altbestand, der noch ungefiltert gespeichert wurde
(Lesansicht, Versionshistorie, Einreichungsvorschau, Vorlagenvorschau, Datenauskunft).
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, cast

import pytest
from channels.testing import WebsocketCommunicator
from django.urls import reverse

from apps.common.tests.factories import PermissionFactory
from apps.work.motions.consumers import DocumentCollaborationConsumer
from apps.work.motions.models import Motion, MotionRevision, MotionTemplate
from apps.work.sanitize import sanitize_editor_html

BOESE = (
    '<p>Hallo</p><img src="x" onerror="alert(1)"><script>alert(2)</script>'
    '<a href="javascript:alert(3)">Link</a><iframe src="https://evil.example"></iframe>'
    '<p style="background-image: url(javascript:alert(4))" onclick="alert(5)">Text</p>'
)
MERKMALE = ("onerror", "alert(2)", "javascript:", "<iframe", "onclick", "url(")

EDITOR_HTML = (
    '<h1 style="text-align: center">Titel</h1>'
    '<p style="margin-left: 40px">Eingerückt <strong>fett</strong> <em>kursiv</em> <u>unter</u> <s>durch</s> '
    "<code>code</code></p>"
    '<p><a target="_blank" rel="noopener noreferrer nofollow" class="text-primary-600 underline '
    'hover:text-primary-700" href="https://example.org/a?b=1&amp;c=2">Link</a> '
    '<mark data-color="#ffc078" style="background-color: #ffc078; color: inherit">markiert</mark> '
    '<span style="color: #958DF1">farbig</span> '
    '<span data-comment-id="c-1" class="comment-mark">kommentiert</span></p>'
    '<ul data-type="taskList"><li data-checked="true" data-type="taskItem"><label contenteditable="false">'
    '<input type="checkbox" checked="checked"><span></span></label><div><p>Erledigt</p></div></li></ul>'
    '<table class="editor-table" style="min-width: 50px"><colgroup><col style="min-width: 25px">'
    '<col style="width: 120px"></colgroup><tbody><tr><th colspan="1" rowspan="1"><p>Kopf</p></th>'
    '<td colspan="1" rowspan="1" colwidth="120"><p>Zelle</p></td></tr></tbody></table>'
    '<img src="data:image/png;base64,iVBORw0KGgo=" class="editor-image">'
    '<div data-page-break="true" class="page-break"></div>'
    '<ol start="3"><li><p>Punkt</p></li></ol><blockquote><p>Zitat</p></blockquote>'
    '<pre><code class="language-python">x = 1 &lt; 2</code></pre><hr><p>Umbruch<br>Zeile &amp; mehr</p>'
)

PERMISSIONS = ["motions.view", "motions.edit", "motions.comment"]


def _frei_von_skript(html: str) -> None:
    for merkmal in MERKMALE:
        assert merkmal not in html, merkmal


def _recht_geben(membership: Any, code: str) -> None:
    """Der Rolle eines Test-Mitglieds (make_member) ein weiteres Recht geben."""
    membership.roles.get().permissions.add(PermissionFactory(codename=code))  # type: ignore[no-untyped-call]


# ---------------------------------------------------------------------------
# Positivliste
# ---------------------------------------------------------------------------


def test_editor_markup_bleibt_unveraendert() -> None:
    assert sanitize_editor_html(EDITOR_HTML) == EDITOR_HTML
    assert sanitize_editor_html(sanitize_editor_html(BOESE)) == sanitize_editor_html(BOESE)


@pytest.mark.parametrize(
    "eingabe",
    [
        '<img src=x onerror="alert(1)">',
        "<svg><script>alert(1)</script></svg>",
        '<a href="  jav&#x09;ascript:alert(1)">x</a>',
        '<a href="data:text/html,<script>alert(1)</script>">x</a>',
        '<img src="data:image/svg+xml;base64,PHN2Zz4=">',
        "<style>body{display:none}</style>",
        '<p style="position: fixed; top: 0">x</p>',
        "</template><img src=x onerror=alert(1)>",
        '<p class="fixed inset-0 z-50">x</p>',
        '<input type="text" autofocus onfocus="alert(1)">',
    ],
)
def test_gefaehrliches_markup_faellt_weg(eingabe: str) -> None:
    ergebnis = sanitize_editor_html(eingabe)

    for merkmal in ("onerror", "<script", "javascript", "data:text", "svg", "<style", "position", "fixed", "onfocus"):
        assert merkmal not in ergebnis, (eingabe, ergebnis)
    assert "<template" not in ergebnis and "</template" not in ergebnis


# ---------------------------------------------------------------------------
# Speichern
# ---------------------------------------------------------------------------


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, PERMISSIONS, email="autorin@example.org")


@pytest.fixture
def leser(org: Any, make_member: Any) -> Any:
    return make_member(org, ["motions.view"], email="leser@example.org")


@pytest.fixture
def motion(org: Any, autorin: Any) -> Motion:
    return Motion.objects.create(organization=org, author=autorin, title="Antrag", visibility="organization")


def _gespeichert(motion: Motion) -> str:
    motion.refresh_from_db()
    return str(cast(Any, motion).get_content_decrypted())


@pytest.mark.django_db
def test_speichern_im_editor_bereinigt_den_inhalt(org: Any, autorin: Any, motion: Motion, client_for: Any) -> None:
    url = reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": motion.id})

    response = client_for(autorin.user).post(
        url, {"action": "save", "content": BOESE}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
    )

    assert response.status_code == 200
    gespeichert = _gespeichert(motion)
    _frei_von_skript(gespeichert)
    assert "<p>Hallo</p>" in gespeichert
    assert response.json()["content_hash"]


@pytest.mark.django_db(transaction=True)
def test_kollaboration_speichert_nur_bereinigtes_html(autorin: Any, motion: Motion) -> None:
    async def lauf() -> None:
        communicator = WebsocketCommunicator(DocumentCollaborationConsumer.as_asgi(), f"/ws/documents/{motion.id}/")
        communicator.scope["user"] = autorin.user
        communicator.scope["url_route"] = {"kwargs": {"document_id": str(motion.id)}}
        connected, _ = await communicator.connect()
        assert connected
        await communicator.receive_json_from()  # connected
        await communicator.receive_json_from()  # yjs_state
        await communicator.send_json_to(
            {"type": "yjs_save", "data": base64.b64encode(b"zustand").decode("ascii"), "html": BOESE}
        )
        assert (await communicator.receive_json_from())["type"] == "yjs_saved"
        await communicator.disconnect()

    asyncio.run(lauf())
    _frei_von_skript(_gespeichert(motion))


# ---------------------------------------------------------------------------
# Ausgabe von Altbestand
# ---------------------------------------------------------------------------


@pytest.fixture
def altbestand(motion: Motion, autorin: Any) -> Motion:
    """Inhalt, der vor der Bereinigung ungefiltert gespeichert wurde."""
    cast(Any, motion).set_content_encrypted(BOESE)
    motion.save()
    return motion


@pytest.mark.django_db
def test_lesansicht_gibt_altbestand_bereinigt_aus(org: Any, leser: Any, altbestand: Motion, client_for: Any) -> None:
    url = reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": altbestand.id})

    response = client_for(leser.user).get(url)

    assert response.status_code == 200
    html = response.content.decode()
    _frei_von_skript(html.split('class="content-readonly"', 1)[1][:800])
    assert "<p>Hallo</p>" in html


@pytest.mark.django_db
def test_editor_startinhalt_ist_bereinigt(org: Any, autorin: Any, altbestand: Motion, client_for: Any) -> None:
    url = reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": altbestand.id})

    html = client_for(autorin.user).get(url).content.decode()

    start = html.split('id="editor-initial-content">', 1)[1].split("</template>", 1)[0]
    _frei_von_skript(start)


@pytest.mark.django_db
def test_versionshistorie_liefert_bereinigten_inhalt(
    org: Any, autorin: Any, leser: Any, motion: Motion, client_for: Any
) -> None:
    revision = MotionRevision(motion=motion, version=1, changed_by=autorin)
    cast(Any, revision).set_content_encrypted(BOESE)
    revision.save()
    url = reverse(
        "work:document_revision_detail",
        kwargs={"org_slug": org.slug, "motion_id": motion.id, "revision_id": revision.id},
    )

    response = client_for(leser.user).get(url)

    assert response.status_code == 200
    _frei_von_skript(response.json()["revision"]["content"])


@pytest.mark.django_db
def test_einreichungsvorschau_ist_bereinigt(org: Any, autorin: Any, altbestand: Motion, client_for: Any) -> None:
    _recht_geben(autorin, "motions.submit_to_ris")
    url = reverse("work:document_submit_ris", kwargs={"org_slug": org.slug, "motion_id": altbestand.id})

    response = client_for(autorin.user).get(url)

    assert response.status_code == 200
    vorschau = response.content.decode().split("Vorschau des Dokuments", 1)[1][:1500]
    assert "<p>Hallo</p>" in vorschau
    _frei_von_skript(vorschau)


@pytest.mark.django_db
def test_vorlagenvorschau_ist_bereinigt(org: Any, make_member: Any, client_for: Any) -> None:
    verwaltung = make_member(org, ["organization.edit", "motions.view"], email="verwaltung@example.org")
    vorlage = MotionTemplate.objects.create(organization=org, name="Vorlage", content_template=BOESE)
    url = reverse("work:document_template_preview", kwargs={"org_slug": org.slug, "template_id": vorlage.id})

    response = client_for(verwaltung.user).get(url)

    assert response.status_code == 200
    _frei_von_skript(response.content.decode())


@pytest.mark.django_db
def test_neues_dokument_aus_vorlage_wird_bereinigt(org: Any, autorin: Any, client_for: Any) -> None:
    _recht_geben(autorin, "motions.create")
    vorlage = MotionTemplate.objects.create(organization=org, name="Vorlage", content_template=BOESE)

    response = client_for(autorin.user).post(
        reverse("work:document_create", kwargs={"org_slug": org.slug}),
        {"title": "Neu", "template": str(vorlage.id)},
    )

    assert response.status_code == 302
    neu = Motion.objects.get(organization=org, title="Neu")
    _frei_von_skript(str(cast(Any, neu).get_content_decrypted()))


@pytest.mark.django_db
def test_datenauskunft_gibt_inhalt_bereinigt_aus(org: Any, autorin: Any, altbestand: Motion) -> None:
    from django.template.loader import render_to_string

    from apps.work.organization.export_service import DsgvoExportService

    service = DsgvoExportService()
    data = service.collect_user_data(autorin.user, autorin, org)
    html = render_to_string(
        "work/profile/export/dsgvo_export.html",
        {"data": service.pdf_data(data), "user": autorin.user, "organization": org},
    )

    assert "Hallo" in html
    _frei_von_skript(html)
