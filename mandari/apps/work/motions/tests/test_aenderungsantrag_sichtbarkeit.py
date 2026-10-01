# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Änderungsantrag schlägt die Sichtbarkeit des Bezugsantrags vor (Issue #735).

Änderungsanträge behalten eigene Rechte. Beim Anlegen (``?bezug=<id>``) und nach dem Setzen des Bezugsantrags
wird nur die Sichtbarkeit des Bezugsantrags vorgeschlagen – wenn die Person ihn sehen darf; RIS-Vorlagen ergeben
keinen Vorschlag. Übernehmen kann ihn nur, wer die Sichtbarkeit ändern darf (``Motion.can_share``); Freigaben,
Federführung und Mitarbeit des Bezugsantrags werden nie übernommen.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.urls import reverse

from apps.work.motions import references
from apps.work.motions.models import Motion, MotionShare
from insight_core.models import OParlBody, OParlPaper, OParlSource

RECHTE = ["motions.view", "motions.view_drafts", "motions.create", "motions.edit", "motions.comment"]
MIT_FREIGABE = [*RECHTE, "motions.share"]


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, MIT_FREIGABE, email="autorin@example.org")


@pytest.fixture
def ohne_freigabe(org: Any, make_member: Any) -> Any:
    """Darf anlegen und bearbeiten, aber keine Sichtbarkeit ändern (kein ``motions.share``)."""
    return make_member(org, RECHTE, email="ohne-freigabe@example.org")


@pytest.fixture
def kollege(org: Any, make_member: Any) -> Any:
    return make_member(org, MIT_FREIGABE, email="kollege@example.org")


@pytest.fixture
def leserin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["motions.view", "motions.view_drafts", "motions.comment"], email="leserin@example.org")


def _dokument(org: Any, autor: Any, titel: str, visibility: str = "organization", status: str = "draft") -> Motion:
    motion: Any = Motion(organization=org, author=autor, title=titel, visibility=visibility, status=status)
    motion.set_content_encrypted("<p>Text</p>")
    motion.save()
    return motion  # type: ignore[no-any-return]


def _anlegen_seite(client: Any, org: Any, bezug: str) -> Any:
    response = client.get(reverse("work:document_create", kwargs={"org_slug": org.slug}), {"bezug": bezug})
    assert response.status_code == 200
    return response


def _anlegen(client: Any, org: Any, **data: str) -> Any:
    return client.post(reverse("work:document_create", kwargs={"org_slug": org.slug}), {"title": "Änderung", **data})


