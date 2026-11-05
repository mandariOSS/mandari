# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsarten (Issue #1231, Epic #1226): Grundlage ohne sichtbare Änderung.

- Jede Organisation hat genau eine Standardart „Fraktionssitzung“; „Standardart holen oder anlegen“ legt nie doppelt an.
- Neue Reihen, Sitzungen und Standard-TOPs ohne Art bekommen die Standardart, von Hand angelegte Sitzungen auch.
- Nummern, Neunummerierung und die vorherige Sitzung gelten je Art; Sitzungen aus einer Reihe übernehmen ihre Art.
- Eine Art mit Sitzungen lässt sich nicht löschen, die Organisation samt Arten schon.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from unittest import mock

import pytest
from django.contrib import admin
from django.db import IntegrityError, models, transaction
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import OrganizationFactory, UserFactory
from apps.tenants.models import Organization
from apps.work.faction.generation import generate_meetings_for_schedule, renumber_upcoming
from apps.work.faction.models import (
    FactionMeeting,
    FactionMeetingSchedule,
    FactionMeetingType,
    FactionStandardAgendaItem,
)
from apps.work.organization.export_service import dsgvo_export_service

# Montag, 05.10.2026, 12:00 Uhr Ortszeit (Sommerzeit)
JETZT = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)

VORSITZ_RECHTE = ["faction.view_public", "faction.view_non_public", "faction.create", "faction.manage"]


@pytest.fixture(autouse=True)
def _horizont(settings: Any) -> None:
    settings.FACTION_SCHEDULE_HORIZON_DAYS = 21


@pytest.fixture
def standardart(org: Any) -> FactionMeetingType:
    return FactionMeetingType.objects.get(organization=org, is_default=True)


@pytest.fixture
def arbeitsgruppe(org: Any) -> FactionMeetingType:
    return FactionMeetingType.objects.create(organization=org, name="Arbeitsgruppe Umwelt", position=1)


def _sitzung(org: Any, art: FactionMeetingType | None, start: datetime, **felder: Any) -> FactionMeeting:
    werte: dict[str, Any] = {"title": "Sitzung", "status": "completed", **felder}
    if art is not None:
        werte["meeting_type"] = art
    return FactionMeeting.objects.create(organization=org, start=start, **werte)


# =============================================================================
# Standardart
# =============================================================================


@pytest.mark.django_db
def test_neue_organisation_hat_genau_eine_standardart() -> None:
    org = cast(Any, OrganizationFactory)(name="Neue Fraktion")

    arten = list(FactionMeetingType.objects.filter(organization=org))

    assert len(arten) == 1
    art = arten[0]
    assert (art.name, art.is_default, art.is_active, art.number_start) == ("Fraktionssitzung", True, True, 1)
    assert (art.participants_mode, art.visibility, art.agenda_mode) == ("all", "members", "standard")
    assert art.non_public_enabled is True
    assert art.voting_enabled is True
    assert art.settings == {}
    assert art.participants_role is None
    assert art.participants_committee is None
    assert not art.participants.exists()


@pytest.mark.django_db
def test_standardart_holen_legt_nie_doppelt_an(org: Any, standardart: FactionMeetingType) -> None:
    assert FactionMeetingType.default_for(org) == standardart
    assert FactionMeetingType.default_for(org.pk) == standardart

    # Gleichzeitiger Aufruf: Die Suche fand noch nichts, der andere hat inzwischen angelegt
    with mock.patch.object(models.QuerySet, "first", return_value=None):
        assert FactionMeetingType.default_for(org) == standardart

    assert FactionMeetingType.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
def test_fehlende_standardart_wird_einmal_angelegt(org: Any, standardart: FactionMeetingType) -> None:
    standardart.delete()

    erste = FactionMeetingType.default_for(org)
    zweite = FactionMeetingType.default_for(org)

    assert erste == zweite
    assert erste.is_default
    assert erste.name == "Fraktionssitzung"
    assert FactionMeetingType.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
def test_zweite_standardart_verhindert_der_constraint(org: Any, standardart: FactionMeetingType) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        FactionMeetingType.objects.create(organization=org, name="Noch eine", is_default=True)

    # Weitere Arten ohne Standard gehen beliebig
    FactionMeetingType.objects.create(organization=org, name="AG 1")
    FactionMeetingType.objects.create(organization=org, name="AG 2")
    assert FactionMeetingType.objects.filter(organization=org, is_default=True).get() == standardart


@pytest.mark.django_db
def test_neue_reihe_sitzung_und_standard_top_bekommen_die_standardart(
    org: Any, standardart: FactionMeetingType, arbeitsgruppe: FactionMeetingType
) -> None:
    reihe = FactionMeetingSchedule.objects.create(organization=org, name="Reihe", weekday=0, time=time(18, 0))
    punkt = FactionStandardAgendaItem.objects.create(organization=org, title="Termine")
    sitzung = FactionMeeting.objects.create(organization=org, title="Sitzung", start=JETZT)
    ag = FactionMeeting.objects.create(organization=org, title="AG", start=JETZT, meeting_type=arbeitsgruppe)

    assert reihe.meeting_type == standardart
    assert punkt.meeting_type == standardart
    assert sitzung.meeting_type == standardart
    assert ag.meeting_type == arbeitsgruppe, "eine angegebene Art bleibt"


