# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Die Protokollgenehmigung bleibt, was sie ist: ein Beschluss in der Sitzung und danach endgültig (Issue #63).

- Über das Protokoll der vorigen Sitzung stimmt die Fraktion auf dem Genehmigungs-TOP der Folgesitzung ab. Eine
  Abstimmung, die vor dem Beginn dieser Sitzung erfasst wird, nimmt Work nicht an und genehmigt damit nichts; außerhalb
  der Sitzung bleibt der direkte Weg mit ``protocols.approve``. In der laufenden und in der abgeschlossenen
  Folgesitzung genehmigt die erfasste Abstimmung wie bisher.
- Statusaktionen und das Bearbeiten einer Sitzung schreiben nur ihre eigenen Felder. Eine Genehmigung, die zwischen
  Lesen und Speichern erteilt wurde, bleibt bestehen und steht in der Änderungshistorie nicht als zurückgenommen.
- Die erste Genehmigung gilt: Eine zweite über ein älter gelesenes Objekt ersetzt Person und Zeitpunkt nicht.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any, cast

import pytest
from django.db.models.signals import pre_save
from django.urls import reverse
from django.utils import timezone

from apps.common.permissions import DEFAULT_ROLES
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAuditLog,
    FactionDecision,
    FactionMeeting,
)
from apps.work.faction.regeln import (
    GENEHMIGUNG_NUR_IN_SITZUNG,
    PROTOKOLL_GENEHMIGT,
    ProtokollGenehmigtError,
    abstimmung_erfassbar,
)
from apps.work.faction.services import ProtocolApprovalService, apply_approval_item_decision
from apps.work.faction.views import actions

STIMMEN = {"votes_yes": "0", "votes_no": "0", "votes_abstain": "0", "result": "accepted"}
VORSITZ = [str(recht) for recht in cast(list[str], DEFAULT_ROLES["faction_chair"]["permissions"])]
PERSONAL = [str(recht) for recht in cast(list[str], DEFAULT_ROLES["faction_staff"]["permissions"])]


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    return make_member(org, VORSITZ, email="vorsitz@example.org")


@pytest.fixture
def personal(org: Any, make_member: Any) -> Any:
    """Fraktionspersonal: legt Sitzungen an und protokolliert, genehmigt aber keine Protokolle."""
    assert "protocols.approve" not in PERSONAL and "faction.create" in PERSONAL
    return make_member(org, PERSONAL, email="personal@example.org")


@pytest.fixture
def vorige(org: Any, vorsitz: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org,
        title="Sitzung im Oktober",
        start=timezone.now() - timedelta(days=14),
        status="completed",
        created_by=vorsitz,
    )


def _folge(org: Any, vorige: FactionMeeting, ersteller: Any, status: str) -> tuple[FactionMeeting, FactionAgendaItem]:
    """Folgesitzung im gewünschten Status mit dem Genehmigungs-TOP für ``vorige``."""
    folge = FactionMeeting.objects.create(
        organization=org,
        title="Sitzung im November",
        start=timezone.now() + timedelta(days=1),
        status=status,
        created_by=ersteller,
    )
    top = FactionAgendaItem.objects.create(
        meeting=folge, number="1", title="Protokoll genehmigen", is_approval_item=True, order=0
    )
    # Ohne Speichern des Objekts: Der Bezug steht fest, gleich wie die Kette den Status der Folgesitzung einordnet
    FactionAgendaItem.objects.filter(pk=top.pk).update(approves_meeting=vorige)
    top.refresh_from_db()
    return folge, top


def _aktion(client_for: Any, member: Any, meeting: FactionMeeting, **daten: str) -> Any:
    url = reverse("work:faction_action", kwargs={"org_slug": meeting.organization.slug, "meeting_id": meeting.pk})
    return client_for(member.user).post(url, daten, HTTP_HX_REQUEST="true")


def _panel(client_for: Any, member: Any, top: FactionAgendaItem, **daten: str) -> Any:
    url = reverse(
        "work:faction_item_panel_action",
        kwargs={"org_slug": top.meeting.organization.slug, "meeting_id": top.meeting_id, "item_id": top.pk},
    )
    return client_for(member.user).post(url, daten, HTTP_HX_REQUEST="true")


def _genehmigt(meeting: FactionMeeting) -> bool:
    return bool(FactionMeeting.objects.values_list("protocol_approved", flat=True).get(pk=meeting.pk))


