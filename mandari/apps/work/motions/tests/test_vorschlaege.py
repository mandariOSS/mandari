# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Änderungsvorschläge im Antragseditor (Teil von #856, Modus „Vorschlagen“).

Ein Vorschlag ist ein Inline-Kommentar mit vorgeschlagenem Ersatz für die markierte Stelle. Angelegt wird er über
den vorhandenen Kommentar-Weg, entschieden über „Erledigen“ mit ``entscheidung``. Annehmen darf nur, wer den Text
bearbeiten darf; ablehnen auch, wer den Vorschlag gemacht hat. Bestehende Kommentare bleiben gewöhnliche
Kommentare, und nichts davon ändert den gespeicherten Text (den ersetzt der Editor).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, cast

import pytest
from django.urls import reverse

from apps.work.motions import ablauf, editor_neu, vorschlaege
from apps.work.motions.models import Motion, MotionComment

EDIT = ["motions.view", "motions.view_drafts", "motions.edit", "motions.comment"]
NUR_KOMMENTIEREN = ["motions.view", "motions.view_drafts", "motions.comment"]
CONFIG_RE = re.compile(r'<script[^>]*id="document-editor-config"[^>]*>(.*?)</script>', re.S)
XHR = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
INHALT = (
    '<p>Die Verwaltung prüft die <span data-comment-id="6a1f0d2e-3b4c-4d5e-8f60-718293a4b5c6">Begrenzung</span>.</p>'
)


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, EDIT, email="autorin-vorschlag@example.org")


@pytest.fixture
def kommentierer(org: Any, make_member: Any) -> Any:
    return make_member(org, NUR_KOMMENTIEREN, email="kommentar-vorschlag@example.org")


@pytest.fixture
def motion(org: Any, autorin: Any) -> Motion:
    m = Motion.objects.create(organization=org, author=autorin, title="Antrag Musterweg", visibility="organization")
    cast(Any, m).set_content_encrypted(INHALT)
    m.save()
    return m


def kommentar_url(org: Any, motion: Motion) -> str:
    return reverse("work:document_comment", kwargs={"org_slug": org.slug, "motion_id": motion.id})


def erledigen_url(org: Any, comment: MotionComment) -> str:
    return reverse(
        "work:document_comment_resolve",
        kwargs={"org_slug": org.slug, "motion_id": comment.motion_id, "comment_id": comment.id},
    )


def vorschlag_anlegen(client: Any, org: Any, motion: Motion, **felder: str) -> Any:
    daten = {
        "content": "",
        "selected_text": "Begrenzung",
        "vorschlag": "Begrenzung an Schultagen",
        "mark_id": str(uuid.uuid4()),
        **felder,
    }
    return client.post(kommentar_url(org, motion), daten, **XHR)


def text_von(motion: Motion) -> str:
    motion.refresh_from_db()
    return str(cast(Any, motion).get_content_decrypted() or "")


@pytest.mark.django_db
class TestAnlegen:
    def test_vorschlag_wird_kommentar_mit_beschreibung(
        self, org: Any, motion: Motion, kommentierer: Any, client_for: Any
    ) -> None:
        response = vorschlag_anlegen(client_for(kommentierer.user), org, motion)
        assert response.status_code == 200
        data = response.json()
        assert data["comment"]["vorschlag"] == "Begrenzung an Schultagen"
        comment = MotionComment.objects.get(id=data["comment"]["id"])
        assert comment.vorschlag == "Begrenzung an Schultagen"
        assert comment.vorschlag_angenommen is None
        assert comment.is_resolved is False
        assert comment.content == "Änderungsvorschlag: „Begrenzung“ ersetzen durch „Begrenzung an Schultagen“"
        assert vorschlaege.notiz(comment) == ""
        # Der Text selbst ändert sich erst beim Annehmen (im Editor)
        assert text_von(motion) == INHALT

    def test_eigene_begruendung_bleibt_kommentartext(
        self, org: Any, motion: Motion, kommentierer: Any, client_for: Any
    ) -> None:
        data = vorschlag_anlegen(client_for(kommentierer.user), org, motion, content="Nur an Schultagen nötig").json()
        comment = MotionComment.objects.get(id=data["comment"]["id"])
        assert comment.content == "Nur an Schultagen nötig"
        assert vorschlaege.notiz(comment) == "Nur an Schultagen nötig"

    def test_streichen_ist_leerer_vorschlag(self, org: Any, motion: Motion, autorin: Any, client_for: Any) -> None:
        data = vorschlag_anlegen(client_for(autorin.user), org, motion, vorschlag="").json()
        comment = MotionComment.objects.get(id=data["comment"]["id"])
        assert comment.vorschlag == ""
        assert comment.content == "Änderungsvorschlag: „Begrenzung“ streichen"

    def test_unveraenderter_text_ist_kein_vorschlag(
        self, org: Any, motion: Motion, autorin: Any, client_for: Any
    ) -> None:
        response = vorschlag_anlegen(client_for(autorin.user), org, motion, vorschlag="Begrenzung")
        assert response.status_code == 400
        assert not MotionComment.objects.filter(motion=motion).exists()

    def test_gewoehnlicher_kommentar_und_antwort_bleiben_kommentare(
        self, org: Any, motion: Motion, autorin: Any, client_for: Any
    ) -> None:
        client = client_for(autorin.user)
        data = client.post(
            kommentar_url(org, motion),
            {"content": "Bitte prüfen", "selected_text": "Begrenzung", "mark_id": str(uuid.uuid4())},
            **XHR,
        ).json()
        kommentar = MotionComment.objects.get(id=data["comment"]["id"])
        assert kommentar.vorschlag is None
        # Eine Antwort trägt nie einen Vorschlag, auch wenn das Feld mitkommt
        antwort = client.post(
            kommentar_url(org, motion), {"content": "Mache ich", "parent": str(kommentar.id), "vorschlag": "x"}, **XHR
        ).json()
        assert MotionComment.objects.get(id=antwort["comment"]["id"]).vorschlag is None


