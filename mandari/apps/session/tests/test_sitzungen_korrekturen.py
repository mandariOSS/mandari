# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Korrekturen im Bereich Sitzungen, Tagesordnung, Ladung, Anwesenheit, Abstimmung und Niederschrift (Teil von #708).

Je Befund ein Test, der ohne die Korrektur scheitert:
- Beschlussfähigkeit: Stellvertretungen zählen nur, wenn sie für ein nicht anwesendes Mitglied nachrücken
- „Nur Summen“: Die Abstimmungsseite überschreibt erfasste Summen nicht mit 0
- Niederschrift: Summen offener und namentlicher Abstimmungen ergeben sich aus den Einzelstimmen
- Sperre nach Genehmigung: Tagesordnung und Anwesenheit; Hinweis statt 403-Seite; Nummernvergabe bleibt möglich
- Ladung: keine Ladung zu abgesagten Sitzungen, keine Nachladung ohne Nachtrags-TOPs
- Absage: Status und Häkchen bleiben abgeglichen (Formular, Modell, Bestand)
- Sitzungsformular: Ende nach Beginn, Gremium vorausgewählt, Wahlperiode bei Planung und Verschiebung
- Robustheit: Jahres-/Monatsparameter, ungültige Kennungen, Blättern mit Filtern
- Rückmeldungen: Zusage in der Zeile entlastet die Stellvertretung; Erinnerung erst nach der Ladung
- Darstellung: Formularfelder mit Rahmen, umbrechende Anlagenzeile, Mitwirkungsverbot ohne Landesnorm
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.core import mail
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionInvitationDispatch,
    SessionInvitationRecipient,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionNumberRange,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionProtocol,
    SessionRole,
    SessionTenant,
    SessionUser,
    SessionVote,
)
from apps.session.services import (
    attendance_service,
    invitation_response_service,
    invitation_service,
    protocol_lock,
    protocol_publication,
    rsvp_reminders,
    voting_service,
)

pytestmark = pytest.mark.django_db

TEMPLATES = Path(__file__).resolve().parents[3] / "templates"


@dataclass
class Welt:
    tenant: SessionTenant
    gremium: SessionOrganization
    sitzung: SessionMeeting
    mitglieder: list[SessionPerson]
    admin: SessionUser
    client: Client

    def url(self, pfad: str) -> str:
        return f"/session/{self.tenant.slug}{pfad}"


def _person(tenant: SessionTenant, name: str, email: str = "") -> SessionPerson:
    return SessionPerson.objects.create(tenant=tenant, given_name="P", family_name=name, email=email)


@pytest.fixture
def welt() -> Welt:
    tenant = SessionTenant.objects.create(name="Stadt Korrektur", slug="korrektur")
    rolle = SessionRole.objects.create(tenant=tenant, name="Administrator", is_admin=True)
    user = cast(Any, UserFactory)(email="admin@korrektur.example.org")
    admin = SessionUser.objects.create(user=user, tenant=tenant)
    admin.roles.add(rolle)
    client = Client()
    client.force_login(user)
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss", invitation_period_days=7)
    mitglieder = [_person(tenant, name, f"{name.lower()}@example.org") for name in ("Amsel", "Buche", "Carl")]
    for person in mitglieder:
        SessionOrganizationMembership.objects.create(organization=gremium, person=person, has_voting_rights=True)
    start = (timezone.now() + timedelta(days=14)).replace(hour=17, minute=0, second=0, microsecond=0)
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Hauptausschuss", organization=gremium, start=start, meeting_state="scheduled"
    )
    return Welt(tenant, gremium, sitzung, mitglieder, admin, client)


def _meldungen(response: Any) -> list[str]:
    return [str(m) for m in get_messages(response.wsgi_request)]


def _genehmigen(sitzung: SessionMeeting) -> SessionProtocol:
    return SessionProtocol.objects.create(meeting=sitzung, content="Niederschrift", status="approved")


# =============================================================================
# Beschlussfähigkeit und Stellvertretungen
# =============================================================================


def _mit_stellvertretungen(welt: Welt) -> list[SessionPerson]:
    """Je Mitglied eine Stellvertretung, wie sie das Besetzungsformular anlegt (stimmberechtigt vorbelegt)."""
    vertretungen = []
    for mitglied in welt.mitglieder:
        vertretung = _person(welt.tenant, f"Vertretung-{mitglied.family_name}")
        SessionOrganizationMembership.objects.create(
            organization=welt.gremium, person=vertretung, has_voting_rights=True, substitute_for=mitglied
        )
        vertretungen.append(vertretung)
    attendance_service.generate_attendance(welt.sitzung)
    return vertretungen


def _status(sitzung: SessionMeeting, person: SessionPerson, status: str) -> None:
    SessionAttendance.objects.filter(meeting=sitzung, person=person).update(status=status)


def test_stellvertretungen_erhoehen_die_zahl_der_sitze_nicht(welt: Welt) -> None:
    _mit_stellvertretungen(welt)
    assert welt.sitzung.attendances.count() == 6
    for mitglied in welt.mitglieder:
        _status(welt.sitzung, mitglied, "present")

    quorum = attendance_service.quorum_status(welt.sitzung)
    assert (quorum["voting_total"], quorum["voting_present"], quorum["met"]) == (3, 3, True)

    beurteilt = voting_service.eligibility(welt.sitzung)
    assert beurteilt.complete, "Vollständig, obwohl die Stellvertretungen noch „eingeladen“ sind"
    assert {a.person_id for a in beurteilt.voting} == {m.pk for m in welt.mitglieder}
    assert beurteilt.standby == [], "Nicht anwesende Stellvertretungen sind keine Anwesenden ohne Stimme"