# ---- Abstimmung nur in der Sitzung ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["draft", "planned", "invited", "cancelled"])
@pytest.mark.parametrize("weg", ["aktion", "panel"])
def test_abstimmung_vor_der_sitzung_genehmigt_das_vorprotokoll_nicht(
    org: Any, vorige: FactionMeeting, personal: Any, client_for: Any, status: str, weg: str
) -> None:
    folge, top = _folge(org, vorige, personal, status)

    if weg == "aktion":
        antwort = _aktion(client_for, personal, folge, action="record_decision", agenda_item_id=str(top.pk), **STIMMEN)
    else:
        antwort = _panel(client_for, personal, top, action="record_decision", **STIMMEN)

    assert antwort.status_code == 400
    assert GENEHMIGUNG_NUR_IN_SITZUNG in antwort.content.decode()
    assert not _genehmigt(vorige)
    assert not FactionDecision.objects.filter(agenda_item=top).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("weg", ["aktion", "panel"])
def test_absage_nach_dem_laden_verhindert_die_abstimmung_am_genehmigungs_top(
    org: Any, vorige: FactionMeeting, personal: Any, client_for: Any, monkeypatch: pytest.MonkeyPatch, weg: str
) -> None:
    """Die Ansicht sah die Sitzung noch laufen; gespeichert ist sie abgesagt. Es entsteht keine Abstimmung."""
    folge, top = _folge(org, vorige, personal, "cancelled")
    # Der Stand, den die Anfrage geladen hat: Die Sitzung läuft
    monkeypatch.setattr("apps.work.faction.regeln.abstimmung_erfassbar", lambda item: True)
    monkeypatch.setattr(actions, "abstimmung_erfassbar", lambda item: True)
    monkeypatch.setattr("apps.work.faction.views.panel.abstimmung_erfassbar", lambda item: True)

    if weg == "aktion":
        antwort = _aktion(client_for, personal, folge, action="record_decision", agenda_item_id=str(top.pk), **STIMMEN)
    else:
        antwort = _panel(client_for, personal, top, action="record_decision", **STIMMEN)

    assert antwort.status_code == 400
    assert GENEHMIGUNG_NUR_IN_SITZUNG in antwort.content.decode()
    assert not _genehmigt(vorige)
    assert not FactionDecision.objects.filter(agenda_item=top).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("weg", ["aktion", "panel"])
def test_genehmigung_waehrend_des_erfassens_meldet_den_grund(
    org: Any, vorige: FactionMeeting, personal: Any, client_for: Any, monkeypatch: pytest.MonkeyPatch, weg: str
) -> None:
    """Wird das Protokoll zwischen Prüfung und Speichern genehmigt, kommt die feste Meldung statt eines Serverfehlers."""
    folge, top = _folge(org, vorige, personal, "ongoing")

    def genehmigt(*args: Any, **kwargs: Any) -> None:
        raise ProtokollGenehmigtError()

    monkeypatch.setattr("apps.work.faction.services.record_decision", genehmigt)

    if weg == "aktion":
        antwort = _aktion(client_for, personal, folge, action="record_decision", agenda_item_id=str(top.pk), **STIMMEN)
    else:
        antwort = _panel(client_for, personal, top, action="record_decision", **STIMMEN)

    assert antwort.status_code == 400
    assert PROTOKOLL_GENEHMIGT in antwort.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["ongoing", "completed"])
@pytest.mark.parametrize("weg", ["aktion", "panel"])
def test_abstimmung_in_der_sitzung_genehmigt_wie_bisher(
    org: Any, vorige: FactionMeeting, personal: Any, client_for: Any, status: str, weg: str
) -> None:
    folge, top = _folge(org, vorige, personal, status)

    if weg == "aktion":
        antwort = _aktion(client_for, personal, folge, action="record_decision", agenda_item_id=str(top.pk), **STIMMEN)
    else:
        antwort = _panel(client_for, personal, top, action="record_decision", **STIMMEN)

    assert antwort.status_code == 200
    vorige.refresh_from_db()
    assert vorige.protocol_approved and vorige.protocol_status == "approved"
    assert vorige.protocol_approved_in_id == folge.pk and vorige.protocol_approved_by_id == personal.pk