@pytest.mark.django_db
class TestEntscheiden:
    @pytest.fixture
    def vorschlag(self, motion: Motion, kommentierer: Any) -> MotionComment:
        return MotionComment.objects.create(
            motion=motion,
            author=kommentierer,
            content=vorschlaege.beschreibung("Begrenzung", "Begrenzung an Schultagen"),
            selected_text="Begrenzung",
            mark_id=uuid.UUID("6a1f0d2e-3b4c-4d5e-8f60-718293a4b5c6"),
            vorschlag="Begrenzung an Schultagen",
        )

    def test_annehmen_mit_schreibrecht(self, org: Any, vorschlag: MotionComment, autorin: Any, client_for: Any) -> None:
        response = client_for(autorin.user).post(erledigen_url(org, vorschlag), {"entscheidung": "annehmen"}, **XHR)
        assert response.status_code == 200
        assert response.json()["vorschlag_angenommen"] is True
        vorschlag.refresh_from_db()
        assert vorschlag.is_resolved is True
        assert vorschlag.vorschlag_angenommen is True
        assert vorschlag.resolved_by == autorin
        # Inhalt, Vorschlagstext und Stelle bleiben erhalten (nachlesbar im Verlauf der Kommentare)
        assert vorschlag.vorschlag == "Begrenzung an Schultagen"
        assert vorschlag.selected_text == "Begrenzung"

    def test_annehmen_ohne_schreibrecht_verboten(
        self, org: Any, vorschlag: MotionComment, kommentierer: Any, client_for: Any
    ) -> None:
        response = client_for(kommentierer.user).post(
            erledigen_url(org, vorschlag), {"entscheidung": "annehmen"}, **XHR
        )
        assert response.status_code == 403
        vorschlag.refresh_from_db()
        assert vorschlag.is_resolved is False
        assert vorschlag.vorschlag_angenommen is None

    def test_eigenen_vorschlag_zurueckziehen(
        self, org: Any, vorschlag: MotionComment, kommentierer: Any, client_for: Any
    ) -> None:
        response = client_for(kommentierer.user).post(
            erledigen_url(org, vorschlag), {"entscheidung": "ablehnen"}, **XHR
        )
        assert response.status_code == 200
        vorschlag.refresh_from_db()
        assert vorschlag.is_resolved is True
        assert vorschlag.vorschlag_angenommen is False

    def test_ablehnen_durch_bearbeitende_person(
        self, org: Any, vorschlag: MotionComment, autorin: Any, client_for: Any
    ) -> None:
        # Bisher durften nur Verfasser und „alle bearbeiten“ erledigen; über Vorschläge entscheidet, wer schreibt
        response = client_for(autorin.user).post(erledigen_url(org, vorschlag), {"entscheidung": "ablehnen"}, **XHR)
        assert response.status_code == 200
        vorschlag.refresh_from_db()
        assert vorschlag.vorschlag_angenommen is False

    def test_entschiedenes_wird_nicht_ueberschrieben(
        self, org: Any, vorschlag: MotionComment, autorin: Any, client_for: Any
    ) -> None:
        client = client_for(autorin.user)
        client.post(erledigen_url(org, vorschlag), {"entscheidung": "annehmen"}, **XHR)
        response = client.post(erledigen_url(org, vorschlag), {"entscheidung": "ablehnen"}, **XHR)
        assert response.status_code == 200
        assert response.json()["vorschlag_angenommen"] is True
        vorschlag.refresh_from_db()
        assert vorschlag.vorschlag_angenommen is True

    def test_gewoehnlicher_kommentar_erledigen_wie_bisher(
        self, org: Any, motion: Motion, autorin: Any, kommentierer: Any, client_for: Any
    ) -> None:
        kommentar = MotionComment.objects.create(motion=motion, author=kommentierer, content="Bitte prüfen")
        # Fremde Kommentare erledigen weiterhin nur Verfasser oder „alle bearbeiten“, auch mit „entscheidung“
        response = client_for(autorin.user).post(erledigen_url(org, kommentar), {"entscheidung": "annehmen"}, **XHR)
        assert response.status_code == 403
        response = client_for(kommentierer.user).post(erledigen_url(org, kommentar), **XHR)
        assert response.status_code == 200
        kommentar.refresh_from_db()
        assert kommentar.is_resolved is True
        assert kommentar.vorschlag_angenommen is None

    def test_text_bleibt_unberuehrt(
        self, org: Any, motion: Motion, vorschlag: MotionComment, autorin: Any, client_for: Any
    ) -> None:
        client_for(autorin.user).post(erledigen_url(org, vorschlag), {"entscheidung": "annehmen"}, **XHR)
        assert text_von(motion) == INHALT