def _editor(client: Any, org: Any, motion: Motion) -> str:
    response = client.get(reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": motion.id}))
    assert response.status_code == 200
    return str(response.content.decode())


def _neu(org: Any) -> Any:
    return Motion.objects.get(organization=org, title="Änderung")


# ---------------------------------------------------------------------------
# Anlegen eines Änderungsantrags
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("sichtbarkeit", ["organization", "shared", "private"])
def test_anlegen_schlaegt_sichtbarkeit_des_bezugsantrags_vor(
    org: Any, autorin: Any, client_for: Any, sichtbarkeit: str
) -> None:
    haupt = _dokument(org, autorin, "Haushaltsantrag Radwege", visibility=sichtbarkeit)
    client = client_for(autorin.user)

    seite = _anlegen_seite(client, org, str(haupt.id))

    assert seite.context["bezug_parent"] == haupt
    assert seite.context["suggested_visibility"] == sichtbarkeit
    assert seite.context["can_choose_visibility"] is True
    html = seite.content.decode()
    assert 'data-testid="aenderungsantrag-bezug"' in html and "Haushaltsantrag Radwege" in html
    assert f'name="parent_motion" value="{haupt.id}"' in html
    assert f'value="{sichtbarkeit}" checked' in html

    antwort = _anlegen(client, org, parent_motion=str(haupt.id), visibility=sichtbarkeit)

    assert antwort.status_code == 302
    neu = _neu(org)
    assert neu.parent_motion_id == haupt.id
    assert neu.visibility == sichtbarkeit


@pytest.mark.django_db
def test_vorschlag_laesst_sich_beim_anlegen_aendern(org: Any, autorin: Any, client_for: Any) -> None:
    haupt = _dokument(org, autorin, "Haushaltsantrag Radwege", visibility="organization")

    antwort = _anlegen(client_for(autorin.user), org, parent_motion=str(haupt.id), visibility="private")

    assert antwort.status_code == 302
    neu = _neu(org)
    assert neu.parent_motion_id == haupt.id and neu.visibility == "private"


@pytest.mark.django_db
def test_anlegen_ohne_bezug_bleibt_wie_bisher(org: Any, autorin: Any, client_for: Any) -> None:
    client = client_for(autorin.user)

    seite = _anlegen_seite(client, org, "")
    assert seite.context["bezug_parent"] is None and seite.context["can_choose_visibility"] is False
    assert 'data-testid="aenderungsantrag-bezug"' not in seite.content.decode()

    assert _anlegen(client, org).status_code == 302
    neu = _neu(org)
    assert neu.parent_motion_id is None and neu.visibility == "private"


@pytest.mark.django_db
def test_bezugsantrag_ohne_leserecht_kein_vorschlag_kein_titel(
    org: Any, autorin: Any, kollege: Any, client_for: Any
) -> None:
    geheim = _dokument(org, kollege, "Geheimer Entwurf", visibility="private")
    client = client_for(autorin.user)

    seite = _anlegen_seite(client, org, str(geheim.id))

    assert seite.context["bezug_parent"] is None
    assert seite.context["suggested_visibility"] == "private" and seite.context["can_choose_visibility"] is False
    assert "Geheimer Entwurf" not in seite.content.decode()

    antwort = _anlegen(client, org, parent_motion=str(geheim.id), visibility="private")

    assert antwort.status_code == 200
    assert references.DOCUMENT_NOT_FOUND in antwort.content.decode()
    assert not Motion.objects.filter(organization=org, title="Änderung").exists()


@pytest.mark.django_db
def test_bezugsantrag_im_papierkorb_kein_vorschlag(org: Any, autorin: Any, client_for: Any) -> None:
    geloescht = _dokument(org, autorin, "Gelöschter Antrag", status="deleted")

    seite = _anlegen_seite(client_for(autorin.user), org, str(geloescht.id))

    assert seite.context["bezug_parent"] is None
    assert references.suggested_visibility(geloescht, autorin) is None


@pytest.mark.django_db
def test_ohne_freigaberecht_kein_vorschlag_und_dokument_bleibt_privat(
    org: Any, autorin: Any, ohne_freigabe: Any, client_for: Any
) -> None:
    """Keine Rechteausweitung: Wer die Sichtbarkeit nicht ändern darf, legt auch mit Bezug privat an."""
    haupt = _dokument(org, autorin, "Haushaltsantrag Radwege", visibility="organization")
    client = client_for(ohne_freigabe.user)

    seite = _anlegen_seite(client, org, str(haupt.id))

    assert seite.context["bezug_parent"] == haupt and seite.context["can_choose_visibility"] is False
    html = seite.content.decode()
    assert 'name="visibility"' not in html and "Das Dokument wird privat angelegt." in html

    antwort = _anlegen(client, org, parent_motion=str(haupt.id), visibility="organization")

    assert antwort.status_code == 302
    neu = _neu(org)
    assert neu.parent_motion_id == haupt.id
    assert neu.visibility == "private"


@pytest.mark.django_db
def test_rechte_des_bezugsantrags_werden_nicht_uebernommen(
    org: Any, autorin: Any, kollege: Any, leserin: Any, make_member: Any, client_for: Any
) -> None:
    """Freigaben, Federführung und Mitarbeit des Bezugsantrags gelten für den Änderungsantrag nicht."""
    freigegeben = make_member(org, MIT_FREIGABE, email="freigegeben@example.org")
    haupt = _dokument(org, kollege, "Geteilter Antrag", visibility="shared")
    MotionShare.objects.create(motion=haupt, scope="user", user=freigegeben.user, level="edit", created_by=kollege.user)
    MotionShare.objects.create(motion=haupt, scope="user", user=autorin.user, level="edit", created_by=kollege.user)
    haupt.contributors.add(leserin)
    assert haupt.can_access(freigegeben) and haupt.can_access(leserin)

    antwort = _anlegen(client_for(autorin.user), org, parent_motion=str(haupt.id), visibility="shared")

    assert antwort.status_code == 302
    neu = _neu(org)
    assert neu.parent_motion_id == haupt.id and neu.visibility == "shared"
    assert not MotionShare.objects.filter(motion=neu).exists()
    assert list(neu.contributors.all()) == []
    assert neu.author_id == autorin.id and neu.responsible_id == autorin.id
    for person in (freigegeben, leserin, kollege):
        assert not neu.can_access(person), person.user.email
        assert neu not in cast(Any, Motion).visible_to(person)

    # Spätere Änderungen am Bezugsantrag wirken nicht auf den Änderungsantrag
    haupt.visibility = "organization"
    haupt.save(update_fields=["visibility"])
    neu.refresh_from_db()
    assert neu.visibility == "shared" and not neu.can_access(kollege)


# ---------------------------------------------------------------------------
# Setzen des Bezugsantrags: Vorschlag in der Details-Seitenleiste
# ---------------------------------------------------------------------------


def _bezug_setzen(client: Any, org: Any, motion: Motion, **data: str) -> Any:
    url = reverse("work:document_meta", kwargs={"org_slug": org.slug, "motion_id": motion.id})
    return client.post(url, {"action": "set_parent", **data}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


@pytest.mark.django_db
def test_setzen_des_bezugsantrags_schlaegt_sichtbarkeit_vor_ohne_sie_zu_aendern(
    org: Any, autorin: Any, client_for: Any
) -> None:
    haupt = _dokument(org, autorin, "Haushaltsantrag Radwege", visibility="organization")
    aenderung = _dokument(org, autorin, "Änderung zum Radwegeantrag", visibility="private")
    client = client_for(autorin.user)

    assert _bezug_setzen(client, org, aenderung, parent_motion=str(haupt.id)).status_code == 200

    aenderung.refresh_from_db()
    assert aenderung.visibility == "private"  # nur ein Vorschlag, keine stille Rechteänderung
    html = _editor(client, org, aenderung)
    assert 'data-testid="bezug-sichtbarkeit-vorschlag"' in html
    assert 'data-visibility="organization"' in html and "suggestVisibility" in html
    assert references.visibility_suggestion(aenderung, autorin) == {
        "value": "organization",
        "label": "Gesamte Organisation",
        "current": "Privat",
    }


@pytest.mark.django_db
def test_kein_vorschlag_bei_gleicher_sichtbarkeit(org: Any, autorin: Any, client_for: Any) -> None:
    haupt = _dokument(org, autorin, "Haushaltsantrag Radwege", visibility="organization")
    aenderung = _dokument(org, autorin, "Änderung zum Radwegeantrag", visibility="organization")
    aenderung.parent_motion = haupt
    aenderung.save(update_fields=["parent_motion"])

    assert 'data-testid="bezug-sichtbarkeit-vorschlag"' not in _editor(client_for(autorin.user), org, aenderung)


@pytest.mark.django_db
def test_kein_vorschlag_bei_ris_vorlage(org: Any, autorin: Any, client_for: Any) -> None:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    org.body = body
    org.save(update_fields=["body"])
    vorlage = OParlPaper.objects.create(
        external_id="https://ris.example.org/paper/1", body=body, name="Spielplatz am Markt", reference="V/2026/17"
    )
    aenderung = _dokument(org, autorin, "Änderung zur Vorlage", visibility="private")
    client = client_for(autorin.user)

    assert _bezug_setzen(client, org, aenderung, parent_paper=str(vorlage.id)).status_code == 200

    aenderung.refresh_from_db()
    assert aenderung.parent_paper_id == vorlage.id
    assert references.visibility_suggestion(aenderung, autorin) is None
    assert 'data-testid="bezug-sichtbarkeit-vorschlag"' not in _editor(client, org, aenderung)


@pytest.mark.django_db
def test_kein_vorschlag_ohne_leserecht_am_bezugsantrag(org: Any, autorin: Any, kollege: Any, client_for: Any) -> None:
    """Gesetzt von jemand anderem: Wer den Bezugsantrag nicht sehen darf, erfährt auch seine Sichtbarkeit nicht."""
    geheim = _dokument(org, kollege, "Geheimer Entwurf", visibility="shared")
    aenderung = _dokument(org, autorin, "Änderung", visibility="organization")
    aenderung.parent_motion = geheim
    aenderung.save(update_fields=["parent_motion"])

    html = _editor(client_for(autorin.user), org, aenderung)

    assert references.visibility_suggestion(aenderung, autorin) is None
    assert 'data-testid="bezug-sichtbarkeit-vorschlag"' not in html
    assert "Geheimer Entwurf" not in html and "Dokument ohne Freigabe" in html


@pytest.mark.django_db
def test_kein_vorschlag_ohne_freigaberecht(org: Any, ohne_freigabe: Any, autorin: Any, client_for: Any) -> None:
    haupt = _dokument(org, autorin, "Haushaltsantrag Radwege", visibility="organization")
    aenderung = _dokument(org, ohne_freigabe, "Änderung", visibility="private")
    client = client_for(ohne_freigabe.user)

    assert _bezug_setzen(client, org, aenderung, parent_motion=str(haupt.id)).status_code == 200

    assert references.visibility_suggestion(aenderung, ohne_freigabe) is None
    assert 'data-testid="bezug-sichtbarkeit-vorschlag"' not in _editor(client, org, aenderung)


# ---------------------------------------------------------------------------
# Verweis „Änderungsantrag anlegen“
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_verweis_aenderungsantrag_anlegen(org: Any, autorin: Any, leserin: Any, client_for: Any) -> None:
    haupt = _dokument(org, autorin, "Haushaltsantrag Radwege", visibility="organization")
    ziel = reverse("work:document_create", kwargs={"org_slug": org.slug}) + f"?bezug={haupt.id}"

    assert f'href="{ziel}"' in _editor(client_for(autorin.user), org, haupt)
    # ohne motions.create kein Verweis
    assert 'data-testid="aenderungsantrag-anlegen"' not in _editor(client_for(leserin.user), org, haupt)