@pytest.mark.django_db
def test_auswertung_des_genehmigungs_tops_prueft_die_sitzung_selbst(
    org: Any, vorige: FactionMeeting, personal: Any
) -> None:
    """Auch ein Weg an den Ansichten vorbei genehmigt nur in einer begonnenen Sitzung (Stand der Datenbank)."""

    class Angenommen:
        passed = True

    folge, top = _folge(org, vorige, personal, "planned")
    assert not abstimmung_erfassbar(top)
    assert cast(Any, apply_approval_item_decision)(top, Angenommen(), folge, personal) is False
    assert not _genehmigt(vorige)

    # Das geladene Objekt behauptet „läuft“, gespeichert ist „geplant“
    folge.status = "ongoing"
    assert cast(Any, apply_approval_item_decision)(top, Angenommen(), folge, personal) is False

    FactionMeeting.objects.filter(pk=folge.pk).update(status="ongoing")
    top.refresh_from_db()
    assert abstimmung_erfassbar(top)
    assert cast(Any, apply_approval_item_decision)(top, Angenommen(), folge, personal) is True
    assert _genehmigt(vorige)


@pytest.mark.django_db
def test_andere_tops_nehmen_vor_der_sitzung_weiter_abstimmungen_an(
    org: Any, vorige: FactionMeeting, vorsitz: Any, client_for: Any
) -> None:
    """Die Regel gilt nur für den Genehmigungs-TOP: Wer die Sitzung verwaltet, erfasst am Panel wie bisher."""
    folge, top = _folge(org, vorige, vorsitz, "planned")
    haushalt = FactionAgendaItem.objects.create(meeting=folge, number="2", title="Haushalt", order=1)

    antwort = _panel(client_for, vorsitz, haushalt, action="record_decision", **STIMMEN)

    assert antwort.status_code == 200
    assert FactionDecision.objects.filter(agenda_item=haushalt).exists()
    assert antwort.context["can_record_decision"] is True
    # Am Genehmigungs-TOP bietet das Panel das Erfassen erst an, wenn die Sitzung begonnen hat
    url = reverse("work:faction_item_panel", kwargs={"org_slug": org.slug, "meeting_id": folge.pk, "item_id": top.pk})
    client = client_for(vorsitz.user)
    assert client.get(url).context["can_record_decision"] is False
    FactionMeeting.objects.filter(pk=folge.pk).update(status="ongoing")
    assert client.get(url).context["can_record_decision"] is True


@pytest.mark.django_db
@pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neu"])
def test_panel_nennt_am_genehmigungs_top_vor_der_sitzung_den_grund(
    org: Any, vorige: FactionMeeting, vorsitz: Any, make_member: Any, client_for: Any, neu: bool
) -> None:
    """Wer die Sitzung verwaltet, liest vor dem Beginn statt des Knopfs, ab wann sich die Abstimmung erfassen lässt."""
    org.work_new_design = neu
    org.save(update_fields=["work_new_design"])
    folge, top = _folge(org, vorige, vorsitz, "planned")
    haushalt = FactionAgendaItem.objects.create(meeting=folge, number="2", title="Haushalt", order=1)
    lesend = make_member(org, ["faction.view_public", "protocols.view_public"], email="lesend@example.org")

    def panel(member: Any, item: FactionAgendaItem) -> str:
        url = reverse(
            "work:faction_item_panel", kwargs={"org_slug": org.slug, "meeting_id": folge.pk, "item_id": item.pk}
        )
        antwort = client_for(member.user).get(url)
        assert antwort.status_code == 200
        return cast(str, antwort.content.decode())

    html = panel(vorsitz, top)
    assert GENEHMIGUNG_NUR_IN_SITZUNG in html and "Keine Abstimmung erfasst" not in html
    # Andere TOPs und Mitglieder, die ohnehin nicht erfassen, sehen den Satz nicht
    assert GENEHMIGUNG_NUR_IN_SITZUNG not in panel(vorsitz, haushalt)
    html = panel(lesend, top)
    assert GENEHMIGUNG_NUR_IN_SITZUNG not in html and "Keine Abstimmung erfasst" in html

    # Mit dem Beginn der Sitzung steht dort wieder der Knopf zum Erfassen
    FactionMeeting.objects.filter(pk=folge.pk).update(status="ongoing")
    html = panel(vorsitz, top)
    assert GENEHMIGUNG_NUR_IN_SITZUNG not in html and "Keine Abstimmung erfasst" not in html


# ---- Genehmigung bleibt bestehen -----------------------------------------------------------------------------


def _genehmigen(meeting_id: Any, von: Any) -> None:
    """Genehmigung in der Datenbank, wie sie eine andere Anfrage gerade erteilt hat."""
    FactionMeeting.objects.filter(pk=meeting_id).update(
        protocol_approved=True,
        protocol_status="approved",
        protocol_approved_at=timezone.now(),
        protocol_approved_by=von,
    )