@pytest.mark.django_db
def test_editor_liefert_vorschlaege_und_zaehlt_sie(
    org: Any, motion: Motion, autorin: Any, kommentierer: Any, client_for: Any
) -> None:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    MotionComment.objects.create(
        motion=motion,
        author=kommentierer,
        content="Begründung dazu",
        selected_text="Begrenzung",
        mark_id=uuid.UUID("6a1f0d2e-3b4c-4d5e-8f60-718293a4b5c6"),
        vorschlag="Begrenzung an Schultagen",
    )
    MotionComment.objects.create(motion=motion, author=kommentierer, content="Allgemein", mark_id=uuid.uuid4())
    response = client_for(autorin.user).get(f"/work/{org.slug}/documents/{motion.id}/")
    html = response.content.decode()
    match = CONFIG_RE.search(html)
    assert match
    eintraege = {c["content"]: c for c in json.loads(match.group(1))["inlineComments"]}
    assert eintraege["Begründung dazu"]["vorschlag"] == "Begrenzung an Schultagen"
    assert eintraege["Begründung dazu"]["notiz"] == "Begründung dazu"
    assert eintraege["Allgemein"]["vorschlag"] is None
    assert response.context["offen_text"] == "1 Kommentar, 1 Vorschlag"
    assert "Offen: 1 Kommentar, 1 Vorschlag" in html


def test_offen_text_und_ablauf() -> None:
    assert ablauf.offen_text(0, 0) == ""
    assert ablauf.offen_text(2, 0) == "2 Kommentare"
    assert ablauf.offen_text(0, 1) == "1 Vorschlag"
    assert ablauf.offen_text(1, 2) == "1 Kommentar, 2 Vorschläge"
    assert editor_neu.MENUE_LEISTE[-1] == ("ablauf", "Ablauf")


def test_beschreibung_kuerzt_lange_zitate() -> None:
    text = vorschlaege.beschreibung("a" * 500, "b")
    assert "…" in text
    assert len(text) < 400


def test_link_ins_ratsinformationssystem_mit_textlink_baustein() -> None:
    """Nach der Einreichung: Link zum Vorgang über den Textlink-Baustein der Suche (cotton/suche/textlink)."""
    from types import SimpleNamespace

    from django.template.loader import render_to_string

    paper_id = uuid.uuid4()
    html = render_to_string(
        "work/motions/partials/neu/_werkzeug.html",
        {
            "can_edit": False,
            "is_guest": False,
            "ablauf": SimpleNamespace(info_titel="Eingereicht", info_text="am 01.10.2026.", aktuell=4, aktion=""),
            "motion": SimpleNamespace(id=uuid.uuid4(), related_paper_id=paper_id),
            "organization": SimpleNamespace(slug="musterfraktion"),
        },
    )
    assert f'href="/work/musterfraktion/ris/papers/{paper_id}/"' in html
    assert "Im Ratsinformationssystem ansehen" in html
    assert "underline-offset-4" in html
