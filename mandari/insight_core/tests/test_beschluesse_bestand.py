# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Umsetzung der Beschlüsse im Bürgerportal aus dem RIS-Bestand (Issue #538, ``RIS_DECISION_TRACKING=bestand``).

Reiter und Detailseite lesen die Beschlüsse über die Lese-Fassade; die Detailseite heißt nach dem Tagesordnungspunkt im
Bestand. Abos gelten weiter dem Session-Beschluss dahinter, Adressen mit dessen Kennung (Abo-Mails) bleiben gültig.
Ein Beschluss aus einer fremden Quelle erscheint ebenso, nur ohne Abo.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionTenant
from insight_core import publication
from insight_core.models import (
    DecisionSubscription,
    OParlAgendaItem,
    OParlBody,
    OParlMeeting,
    OParlOrganization,
    OParlSource,
)
from insight_core.services import decision_tracking

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _leerer_cache() -> Any:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def welt() -> dict[str, Any]:
    """Veröffentlichender Session-Mandant mit gespiegeltem Beschluss im Bestand und eine Kommune mit fremdem RIS."""
    tenant = SessionTenant.objects.create(
        name="Bezirk Süd",
        slug="sued",
        insight_publish=True,
        implementation_publish=True,
        oparl_public_since=timezone.now(),
    )
    source = OParlSource.objects.get(sync_config__session_tenant="sued")
    basis = source.url
    body = OParlBody.objects.create(external_id=f"{basis}body/", source=source, name="Bezirk Süd", slug="bezirk-sued")
    tenant.oparl_body = body
    tenant.save(update_fields=["oparl_body"])

    gremium = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant,
        name="Bauausschuss",
        organization=gremium,
        start=timezone.now() - timedelta(days=3),
        is_public=True,
    )
    beschluss = SessionAgendaItem.objects.create(
        meeting=sitzung,
        number="1",
        order=1,
        name="Radweg Uferstraße",
        vote_result="approved",
        implementation_public=True,
        implementation_status="in_progress",
        implementation_public_note="Die Bauarbeiten haben begonnen.",
    )

    # Spiegel derselben Sitzung im RIS-Bestand, wie ihn der Abruf der Session-Schnittstelle schreibt
    ausschuss = OParlOrganization.objects.create(
        external_id=f"{basis}organization/{gremium.pk}/", body=body, name="Bauausschuss"
    )
    oparl_sitzung = OParlMeeting.objects.create(
        external_id=f"{basis}meeting/{sitzung.pk}/", body=body, name="Bauausschuss", start=sitzung.start
    )
    oparl_sitzung.organizations.set([ausschuss])
    punkt = OParlAgendaItem.objects.create(
        external_id=f"{basis}agendaitem/{beschluss.pk}/",
        meeting=oparl_sitzung,
        number="1",
        order=1,
        name="Radweg Uferstraße",
        vote_result="approved",
        implementation_status="in_progress",
        implementation_public_note="Die Bauarbeiten haben begonnen.",
        implementation_modified=timezone.now(),
    )
    OParlAgendaItem.objects.create(
        external_id=f"{basis}agendaitem/{uuid.uuid4()}/",
        meeting=oparl_sitzung,
        number="2",
        order=2,
        name="Ohne veröffentlichten Stand",
        vote_result="approved",
    )

    fremd_quelle = OParlSource.objects.create(name="Fremd", url="https://ris.fremd.example/oparl/system")
    fremd = OParlBody.objects.create(
        external_id="https://ris.fremd.example/oparl/body/1", source=fremd_quelle, name="Fremdstadt", slug="fremdstadt"
    )
    fremde_sitzung = OParlMeeting.objects.create(
        external_id="https://ris.fremd.example/oparl/meeting/1", body=fremd, name="Rat", start=timezone.now()
    )
    fremder_punkt = OParlAgendaItem.objects.create(
        external_id="https://ris.fremd.example/oparl/agendaitem/1",
        meeting=fremde_sitzung,
        number="1",
        name="Fremder Beschluss",
        vote_result="approved",
        implementation_status="done",
    )
    return {
        "tenant": tenant,
        "source": source,
        "body": body,
        "beschluss": beschluss,
        "punkt": punkt,
        "ausschuss": ausschuss,
        "fremd": fremd,
        "fremder_punkt": fremder_punkt,
    }


def _mit_kommune(body: OParlBody) -> Client:
    client = Client()
    client.get(f"/insight/kommune/{body.pk}/")
    return client


