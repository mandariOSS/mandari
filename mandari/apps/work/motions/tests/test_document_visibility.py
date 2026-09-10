# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sichtbarkeit in allen Dokumentlisten.

Private Dokumente anderer Mitglieder dürfen weder in Liste und Kacheln noch im
Dashboard, in Auswahlfeldern oder im Papierkorb erscheinen. Aus dem Papierkorb
dürfen fremde Dokumente weder wiederhergestellt noch endgültig gelöscht werden.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.dashboard.views import DashboardView
from apps.work.motions.forms import MotionForm
from apps.work.motions.models import Motion, MotionShare

PERMISSIONS = ["motions.view", "motions.create", "motions.edit", "motions.comment"]


@pytest.fixture
def author(org: Any, make_member: Any) -> Any:
    return make_member(org, PERMISSIONS, email="autorin@example.org")


@pytest.fixture
def colleague(org: Any, make_member: Any) -> Any:
    return make_member(org, PERMISSIONS, email="kollege@example.org")


@pytest.fixture
def documents(org: Any, author: Any, colleague: Any) -> dict[str, Motion]:
    private = Motion.objects.create(
        organization=org, author=author, title="Vertraulicher Entwurf", visibility="private"
    )
    public = Motion.objects.create(organization=org, author=author, title="Offener Antrag", visibility="organization")
    shared = Motion.objects.create(organization=org, author=author, title="Geteilter Antrag", visibility="shared")
    MotionShare.objects.create(motion=shared, scope="user", user=colleague.user, level="view", created_by=author.user)
    return {"private": private, "public": public, "shared": shared}


def move_to_trash(motion: Motion) -> Motion:
    motion.status = "deleted"
    motion.deleted_at = timezone.now()
    motion.save(update_fields=["status", "deleted_at"])
    return motion


def url(name: str, org: Any, motion: Motion | None = None) -> str:
    kwargs: dict[str, Any] = {"org_slug": org.slug}
    if motion is not None:
        kwargs["motion_id"] = motion.id
    return reverse(f"work:{name}", kwargs=kwargs)


def parent_choices(form: Any) -> set[str]:
    return set(cast(Any, form.fields["parent_motion"]).queryset.values_list("title", flat=True))


# -- Liste und Kacheln -----------------------------------------------------------


@pytest.mark.django_db
def test_liste_zeigt_keine_fremden_privaten_dokumente(
    org: Any, colleague: Any, documents: dict[str, Motion], client_for: Any
) -> None:
    response = client_for(colleague.user).get(url("documents", org))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Offener Antrag" in html
    assert "Geteilter Antrag" in html
    assert "Vertraulicher Entwurf" not in html


@pytest.mark.django_db
def test_kacheln_und_ordnerzaehler_zaehlen_dieselbe_sichtbare_menge(
    org: Any, colleague: Any, documents: dict[str, Motion], client_for: Any
) -> None:
    response = client_for(colleague.user).get(url("documents", org))

    assert response.context["stats"]["total"] == 2
    assert response.context["root_document_count"] == 2


@pytest.mark.django_db
def test_autorin_sieht_ihre_privaten_dokumente(
    org: Any, author: Any, documents: dict[str, Motion], client_for: Any
) -> None:
    response = client_for(author.user).get(url("documents", org))

    assert "Vertraulicher Entwurf" in response.content.decode()
    assert response.context["stats"]["total"] == 3


@pytest.mark.django_db
def test_privater_aenderungsantrag_bleibt_unter_sichtbarem_hauptantrag_verborgen(
    org: Any, author: Any, colleague: Any, documents: dict[str, Motion], client_for: Any
) -> None:
    Motion.objects.create(
        organization=org,
        author=author,
        title="Privater Zusatz",
        visibility="private",
        parent_motion=documents["public"],
    )
    html = client_for(colleague.user).get(url("documents", org)).content.decode()

    assert "Offener Antrag" in html
    assert "Privater Zusatz" not in html


# -- Dashboard und Auswahlfelder -------------------------------------------------