def test_stellvertretung_rueckt_fuer_abwesendes_mitglied_nach(welt: Welt) -> None:
    vertretungen = _mit_stellvertretungen(welt)
    amsel, buche, carl = welt.mitglieder
    _status(welt.sitzung, amsel, "excused")
    _status(welt.sitzung, buche, "present")
    _status(welt.sitzung, carl, "present")
    _status(welt.sitzung, vertretungen[1], "present")  # Buche ist selbst da: ohne Stimme
    # Amsels Vertretung ist noch offen: Ob sie kommt, entscheidet über die Obergrenze der Stimmen
    assert not voting_service.eligibility(welt.sitzung).complete
    assert attendance_service.quorum_status(welt.sitzung)["voting_present"] == 2

    _status(welt.sitzung, vertretungen[0], "present")  # vertritt Amsel

    quorum = attendance_service.quorum_status(welt.sitzung)
    assert (quorum["voting_total"], quorum["voting_present"]) == (3, 3)
    beurteilt = voting_service.eligibility(welt.sitzung)
    assert {a.person_id for a in beurteilt.voting} == {buche.pk, carl.pk, vertretungen[0].pk}
    assert [a.person_id for a in beurteilt.standby] == [vertretungen[1].pk]
    assert beurteilt.complete, "Carls Vertretung ist offen, kommt aber nicht zum Zug"


def test_stellvertretung_ohne_freien_sitz_stimmt_nicht_ab(welt: Welt) -> None:
    vertretungen = _mit_stellvertretungen(welt)
    for person in [*welt.mitglieder, *vertretungen]:
        _status(welt.sitzung, person, "present")
    top = SessionAgendaItem.objects.create(meeting=welt.sitzung, number="1", order=1, name="Radweg")

    with pytest.raises(voting_service.VotingRightsError) as fehler:
        voting_service.capture_votes(top, {vertretungen[0]: "yes"}, recorded_by=welt.admin)
    # Sachlich richtig benannt: anwesend, aber das vertretene Mitglied ist selbst da
    meldung = str(fehler.value)
    assert "Stellvertretung, deren vertretenes Mitglied selbst anwesend ist:" in meldung
    assert "nicht anwesend" not in meldung
    check = voting_service.check_counts(top, 4, 0, 0)
    assert check.exceeded and check.hard, "Mehr Stimmen als Sitze bei vollständiger Anwesenheit"


def test_umlauf_zaehlt_stellvertretungen_nicht_mit(welt: Welt) -> None:
    from apps.session.models import SessionCircularResolution

    _mit_stellvertretungen(welt)
    umlauf = SessionCircularResolution.objects.create(
        tenant=welt.tenant,
        organization=welt.gremium,
        title="Umlauf",
        resolution_text="Text",
        deadline=timezone.localdate() + timedelta(days=7),
    )
    assert voting_service.circular_tally(umlauf)["total_members"] == 3


# =============================================================================
# Abstimmung „Nur Summen“ und Summen in der Niederschrift
# =============================================================================


def test_nur_summen_bleiben_beim_speichern_der_abstimmung_erhalten(welt: Welt) -> None:
    top = SessionAgendaItem.objects.create(
        meeting=welt.sitzung, number="2", order=1, name="Haushalt", voting_method="summary",
        vote_result="approved", votes_yes=4, votes_no=1, votes_abstain=1,
    )  # fmt: skip
    url = welt.url(f"/agenda/{top.pk}/voting/")

    antwort = welt.client.post(url, {"voting_method": "summary", "vote_result": "approved"})
    assert antwort.status_code == 302
    top.refresh_from_db()
    assert (top.votes_yes, top.votes_no, top.votes_abstain) == (4, 1, 1)

    seite = welt.client.get(url).content.decode()
    assert "method === 'secret' || method === 'summary'" in seite, "Summenfelder auch bei „Nur Summen“"
    welt.client.post(
        url,
        {
            "voting_method": "summary",
            "vote_result": "approved",
            "votes_yes": "3",
            "votes_no": "0",
            "votes_abstain": "0",
        },
    )
    top.refresh_from_db()
    assert (top.votes_yes, top.votes_no, top.votes_abstain) == (3, 0, 0)


def test_niederschrift_ueberschreibt_summen_namentlicher_abstimmung_nicht(welt: Welt) -> None:
    for mitglied in welt.mitglieder:
        SessionAttendance.objects.create(meeting=welt.sitzung, person=mitglied, status="present")
    top = SessionAgendaItem.objects.create(
        meeting=welt.sitzung, number="1", order=1, name="Radweg", voting_method="roll_call"
    )
    voting_service.capture_votes(top, dict.fromkeys(welt.mitglieder, "yes"), recorded_by=welt.admin)
    SessionProtocol.objects.create(meeting=welt.sitzung, content="Entwurf")
    url = welt.url(f"/meetings/{welt.sitzung.pk}/protocol/edit/")

    seite = welt.client.get(url).content.decode()
    assert f'name="votes_yes_{top.pk}"' not in seite
    assert 'data-testid="summen-aus-einzelstimmen"' in seite

    welt.client.post(
        url,
        {f"protocol_note_{top.pk}": "", f"vote_result_{top.pk}": "approved", f"votes_yes_{top.pk}": "0",
         f"votes_no_{top.pk}": "3", f"votes_abstain_{top.pk}": "0"},
    )  # fmt: skip
    top.refresh_from_db()
    assert (top.votes_yes, top.votes_no) == (3, 0)
    assert top.vote_result == "approved"


