# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Der Reiter der Gremien- und Personenseite (``?tab=``) kommt nur aus der festen Liste der Seite.

Der Wert steht in der Vorlage in einem Alpine-Ausdruck; ein beliebiger Text aus der Adresse würde dort als Skript
ausgewertet.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.urls import reverse

from insight_core.models import OParlBody, OParlOrganization, OParlPerson, OParlSource

RIS = "https://ris.example.org/oparl"


@pytest.fixture
def kommune(org: Any) -> OParlBody:
    source = OParlSource.objects.create(name="RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/bodies/{uuid.uuid4().hex}", source=source, name="Musterstadt")
    org.body = body
    org.save(update_fields=["body"])
    return body


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "modell", "kwarg", "vorgabe", "bekannt"),
    [
        ("work:ris_organization_detail", OParlOrganization, "org_id", "members", "calendar"),
        ("work:ris_person_detail", OParlPerson, "person_id", "memberships", "info"),
    ],
)
def test_reiter_aus_der_adresse_nur_aus_der_festen_liste(
    org: Any,
    kommune: OParlBody,
    make_member: Any,
    client_for: Any,
    name: str,
    modell: Any,
    kwarg: str,
    vorgabe: str,
    bekannt: str,
) -> None:
    """Unbekannte Werte fallen auf die Vorgabe der Seite zurück."""
    objekt = modell.objects.create(external_id=f"{RIS}/{kwarg}/{uuid.uuid4().hex}", body=kommune, name="Hauptausschuss")
    mitglied = make_member(org, ["ris.view"], email="reiter@example.org")
    url = reverse(name, kwargs={"org_slug": org.slug, kwarg: objekt.pk})
    client = client_for(mitglied.user)

    fremd = client.get(url, {"tab": "x'};window.kaputt=1;({a:'"})
    assert fremd.status_code == 200
    assert fremd.context["active_tab"] == vorgabe
    html = fremd.content.decode()
    assert "window.kaputt" not in html
    assert f"x-data=\"{{ tab: '{vorgabe}' }}\"" in html

    gewaehlt = client.get(url, {"tab": bekannt})
    assert gewaehlt.context["active_tab"] == bekannt
    assert f"x-data=\"{{ tab: '{bekannt}' }}\"" in gewaehlt.content.decode()
