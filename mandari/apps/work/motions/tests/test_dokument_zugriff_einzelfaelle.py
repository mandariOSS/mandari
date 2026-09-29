# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einzelfälle der einheitlichen Zugriffsregel für Dokumente (Ergänzung zu test_dokument_zugriffsmatrix.py).

- Autor:in ohne ``motions.edit`` kommentiert nur.
- Federführung und Mitarbeit öffnen das Dokument – wer damit jemandem erst Zugang gibt, braucht das
  Freigaberecht.
- Freigabe-Entscheidungen nur mit Zugang; Gast-Verwaltung darf Gastfreigaben weiterhin entziehen.
- Übersicht „Freigegebene Dokumente“ und Gast-Einladung zeigen nur, was die Person selbst sehen darf.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import reverse

from apps.work.motions.models import Motion, MotionApproval, MotionShare
from apps.work.organization import selectors as organization_selectors

MITGLIED = ["motions.view", "motions.create", "motions.edit", "motions.comment", "motions.share"]


def _dokument(org: Any, autor: Any, sichtbarkeit: str, titel: str = "Radweg") -> Motion:
    return Motion.objects.create(organization=org, author=autor, title=titel, visibility=sichtbarkeit)


def _xhr(client: Any, url: str, daten: dict[str, Any]) -> Any:
    return client.post(url, daten, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


@pytest.mark.django_db
def test_autorin_ohne_bearbeitungsrecht_kommentiert_nur(org: Any, make_member: Any, client_for: Any) -> None:
    autorin = make_member(org, ["motions.view", "motions.comment"], email="autorin@example.org")
    dokument = _dokument(org, autorin, "private")

    assert dokument.access_level(autorin) == "comment"
    assert not dokument.can_edit(autorin)
    antwort = _xhr(
        client_for(autorin.user),
        reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": dokument.id}),
        {"action": "save", "title": "Radweg", "content": "<p>neu</p>"},
    )
    assert antwort.status_code == 403


@pytest.mark.django_db
def test_zuweisung_ohne_zugang_ist_eine_freigabe(org: Any, make_member: Any, client_for: Any) -> None:
    autorin = make_member(org, MITGLIED, email="autorin@example.org")
    bearbeiter = make_member(org, MITGLIED, email="bearbeiter@example.org")
    aussen = make_member(org, MITGLIED, email="aussen@example.org")
    dokument = _dokument(org, autorin, "shared")
    MotionShare.objects.create(
        motion=dokument, scope="user", user=bearbeiter.user, level="edit", created_by=autorin.user
    )
    url = reverse("work:document_meta", kwargs={"org_slug": org.slug, "motion_id": dokument.id})
    assert dokument.can_edit(bearbeiter) and not dokument.can_share(bearbeiter)

    antwort = _xhr(client_for(bearbeiter.user), url, {"action": "set_contributors", "contributors": [str(aussen.id)]})
    assert antwort.status_code == 403
    assert not dokument.contributors.exists()
    antwort = _xhr(client_for(bearbeiter.user), url, {"action": "set_responsible", "responsible": str(aussen.id)})
    assert antwort.status_code == 403

    # Mit Freigaberecht (Autorin) ist die Zuweisung eine gewollte Freigabe
    antwort = _xhr(client_for(autorin.user), url, {"action": "set_contributors", "contributors": [str(aussen.id)]})
    assert antwort.status_code == 200
    assert dokument.can_access(aussen)
    # Wer schon Zugang hat, darf ohne Freigaberecht zugewiesen werden
    antwort = _xhr(client_for(bearbeiter.user), url, {"action": "set_responsible", "responsible": str(aussen.id)})
    assert antwort.status_code == 200


@pytest.mark.django_db
def test_freigabe_entscheiden_nur_mit_zugang(org: Any, make_member: Any, client_for: Any) -> None:
    autorin = make_member(org, MITGLIED, email="autorin@example.org")
    pruefer = make_member(org, MITGLIED, email="pruefer@example.org")
    dokument = _dokument(org, autorin, "private")
    freigabe = MotionApproval.objects.create(motion=dokument, approver=pruefer, approval_type="chair")

    antwort = _xhr(
        client_for(pruefer.user),
        reverse(
            "work:document_approval_decide",
            kwargs={"org_slug": org.slug, "motion_id": dokument.id, "approval_id": freigabe.id},
        ),
        {"decision": "approve"},
    )

    assert antwort.status_code == 403
    freigabe.refresh_from_db()
    assert freigabe.approved is None


@pytest.mark.django_db
def test_gastverwaltung_entzieht_gastfreigaben(org: Any, make_member: Any, client_for: Any) -> None:
    autorin = make_member(org, MITGLIED, email="autorin@example.org")
    verwaltung = make_member(org, ["motions.view", "motions.share", "guests.manage"], email="gaeste@example.org")
    gast = make_member(org, [], email="gast@example.org")
    gast.is_guest = True
    gast.save(update_fields=["is_guest"])
    dokument = _dokument(org, autorin, "private")
    freigabe = MotionShare.objects.create(
        motion=dokument, scope="user", user=gast.user, level="view", created_by=autorin.user
    )

    antwort = client_for(verwaltung.user).post(
        reverse("work:document_share_remove", kwargs={"org_slug": org.slug, "share_id": freigabe.id})
    )

    assert antwort.status_code == 204
    assert not MotionShare.objects.filter(pk=freigabe.pk).exists()


@pytest.mark.django_db
def test_uebersicht_zeigt_keine_freigaben_privater_dokumente(org: Any, make_member: Any, client_for: Any) -> None:
    autorin = make_member(org, MITGLIED, email="autorin@example.org")
    mitglied = make_member(org, MITGLIED, email="mitglied@example.org")
    privat = _dokument(org, autorin, "private", titel="Wieder privat")
    geteilt = _dokument(org, autorin, "shared", titel="Weiter geteilt")
    for dokument in (privat, geteilt):
        MotionShare.objects.create(
            motion=dokument, scope="user", user=mitglied.user, level="view", created_by=autorin.user
        )

    html = client_for(mitglied.user).get(reverse("work:guest_documents", kwargs={"org_slug": org.slug})).content
    assert "Weiter geteilt" in html.decode()
    assert "Wieder privat" not in html.decode()


@pytest.mark.django_db
def test_gast_einladung_bietet_nur_sichtbare_dokumente_an(org: Any, make_member: Any) -> None:
    autorin = make_member(org, MITGLIED, email="autorin@example.org")
    ohne_sicht = make_member(org, ["guests.invite"], email="einladend@example.org")
    mit_sicht = make_member(org, ["guests.invite", "motions.view"], email="sieht@example.org")
    org_weit = _dokument(org, autorin, "organization")

    assert list(organization_selectors.shareable_documents(org, ohne_sicht)) == []
    assert list(organization_selectors.shareable_documents(org, mit_sicht)) == [org_weit]


@pytest.mark.django_db
def test_freigabeanfrage_an_personen_ohne_zugang_braucht_das_freigaberecht(
    org: Any, make_member: Any, client_for: Any
) -> None:
    """Die Anfrage legt für die angefragte Person eine Freigabe an – wie der Teilen-Dialog nur mit ``can_share``."""
    autorin = make_member(org, ["motions.view", "motions.edit", "motions.comment"], email="autorin@example.org")
    pruefer = make_member(org, MITGLIED, email="pruefer@example.org")
    privat = _dokument(org, autorin, "private")
    offen = _dokument(org, autorin, "organization", titel="Offen")

    def anfrage(dokument: Motion) -> Any:
        return _xhr(
            client_for(autorin.user),
            reverse("work:document_approval_request", kwargs={"org_slug": org.slug, "motion_id": dokument.id}),
            {"approver": str(pruefer.id), "approval_type": "chair"},
        )

    assert anfrage(privat).status_code == 403
    assert not privat.can_access(pruefer)
    # Hat die angefragte Person schon Zugang, ist die Anfrage keine Freigabe
    assert anfrage(offen).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("sichtbarkeit", ["private", "shared", "organization"])
def test_federfuehrung_bearbeitet_ihr_dokument_mit_bearbeitungsrecht(
    org: Any, make_member: Any, client_for: Any, sichtbarkeit: str
) -> None:
    """Federführung und Mitarbeit behalten Status, Metadaten und Inhalt – sofern sie ``motions.edit`` haben."""
    autorin = make_member(org, MITGLIED, email="autorin@example.org")
    zustaendig = make_member(org, MITGLIED, email="zustaendig@example.org")
    ohne_recht = make_member(org, ["motions.view", "motions.comment"], email="ohne@example.org")
    dokument = Motion.objects.create(
        organization=org, author=autorin, title="Radweg", visibility=sichtbarkeit, responsible=zustaendig
    )
    status_url = reverse("work:document_status", kwargs={"org_slug": org.slug, "motion_id": dokument.id})

    assert dokument.access_level(zustaendig) == "edit"
    assert _xhr(client_for(zustaendig.user), status_url, {"status": "internal_review"}).status_code == 200
    # Teilen und endgültiges Löschen bleiben Autor:in bzw. motions.edit_all vorbehalten
    assert not dokument.can_share(zustaendig)
    assert not dokument.can_manage(zustaendig)

    Motion.objects.filter(pk=dokument.pk).update(responsible=ohne_recht)
    dokument.refresh_from_db()
    assert dokument.access_level(ohne_recht) == "comment"
    assert not dokument.can_edit(ohne_recht)