def test_niederschrift_ignoriert_hochgestellte_ziffern(welt: Welt) -> None:
    top = SessionAgendaItem.objects.create(meeting=welt.sitzung, number="1", order=1, name="Radweg")
    SessionProtocol.objects.create(meeting=welt.sitzung, content="Entwurf")
    antwort = welt.client.post(
        welt.url(f"/meetings/{welt.sitzung.pk}/protocol/edit/"),
        {f"protocol_note_{top.pk}": "", f"votes_yes_{top.pk}": "²"},
    )
    assert antwort.status_code == 302


# =============================================================================
# Sperre nach der Genehmigung
# =============================================================================


@pytest.fixture
def gesperrt(welt: Welt) -> dict[str, Any]:
    eins = SessionAgendaItem.objects.create(meeting=welt.sitzung, number="1", order=1, name="Eröffnung")
    zwei = SessionAgendaItem.objects.create(meeting=welt.sitzung, number="2", order=2, name="Radweg")
    zeile = SessionAttendance.objects.create(meeting=welt.sitzung, person=welt.mitglieder[0], status="present")
    _genehmigen(welt.sitzung)
    return {"eins": eins, "zwei": zwei, "zeile": zeile}


def _stand(sitzung: SessionMeeting) -> tuple[Any, ...]:
    tops = tuple(sitzung.agenda_items.order_by("order").values_list("name", "number", "is_public", "is_withdrawn"))
    zeilen = tuple(sitzung.attendances.order_by("person__family_name").values_list("person_id", "status"))
    return tops, zeilen


def test_tagesordnung_und_anwesenheit_nach_genehmigung_unveraendert(welt: Welt, gesperrt: dict[str, Any]) -> None:
    vorher = _stand(welt.sitzung)
    eins, zwei, zeile = gesperrt["eins"], gesperrt["zwei"], gesperrt["zeile"]
    aufrufe = [
        (f"/agenda/{eins.pk}/move/", {"direction": "down"}, protocol_lock.MESSAGE_AGENDA),
        (f"/agenda/{zwei.pk}/edit/", {"name": "Umbenannt", "is_public": "on"}, protocol_lock.MESSAGE_RETRACT_ONLY),
        (f"/agenda/{zwei.pk}/edit/", {"name": "Umbenannt"}, protocol_lock.MESSAGE_RETRACT_ONLY),
        (f"/meetings/{welt.sitzung.pk}/agenda/add/", {"name": "Neu nach Genehmigung", "is_public": "on"},
         protocol_lock.MESSAGE_AGENDA),
        (f"/agenda/{zwei.pk}/withdraw/", {"reason": "x"}, protocol_lock.MESSAGE_AGENDA),
        (f"/agenda/{zwei.pk}/delete/", {}, protocol_lock.MESSAGE_DELETE_ITEM),
        (f"/meetings/{welt.sitzung.pk}/agenda/reorder/", {"order": f"{zwei.pk},{eins.pk}"},
         protocol_lock.MESSAGE_AGENDA),
        (f"/attendance/{zeile.pk}/update/", {"status": "absent"}, protocol_lock.MESSAGE_ATTENDANCE),
        (f"/attendance/{zeile.pk}/delete/", {}, protocol_lock.MESSAGE_ATTENDANCE),
        (f"/meetings/{welt.sitzung.pk}/attendance/add/", {"person": welt.mitglieder[1].pk},
         protocol_lock.MESSAGE_ATTENDANCE),
        (f"/meetings/{welt.sitzung.pk}/attendance/generate/", {}, protocol_lock.MESSAGE_ATTENDANCE),
    ]  # fmt: skip
    for pfad, daten, meldung in aufrufe:
        antwort = welt.client.post(welt.url(pfad), daten)
        assert antwort.status_code == 302, f"{pfad}: {antwort.status_code} statt Hinweis auf der Sitzungsseite"
        assert meldung in _meldungen(antwort), pfad
    assert _stand(welt.sitzung) == vorher


def test_sperre_greift_auch_ohne_view(welt: Welt, gesperrt: dict[str, Any]) -> None:
    from apps.session.services import agenda_service

    zwei = gesperrt["zwei"]
    zwei.name = "Umbenannt"
    with pytest.raises(protocol_lock.ProtocolLockedError):
        zwei.save()
    with pytest.raises(protocol_lock.ProtocolLockedError):
        agenda_service.move_item(SessionAgendaItem.objects.get(pk=gesperrt["eins"].pk), "down")
    with pytest.raises(protocol_lock.ProtocolLockedError):
        SessionAgendaItem.objects.create(meeting=welt.sitzung, number="?", name="Neu")
    zeile = gesperrt["zeile"]
    zeile.status = "absent"
    with pytest.raises(protocol_lock.ProtocolLockedError):
        zeile.save()
    with pytest.raises(protocol_lock.ProtocolLockedError):
        SessionAttendance.objects.get(pk=zeile.pk).delete()
    # Berichtigung und Datenschutz-Löschung dürfen weiter schreiben
    with protocol_lock.permit(welt.sitzung.pk):
        zeile.save()