@pytest.fixture
def genehmigung_beim_speichern(vorsitz: Any) -> Iterator[None]:
    """Die Genehmigung trifft ein, nachdem die Aktion die Sitzung gelesen hat und bevor sie schreibt."""

    def dazwischen(sender: Any, instance: FactionMeeting, **kwargs: Any) -> None:
        _genehmigen(instance.pk, vorsitz)

    pre_save.connect(dazwischen, sender=FactionMeeting, dispatch_uid="test_genehmigung_beim_speichern")
    yield
    pre_save.disconnect(sender=FactionMeeting, dispatch_uid="test_genehmigung_beim_speichern")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("aktion", "status", "daten", "erwartet"),
    [
        ("start", "planned", {}, "ongoing"),
        ("end", "ongoing", {}, "completed"),
        ("cancel", "ongoing", {}, "cancelled"),
        ("update_status", "completed", {"status": "completed"}, "completed"),
        ("update", "ongoing", {"title": "Neuer Titel", "location": "Ratssaal"}, "ongoing"),
    ],
)
def test_statusaktion_nimmt_eine_inzwischen_erteilte_genehmigung_nicht_zurueck(
    org: Any,
    vorsitz: Any,
    client_for: Any,
    genehmigung_beim_speichern: None,
    aktion: str,
    status: str,
    daten: dict[str, str],
    erwartet: str,
) -> None:
    meeting = FactionMeeting.objects.create(
        organization=org, title="Sitzung", start=timezone.now(), status=status, created_by=vorsitz
    )
    # Erst ab hier zählt der Empfänger: Das Anlegen selbst genehmigt nichts
    FactionMeeting.objects.filter(pk=meeting.pk).update(
        protocol_approved=False, protocol_status="draft", protocol_approved_at=None, protocol_approved_by=None
    )

    antwort = _aktion(client_for, vorsitz, meeting, action=aktion, **daten)

    assert antwort.status_code == 200
    meeting.refresh_from_db()
    assert meeting.status == erwartet
    if aktion == "update":
        assert (meeting.title, meeting.location) == ("Neuer Titel", "Ratssaal")
    assert meeting.protocol_approved and meeting.protocol_status == "approved"
    assert meeting.protocol_approved_by_id == vorsitz.pk and meeting.protocol_approved_at is not None


@pytest.mark.django_db
def test_historie_meldet_die_genehmigung_nicht_als_zurueckgenommen(
    org: Any, vorsitz: Any, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Genehmigung nach dem Lesen der Sitzung, aber vor dem Speichern: Das Objekt der Aktion trägt den alten Stand."""
    meeting = FactionMeeting.objects.create(
        organization=org, title="Sitzung", start=timezone.now(), status="ongoing", created_by=vorsitz
    )
    lesen = cast(Any, actions).sitzung_oder_404

    def lesen_dann_genehmigen(*args: Any, **kwargs: Any) -> Any:
        gelesen = lesen(*args, **kwargs)
        _genehmigen(gelesen.pk, vorsitz)
        return gelesen

    monkeypatch.setattr(actions, "sitzung_oder_404", lesen_dann_genehmigen)

    antwort = _aktion(client_for, vorsitz, meeting, action="end")

    assert antwort.status_code == 200
    meeting.refresh_from_db()
    assert meeting.status == "completed" and meeting.protocol_approved
    eintraege = FactionAuditLog.objects.filter(model_name="FactionMeeting", object_id=meeting.pk)
    assert eintraege.filter(action="status").exists()
    for eintrag in eintraege:
        assert "protocol_approved" not in eintrag.changes, eintrag.changes
        assert "protocol_status" not in eintrag.changes, eintrag.changes


@pytest.mark.django_db
def test_erste_genehmigung_gilt(org: Any, vorige: FactionMeeting, vorsitz: Any, personal: Any) -> None:
    aelter_gelesen = FactionMeeting.objects.get(pk=vorige.pk)
    dienst = cast(Any, ProtocolApprovalService)
    assert dienst.approve_protocol(vorige, approved_in_meeting=None, approved_by=vorsitz) is True
    zeitpunkt = FactionMeeting.objects.get(pk=vorige.pk).protocol_approved_at

    assert dienst.approve_protocol(aelter_gelesen, approved_in_meeting=None, approved_by=personal) is False

    vorige.refresh_from_db()
    assert vorige.protocol_approved_by_id == vorsitz.pk and vorige.protocol_approved_at == zeitpunkt