@pytest.mark.django_db
def test_dashboard_zeigt_keine_fremden_privaten_dokumente(
    org: Any, colleague: Any, documents: dict[str, Motion]
) -> None:
    view = cast(Any, DashboardView())
    view.organization = org
    view.membership = colleague
    titles = {motion.title for motion in view.get_recent_documents()}

    assert "Vertraulicher Entwurf" not in titles
    assert {"Offener Antrag", "Geteilter Antrag"} <= titles


@pytest.mark.django_db
def test_auswahl_des_hauptantrags_enthaelt_keine_fremden_privaten(
    org: Any, colleague: Any, documents: dict[str, Motion]
) -> None:
    form = cast(Any, MotionForm)(organization=org, membership=colleague)

    assert "Vertraulicher Entwurf" not in parent_choices(form)
    assert "Offener Antrag" in parent_choices(form)


@pytest.mark.django_db
def test_gesetzter_hauptantrag_bleibt_beim_bearbeiten_waehlbar(
    org: Any, author: Any, colleague: Any, documents: dict[str, Motion]
) -> None:
    amendment = Motion(organization=org, author=author, title="Zusatz", parent_motion=documents["private"])
    form = cast(Any, MotionForm)(instance=amendment, organization=org, membership=colleague)

    assert "Vertraulicher Entwurf" in parent_choices(form)


# -- Papierkorb --------------------------------------------------------------------


@pytest.mark.django_db
def test_papierkorb_zeigt_keine_fremden_privaten_dokumente(
    org: Any, colleague: Any, documents: dict[str, Motion], client_for: Any
) -> None:
    move_to_trash(documents["private"])
    own = move_to_trash(Motion.objects.create(organization=org, author=colleague, title="Eigener Entwurf"))

    html = client_for(colleague.user).get(url("document_trash", org)).content.decode()

    assert own.title in html
    assert "Vertraulicher Entwurf" not in html


@pytest.mark.django_db
def test_fremdes_privates_dokument_ist_weder_wiederherstellbar_noch_loeschbar(
    org: Any, colleague: Any, documents: dict[str, Motion], client_for: Any
) -> None:
    private = move_to_trash(documents["private"])
    client = client_for(colleague.user)

    assert client.post(url("document_restore", org, private)).status_code == 404
    assert client.post(url("document_permanent_delete", org, private)).status_code == 404
    private.refresh_from_db()
    assert private.status == "deleted"


@pytest.mark.django_db
def test_organisationsweites_dokument_darf_wiederhergestellt_werden(
    org: Any, colleague: Any, documents: dict[str, Motion], client_for: Any
) -> None:
    public = move_to_trash(documents["public"])

    response = client_for(colleague.user).post(url("document_restore", org, public))

    assert response.status_code == 302
    public.refresh_from_db()
    assert public.status == "draft"


@pytest.mark.django_db
def test_endgueltig_loeschen_nur_durch_autorin(
    org: Any, author: Any, colleague: Any, documents: dict[str, Motion], client_for: Any
) -> None:
    public = move_to_trash(documents["public"])

    assert client_for(colleague.user).post(url("document_permanent_delete", org, public)).status_code == 403
    assert Motion.objects.filter(pk=public.pk).exists()

    assert client_for(author.user).post(url("document_permanent_delete", org, public)).status_code == 302
    assert not Motion.objects.filter(pk=public.pk).exists()


@pytest.mark.django_db
def test_papierkorb_leeren_loescht_nur_eigene_dokumente(
    org: Any, colleague: Any, documents: dict[str, Motion], client_for: Any
) -> None:
    private = move_to_trash(documents["private"])
    public = move_to_trash(documents["public"])
    own = move_to_trash(Motion.objects.create(organization=org, author=colleague, title="Eigener Entwurf"))

    response = client_for(colleague.user).post(url("document_empty_trash", org))

    assert response.status_code == 302
    assert not Motion.objects.filter(pk=own.pk).exists()
    assert Motion.objects.filter(pk=private.pk).exists()
    assert Motion.objects.filter(pk=public.pk).exists()
