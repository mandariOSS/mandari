# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Die Rechte „Entwürfe anderer anzeigen“ (``motions.view_drafts``) und „Anträge anderer löschen“
(``motions.delete``) wirken über ``Motion.access_level`` bzw. ``Motion.can_delete`` auf allen Wegen
(Ergänzung zu test_dokument_zugriffsmatrix.py).
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.permissions import PERMISSIONS
from apps.work.motions.models import Motion

MITGLIED = ["motions.view", "motions.view_drafts", "motions.create", "motions.edit", "motions.comment"]


def _dokument(org: Any, autor: Any, titel: str, **felder: Any) -> Motion:
    felder.setdefault("visibility", "organization")
    return Motion.objects.create(organization=org, author=autor, title=titel, **felder)


def _xhr(client: Any, url: str, daten: dict[str, Any]) -> Any:
    return client.post(url, daten, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


@pytest.mark.django_db
def test_entwuerfe_anderer_nur_mit_recht(org: Any, make_member: Any, client_for: Any) -> None:
    autorin = make_member(org, MITGLIED, email="autorin@example.org")
    partei = make_member(org, ["motions.view", "motions.comment"], email="partei@example.org")
    entwurf = _dokument(org, autorin, "Entwurf Radweg")
    eingereicht = _dokument(org, autorin, "Eingereicht Radweg", status="submitted")
    eigener = _dokument(org, partei, "Mein Entwurf")

    sichtbar = set(cast(Any, Motion).visible_to(partei).values_list("title", flat=True))
    assert sichtbar == {"Eingereicht Radweg", "Mein Entwurf"}
    assert not entwurf.can_access(partei)
    assert eigener.can_access(partei), "Eigene Entwürfe sieht jede Autorin"

    client = client_for(partei.user)
    liste = client.get(reverse("work:documents", kwargs={"org_slug": org.slug})).content.decode()
    assert "Eingereicht Radweg" in liste and "Entwurf Radweg" not in liste
    editor = reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": entwurf.id})
    assert client.get(editor).status_code == 403
    assert eingereicht.can_access(partei)


@pytest.mark.django_db
def test_eigene_dokumente_loeschen_ohne_loeschrecht(org: Any, make_member: Any, client_for: Any) -> None:
    autorin = make_member(org, MITGLIED, email="autorin@example.org")
    dokument = _dokument(org, autorin, "Radweg")
    editor = reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": dokument.id})

    assert _xhr(client_for(autorin.user), editor, {"action": "delete"}).status_code == 200
    dokument.refresh_from_db()
    assert dokument.status == "deleted"
    loeschen = reverse("work:document_permanent_delete", kwargs={"org_slug": org.slug, "motion_id": dokument.id})
    assert client_for(autorin.user).post(loeschen).status_code == 302
    assert not Motion.objects.filter(pk=dokument.pk).exists()


@pytest.mark.django_db
def test_dokumente_anderer_nur_mit_loeschrecht(org: Any, make_member: Any, client_for: Any) -> None:
    autorin = make_member(org, MITGLIED, email="autorin@example.org")
    vorsitz = make_member(org, [*MITGLIED, "motions.edit_all"], email="vorsitz@example.org")
    verwaltung = make_member(org, [*MITGLIED, "motions.edit_all", "motions.delete"], email="loeschen@example.org")
    dokument = _dokument(org, autorin, "Radweg")
    editor = reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": dokument.id})

    # Bearbeiten darf der Vorsitz weiterhin, löschen nicht
    assert dokument.can_edit(vorsitz) and not dokument.can_delete(vorsitz)
    assert _xhr(client_for(vorsitz.user), editor, {"action": "delete"}).status_code == 403
    assert _xhr(client_for(verwaltung.user), editor, {"action": "delete"}).status_code == 200
    dokument.refresh_from_db()
    assert dokument.status == "deleted"

    loeschen = reverse("work:document_permanent_delete", kwargs={"org_slug": org.slug, "motion_id": dokument.id})
    assert client_for(vorsitz.user).post(loeschen).status_code == 403
    assert client_for(verwaltung.user).post(loeschen).status_code == 302
    assert not Motion.objects.filter(pk=dokument.pk).exists()


@pytest.mark.django_db
def test_papierkorb_bietet_endgueltiges_loeschen_nur_mit_recht(org: Any, make_member: Any, client_for: Any) -> None:
    autorin = make_member(org, MITGLIED, email="autorin@example.org")
    vorsitz = make_member(org, [*MITGLIED, "motions.edit_all"], email="vorsitz@example.org")
    fremd = _dokument(org, autorin, "Fremd", status="deleted", deleted_at=timezone.now())
    eigen = _dokument(org, vorsitz, "Eigen", status="deleted", deleted_at=timezone.now())
    client = client_for(vorsitz.user)

    html = client.get(reverse("work:document_trash", kwargs={"org_slug": org.slug})).content.decode()
    eigen_url = reverse("work:document_permanent_delete", kwargs={"org_slug": org.slug, "motion_id": eigen.id})
    fremd_url = reverse("work:document_permanent_delete", kwargs={"org_slug": org.slug, "motion_id": fremd.id})
    assert eigen_url in html and fremd_url not in html

    client.post(reverse("work:document_empty_trash", kwargs={"org_slug": org.slug}))
    assert not Motion.objects.filter(pk=eigen.pk).exists()
    assert Motion.objects.filter(pk=fremd.pk).exists()


def test_bezeichnungen_beschreiben_die_wirkung() -> None:
    assert PERMISSIONS["motions.view_drafts"] == "Entwürfe anderer anzeigen"
    assert PERMISSIONS["motions.delete"] == "Anträge anderer löschen"