@pytest.mark.django_db
def test_andere_organisation_zaehlt_eigene_standardart(org: Any, standardart: FactionMeetingType) -> None:
    andere = cast(Any, OrganizationFactory)(name="Andere Fraktion")
    _sitzung(org, None, JETZT, meeting_number=7)

    sitzung = FactionMeeting.objects.create(organization=andere, title="Erste", start=JETZT)

    assert sitzung.meeting_type.organization_id == andere.pk
    assert sitzung.meeting_type != standardart
    assert FactionMeeting.get_next_meeting_number(andere) == 1
    assert FactionMeeting.get_next_meeting_number(org) == 8


# =============================================================================
# Nummern je Art
# =============================================================================


@pytest.mark.django_db
def test_jede_art_zaehlt_fuer_sich(
    org: Any, standardart: FactionMeetingType, arbeitsgruppe: FactionMeetingType
) -> None:
    for nummer in (1, 2, 3):
        _sitzung(org, standardart, JETZT - timedelta(days=nummer), meeting_number=nummer)

    assert FactionMeeting.get_next_meeting_number(org) == 4
    assert FactionMeeting.get_next_meeting_number(org, standardart) == 4
    assert FactionMeeting.get_next_meeting_number(org, arbeitsgruppe) == 1

    _sitzung(org, arbeitsgruppe, JETZT, meeting_number=1)

    assert FactionMeeting.get_next_meeting_number(org, arbeitsgruppe) == 2
    assert FactionMeeting.get_next_meeting_number(org) == 4, "die Fraktionssitzungen zählen weiter"


@pytest.mark.django_db
def test_startnummer_gilt_bis_die_art_sie_ueberholt(org: Any, arbeitsgruppe: FactionMeetingType) -> None:
    arbeitsgruppe.number_start = 10
    arbeitsgruppe.save(update_fields=["number_start"])

    assert FactionMeeting.get_next_meeting_number(org, arbeitsgruppe) == 10
    _sitzung(org, arbeitsgruppe, JETZT, meeting_number=12)
    assert FactionMeeting.get_next_meeting_number(org, arbeitsgruppe) == 13


@pytest.mark.django_db
def test_neunummerierung_bleibt_in_der_art(
    org: Any, standardart: FactionMeetingType, arbeitsgruppe: FactionMeetingType
) -> None:
    morgen = timezone.now() + timedelta(days=1)
    # Nach Datum gemischt; je Art stehen die Nummern verkehrt herum
    fs_frueh = _sitzung(org, standardart, morgen, status="planned", meeting_number=6)
    ag_frueh = _sitzung(org, arbeitsgruppe, morgen + timedelta(hours=2), status="planned", meeting_number=2)
    fs_spaet = _sitzung(org, standardart, morgen + timedelta(days=1), status="planned", meeting_number=5)
    ag_spaet = _sitzung(org, arbeitsgruppe, morgen + timedelta(days=2), status="planned", meeting_number=1)

    geaendert = renumber_upcoming(org)

    nummern = {m.pk: m.meeting_number for m in FactionMeeting.objects.filter(organization=org)}
    assert geaendert == 4
    assert (nummern[fs_frueh.pk], nummern[fs_spaet.pk]) == (5, 6)
    assert (nummern[ag_frueh.pk], nummern[ag_spaet.pk]) == (1, 2)


# =============================================================================
# Vorherige Sitzung je Art
# =============================================================================


@pytest.mark.django_db
def test_vorherige_sitzung_nur_in_derselben_art(
    org: Any, standardart: FactionMeetingType, arbeitsgruppe: FactionMeetingType
) -> None:
    fraktion = _sitzung(org, standardart, JETZT - timedelta(days=14))
    ag = _sitzung(org, arbeitsgruppe, JETZT - timedelta(days=7))

    assert FactionMeeting.find_previous_meeting(org, JETZT, meeting_type=standardart) == fraktion
    assert FactionMeeting.find_previous_meeting(org, JETZT, meeting_type=arbeitsgruppe) == ag
    assert FactionMeeting.find_previous_meeting(org, JETZT) == fraktion, "ohne Angabe die Standardart"
    assert FactionMeeting.find_previous_meeting(org) == fraktion


# =============================================================================
# Erzeugung aus der Reihe und Anlage von Hand
# =============================================================================


