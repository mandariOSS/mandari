# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Freigaben erweitern den Kreis nie über das eigene Freigaberecht hinaus.

- Freigabeanfragen gehen nur an Mitglieder (keine Gäste); bekommt die angefragte Person
  dafür Zugriff, braucht die anfragende Person das Freigaberecht (Autor:in oder ``motions.share``).
- Eine Ordner-Freigabe an Gäste umfasst organisationsweite Dokumente und die eigenen Dokumente
  der freigebenden Person – nicht die privaten oder gezielt geteilten Dokumente anderer.
- In einen für Gäste freigegebenen Ordner verschiebt nur, wer das Dokument freigeben darf.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.urls import reverse

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.motions.models import DocumentFolder, FolderGuestShare, Motion, MotionApproval, MotionShare

RECHTE = ["motions.view", "motions.edit", "motions.comment"]


def _gast(org: Any, email: str) -> Any:
    konto = UserFactory(email=email)  # type: ignore[no-untyped-call]
    return MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, RECHTE, email="autorin@example.org")


@pytest.fixture
def kollegin(org: Any, make_member: Any) -> Any:
    return make_member(org, RECHTE, email="kollegin@example.org")


@pytest.fixture
def freigebende(org: Any, make_member: Any) -> Any:
    return make_member(org, [*RECHTE, "motions.share", "motions.create"], email="freigabe@example.org")


def _anfrage(client: Any, org: Any, motion: Motion, approver: Any) -> Any:
    return client.post(
        reverse("work:document_approval_request", kwargs={"org_slug": org.slug, "motion_id": motion.id}),
        {"approver": str(approver.id), "approval_type": "chair"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )


@pytest.mark.django_db
def test_freigabeanfrage_nicht_an_gaeste(org: Any, autorin: Any, client_for: Any) -> None:
    dokument = Motion.objects.create(organization=org, author=autorin, title="Privat", visibility="private")
    gast = _gast(org, "gast@example.org")

    response = _anfrage(client_for(autorin.user), org, dokument, gast)

    assert response.status_code in (400, 403, 404)
    assert not MotionShare.objects.filter(motion=dokument, user=gast.user).exists()
    assert not MotionApproval.objects.filter(motion=dokument).exists()


@pytest.mark.django_db
def test_freigabeanfrage_oeffnet_nur_mit_freigaberecht(
    org: Any, autorin: Any, kollegin: Any, make_member: Any, client_for: Any
) -> None:
    dokument = Motion.objects.create(organization=org, author=autorin, title="Geteilt", visibility="shared")
    MotionShare.objects.create(motion=dokument, scope="user", user=kollegin.user, level="edit", created_by=autorin.user)
    dritte = make_member(org, RECHTE, email="dritte@example.org")

    response = _anfrage(client_for(kollegin.user), org, dokument, dritte)

    assert response.status_code == 403
    assert not dokument.can_access(dritte)
    # Die Autorin darf es
    assert _anfrage(client_for(autorin.user), org, dokument, dritte).status_code == 200
    assert dokument.can_access(dritte)


@pytest.mark.django_db
def test_ordnerfreigabe_umfasst_keine_privaten_dokumente_anderer(
    org: Any, autorin: Any, freigebende: Any, client_for: Any
) -> None:
    ordner = DocumentFolder.objects.create(organization=org, name="AG Verkehr", created_by=freigebende)
    unterordner = DocumentFolder.objects.create(organization=org, name="Entwürfe", parent=ordner)
    gast = _gast(org, "gast@example.org")
    FolderGuestShare.objects.create(folder=ordner, user=gast.user, level="edit", created_by=freigebende.user)

    fremd_privat = Motion.objects.create(
        organization=org, author=autorin, title="Privat Autorin", visibility="private", folder=unterordner
    )
    fremd_geteilt = Motion.objects.create(
        organization=org, author=autorin, title="Geteilt Autorin", visibility="shared", folder=ordner
    )
    eigen_privat = Motion.objects.create(
        organization=org, author=freigebende, title="Eigenes", visibility="private", folder=ordner
    )
    organisationsweit = Motion.objects.create(
        organization=org, author=autorin, title="Für alle", visibility="organization", folder=unterordner
    )

    sichtbar = set(cast(Any, Motion).visible_to(gast).values_list("title", flat=True))

    assert sichtbar == {"Eigenes", "Für alle"}
    assert not fremd_privat.can_access(gast)
    assert not fremd_geteilt.can_access(gast)
    assert eigen_privat.can_edit(gast)
    assert organisationsweit.can_access(gast)

    url = reverse("work:guest_documents", kwargs={"org_slug": org.slug})
    html = client_for(gast.user).get(url, {"ordner": str(unterordner.id)}).content.decode()
    assert "Für alle" in html
    assert "Privat Autorin" not in html


@pytest.mark.django_db
def test_verschieben_in_gastordner_nur_mit_freigaberecht(
    org: Any, autorin: Any, kollegin: Any, freigebende: Any, client_for: Any
) -> None:
    ordner = DocumentFolder.objects.create(organization=org, name="Extern", created_by=freigebende)
    FolderGuestShare.objects.create(
        folder=ordner, user=_gast(org, "gast@example.org").user, level="view", created_by=freigebende.user
    )
    dokument = Motion.objects.create(organization=org, author=autorin, title="Für alle", visibility="organization")
    kollegin.roles.get().permissions.add(*autorin.roles.get().permissions.all())
    url = reverse("work:document_move_to_folder", kwargs={"org_slug": org.slug})

    # Die Kollegin darf das organisationsweite Dokument bearbeiten, aber nicht freigeben
    client_for(kollegin.user).post(url, {"folder": str(ordner.id), "motion_ids": [str(dokument.id)]})
    dokument.refresh_from_db()
    assert dokument.folder_id is None

    client_for(autorin.user).post(url, {"folder": str(ordner.id), "motion_ids": [str(dokument.id)]})
    dokument.refresh_from_db()
    assert dokument.folder_id == ordner.id
