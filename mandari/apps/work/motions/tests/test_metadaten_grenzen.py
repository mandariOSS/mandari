# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Grenzen beim Speichern im Editor und bei Kommentaren.

- Gäste mit Freigabe „Bearbeiten“ ändern nur Titel und Inhalt; Papierkorb und Metadaten
  (Dokumenttyp, Briefkopf, Zusammenfassung, Formularfelder) bleiben Mitgliedern vorbehalten (#76).
- Dokumenttyp und Briefkopf lassen sich nur aus der eigenen Organisation wählen.
- Antworten hängen nur an Kommentaren desselben Dokuments.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.urls import reverse

from apps.common.tests.factories import MembershipFactory, OrganizationFactory, UserFactory
from apps.work.motions.models import Motion, MotionComment, MotionShare, MotionType, OrganizationLetterhead

PERMISSIONS = ["motions.view", "motions.edit", "motions.comment"]


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, PERMISSIONS, email="autorin@example.org")


@pytest.fixture
def dokument(org: Any, autorin: Any) -> Motion:
    typ = MotionType.objects.create(organization=org, name="Antrag Eigen", slug="antrag-eigen")
    motion = Motion.objects.create(
        organization=org, author=autorin, title="Antrag", visibility="organization", document_type=typ
    )
    cast(Any, motion).set_content_encrypted("<p>Alt</p>")
    motion.save()
    return motion


@pytest.fixture
def gast(org: Any, autorin: Any, dokument: Motion) -> Any:
    konto: Any = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    gast: Any = MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]
    MotionShare.objects.create(motion=dokument, scope="user", user=konto, level="edit", created_by=autorin.user)
    return gast


@pytest.fixture
def fremde_org(db: Any) -> Any:
    return OrganizationFactory(name="Andere Fraktion", slug="andere-fraktion")  # type: ignore[no-untyped-call]


def _editor_url(org: Any, motion: Motion) -> str:
    return reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": motion.id})


def _post(client: Any, url: str, **daten: str) -> Any:
    return client.post(url, daten, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


@pytest.mark.django_db
def test_gast_legt_nichts_in_den_papierkorb(org: Any, gast: Any, dokument: Motion, client_for: Any) -> None:
    response = _post(client_for(gast.user), _editor_url(org, dokument), action="delete")

    dokument.refresh_from_db()
    assert response.status_code == 403
    assert dokument.status != "deleted"
    assert dokument.deleted_at is None


@pytest.mark.django_db
def test_gast_aendert_nur_titel_und_inhalt(org: Any, gast: Any, dokument: Motion, client_for: Any) -> None:
    typ = dokument.document_type
    briefkopf = OrganizationLetterhead.objects.create(organization=org, name="Kopf")
    dokument.letterhead = briefkopf
    dokument.summary = "Kurzfassung"
    dokument.save()

    response = _post(
        client_for(gast.user),
        _editor_url(org, dokument),
        action="save",
        title="Neuer Titel",
        content="<p>Neu</p>",
        summary="Überschrieben",
        document_type_id="",
        letterhead_id="",
    )

    dokument.refresh_from_db()
    assert response.status_code == 200
    assert dokument.title == "Neuer Titel"
    assert cast(Any, dokument).get_content_decrypted() == "<p>Neu</p>"
    assert dokument.document_type == typ
    assert dokument.letterhead == briefkopf
    assert dokument.summary == "Kurzfassung"


@pytest.mark.django_db
def test_gast_nutzt_das_formular_nicht(org: Any, gast: Any, dokument: Motion, client_for: Any) -> None:
    response = _post(client_for(gast.user), _editor_url(org, dokument), action="update", title="Formular")

    dokument.refresh_from_db()
    assert response.status_code == 403
    assert dokument.title == "Antrag"


@pytest.mark.django_db
def test_briefkopf_und_dokumenttyp_nur_aus_der_eigenen_organisation(
    org: Any, fremde_org: Any, autorin: Any, dokument: Motion, client_for: Any
) -> None:
    fremder_kopf = OrganizationLetterhead.objects.create(organization=fremde_org, name="Fremd")
    fremder_typ = MotionType.objects.create(organization=fremde_org, name="Fremdtyp", slug="fremdtyp")
    typ = dokument.document_type
    client = client_for(autorin.user)

    for feld, wert in (("letterhead_id", fremder_kopf.id), ("document_type_id", fremder_typ.id)):
        daten = {"action": "save", "title": "Antrag", "content": "<p>Alt</p>"}
        daten["document_type_id"] = str(typ.id) if typ else ""
        daten[feld] = str(wert)
        response = _post(client, _editor_url(org, dokument), **daten)
        assert response.status_code == 400, feld

    dokument.refresh_from_db()
    assert dokument.letterhead_id is None
    assert dokument.document_type == typ

    eigener_kopf = OrganizationLetterhead.objects.create(organization=org, name="Eigen")
    response = _post(
        client,
        _editor_url(org, dokument),
        action="save",
        title="Antrag",
        content="<p>Alt</p>",
        document_type_id=str(typ.id) if typ else "",
        letterhead_id=str(eigener_kopf.id),
    )
    dokument.refresh_from_db()
    assert response.status_code == 200
    assert dokument.letterhead == eigener_kopf


@pytest.mark.django_db
def test_antwort_nur_auf_kommentare_desselben_dokuments(
    org: Any, autorin: Any, dokument: Motion, client_for: Any
) -> None:
    anderes = Motion.objects.create(organization=org, author=autorin, title="Privat", visibility="private")
    fremder_kommentar = MotionComment.objects.create(motion=anderes, author=autorin, content="Intern")
    url = reverse("work:document_comment", kwargs={"org_slug": org.slug, "motion_id": dokument.id})

    response = _post(client_for(autorin.user), url, content="Antwort", parent=str(fremder_kommentar.id))

    assert response.status_code == 400
    assert not MotionComment.objects.filter(parent=fremder_kommentar).exists()