def test_gesperrte_sitzung_zeigt_keine_bearbeitungsknoepfe(welt: Welt, gesperrt: dict[str, Any]) -> None:
    seite = welt.client.get(welt.url(f"/meetings/{welt.sitzung.pk}/")).content.decode()
    assert 'data-testid="tagesordnung-gesperrt"' in seite
    assert f"/agenda/{gesperrt['zwei'].pk}/withdraw/" not in seite
    assert f"/attendance/{gesperrt['zeile'].pk}/delete/" not in seite
    # Bleibt: Rücknahme auf nichtöffentlich
    assert f"/agenda/{gesperrt['zwei'].pk}/edit/" in seite


def test_ruecknahme_auf_nichtoeffentlich_bleibt_moeglich(welt: Welt, gesperrt: dict[str, Any]) -> None:
    """Schutz geht vor: Ein öffentlicher TOP lässt sich auch nach der Genehmigung zurücknehmen – ohne neue Nummern."""
    zwei = gesperrt["zwei"]
    url = welt.url(f"/agenda/{zwei.pk}/edit/")
    assert 'data-testid="top-gesperrt"' in welt.client.get(url).content.decode()

    antwort = welt.client.post(url, {"name": "Radweg"})

    assert antwort.status_code == 302
    zwei.refresh_from_db()
    assert (zwei.is_public, zwei.number, zwei.order) == (False, "2", 2)
    with pytest.raises(protocol_lock.ProtocolLockedError):
        zwei.is_public = True
        zwei.save()


def test_nummernvergabe_nach_genehmigter_niederschrift(welt: Welt) -> None:
    """
    Die Vorlage bekommt ihre Nummer erst bei der Freigabe, ein TOP mit ihr steht schon in einer genehmigten
    Niederschrift: Die Freigabe gelingt, der TOP behält den genehmigten Betreff; offene Sitzungen ziehen nach.
    """
    SessionNumberRange.objects.filter(tenant=welt.tenant).update(assign_on="release")
    vorlage = SessionPaper.objects.create(tenant=welt.tenant, name="Radweg", is_public=True)
    genehmigt = SessionAgendaItem.objects.create(
        meeting=welt.sitzung, number="1", order=1, name="Radweg", paper=vorlage
    )
    _genehmigen(welt.sitzung)
    offen_sitzung = SessionMeeting.objects.create(
        tenant=welt.tenant, name="Rat", organization=welt.gremium, start=welt.sitzung.start + timedelta(days=7)
    )
    offen = SessionAgendaItem.objects.create(meeting=offen_sitzung, number="1", order=1, name="Radweg", paper=vorlage)

    vorlage.status = "approved"
    vorlage.save()  # scheiterte an der Sperre der genehmigten Niederschrift

    vorlage.refresh_from_db()
    assert vorlage.reference
    genehmigt.refresh_from_db()
    offen.refresh_from_db()
    assert genehmigt.name == "Radweg"
    assert offen.name == f"{vorlage.reference}: Radweg"


def test_unterpunkt_eines_oeffentlichen_tops_bietet_keine_ruecknahme(welt: Welt) -> None:
    """Unterpunkte folgen ihrem TOP; einzeln auf nichtöffentlich setzen ließe die Prüfung der Sichtbarkeit nicht zu."""
    top = SessionAgendaItem.objects.create(meeting=welt.sitzung, number="1", order=1, name="Radweg")
    unter = SessionAgendaItem.objects.create(
        meeting=welt.sitzung, number="1.1", order=2, name="Abschnitt Nord", parent=top
    )
    _genehmigen(welt.sitzung)

    seite = welt.client.get(welt.url(f"/meetings/{welt.sitzung.pk}/")).content.decode()

    assert f"/agenda/{top.pk}/edit/" in seite
    assert f"/agenda/{unter.pk}/edit/" not in seite


def test_terminieren_in_gesperrte_sitzung_meldet_die_sperre(welt: Welt) -> None:
    from apps.session.models import SessionConsultation

    vorlage = SessionPaper.objects.create(tenant=welt.tenant, reference="V/1", name="Radweg", status="approved")
    station = SessionConsultation.objects.create(
        paper=vorlage, organization=welt.gremium, meeting=welt.sitzung, order=1
    )
    _genehmigen(welt.sitzung)

    antwort = welt.client.post(welt.url(f"/consultations/{station.pk}/schedule/"))

    assert antwort.status_code == 302
    assert protocol_lock.MESSAGE_AGENDA in _meldungen(antwort)
    assert not welt.sitzung.agenda_items.exists()


# =============================================================================
# Tagesordnung: Ö/NÖ-Wechsel vor die Ende-TOPs
# =============================================================================