@pytest.mark.django_db
def test_reihe_gibt_ihre_art_an_die_sitzungen_weiter(
    org: Any, standardart: FactionMeetingType, arbeitsgruppe: FactionMeetingType
) -> None:
    arbeitsgruppe.number_start = 3
    arbeitsgruppe.save(update_fields=["number_start"])
    _sitzung(org, standardart, JETZT - timedelta(days=3), meeting_number=40)
    ag_vorher = _sitzung(org, arbeitsgruppe, JETZT - timedelta(days=10), meeting_number=0)
    reihe = FactionMeetingSchedule.objects.create(
        organization=org, meeting_type=arbeitsgruppe, name="AG Umwelt", weekday=0, time=time(18, 0)
    )

    generate_meetings_for_schedule(reihe, now=JETZT)

    termine = list(FactionMeeting.objects.filter(schedule=reihe).order_by("start"))
    assert [t.scheduled_date for t in termine] == [
        date(2026, 10, 5),
        date(2026, 10, 12),
        date(2026, 10, 19),
        date(2026, 10, 26),
    ]
    assert {t.meeting_type_id for t in termine} == {arbeitsgruppe.pk}
    assert [t.meeting_number for t in termine] == [3, 4, 5, 6]
    # Vorherige Sitzung aus derselben Art, nicht die jüngere Fraktionssitzung
    assert termine[0].previous_meeting == ag_vorher
    assert FactionMeeting.get_next_meeting_number(org) == 41


@pytest.mark.django_db
def test_von_hand_angelegte_sitzung_bekommt_die_standardart(
    org: Any,
    standardart: FactionMeetingType,
    arbeitsgruppe: FactionMeetingType,
    make_member: Any,
    client_for: Any,
) -> None:
    vorsitz = make_member(org, VORSITZ_RECHTE, email="vorsitz@example.org")
    fraktion = _sitzung(org, standardart, JETZT - timedelta(days=14), meeting_number=2)
    _sitzung(org, arbeitsgruppe, JETZT - timedelta(days=7), meeting_number=50)

    daten = {"title": "Fraktionssitzung", "start_date": "2030-03-04", "start_time": "18:00"}
    antwort = client_for(vorsitz.user).post(reverse("work:faction", kwargs={"org_slug": org.slug}), daten)

    assert antwort.status_code == 302
    neu = FactionMeeting.objects.get(organization=org, title="Fraktionssitzung")
    assert neu.meeting_type == standardart
    assert neu.meeting_number == 3
    assert neu.previous_meeting == fraktion


# =============================================================================
# Löschen und Admin
# =============================================================================


@pytest.mark.django_db
def test_art_mit_sitzungen_laesst_sich_nicht_loeschen(org: Any, arbeitsgruppe: FactionMeetingType) -> None:
    _sitzung(org, arbeitsgruppe, JETZT)

    with pytest.raises(models.RestrictedError):
        arbeitsgruppe.delete()


@pytest.mark.django_db
def test_organisation_samt_arten_und_sitzungen_laesst_sich_loeschen(
    org: Any, standardart: FactionMeetingType, arbeitsgruppe: FactionMeetingType
) -> None:
    FactionMeetingSchedule.objects.create(organization=org, name="Reihe", weekday=0, time=time(18, 0))
    FactionStandardAgendaItem.objects.create(organization=org, title="Termine")
    _sitzung(org, standardart, JETZT)
    _sitzung(org, arbeitsgruppe, JETZT)

    Organization.objects.get(pk=org.pk).delete()

    assert not FactionMeetingType.objects.filter(organization_id=org.pk).exists()
    assert not FactionMeeting.objects.filter(organization_id=org.pk).exists()


@pytest.mark.django_db
def test_admin_zeigt_sitzungsarten_und_schuetzt_die_standardart(
    org: Any, standardart: FactionMeetingType, arbeitsgruppe: FactionMeetingType
) -> None:
    superuser = cast(Any, UserFactory)(email="betrieb@example.org", is_staff=True, is_superuser=True)
    client = Client()
    client.force_login(superuser)
    model_admin = admin.site._registry[FactionMeetingType]
    request = mock.Mock(user=superuser)

    assert client.get(reverse("admin:work_factionmeetingtype_changelist")).status_code == 200
    assert client.get(reverse("admin:work_factionmeetingtype_change", args=[standardart.pk])).status_code == 200
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_delete_permission(request, standardart)
    assert model_admin.has_delete_permission(request, arbeitsgruppe)


@pytest.mark.django_db
def test_dsgvo_export_nennt_die_auswahl_als_teilnehmende(
    org: Any, arbeitsgruppe: FactionMeetingType, make_member: Any
) -> None:
    person = make_member(org, ["dashboard.view"], email="person@example.org")

    def pdf() -> str:
        daten = dsgvo_export_service.collect_user_data(user=person.user, membership=person, organization=org)
        kontext = {"data": dsgvo_export_service.pdf_data(daten), "user": person.user, "organization": org}
        return str(render_to_string("work/profile/export/dsgvo_export.html", {**kontext, "export_date": JETZT}))

    assert "Teilnehmend in Sitzungsarten" not in pdf(), "ohne Auswahl keine neue Zeile"

    arbeitsgruppe.participants.add(person)
    daten = dsgvo_export_service.collect_user_data(user=person.user, membership=person, organization=org)

    assert daten["membership"]["meeting_types"] == ["Arbeitsgruppe Umwelt"]
    assert "Teilnehmend in Sitzungsarten" in pdf()
