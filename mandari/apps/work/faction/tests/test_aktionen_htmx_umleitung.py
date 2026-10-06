# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Umleitungen nach HTMX-Aktionen einer Fraktionssitzung (Issue #895).

htmx folgt einer 302 selbst und tauscht die ganze Zielseite samt Rahmen in den Teilbereich, aus dem die Aktion kam
(nach dem Löschen stand die Liste in der Seitenleiste der Sitzung). Bei HTMX-Anfragen antwortet der Aktions-Endpunkt
deshalb mit ``HX-Redirect``; der Browser lädt die Zielseite vollständig. Ohne HTMX bleibt es bei der 302.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.faction.models import FactionMeeting

HTMX = {"HTTP_HX_REQUEST": "true"}
VORSITZ_RECHTE = ["faction.view_public", "faction.create", "faction.manage", "faction.start"]


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    return make_member(org, VORSITZ_RECHTE, email="vorsitz@example.org")


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["faction.view_public"], email="mitglied@example.org")


@pytest.fixture
def sitzung(org: Any, vorsitz: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() + timedelta(days=3),
        status="planned",
        created_by=vorsitz,
    )


def _aktion(client: Any, meeting: FactionMeeting, *, htmx: bool, **daten: str) -> Any:
    url = reverse("work:faction_action", kwargs={"org_slug": meeting.organization.slug, "meeting_id": meeting.id})
    return client.post(url, daten, **(HTMX if htmx else {}))


def _liste(org: Any) -> str:
    return reverse("work:faction", kwargs={"org_slug": org.slug})


def _detail(meeting: FactionMeeting) -> str:
    return reverse("work:faction_detail", kwargs={"org_slug": meeting.organization.slug, "meeting_id": meeting.id})


@pytest.mark.django_db
@pytest.mark.parametrize("neuer_rahmen", [False, True], ids=["alter-rahmen", "neuer-rahmen"])
def test_htmx_loeschen_leitet_per_hx_redirect_auf_die_liste(
    org: Any, vorsitz: Any, sitzung: FactionMeeting, client_for: Any, neuer_rahmen: bool
) -> None:
    org.work_new_design = neuer_rahmen
    org.save(update_fields=["work_new_design"])

    response = _aktion(client_for(vorsitz.user), sitzung, htmx=True, action="delete")

    assert response.status_code == 200
    assert response["HX-Redirect"] == _liste(org)
    assert not response.has_header("Location"), "keine 302, der htmx folgen und die Liste einsetzen würde"
    assert response.content == b"", "keine Vollseite als Teilantwort"
    assert not FactionMeeting.objects.filter(id=sitzung.id).exists()


@pytest.mark.django_db
def test_ohne_htmx_bleibt_die_umleitung_auf_die_liste(
    org: Any, vorsitz: Any, sitzung: FactionMeeting, client_for: Any
) -> None:
    response = _aktion(client_for(vorsitz.user), sitzung, htmx=False, action="delete")

    assert response.status_code == 302
    assert response["Location"] == _liste(org)
    assert not FactionMeeting.objects.filter(id=sitzung.id).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "daten",
    [
        {"action": "delete"},
        {"action": "cancel"},
        {"action": "start"},
        {"action": "update_status", "status": "cancelled"},
    ],
    ids=lambda daten: daten["action"],
)
def test_htmx_aktion_ohne_recht_leitet_per_hx_redirect_auf_die_sitzung(
    mitglied: Any, sitzung: FactionMeeting, client_for: Any, daten: dict[str, str]
) -> None:
    response = _aktion(client_for(mitglied.user), sitzung, htmx=True, **daten)

    assert response.status_code == 200
    assert response["HX-Redirect"] == _detail(sitzung)
    assert response.content == b""
    sitzung.refresh_from_db()
    assert sitzung.status == "planned"


@pytest.mark.django_db
def test_htmx_absagen_laedt_die_sitzung_weiter_neu(vorsitz: Any, sitzung: FactionMeeting, client_for: Any) -> None:
    response = _aktion(client_for(vorsitz.user), sitzung, htmx=True, action="cancel")

    assert response.status_code == 200
    assert response["HX-Refresh"] == "true"
    sitzung.refresh_from_db()
    assert sitzung.status == "cancelled"