def test_wechsel_auf_oeffentlich_reiht_vor_verschiedenes_ein(welt: Welt) -> None:
    from apps.session.services import agenda_service

    SessionAgendaItem.objects.create(meeting=welt.sitzung, number="1", order=1, name="Eröffnung")
    SessionAgendaItem.objects.create(meeting=welt.sitzung, number="2", order=2, name="Verschiedenes", is_end_item=True)
    noe = SessionAgendaItem.objects.create(
        meeting=welt.sitzung, number="N1", order=3, name="Grundstück", is_public=False
    )
    agenda_service.renumber_agenda(welt.sitzung)

    welt.client.post(welt.url(f"/agenda/{noe.pk}/edit/"), {"name": "Grundstück", "is_public": "on"})

    reihenfolge = list(welt.sitzung.agenda_items.order_by("order").values_list("number", "name"))
    assert reihenfolge == [("1", "Eröffnung"), ("2", "Grundstück"), ("3", "Verschiedenes")]


# =============================================================================
# Ladung
# =============================================================================


def test_keine_ladung_zu_abgesagter_sitzung(welt: Welt) -> None:
    welt.sitzung.meeting_state = "cancelled"
    welt.sitzung.save()
    url = welt.url(f"/meetings/{welt.sitzung.pk}/invitation/")
    mail.outbox.clear()

    antwort = welt.client.post(url, {"dispatch_type": "invitation"})

    assert mail.outbox == []
    assert not SessionInvitationDispatch.objects.filter(meeting=welt.sitzung).exists()
    assert any("abgesagt" in m for m in _meldungen(antwort))
    seite = welt.client.get(url).content.decode()
    assert 'data-testid="ladung-gesperrt"' in seite and 'name="dispatch_type"' not in seite
    with pytest.raises(invitation_service.InvitationError):
        invitation_service.send_invitations(welt.sitzung, sent_by=welt.admin)


def test_keine_nachladung_ohne_nachtrags_tops(welt: Welt) -> None:
    SessionAgendaItem.objects.create(meeting=welt.sitzung, number="1", order=1, name="Radweg")
    invitation_service.send_invitations(welt.sitzung, sent_by=welt.admin)
    url = welt.url(f"/meetings/{welt.sitzung.pk}/invitation/")
    assert 'value="supplementary"' not in welt.client.get(url).content.decode()
    mail.outbox.clear()

    antwort = welt.client.post(url, {"dispatch_type": "supplementary"})

    assert mail.outbox == []
    assert any("Nachtrags-TOPs" in m for m in _meldungen(antwort))
    SessionAgendaItem.objects.create(meeting=welt.sitzung, number="2", order=2, name="Neu", is_supplementary=True)
    assert 'value="supplementary"' in welt.client.get(url).content.decode()


# =============================================================================
# Absage, Sitzungsformular, Wahlperiode
# =============================================================================


def _formular(welt: Welt, **aenderungen: Any) -> dict[str, Any]:
    sitzung = welt.sitzung
    daten = {
        "name": sitzung.name,
        "organization": str(sitzung.organization_id),
        "start": timezone.localtime(sitzung.start).strftime("%Y-%m-%dT%H:%M"),
        "end": "",
        "location": "",
        "room": "",
        "is_public": "on",
        "format": "presence",
        "meeting_state": sitzung.meeting_state,
        "cancellation_reason": "",
        "invitation_text": "",
    }
    if sitzung.cancelled:
        daten["cancelled"] = "on"
    daten.update(aenderungen)
    return {key: value for key, value in daten.items() if value is not None}


def test_status_abgesagt_setzt_das_haekchen(welt: Welt) -> None:
    antwort = welt.client.post(
        welt.url(f"/meetings/{welt.sitzung.pk}/edit/"), _formular(welt, meeting_state="cancelled")
    )
    assert antwort.status_code == 302
    welt.sitzung.refresh_from_db()
    assert welt.sitzung.cancelled
    # Der öffentliche Abo-Feed (ohne Anmeldung) prüft das Häkchen
    feed = Client().get(welt.url(f"/organizations/{welt.gremium.pk}/sitzungen.ics")).content.decode()
    assert "BEGIN:VEVENT" not in feed


def test_haekchen_abgesagt_setzt_den_status_und_ruecknahme(welt: Welt) -> None:
    welt.client.post(welt.url(f"/meetings/{welt.sitzung.pk}/edit/"), _formular(welt, cancelled="on"))
    welt.sitzung.refresh_from_db()
    assert welt.sitzung.meeting_state == "cancelled"

    welt.client.post(welt.url(f"/meetings/{welt.sitzung.pk}/edit/"), _formular(welt, cancelled=None))
    welt.sitzung.refresh_from_db()
    assert (welt.sitzung.cancelled, welt.sitzung.meeting_state) == (False, "scheduled")


def test_haekchen_und_anderer_status_zugleich_absage_gilt(welt: Welt) -> None:
    antwort = welt.client.post(
        welt.url(f"/meetings/{welt.sitzung.pk}/edit/"), _formular(welt, cancelled="on", meeting_state="completed")
    )
    assert antwort.status_code == 302
    welt.sitzung.refresh_from_db()
    assert (welt.sitzung.cancelled, welt.sitzung.meeting_state) == (True, "cancelled")


def test_modell_gleicht_absage_ab(welt: Welt) -> None:
    welt.sitzung.cancelled = True
    welt.sitzung.save(update_fields=["cancelled"])
    welt.sitzung.refresh_from_db()
    assert welt.sitzung.meeting_state == "cancelled"


