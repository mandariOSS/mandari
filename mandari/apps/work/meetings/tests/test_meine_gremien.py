# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Meine Gremien“ in der Sitzungsliste nach derselben Regel wie das Dashboard (Issue #647).

Bevorzugt die selbst gewählten (gefolgten) Gremien, ersatzweise die von der Organisation zugewiesenen.
Wer nur Gremien folgt, sieht im Reiter „Meine Gremien“ deren Sitzungen – nicht den Hinweis, dass keine
Gremien zugewiesen seien.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.organization import selectors as organization_selectors
from insight_core.models import OParlBody, OParlMeeting, OParlOrganization, OParlSource

pytestmark = pytest.mark.django_db

KEIN_GREMIUM = "Keine Gremien ausgewählt"


@pytest.fixture
def ris(org: Any) -> dict[str, Any]:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt A")
    fremd = OParlBody.objects.create(external_id="https://ris.example.org/body/2", source=source, name="Stadt B")
    org.body = body
    org.save(update_fields=["body"])
    gremien = {
        name: OParlOrganization.objects.create(
            external_id=f"https://ris.example.org/organization/{name}", body=body, name=name
        )
        for name in ("Bauausschuss", "Jugendhilfeausschuss", "Rat")
    }
    gremien["Fremdausschuss"] = OParlOrganization.objects.create(
        external_id="https://ris.example.org/organization/fremd", body=fremd, name="Fremdausschuss"
    )
    start = timezone.now() + timedelta(days=3)
    for tage, name in enumerate(("Bauausschuss", "Jugendhilfeausschuss", "Rat")):
        sitzung = OParlMeeting.objects.create(
            external_id=f"https://ris.example.org/meeting/{name}",
            body=body,
            name=f"Sitzung {name}",
            start=start + timedelta(days=tage),
        )
        sitzung.organizations.add(gremien[name])
    return gremien


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["meetings.view", "dashboard.view"], email="gremien@example.org")


def _liste(client: Any, org: Any, view: str = "my") -> Any:
    antwort = client.get(reverse("work:meetings", kwargs={"org_slug": org.slug}), {"view": view})
    assert antwort.status_code == 200
    return antwort


def _sitzungen(antwort: Any) -> list[str]:
    return sorted(m.name for m in antwort.context["meetings"])


def test_nur_gefolgte_gremien_filtern_die_sitzungsliste(
    org: Any, mitglied: Any, ris: dict[str, Any], client_for: Any
) -> None:
    mitglied.followed_organizations.set([ris["Bauausschuss"], ris["Rat"]])

    antwort = _liste(client_for(mitglied.user), org)

    assert _sitzungen(antwort) == ["Sitzung Bauausschuss", "Sitzung Rat"]
    inhalt = antwort.content.decode()
    assert KEIN_GREMIUM not in inhalt
    assert "keine Gremien zugewiesen" not in inhalt and "Ihre zugewiesenen Gremien" not in inhalt
    assert "Gremien, denen Sie folgen" in inhalt
    assert [c.name for c in antwort.context["my_committees"]] == ["Bauausschuss", "Rat"]


def test_gefolgte_gremien_gehen_zugewiesenen_vor(org: Any, mitglied: Any, ris: dict[str, Any], client_for: Any) -> None:
    mitglied.followed_organizations.set([ris["Jugendhilfeausschuss"]])
    mitglied.oparl_committees.set([ris["Bauausschuss"]])

    antwort = _liste(client_for(mitglied.user), org)

    assert _sitzungen(antwort) == ["Sitzung Jugendhilfeausschuss"]


def test_ohne_gefolgte_gelten_die_zugewiesenen(org: Any, mitglied: Any, ris: dict[str, Any], client_for: Any) -> None:
    mitglied.oparl_committees.set([ris["Bauausschuss"]])

    antwort = _liste(client_for(mitglied.user), org)

    assert _sitzungen(antwort) == ["Sitzung Bauausschuss"]
    assert "Ihre zugewiesenen Gremien" in antwort.content.decode()


def test_ohne_gremien_hinweis_und_alle_sitzungen(org: Any, mitglied: Any, ris: dict[str, Any], client_for: Any) -> None:
    antwort = _liste(client_for(mitglied.user), org)

    assert len(antwort.context["meetings"]) == 3
    inhalt = antwort.content.decode()
    assert KEIN_GREMIUM in inhalt
    assert reverse("work:profile_committees", kwargs={"org_slug": org.slug}) in inhalt


def test_dashboard_und_sitzungsliste_zeigen_dieselben_gremien(
    org: Any, mitglied: Any, ris: dict[str, Any], client_for: Any
) -> None:
    mitglied.followed_organizations.set([ris["Rat"], ris["Bauausschuss"]])
    mitglied.oparl_committees.set([ris["Jugendhilfeausschuss"]])
    client = client_for(mitglied.user)

    dashboard = client.get(reverse("work:dashboard", kwargs={"org_slug": org.slug}))
    liste = _liste(client, org)

    assert dashboard.status_code == 200
    assert dashboard.context["dashboard_personalized"] is True
    namen = ["Bauausschuss", "Rat"]
    assert [c.name for c in dashboard.context["my_committees"]] == namen
    assert [c.name for c in liste.context["my_committees"]] == namen


def test_regel_meine_gremien(mitglied: Any, ris: dict[str, Any]) -> None:
    leer = organization_selectors.my_committees(mitglied)
    assert not leer and leer.source == "" and leer.ids == set()
    assert not organization_selectors.my_committees(None)

    mitglied.oparl_committees.set([ris["Rat"]])
    zugewiesen = organization_selectors.my_committees(mitglied)
    assert zugewiesen.source == organization_selectors.MY_COMMITTEES_ASSIGNED and not zugewiesen.followed
    assert zugewiesen.ids == {ris["Rat"].pk}

    mitglied.followed_organizations.set([ris["Fremdausschuss"], ris["Bauausschuss"]])
    gefolgt = organization_selectors.my_committees(mitglied)
    assert gefolgt.followed
    assert [c.name for c in gefolgt.committees] == ["Bauausschuss", "Fremdausschuss"]
    # In der Sitzungsliste zählen nur Gremien der verknüpften Körperschaften; die Herkunft bleibt
    eigene = gefolgt.within(OParlBody.objects.filter(pk=ris["Bauausschuss"].body_id))
    assert [c.name for c in eigene.committees] == ["Bauausschuss"] and eigene.followed
