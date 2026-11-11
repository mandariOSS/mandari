# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsarten: Teilnehmende und Sichtbarkeit (Issue #1239, Epic #1226).

Zwei Arten in einer Organisation: die Standardart „Fraktionssitzung“ (alle Mitglieder) und „Vorstand“ (Rolle
„Kernteam“, nur Teilnehmende). Ein Mitglied ohne die Rolle sieht die Vorstandssitzung nirgends: nicht in Liste,
Detail, Aktionen, PDF, Kalenderdatei, Feed, Startseite, Suche, Aufgaben, Änderungshistorie, Benachrichtigungen und
Live-Notizen (404 bzw. keine Verbindung). Mit der Rolle sieht es sie, mit ``faction.manage`` ebenfalls, ebenso als
Eingeladene, Sitzungsleitung oder Schriftführung. Eingeladen werden nur die Teilnehmenden der Art (Rolle, Gremium,
Auswahl). Öffentliche API und öffentliche Protokolle zeigen nur öffentliche Arten; Einschalten macht die Standardart
öffentlich. Mit nur der Standardart bleibt alles wie bisher.
"""

from __future__ import annotations

import asyncio
from datetime import time, timedelta
from typing import Any, cast

import pytest
from channels.testing import WebsocketCommunicator
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, RoleFactory, UserFactory
from apps.tenants.models import Membership, Role
from apps.work.faction import sitzungsarten
from apps.work.faction.feeds import build_personal_feed
from apps.work.faction.generation import create_occurrence
from apps.work.faction.invitations import _send_release_notice
from apps.work.faction.live import ProtokollNotizenConsumer
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionMeeting,
    FactionMeetingSchedule,
    FactionMeetingType,
    FactionProtocolEntry,
    FactionPublicApiAccess,
)
from apps.work.faction.protocol_dispatch import protocol_recipients
from apps.work.faction.services import FactionMeetingEmailService, ProtocolApprovalService
from apps.work.notifications.models import Notification, NotificationType
from apps.work.notifications.services import NotificationHub
from apps.work.organization.models import MemberAbsence
from apps.work.organization.services import _auto_decline_meetings
from apps.work.ris.bezug import fraktionsbezug
from apps.work.tasks import selectors as aufgaben
from apps.work.tasks.models import Task
from insight_core.models import OParlBody, OParlOrganization, OParlPaper, OParlSource

ANSICHT = ["faction.view_public", "tasks.view", "protocols.view_public", "dashboard.view"]
GEHEIM = "Vorstandssitzung Kassenlage"
OFFEN = "Fraktionssitzung Haushalt"
RIS = "https://ris.beispiel.example/oparl"


def _morgen() -> Any:
    return timezone.now() + timedelta(days=1)


@pytest.fixture
def standardart(org: Any) -> FactionMeetingType:
    return FactionMeetingType.objects.get(organization=org, is_default=True)


@pytest.fixture
def kernrolle(org: Any) -> Role:
    rolle: Role = cast(Any, RoleFactory)(organization=org, name="Kernteam", permissions=[])
    return rolle


@pytest.fixture
def vorstand(org: Any, kernrolle: Role) -> FactionMeetingType:
    return FactionMeetingType.objects.create(
        organization=org,
        name="Vorstand",
        position=1,
        participants_mode="role",
        participants_role=kernrolle,
        visibility="participants",
    )


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Membership:
    return make_member(org, ANSICHT, email="mitglied@example.org")  # type: ignore[no-any-return]


@pytest.fixture
def kern(org: Any, make_member: Any, kernrolle: Role) -> Membership:
    m: Membership = make_member(org, ANSICHT, email="kern@example.org")
    m.roles.add(kernrolle)
    return m


@pytest.fixture
def verwaltung(org: Any, make_member: Any) -> Membership:
    rechte = [*ANSICHT, "faction.manage", "faction.view_audit"]
    return make_member(org, rechte, email="verwaltung@example.org")  # type: ignore[no-any-return]


@pytest.fixture
def geheim(org: Any, vorstand: FactionMeetingType) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org, meeting_type=vorstand, title=GEHEIM, start=_morgen(), status="planned", meeting_number=1
    )


@pytest.fixture
def offen(org: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(organization=org, title=OFFEN, start=_morgen(), status="planned")


def _sitzungs_urls(org: Any, meeting: FactionMeeting) -> dict[str, str]:
    kw = {"org_slug": org.slug, "meeting_id": meeting.pk}
    return {
        "detail": reverse("work:faction_detail", kwargs=kw),
        "ics": reverse("work:faction_ics", kwargs=kw),
        "pdf": reverse("work:faction_protocol_pdf", kwargs={**kw, "variant": "oeffentlich"}),
    }


# =============================================================================
# Zentrale Regeln
# =============================================================================


@pytest.mark.django_db
def test_teilnehmende_je_modus(org: Any, make_member: Any, kernrolle: Role, kern: Any, mitglied: Any) -> None:
    gast: Membership = cast(Any, MembershipFactory)(
        user=cast(Any, UserFactory)(email="gast@example.org"),
        organization=org,
        is_guest=True,
    )
    inaktiv = make_member(org, ANSICHT, email="inaktiv@example.org")
    inaktiv.roles.add(kernrolle)
    inaktiv.is_active = False
    inaktiv.save(update_fields=["is_active"])

    alle = FactionMeetingType.objects.get(organization=org, is_default=True)
    assert set(sitzungsarten.teilnehmende(alle)) == {kern, mitglied}
    assert gast not in sitzungsarten.teilnehmende(alle)

    rolle = FactionMeetingType(organization=org, participants_mode="role", participants_role=kernrolle)
    assert set(sitzungsarten.teilnehmende(rolle)) == {kern}
    # Rolle gelöscht (SET_NULL) oder unbekannter Modus: niemand, nie alle
    assert not sitzungsarten.teilnehmende(FactionMeetingType(organization=org, participants_mode="role")).exists()
    assert not sitzungsarten.teilnehmende(FactionMeetingType(organization=org, participants_mode="x")).exists()

    auswahl = FactionMeetingType.objects.create(organization=org, name="AG", participants_mode="persons")
    auswahl.participants.add(mitglied, gast)
    assert set(sitzungsarten.teilnehmende(auswahl)) == {mitglied}


@pytest.mark.django_db
def test_wer_eine_sitzung_nur_fuer_teilnehmende_sieht(
    org: Any, mitglied: Any, kern: Any, verwaltung: Any, geheim: FactionMeeting, offen: FactionMeeting
) -> None:
    alle = FactionMeeting.objects.filter(organization=org)
    assert set(sitzungsarten.sichtbar_fuer(mitglied, alle)) == {offen}
    assert set(sitzungsarten.sichtbar_fuer(kern, alle)) == {offen, geheim}
    assert set(sitzungsarten.sichtbar_fuer(verwaltung, alle)) == {offen, geheim}
    assert set(sitzungsarten.sichtbar_fuer(None, alle)) == set()
    assert not sitzungsarten.darf_sehen(mitglied, geheim)
    assert sitzungsarten.darf_sehen(kern, geheim) and sitzungsarten.darf_sehen(verwaltung, geheim)

    # Eingeladen (Anwesenheit), Sitzungsleitung oder Schriftführung dieser Sitzung
    for felder in ({"chaired_by": mitglied}, {"minute_taker": mitglied}):
        FactionMeeting.objects.filter(pk=geheim.pk).update(**felder)
        geheim.refresh_from_db()
        assert sitzungsarten.darf_sehen(mitglied, geheim)
        assert geheim in sitzungsarten.sichtbar_fuer(mitglied, alle)
        FactionMeeting.objects.filter(pk=geheim.pk).update(chaired_by=None, minute_taker=None)
        geheim.refresh_from_db()
    assert not sitzungsarten.darf_sehen(mitglied, geheim)
    FactionAttendance.objects.create(meeting=geheim, membership=mitglied, status="invited")
    assert sitzungsarten.darf_sehen(mitglied, geheim)
    assert geheim in sitzungsarten.sichtbar_fuer(mitglied, alle)


@pytest.mark.django_db
def test_nur_standardart_alles_wie_bisher(org: Any, mitglied: Any, kern: Any, offen: FactionMeeting) -> None:
    standard = FactionMeetingType.objects.get(organization=org, is_default=True)
    alle = FactionMeeting.objects.filter(organization=org)
    assert list(sitzungsarten.sichtbar_fuer(mitglied, alle)) == [offen]
    assert sitzungsarten.darf_sehen(mitglied, offen)
    assert set(sitzungsarten.teilnehmende(standard)) == set(org.memberships.filter(is_active=True, is_guest=False))
    # Art mit Sichtbarkeit „alle Mitglieder“, aber nur einer Auswahl als Teilnehmende: sichtbar für alle wie bisher
    ag = FactionMeetingType.objects.create(
        organization=org, name="AG", participants_mode="persons", visibility="members"
    )
    ag_sitzung = FactionMeeting.objects.create(organization=org, meeting_type=ag, title="AG", start=_morgen())
    assert sitzungsarten.darf_sehen(mitglied, ag_sitzung)


# =============================================================================
# Seiten: Liste, Detail, Aktionen, PDF, Kalenderdatei, Panel, Startseite
# =============================================================================


@pytest.mark.django_db
def test_seiten_ohne_rolle_404_mit_rolle_und_verwaltung_sichtbar(
    org: Any,
    client_for: Any,
    mitglied: Any,
    kern: Any,
    verwaltung: Any,
    geheim: FactionMeeting,
    offen: FactionMeeting,
) -> None:
    item = FactionAgendaItem.objects.create(meeting=geheim, number="1", title="Kasse", visibility="public", order=1)
    liste = reverse("work:faction", kwargs={"org_slug": org.slug})
    aktion = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": geheim.pk})
    panel = reverse(
        "work:faction_item_panel", kwargs={"org_slug": org.slug, "meeting_id": geheim.pk, "item_id": item.pk}
    )

    client = client_for(mitglied.user)
    html = client.get(liste).content.decode()
    assert OFFEN in html and GEHEIM not in html
    for adresse in _sitzungs_urls(org, geheim).values():
        assert client.get(adresse).status_code == 404, adresse
    assert client.get(panel).status_code == 404
    assert client.post(aktion, {"action": "set_rsvp", "status": "confirmed"}).status_code == 404
    assert client.get(_sitzungs_urls(org, offen)["detail"]).status_code == 200
    # Startseite: kommende Sitzungen ohne die Vorstandssitzung
    start = client.get(reverse("work:dashboard", kwargs={"org_slug": org.slug})).content.decode()
    assert OFFEN in start and GEHEIM not in start

    for person in (kern, verwaltung):
        client = client_for(person.user)
        assert GEHEIM in client.get(liste).content.decode()
        assert client.get(_sitzungs_urls(org, geheim)["detail"]).status_code == 200
        assert client.get(_sitzungs_urls(org, geheim)["ics"]).status_code == 200


@pytest.mark.django_db
def test_neue_liste_ohne_reihen_und_saetze_unsichtbarer_arten(
    org: Any, client_for: Any, mitglied: Any, kern: Any, vorstand: FactionMeetingType, geheim: FactionMeeting
) -> None:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    # Die Nebenspalte nennt die Reihe mit Rhythmus und Ort
    FactionMeetingSchedule.objects.create(
        organization=org,
        meeting_type=vorstand,
        name="Vorstandsrunde",
        weekday=0,
        time=time(18, 0),
        default_location="Vorstandszimmer",
    )
    liste = reverse("work:faction", kwargs={"org_slug": org.slug})

    html = client_for(mitglied.user).get(liste).content.decode()
    assert "Vorstandszimmer" not in html and GEHEIM not in html
    html = client_for(kern.user).get(liste).content.decode()
    assert "Vorstandszimmer" in html and GEHEIM in html


# =============================================================================
# Kalender-Feed, Suche, Aufgaben, Änderungshistorie
# =============================================================================


@pytest.mark.django_db
def test_persoenlicher_feed(mitglied: Any, kern: Any, geheim: FactionMeeting, offen: FactionMeeting) -> None:
    feed = build_personal_feed(mitglied.user)
    assert OFFEN.encode() in feed and GEHEIM.encode() not in feed
    assert GEHEIM.encode() in build_personal_feed(kern.user)


@pytest.mark.django_db
def test_suche_gewichtet_nicht_nach_unsichtbaren_sitzungen(
    org: Any, mitglied: Any, kern: Any, geheim: FactionMeeting
) -> None:
    quelle = OParlSource.objects.create(name="RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=quelle, name="Beispielstadt")
    org.body = body
    org.save(update_fields=["body"])
    vorlage = OParlPaper.objects.create(external_id=f"{RIS}/paper/1", body=body, name="Vorlage Kasse")
    FactionAgendaItem.objects.create(meeting=geheim, number="1", title="Kasse", visibility="public").related_papers.add(
        vorlage
    )

    assert str(vorlage.pk) not in fraktionsbezug(org, mitglied).bearbeitet
    assert str(vorlage.pk) in fraktionsbezug(org, kern).bearbeitet


@pytest.mark.django_db
def test_aufgaben_und_protokolleintraege(
    org: Any, mitglied: Any, kern: Any, verwaltung: Any, geheim: FactionMeeting
) -> None:
    aufgabe = Task.objects.create(
        organization=org,
        title="Kasse prüfen",
        visibility="organization",
        related_faction_meeting=geheim,
        created_by=verwaltung,
    )
    assert aufgabe not in aufgaben.visible_tasks(org, mitglied)
    assert not aufgabe.can_access(mitglied)
    assert aufgabe in aufgaben.visible_tasks(org, kern) and aufgabe.can_access(kern)
    # Wer die Aufgabe zugewiesen bekommt, sieht sie
    aufgabe.assigned_to = mitglied
    aufgabe.save(update_fields=["assigned_to"])
    assert aufgabe in aufgaben.visible_tasks(org, mitglied) and aufgabe.can_access(mitglied)

    eintrag = FactionProtocolEntry.objects.create(meeting=geheim, entry_type="action", created_by=verwaltung, order=1)
    assert aufgaben.open_protocol_action_items(org, mitglied) == []
    assert aufgaben.find_protocol_entry(org, mitglied, eintrag.pk) is None
    assert aufgaben.open_protocol_action_items(org, kern) == [eintrag]


@pytest.mark.django_db
def test_aenderungshistorie_ohne_unsichtbare_sitzungen_und_reihen(
    org: Any, make_member: Any, client_for: Any, vorstand: FactionMeetingType, geheim: FactionMeeting, offen: Any
) -> None:
    FactionMeetingSchedule.objects.create(
        organization=org, meeting_type=vorstand, name="Vorstandsrunde", weekday=0, time=time(18, 0)
    )
    pruefer = make_member(org, ["faction.view_public", "faction.view_audit"], email="pruefer@example.org")
    html = client_for(pruefer.user).get(reverse("work:faction_audit", kwargs={"org_slug": org.slug})).content.decode()
    assert OFFEN in html
    assert GEHEIM not in html and "Vorstandsrunde" not in html


@pytest.mark.django_db
def test_aenderungshistorie_ohne_eintraege_geloeschter_sitzungen(
    org: Any, make_member: Any, client_for: Any, verwaltung: Membership, geheim: FactionMeeting, offen: Any
) -> None:
    """Nach dem Löschen lässt sich eine Sitzung keiner Art mehr zuordnen: Ihre Einträge sieht nur die Verwaltung."""
    from apps.work.faction.models import FactionAuditLog

    FactionAuditLog.objects.create(
        organization=org,
        membership=verwaltung,
        action="delete",
        model_name="FactionMeeting",
        object_id=geheim.pk,
        object_repr=GEHEIM,
        meeting_id_ref=geheim.pk,
    )
    geheim.delete()
    pruefer = make_member(org, ["faction.view_public", "faction.view_audit"], email="pruefer@example.org")
    url = reverse("work:faction_audit", kwargs={"org_slug": org.slug})

    assert GEHEIM not in client_for(pruefer.user).get(url).content.decode()
    assert GEHEIM in client_for(verwaltung.user).get(url).content.decode()


@pytest.mark.django_db
def test_genehmigende_sitzung_nur_mit_sicht(
    org: Any, mitglied: Membership, kern: Membership, geheim: FactionMeeting, offen: FactionMeeting
) -> None:
    """Wird das Protokoll in einer Sitzung „nur Teilnehmende“ genehmigt, nennt die Protokollseite sie nur Teilnehmenden."""
    from apps.work.faction import protokoll
    from apps.work.faction.services import build_faction_protocol_pdf

    FactionAgendaItem.objects.create(
        meeting=geheim, number="1", title="Protokoll genehmigen", order=1, is_approval_item=True, approves_meeting=offen
    )
    geplant_kern = protokoll.genehmigung(offen, kern)
    geplant_mitglied = protokoll.genehmigung(offen, mitglied)
    assert geplant_kern.sitzung == geheim and "Nr. 1" in geplant_kern.wo
    assert geplant_mitglied.sitzung is None and geplant_mitglied.verborgen
    assert geplant_mitglied.wo == "einer späteren Sitzung"

    FactionMeeting.objects.filter(pk=offen.pk).update(
        status="completed", protocol_approved=True, protocol_approved_at=timezone.now(), protocol_approved_in=geheim
    )
    offen.refresh_from_db()
    erfolgt_kern = protokoll.genehmigung(offen, kern)
    erfolgt_mitglied = protokoll.genehmigung(offen, mitglied)
    assert erfolgt_kern.sitzung == geheim and not erfolgt_kern.verborgen
    assert erfolgt_mitglied.sitzung is None and erfolgt_mitglied.verborgen and not erfolgt_mitglied.direkt

    # Das PDF gibt es einmal für alle Empfänger: ohne Titel und Datum der genehmigenden Sitzung
    from django.template.loader import render_to_string

    html = render_to_string(
        "work/faction/pdf/protocol.html", {"meeting": offen, "organization": org, "participants": {"all": []}}
    )
    assert "Genehmigt:" in html and "in einer späteren Sitzung" in html and GEHEIM not in html
    assert build_faction_protocol_pdf(offen, internal=False)[:4] == b"%PDF"


# =============================================================================
# Benachrichtigungen und Live-Notizen
# =============================================================================


@pytest.mark.django_db
def test_benachrichtigungen_nur_an_wer_die_sitzung_sieht(
    org: Any, mitglied: Any, kern: Any, verwaltung: Any, geheim: FactionMeeting
) -> None:
    def senden(empfaenger: Any) -> Any:
        return NotificationHub.send(
            recipient=empfaenger,
            notification_type=NotificationType.FACTION_MEETING_UPDATED,
            title="Sitzung geändert",
            message=GEHEIM,
            metadata={"meeting_id": str(geheim.pk)},
            send_email=False,
        )

    assert senden(mitglied) is None
    assert senden(kern) is not None

    # Vertretung bei Abwesenheit: Die Vertretung sieht die Sitzung nicht und bekommt nichts
    heute = timezone.localdate()
    MemberAbsence.objects.create(
        organization=org, membership=kern, start_date=heute, end_date=heute, deputy=mitglied, notify_deputy=True
    )
    senden(kern)
    assert not Notification.objects.filter(recipient=mitglied).exists()

    # Genehmigung des Protokolls: nur Teilnehmende und Eingeladene
    ProtocolApprovalService._notify_protocol_approved(geheim, verwaltung)  # type: ignore[no-untyped-call]
    genehmigt = NotificationType.FACTION_PROTOCOL_APPROVED
    assert Notification.objects.filter(recipient=kern, notification_type=genehmigt).exists()
    assert not Notification.objects.filter(recipient=mitglied, notification_type=genehmigt).exists()


def _kommunikator(user: Any, item: FactionAgendaItem) -> WebsocketCommunicator:
    kommunikator = WebsocketCommunicator(ProtokollNotizenConsumer.as_asgi(), f"/ws/fraktion/notizen/{item.pk}/")
    kommunikator.scope["user"] = user
    kommunikator.scope["url_route"] = {"kwargs": {"item_id": str(item.pk)}}
    return kommunikator


def _verbindet(user: Any, item: FactionAgendaItem) -> tuple[bool, Any]:
    async def lauf() -> tuple[bool, Any]:
        kommunikator = _kommunikator(user, item)
        verbunden, code = await kommunikator.connect()
        await kommunikator.disconnect()
        return verbunden, code

    return asyncio.run(lauf())


@pytest.mark.django_db(transaction=True)
def test_live_notizen_nur_wer_die_sitzung_sieht(org: Any, mitglied: Any, kern: Any, geheim: FactionMeeting) -> None:
    FactionMeeting.objects.filter(pk=geheim.pk).update(status="ongoing", start=timezone.now() - timedelta(minutes=5))
    item = FactionAgendaItem.objects.create(meeting=geheim, number="1", title="Kasse", visibility="public", order=1)

    assert _verbindet(mitglied.user, item) == (False, 4403)
    assert _verbindet(kern.user, item)[0] is True


# =============================================================================
# Einladung, Erinnerungen, Protokollversand, Abwesenheit
# =============================================================================


@pytest.mark.django_db
def test_reihe_laedt_nur_teilnehmende_ein_rolle_gremium_auswahl(
    org: Any, make_member: Any, mitglied: Any, kern: Any, vorstand: FactionMeetingType
) -> None:
    quelle = OParlSource.objects.create(name="RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=quelle, name="Beispielstadt")
    gremium = OParlOrganization.objects.create(external_id=f"{RIS}/org/1", body=body, name="Bauausschuss")
    bau = make_member(org, ANSICHT, email="bau@example.org")
    bau.oparl_committees.add(gremium)
    ausschuss = FactionMeetingType.objects.create(
        organization=org, name="AK Bau", participants_mode="committee", participants_committee=gremium
    )
    auswahl = FactionMeetingType.objects.create(organization=org, name="AG", participants_mode="persons")
    auswahl.participants.add(mitglied, bau)

    datum = (timezone.now() + timedelta(days=7)).date()
    erwartet = {vorstand: {kern}, ausschuss: {bau}, auswahl: {mitglied, bau}}
    for art, personen in erwartet.items():
        reihe = FactionMeetingSchedule.objects.create(
            organization=org, meeting_type=art, name=art.name, weekday=datum.weekday(), time=time(18, 0)
        )
        sitzung = create_occurrence(reihe, datum)  # type: ignore[no-untyped-call]
        eingeladen = {a.membership for a in sitzung.attendances.all()}
        assert eingeladen == personen, art.name

        mail.outbox.clear()
        FactionMeetingEmailService().send_invitations(sitzung)
        empfaenger = {adresse for nachricht in mail.outbox for adresse in nachricht.to}
        assert empfaenger == {p.user.email for p in personen}, art.name


@pytest.mark.django_db
def test_protokollversand_und_freigabehinweis(
    org: Any,
    make_member: Any,
    mitglied: Any,
    kern: Any,
    geheim: FactionMeeting,
    offen: FactionMeeting,
) -> None:
    assert {m for m, _intern in protocol_recipients(geheim)} == {kern}
    assert {m for m, _intern in protocol_recipients(offen)} == {mitglied, kern}

    # Freigabe-Hinweis: Vorsitz ohne Rolle „Kernteam“ und ohne faction.manage erfährt nichts von der Sitzung
    vorsitz = make_member(org, ANSICHT, email="vorsitz@example.org")
    vorsitz_rolle = Role.objects.get(organization=org, name="Fraktionsvorsitz")
    vorsitz_rolle.permissions.clear()
    vorsitz.roles.add(vorsitz_rolle)
    mail.outbox.clear()
    assert _send_release_notice(geheim, _morgen(), final=False) == 0
    assert not Notification.objects.filter(recipient=vorsitz).exists() and not mail.outbox
    _send_release_notice(offen, _morgen(), final=False)
    assert Notification.objects.filter(recipient=vorsitz).exists()


@pytest.mark.django_db
def test_abwesenheit_entschuldigt_nur_bei_eigenen_sitzungen(
    org: Any, mitglied: Any, kern: Any, geheim: FactionMeeting, offen: FactionMeeting
) -> None:
    tag = timezone.localdate(geheim.start)
    _auto_decline_meetings(org, mitglied, tag, tag)
    assert not FactionAttendance.objects.filter(meeting=geheim, membership=mitglied).exists()
    assert FactionAttendance.objects.get(meeting=offen, membership=mitglied).status == "excused"

    _auto_decline_meetings(org, kern, tag, tag)
    assert FactionAttendance.objects.get(meeting=geheim, membership=kern).status == "excused"


# =============================================================================
# Öffentliche API und öffentliche Protokolle
# =============================================================================


@pytest.mark.django_db
def test_api_zeigt_nur_oeffentliche_arten(
    org: Any, client: Any, standardart: FactionMeetingType, geheim: FactionMeeting, offen: FactionMeeting
) -> None:
    ag = FactionMeetingType.objects.create(organization=org, name="AG", visibility="members")
    ag_sitzung = FactionMeeting.objects.create(organization=org, meeting_type=ag, title="AG Umwelt", start=_morgen())

    zugang = FactionPublicApiAccess.objects.create(organization=org, is_enabled=True)
    standardart.refresh_from_db()
    assert standardart.visibility == "public"

    basis = f"/api/public/v1/fraktionen/{zugang.token}/sitzungen/"
    liste = client.get(basis).content.decode()
    assert OFFEN in liste and GEHEIM not in liste and "AG Umwelt" not in liste
    assert client.get(f"{basis}{offen.pk}/").status_code == 200
    for sitzung in (geheim, ag_sitzung):
        assert client.get(f"{basis}{sitzung.pk}/").status_code == 404


@pytest.mark.django_db
def test_einschalten_macht_standardart_oeffentlich_nur_beim_wechsel(org: Any, standardart: FactionMeetingType) -> None:
    zugang = FactionPublicApiAccess.for_organization(org)
    standardart.refresh_from_db()
    assert standardart.visibility == "members"  # angelegt, aber aus

    zugang.is_enabled = True
    zugang.save(update_fields=["is_enabled", "updated_at"])
    standardart.refresh_from_db()
    assert standardart.visibility == "public"

    # Später bewusst „alle Mitglieder“: erneutes Speichern bei eingeschalteter API ändert nichts
    FactionMeetingType.objects.filter(pk=standardart.pk).update(visibility="members")
    zugang.save()
    org.publish_protocols = False
    org.save()
    standardart.refresh_from_db()
    assert standardart.visibility == "members"

    # „Nur Teilnehmende“ bleibt auch beim Einschalten der öffentlichen Protokolle
    FactionMeetingType.objects.filter(pk=standardart.pk).update(visibility="participants")
    org.publish_protocols = True
    org.save(update_fields=["publish_protocols"])
    standardart.refresh_from_db()
    assert standardart.visibility == "participants"


@pytest.mark.django_db
def test_oeffentliche_protokolle_nur_oeffentlicher_arten(
    org: Any, client: Any, standardart: FactionMeetingType, offen: FactionMeeting
) -> None:
    quelle = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(
        external_id="https://ris.example.org/body/1", source=quelle, name="Stadt Test", slug="stadt-test"
    )
    org.body = body
    org.publish_protocols = True
    org.save(update_fields=["body", "publish_protocols"])
    ag = FactionMeetingType.objects.create(organization=org, name="AG", visibility="members")
    ag_sitzung = FactionMeeting.objects.create(organization=org, meeting_type=ag, title="AG Umwelt", start=_morgen())
    FactionMeeting.objects.filter(organization=org).update(status="completed", protocol_status="approved")

    def detail(sitzung: FactionMeeting) -> int:
        adresse = reverse(
            "insight_core:public_protocol_detail", kwargs={"body_slug": body.slug, "meeting_id": sitzung.pk}
        )
        return int(client.get(adresse).status_code)

    assert detail(offen) == 200
    assert detail(ag_sitzung) == 404
    liste = client.get(reverse("insight_core:public_protocols", kwargs={"body_slug": body.slug})).content.decode()
    assert OFFEN in liste and "AG Umwelt" not in liste