def _admin_formular(welt: Welt, **daten: Any) -> Any:
    from django.forms import modelform_factory

    from apps.session.admin import SessionMeetingAdminForm

    formular_klasse = modelform_factory(
        SessionMeeting, form=SessionMeetingAdminForm, fields=["meeting_state", "cancelled", "cancellation_reason"]
    )
    formular = formular_klasse({"cancellation_reason": "", **daten}, instance=welt.sitzung)
    assert formular.is_valid(), formular.errors
    return formular.save()


def test_admin_nimmt_die_absage_ueber_das_haekchen_zurueck(welt: Welt) -> None:
    welt.sitzung.cancelled = True
    welt.sitzung.save()

    # Häkchen abgewählt, Status unverändert „Abgesagt“: wie im Sitzungsformular zurückgenommen
    sitzung = _admin_formular(welt, meeting_state="cancelled")

    sitzung.refresh_from_db()
    assert (sitzung.cancelled, sitzung.meeting_state) == (False, "scheduled")


def test_admin_aktion_abgeschlossen_ueberspringt_abgesagte(welt: Welt) -> None:
    from django.contrib import admin
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.backends.db import SessionStore
    from django.test import RequestFactory

    from apps.session.admin import SessionMeetingAdmin

    abgesagt = SessionMeeting.objects.create(
        tenant=welt.tenant, name="Abgesagt", organization=welt.gremium, start=welt.sitzung.start, cancelled=True
    )
    request = RequestFactory().post("/admin/")
    request.session = SessionStore()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]

    SessionMeetingAdmin(SessionMeeting, admin.site).mark_completed(
        request, SessionMeeting.objects.filter(tenant=welt.tenant)
    )

    meldungen = [str(m) for m in get_messages(request)]
    assert "1 Sitzung(en) als abgeschlossen markiert." in meldungen
    assert any("1 abgesagte Sitzung(en) übersprungen" in m for m in meldungen)
    abgesagt.refresh_from_db()
    welt.sitzung.refresh_from_db()
    assert (abgesagt.cancelled, abgesagt.meeting_state) == (True, "cancelled")
    assert welt.sitzung.meeting_state == "completed"


