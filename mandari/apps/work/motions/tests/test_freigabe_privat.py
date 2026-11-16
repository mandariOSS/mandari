# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wechsel eines Dokuments auf „Privat“ bei bestehenden Freigaben an Gäste.

Persönliche Freigaben an Gäste gelten unabhängig von der Sichtbarkeit: Einzelne Dokumente lassen sich so auch privat
für einen Gast freigeben (Issue #582). Wer ein geteiltes Dokument auf „Privat“ stellt, soll deshalb wissen, wer danach
noch Zugriff hat:

- Ohne Wahl ändert der Teilen-Dialog nichts und nennt die Gäste mit persönlicher Freigabe.
- Mit der Wahl „beenden“ enden diese Freigaben, mit „behalten“ bleiben sie; erst dann wird das Dokument privat.
- Ohne Freigaben an Gäste braucht der Wechsel keine Wahl. An einem schon privaten Dokument ist sie freiwillig:
  Die gewollte Einzelfreigabe bleibt, bis jemand „beenden“ wählt.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.urls import reverse

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.motions import freigaben
from apps.work.motions.models import Motion, MotionShare

RECHTE = ["motions.view", "motions.view_drafts", "motions.create", "motions.edit", "motions.comment"]


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, [*RECHTE, "motions.share"], email="autorin@example.org")


@pytest.fixture
def kollegin(org: Any, make_member: Any) -> Any:
    return make_member(org, RECHTE, email="kollegin@example.org")


@pytest.fixture
def gast(org: Any) -> Any:
    konto = UserFactory(email="gast@example.org", first_name="Gerda", last_name="Gast")  # type: ignore[no-untyped-call]
    return MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]


@pytest.fixture
def dokument(org: Any, autorin: Any, kollegin: Any, gast: Any) -> Motion:
    """Geteiltes Dokument mit je einer persönlichen Freigabe an ein Mitglied und an einen Gast."""
    motion = Motion.objects.create(organization=org, author=autorin, title="Antrag", visibility="shared")
    for konto in (kollegin.user, gast.user):
        MotionShare.objects.create(motion=motion, scope="user", user=konto, level="edit", created_by=autorin.user)
    return motion


def _url(name: str, org: Any, motion: Motion) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug, "motion_id": motion.id})


def _freigaben(motion: Motion) -> set[str]:
    return set(MotionShare.objects.filter(motion=motion).values_list("user__email", flat=True))


@pytest.mark.django_db
def test_wechsel_auf_privat_ohne_wahl_nennt_die_gaeste_und_aendert_nichts(
    org: Any, autorin: Any, gast: Any, dokument: Motion, client_for: Any
) -> None:
    antwort = client_for(autorin.user).post(_url("document_share_update", org, dokument), {"visibility": "private"})

    assert antwort.status_code == 409
    assert antwort.json()["guests"] == ["Gerda Gast"]
    hinweis = json.loads(antwort["HX-Trigger"])["showToast"]
    assert hinweis["type"] == "error" and "Gerda Gast" in hinweis["message"]
    dokument.refresh_from_db()
    assert dokument.visibility == "shared"
    assert _freigaben(dokument) == {"kollegin@example.org", "gast@example.org"}
    assert dokument.can_edit(gast)


@pytest.mark.django_db
def test_unbekannte_wahl_gilt_nicht_als_wahl(org: Any, autorin: Any, dokument: Motion, client_for: Any) -> None:
    antwort = client_for(autorin.user).post(
        _url("document_share_update", org, dokument), {"visibility": "private", "guest_shares": "vielleicht"}
    )

    assert antwort.status_code == 409
    dokument.refresh_from_db()
    assert dokument.visibility == "shared"


