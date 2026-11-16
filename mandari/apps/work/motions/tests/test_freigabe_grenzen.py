# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Freigaben erweitern den Kreis nie über das eigene Freigaberecht hinaus.

- Freigabeanfragen gehen nur an Mitglieder (keine Gäste); bekommt die angefragte Person
  dafür Zugriff, braucht die anfragende Person das Freigaberecht (``Motion.can_share``: Autor:in
  oder ``motions.edit_all``, jeweils mit ``motions.share``).
- Eine Ordner-Freigabe an Gäste umfasst organisationsweite Dokumente und die geteilten Dokumente
  der freigebenden Person – nicht die privaten oder gezielt geteilten Dokumente anderer und auch
  nicht die privaten der freigebenden Person (Issue #582).
- In einen für Gäste freigegebenen Ordner verschiebt nur, wer das Dokument freigeben darf.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.urls import reverse

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.motions.models import DocumentFolder, FolderGuestShare, Motion, MotionApproval, MotionShare

RECHTE = ["motions.view", "motions.view_drafts", "motions.edit", "motions.comment"]


def _gast(org: Any, email: str) -> Any:
    konto = UserFactory(email=email)  # type: ignore[no-untyped-call]
    return MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, [*RECHTE, "motions.share"], email="autorin@example.org")


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
        organization=org, author=freigebende, title="Eigenes privat", visibility="private", folder=ordner
    )
    eigen_geteilt = Motion.objects.create(
        organization=org, author=freigebende, title="Eigenes geteilt", visibility="shared", folder=ordner
    )
    organisationsweit = Motion.objects.create(
        organization=org, author=autorin, title="Für alle", visibility="organization", folder=unterordner
    )

    sichtbar = set(cast(Any, Motion).visible_to(gast).values_list("title", flat=True))

    assert sichtbar == {"Eigenes geteilt", "Für alle"}
    assert not fremd_privat.can_access(gast)
    assert not fremd_geteilt.can_access(gast)
    # Privat bleibt privat, auch für die eigenen Dokumente der freigebenden Person (Issue #582)
    assert not eigen_privat.can_access(gast)
    assert eigen_geteilt.can_edit(gast)
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
    MotionShare.objects.create(motion=dokument, scope="user", user=kollegin.user, level="edit", created_by=autorin.user)
    url = reverse("work:document_move_to_folder", kwargs={"org_slug": org.slug})

    # Die Kollegin darf das organisationsweite Dokument bearbeiten (Freigabe „Bearbeiten“), aber nicht freigeben
    assert dokument.can_edit(kollegin) and not dokument.can_share(kollegin)
    client_for(kollegin.user).post(url, {"folder": str(ordner.id), "motion_ids": [str(dokument.id)]})
    dokument.refresh_from_db()
    assert dokument.folder_id is None

    client_for(autorin.user).post(url, {"folder": str(ordner.id), "motion_ids": [str(dokument.id)]})
    dokument.refresh_from_db()
    assert dokument.folder_id == ordner.id


def _gastordner(org: Any, freigebende: Any, name: str = "Extern") -> tuple[DocumentFolder, Any]:
    ordner = DocumentFolder.objects.create(organization=org, name=name, created_by=freigebende)
    gast = _gast(org, f"gast-{name.lower()}@example.org")
    FolderGuestShare.objects.create(folder=ordner, user=gast.user, level="view", created_by=freigebende.user)
    return ordner, gast


@pytest.mark.django_db
def test_ordnerwechsel_ueber_die_metadaten_in_gastordner_nur_mit_freigaberecht(
    org: Any, autorin: Any, kollegin: Any, freigebende: Any, client_for: Any
) -> None:
    """Der Metadaten-Endpunkt folgt derselben Regel wie „Verschieben nach“."""
    ordner, gast = _gastordner(org, freigebende)
    dokument = Motion.objects.create(organization=org, author=autorin, title="Für alle", visibility="organization")
    MotionShare.objects.create(motion=dokument, scope="user", user=kollegin.user, level="edit", created_by=autorin.user)
    url = reverse("work:document_meta", kwargs={"org_slug": org.slug, "motion_id": dokument.id})
    daten = {"action": "set_folder", "folder": str(ordner.id)}

    assert dokument.can_edit(kollegin) and not dokument.can_share(kollegin)
    antwort = client_for(kollegin.user).post(url, daten, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert antwort.status_code == 403
    dokument.refresh_from_db()
    assert dokument.folder_id is None
    assert not dokument.can_access(gast)

    assert client_for(autorin.user).post(url, daten, HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code == 200
    dokument.refresh_from_db()
    assert dokument.folder_id == ordner.id


@pytest.mark.django_db
def test_ordner_unter_gastordner_haengen_nur_mit_freigaberecht_an_den_dokumenten(
    org: Any, autorin: Any, make_member: Any, freigebende: Any, client_for: Any
) -> None:
    """Wer einen Ordner unter einen freigegebenen hängt, braucht das Freigaberecht an den Dokumenten darunter."""
    gastordner, gast = _gastordner(org, freigebende)
    kollegin = make_member(org, [*RECHTE, "motions.create"], email="ordner@example.org")
    eigener = DocumentFolder.objects.create(organization=org, name="Sammlung", created_by=kollegin)
    unterordner = DocumentFolder.objects.create(organization=org, name="Tiefer", parent=eigener, created_by=kollegin)
    fremd = Motion.objects.create(
        organization=org, author=autorin, title="Fremd für alle", visibility="organization", folder=unterordner
    )
    url = reverse("work:document_folder_update", kwargs={"org_slug": org.slug, "folder_id": eigener.id})

    client_for(kollegin.user).post(url, {"parent": str(gastordner.id)})
    eigener.refresh_from_db()
    assert eigener.parent_id is None
    assert not fremd.can_access(gast)

    # Private Dokumente erfasst keine Ordner-Freigabe: Sie halten das Umhängen nicht auf
    fremd.visibility = "private"
    fremd.save(update_fields=["visibility"])
    client_for(kollegin.user).post(url, {"parent": str(gastordner.id)})
    eigener.refresh_from_db()
    assert eigener.parent_id == gastordner.id

    # Umbenennen und Umhängen innerhalb desselben freigegebenen Bereichs bleiben möglich
    fremd.visibility = "organization"
    fremd.save(update_fields=["visibility"])
    client_for(kollegin.user).post(url, {"name": "Sammlung neu", "parent": str(gastordner.id)})
    eigener.refresh_from_db()
    assert eigener.name == "Sammlung neu" and eigener.parent_id == gastordner.id


@pytest.mark.django_db
def test_ordner_mit_eigenen_dokumenten_unter_gastordner(org: Any, freigebende: Any, client_for: Any) -> None:
    gastordner, gast = _gastordner(org, freigebende)
    eigener = DocumentFolder.objects.create(organization=org, name="Meine", created_by=freigebende)
    dokument = Motion.objects.create(
        organization=org, author=freigebende, title="Eigenes", visibility="organization", folder=eigener
    )
    url = reverse("work:document_folder_update", kwargs={"org_slug": org.slug, "folder_id": eigener.id})

    client_for(freigebende.user).post(url, {"parent": str(gastordner.id)})

    eigener.refresh_from_db()
    assert eigener.parent_id == gastordner.id
    assert Motion.objects.get(pk=dokument.pk).can_access(gast)