def test_sitzungsende_vor_beginn_wird_abgelehnt(welt: Welt) -> None:
    vorher = (timezone.localtime(welt.sitzung.start) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    antwort = welt.client.post(welt.url(f"/meetings/{welt.sitzung.pk}/edit/"), _formular(welt, end=vorher))
    assert antwort.status_code == 200
    assert "Das Ende muss nach dem Beginn" in antwort.content.decode()
    welt.sitzung.refresh_from_db()
    assert welt.sitzung.end is None


def test_ende_vor_beginn_gilt_auch_ausserhalb_des_sitzungsformulars(welt: Welt) -> None:
    """Der Admin und jedes andere ModelForm prüfen über ``SessionMeeting.clean`` – nicht nur das Sitzungsformular."""
    from django.forms import modelform_factory

    formular_klasse = modelform_factory(SessionMeeting, fields=["start", "end"])
    beginn = timezone.localtime(welt.sitzung.start)
    formular = formular_klasse(
        {"start": beginn.strftime("%Y-%m-%d %H:%M"), "end": (beginn - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")},
        instance=welt.sitzung,
    )
    assert not formular.is_valid()
    assert "Das Ende muss nach dem Beginn" in str(formular.errors.get("end"))


def test_gremium_ist_beim_bearbeiten_vorausgewaehlt(welt: Welt) -> None:
    seite = welt.client.get(welt.url(f"/meetings/{welt.sitzung.pk}/edit/")).content.decode()
    assert re.search(rf'<option value="{welt.gremium.pk}"\s+selected>', seite)


def _wahlperioden(tenant: SessionTenant) -> tuple[SessionLegislativeTerm, SessionLegislativeTerm]:
    jahr = timezone.localdate().year
    alt = SessionLegislativeTerm.objects.create(
        tenant=tenant, name="22. WP", number=22, start_date=date(jahr - 2, 1, 1), end_date=date(jahr + 1, 12, 31)
    )
    neu = SessionLegislativeTerm.objects.create(
        tenant=tenant, name="23. WP", number=23, start_date=date(jahr + 2, 1, 1)
    )
    return alt, neu


def test_jahresplanung_setzt_wahlperiode_und_ersteller(welt: Welt) -> None:
    alt, _neu = _wahlperioden(welt.tenant)
    beginn = timezone.localdate() + timedelta(days=30)
    welt.client.post(
        welt.url("/meetings/plan/"),
        {"organization": welt.gremium.pk, "rhythm": "monthly_2", "weekday": "2", "time": "18:00",
         "date_from": beginn.isoformat(), "date_to": (beginn + timedelta(days=62)).isoformat(),
         "is_public": "1", "action": "create"},
    )  # fmt: skip
    entwuerfe = SessionMeeting.objects.filter(tenant=welt.tenant, meeting_state="draft")
    assert entwuerfe.exists()
    assert set(entwuerfe.values_list("legislative_term_id", "created_by_id")) == {(alt.pk, welt.admin.pk)}


def test_verschieben_fuehrt_die_wahlperiode_nach(welt: Welt) -> None:
    alt, neu = _wahlperioden(welt.tenant)
    welt.sitzung.legislative_term = alt
    welt.sitzung.save()
    assert neu.start_date is not None
    spaeter = timezone.localtime(welt.sitzung.start).replace(year=neu.start_date.year, month=2, day=10)
    welt.client.post(
        welt.url(f"/meetings/{welt.sitzung.pk}/edit/"), _formular(welt, start=spaeter.strftime("%Y-%m-%dT%H:%M"))
    )
    welt.sitzung.refresh_from_db()
    assert welt.sitzung.legislative_term_id == neu.pk


@pytest.mark.django_db(transaction=True)
def test_migration_gleicht_bestehende_absagen_ab() -> None:
    vorher, nachher = ("session", "0050_sitzungscockpit"), ("session", "0052_absage_abgleichen")
    executor = MigrationExecutor(connection)
    executor.migrate([vorher])
    try:
        historisch = executor.loader.project_state([vorher]).apps
        tenant = historisch.get_model("session", "SessionTenant").objects.create(name="Alt", slug="alt-absage")
        gremium = historisch.get_model("session", "SessionOrganization").objects.create(tenant=tenant, name="Rat")
        Meeting = historisch.get_model("session", "SessionMeeting")
        jetzt = timezone.now()
        nur_status = Meeting.objects.create(
            tenant=tenant, organization=gremium, name="A", start=jetzt, meeting_state="cancelled"
        )
        nur_haekchen = Meeting.objects.create(
            tenant=tenant, organization=gremium, name="B", start=jetzt, meeting_state="scheduled", cancelled=True
        )
        offen = Meeting.objects.create(tenant=tenant, organization=gremium, name="C", start=jetzt)

        executor = MigrationExecutor(connection)
        executor.migrate([nachher])
        Meeting = executor.loader.project_state([nachher]).apps.get_model("session", "SessionMeeting")
        stand = {m.pk: (m.cancelled, m.meeting_state) for m in Meeting.objects.filter(tenant_id=tenant.pk)}
        assert stand[nur_status.pk] == (True, "cancelled")
        assert stand[nur_haekchen.pk] == (True, "cancelled")
        assert stand[offen.pk] == (False, "draft")
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


# =============================================================================
# Robustheit: Parameter und Kennungen
# =============================================================================


@pytest.mark.parametrize(
    "pfad",
    [
        "/resolutions/?year=0",
        "/resolutions/?year=99999999999",
        "/resolutions/export.csv?year=99999999999",
        "/resolutions/export.csv?year=0",
        "/meetings/calendar/?month=99999999999",
        "/meetings/calendar/?year=99999999999",
        "/meetings/calendar/?year=-5&month=13",
    ],
)
def test_jahres_und_monatsparameter_ausserhalb_des_bereichs(welt: Welt, pfad: str) -> None:
    assert welt.client.get(welt.url(pfad)).status_code == 200


@pytest.mark.parametrize("person", ["abc", ""])
def test_gast_ergaenzen_mit_ungueltiger_kennung(welt: Welt, person: str) -> None:
    antwort = welt.client.post(welt.url(f"/meetings/{welt.sitzung.pk}/attendance/add/"), {"person": person})
    assert antwort.status_code == 302
    assert "Bitte wählen Sie eine Person aus." in _meldungen(antwort)


def test_genehmigung_mit_ungueltiger_folgesitzung(welt: Welt) -> None:
    SessionProtocol.objects.create(meeting=welt.sitzung, content="x", status="review")
    antwort = welt.client.post(
        welt.url(f"/meetings/{welt.sitzung.pk}/protocol/approve/"), {"approval_meeting": "abc", "approval_item": "x"}
    )
    assert antwort.status_code == 302
    protokoll = SessionProtocol.objects.get(meeting=welt.sitzung)
    assert protokoll.status == "approved" and protokoll.approval_meeting_id is None


def test_blaettern_behaelt_alle_filter(welt: Welt) -> None:
    periode = SessionLegislativeTerm.objects.create(tenant=welt.tenant, name="22. WP", start_date=date(2000, 1, 1))
    for nummer in range(25):
        SessionMeeting.objects.create(
            tenant=welt.tenant, name=f"Sitzung {nummer}", organization=welt.gremium,
            start=welt.sitzung.start + timedelta(days=nummer + 1), legislative_term=periode,
        )  # fmt: skip
    seite = welt.client.get(welt.url(f"/meetings/?term={periode.pk}&from=2000-01-01")).content.decode()
    weiter = re.findall(r'href="(\?[^"]*page=2[^"]*)"', seite)
    assert weiter, "Link zu Seite 2 fehlt"
    assert all(f"term={periode.pk}" in link and "from=2000-01-01" in link for link in weiter)


# =============================================================================
# Rückmeldungen und Erinnerungen
# =============================================================================


def test_zusage_in_der_zeile_entlastet_die_stellvertretung(welt: Welt) -> None:
    amsel = welt.mitglieder[0]
    vertretung = _person(welt.tenant, "Stellv", "stellv@example.org")
    SessionOrganizationMembership.objects.create(
        organization=welt.gremium, person=vertretung, has_voting_rights=True, substitute_for=amsel
    )
    invitation_service.send_invitations(welt.sitzung, sent_by=welt.admin)
    ergebnis = invitation_response_service.record_response(
        welt.sitzung, amsel, decision="decline", source="link", reason="Urlaub", substitute_requested=True
    )
    assert ergebnis.substitutes is not None and ergebnis.substitutes.notified == ["P Stellv"]
    mail.outbox.clear()

    zeile = SessionAttendance.objects.get(meeting=welt.sitzung, person=amsel)
    welt.client.post(welt.url(f"/attendance/{zeile.pk}/update/"), {"status": "confirmed"}, HTTP_HX_REQUEST="true")

    zeile.refresh_from_db()
    assert zeile.status == "confirmed"
    assert not zeile.substitute_requested and zeile.substitutes_notified_at is None
    assert [m.to for m in mail.outbox] == [["stellv@example.org"]], "Entwarnung an die Stellvertretung"


def test_erinnerung_erst_nach_der_ladung(welt: Welt, settings: Any) -> None:
    settings.SITE_URL = "https://mandari.example"
    attendance_service.generate_attendance(welt.sitzung)
    assert rsvp_reminders.targets(welt.sitzung) == []

    welt.sitzung.invitation_sent_at = timezone.now()
    welt.sitzung.save()
    ziele = rsvp_reminders.targets(welt.sitzung)
    assert ziele and all(ziel.recipient is None for ziel in ziele)
    mail.outbox.clear()
    assert rsvp_reminders.send(ziele[0], welt.sitzung)
    nachricht = mail.outbox[0]
    assert "/session/" not in nachricht.body, "Kein Link in die Verwaltungsoberfläche"
    assert "Stadt Korrektur" in nachricht.from_email


def test_keine_erinnerung_zu_abgesagter_sitzung(welt: Welt) -> None:
    attendance_service.generate_attendance(welt.sitzung)
    welt.sitzung.invitation_sent_at = timezone.now()
    welt.sitzung.cancelled = True
    welt.sitzung.save()
    assert rsvp_reminders.targets(welt.sitzung) == []


# =============================================================================
# Anwesenheit ohne Besetzung, Niederschrift-Text, Darstellung
# =============================================================================


def test_liste_aus_besetzung_ohne_besetzung_meldet_das(welt: Welt) -> None:
    leer = SessionOrganization.objects.create(tenant=welt.tenant, name="Regionalausschuss")
    sitzung = SessionMeeting.objects.create(
        tenant=welt.tenant, name="Regionalausschuss", organization=leer, start=welt.sitzung.start
    )
    antwort = welt.client.post(welt.url(f"/meetings/{sitzung.pk}/attendance/generate/"))
    meldungen = _meldungen(antwort)
    assert not any("bereits vollständig" in m for m in meldungen)
    assert any("keine Besetzung" in m for m in meldungen)


def test_mitwirkungsverbot_nennt_keine_landesnorm(welt: Welt) -> None:
    top = SessionAgendaItem.objects.create(meeting=welt.sitzung, number="1", order=1, name="Radweg")
    SessionAttendance.objects.create(meeting=welt.sitzung, person=welt.mitglieder[0], status="present")
    SessionVote.objects.create(agenda_item=top, person=welt.mitglieder[0], vote="excluded")
    text = "\n".join(protocol_publication._item_lines(top))
    assert "Mitwirkungsverbot wegen Befangenheit: P Amsel" in text
    assert "§ 31" not in text
    for vorlage in ("session/pdf/partial_protocol_top.html", "session/pdf/resolution_extract.html"):
        assert "§ 31" not in (TEMPLATES / vorlage).read_text(encoding="utf-8")


#: Feldtypen ohne eigenen Rahmen
_OHNE_RAHMEN = ("hidden", "checkbox", "radio", "submit", "file", "button")


def test_formularfelder_im_sitzungsdienst_haben_einen_rahmen() -> None:
    """Ohne Tailwind-Forms-Plugin ist ein Feld ohne ``border`` weiß auf weißer Karte – praktisch unsichtbar."""
    fehlend = []
    for datei in sorted((TEMPLATES / "session").rglob("*.html")):
        text = datei.read_text(encoding="utf-8")
        for treffer in re.finditer(r"<(input|textarea|select)\b([^>]*)>", text, re.S):
            attribute = treffer.group(2)
            typ = re.search(r'type="([^"]+)"', attribute)
            klasse = re.search(r'class="([^"]*)"', attribute, re.S)
            if (typ and typ.group(1) in _OHNE_RAHMEN) or not klasse:
                continue
            tokens = klasse.group(1).split()
            if any(t in ("border", "border-2", "border-b", "border-0", "sr-only", "hidden") for t in tokens):
                continue
            if any(t.startswith("ring") for t in tokens):
                continue
            zeile = text[: treffer.start()].count("\n") + 1
            fehlend.append(f"{datei.relative_to(TEMPLATES)}:{zeile}")
    assert fehlend == []


def test_anlagenzeile_bricht_um_statt_ueberzulaufen() -> None:
    text = (TEMPLATES / "session/partials/file_section.html").read_text(encoding="utf-8")
    metazeile = re.search(r'<p class="([^"]*)">\s*<span class="whitespace-nowrap">\{\{ file.size_human', text)
    assert metazeile and "flex-wrap" in metazeile.group(1).split()


def test_nachgeladene_empfaenger_ohne_versand_erhalten_keinen_link(welt: Welt) -> None:
    """Gegenprobe: Mit Versand erinnert die Mail mit persönlichem Rückmeldelink (unverändert)."""
    invitation_service.send_invitations(welt.sitzung, sent_by=welt.admin)
    ziele = rsvp_reminders.targets(welt.sitzung)
    assert ziele and all(isinstance(ziel.recipient, SessionInvitationRecipient) for ziel in ziele)