@pytest.mark.django_db
def test_wechsel_auf_privat_mit_beenden_entzieht_den_gaesten_den_zugriff(
    org: Any, autorin: Any, gast: Any, dokument: Motion, client_for: Any
) -> None:
    antwort = client_for(autorin.user).post(
        _url("document_share_update", org, dokument),
        {"visibility": "private", "guest_shares": freigaben.GUEST_SHARES_REVOKE},
    )

    assert antwort.status_code == 204
    dokument.refresh_from_db()
    assert dokument.visibility == "private"
    # Die Freigabe an das Mitglied bleibt gespeichert; an einem privaten Dokument wirkt sie nicht
    assert _freigaben(dokument) == {"kollegin@example.org"}
    assert dokument.access_level(gast) == "none"
    assert client_for(gast.user).get(_url("document_editor", org, dokument)).status_code in (403, 404)


@pytest.mark.django_db
def test_wechsel_auf_privat_mit_behalten_laesst_die_freigaben(
    org: Any, autorin: Any, gast: Any, dokument: Motion, client_for: Any
) -> None:
    antwort = client_for(autorin.user).post(
        _url("document_share_update", org, dokument),
        {"visibility": "private", "guest_shares": freigaben.GUEST_SHARES_KEEP},
    )

    assert antwort.status_code == 204
    dokument.refresh_from_db()
    assert dokument.visibility == "private"
    assert _freigaben(dokument) == {"kollegin@example.org", "gast@example.org"}
    assert dokument.can_edit(gast)


@pytest.mark.django_db
def test_ohne_freigaben_an_gaeste_braucht_der_wechsel_keine_wahl(
    org: Any, autorin: Any, kollegin: Any, client_for: Any
) -> None:
    motion = Motion.objects.create(organization=org, author=autorin, title="Antrag", visibility="organization")
    MotionShare.objects.create(motion=motion, scope="user", user=kollegin.user, level="view", created_by=autorin.user)

    antwort = client_for(autorin.user).post(_url("document_share_update", org, motion), {"visibility": "private"})

    assert antwort.status_code == 204
    motion.refresh_from_db()
    assert motion.visibility == "private"


@pytest.mark.django_db
def test_privates_dokument_mit_einzelfreigabe_bleibt_ohne_wahl_speicherbar(
    org: Any, autorin: Any, gast: Any, client_for: Any
) -> None:
    """Kein Wechsel: Die gewollte Einzelfreigabe eines privaten Dokuments bleibt, wie sie ist."""
    motion = Motion.objects.create(organization=org, author=autorin, title="Antrag", visibility="private")
    MotionShare.objects.create(motion=motion, scope="user", user=gast.user, level="comment", created_by=autorin.user)

    antwort = client_for(autorin.user).post(_url("document_share_update", org, motion), {"visibility": "private"})

    assert antwort.status_code == 204
    assert motion.access_level(gast) == "comment"

    # Die Wahl „beenden“ gilt auch am schon privaten Dokument
    antwort = client_for(autorin.user).post(
        _url("document_share_update", org, motion),
        {"visibility": "private", "guest_shares": freigaben.GUEST_SHARES_REVOKE},
    )

    assert antwort.status_code == 204
    assert motion.access_level(gast) == "none"


@pytest.mark.django_db
def test_andere_sichtbarkeiten_beruehren_die_freigaben_nicht(
    org: Any, autorin: Any, gast: Any, dokument: Motion, client_for: Any
) -> None:
    antwort = client_for(autorin.user).post(
        _url("document_share_update", org, dokument),
        {"visibility": "organization", "guest_shares": freigaben.GUEST_SHARES_REVOKE},
    )

    assert antwort.status_code == 204
    dokument.refresh_from_db()
    assert dokument.visibility == "organization"
    assert _freigaben(dokument) == {"kollegin@example.org", "gast@example.org"}


@pytest.mark.django_db
def test_dialog_beschreibt_den_kreis_und_bietet_die_wahl(
    org: Any, autorin: Any, dokument: Motion, client_for: Any
) -> None:
    seite = client_for(autorin.user).get(_url("document_editor", org, dokument)).content.decode()

    assert 'name="guest_shares" value="revoke"' in seite
    assert 'name="guest_shares" value="keep"' in seite
    assert "Nur Autor:in, Federführung und Mitarbeit sehen dieses Dokument" in seite
    assert "Nur Sie können dieses Dokument sehen" not in seite