def test_reiter_umsetzung_aus_dem_bestand(welt: dict[str, Any], settings: Any) -> None:
    settings.RIS_DECISION_TRACKING = "bestand"

    antwort = _mit_kommune(welt["body"]).get("/insight/beschluesse/umsetzung/")

    assert antwort.status_code == 200
    html = antwort.content.decode()
    assert "Radweg Uferstraße" in html and "Die Bauarbeiten haben begonnen." in html
    assert f"/insight/beschluesse/{welt['punkt'].pk}/" in html
    assert "Ohne veröffentlichten Stand" not in html
    assert antwort.context["stats"]["in_progress"] == 1 and antwort.context["stats"]["total"] == 1
    assert [o.name for o in antwort.context["organizations"]] == ["Bauausschuss"]
    assert decision_tracking.has_public_decisions(welt["body"]) is True
    gefiltert = _mit_kommune(welt["body"]).get("/insight/beschluesse/umsetzung/", {"status": "done"})
    assert list(gefiltert.context["items"]) == []


def test_detailseite_aus_dem_bestand_mit_abo(welt: dict[str, Any], settings: Any) -> None:
    settings.RIS_DECISION_TRACKING = "bestand"
    adresse = f"/insight/beschluesse/{welt['punkt'].pk}/"
    client = Client()

    antwort = client.get(adresse)

    assert antwort.status_code == 200
    html = antwort.content.decode()
    assert "Radweg Uferstraße" in html and "Die Bauarbeiten haben begonnen." in html and "Bauausschuss" in html
    assert antwort.context["can_subscribe"] is True

    abo = client.post(adresse, {"email": "buerger@example.org", "privacy": "on"})

    assert abo.status_code == 302 and abo["Location"] == adresse
    # Das Abo gilt dem Session-Beschluss, dessen Änderungen die Meldungen auslösen
    assert DecisionSubscription.objects.filter(agenda_item=welt["beschluss"], email="buerger@example.org").exists()


def test_adresse_mit_session_kennung_bleibt_gueltig(welt: dict[str, Any], settings: Any) -> None:
    settings.RIS_DECISION_TRACKING = "bestand"

    antwort = Client().get(f"/insight/beschluesse/{welt['beschluss'].pk}/")

    assert antwort.status_code == 200
    assert "Radweg Uferstraße" in antwort.content.decode()


def test_fremde_quelle_ohne_abo(welt: dict[str, Any], settings: Any) -> None:
    settings.RIS_DECISION_TRACKING = "bestand"

    antwort = Client().get(f"/insight/beschluesse/{welt['fremder_punkt'].pk}/")

    assert antwort.status_code == 200
    assert "Fremder Beschluss" in antwort.content.decode()
    assert antwort.context["can_subscribe"] is False
    abo = Client().post(
        f"/insight/beschluesse/{welt['fremder_punkt'].pk}/", {"email": "fremd@example.org", "privacy": "on"}
    )
    assert abo.status_code == 302
    assert not DecisionSubscription.objects.filter(email="fremd@example.org").exists()


def test_stand_der_kommune_gilt_auch_fuer_beschluesse_aus_dem_bestand(welt: dict[str, Any], settings: Any) -> None:
    settings.RIS_DECISION_TRACKING = "bestand"
    publication.set_source_state(welt["source"], publication.PAUSED)

    assert Client().get(f"/insight/beschluesse/{welt['punkt'].pk}/").status_code == 503

    publication.set_source_state(welt["source"], publication.ARCHIVED)
    archiv = Client().get(f"/insight/beschluesse/{welt['punkt'].pk}/")
    assert archiv.status_code == 200 and archiv.context["can_subscribe"] is False


def test_ohne_schalter_kennt_die_detailseite_nur_session_kennungen(welt: dict[str, Any], settings: Any) -> None:
    assert settings.RIS_DECISION_TRACKING == "session"

    assert Client().get(f"/insight/beschluesse/{welt['punkt'].pk}/").status_code == 404
    assert Client().get(f"/insight/beschluesse/{welt['beschluss'].pk}/").status_code == 200
    OParlAgendaItem.objects.filter(pk=welt["punkt"].pk).update(implementation_status=None)
    # Der Reiter liest weiter Session: Der Beschluss bleibt, auch wenn der Bestand ihn (noch) nicht kennt
    assert decision_tracking.has_public_decisions(welt["body"]) is True
