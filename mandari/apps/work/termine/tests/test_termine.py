# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigene Termine im Kalender (Issue #1489, Schritt 1): anlegen, ansehen, ändern, löschen, einladen und antworten;
Sichtbarkeit für alle, eine Sitzungsart oder nur Eingeladene; Rechte ``calendar.create`` und ``calendar.manage``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.common.tests import factories
from apps.work.faction.feeds import build_personal_feed
from apps.work.faction.models import FactionMeeting, FactionMeetingType
from apps.work.notifications.models import Notification, NotificationType
from apps.work.organization.export_service import dsgvo_export_service
from apps.work.organization.models import MemberAbsence
from apps.work.termine import termine
from apps.work.termine.models import Termin, TerminTeilnahme

pytestmark = pytest.mark.django_db

LESEN = ["dashboard.view", "meetings.view"]
ANLEGEN = [*LESEN, "calendar.create"]


@pytest.fixture
def org(org: Any) -> Any:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


@pytest.fixture
def vera(org: Any, make_member: Any) -> Any:
    return make_member(org, ANLEGEN, first_name="Vera", last_name="Beispiel")


@pytest.fixture
def karim(org: Any, make_member: Any) -> Any:
    return make_member(org, LESEN, first_name="Karim", last_name="Kaya")


def _url(name: str, org: Any, **kwargs: Any) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug, **kwargs})


def _formular(**felder: Any) -> dict[str, Any]:
    morgen = timezone.localdate() + timedelta(days=1)
    return {
        "title": "Ortstermin Brücke Mühlenweg",
        "tag": morgen.isoformat(),
        "beginn": "17:30",
        "ende": "18:30",
        "location": "Mühlenweg 4",
        "video_url": "https://meet.example.org/bruecke",
        "description": "Zustand vor der Sanierung ansehen.\nTreffpunkt am Parkplatz.",
        "visibility": Termin.ALLE,
        **felder,
    }


def _im_kalender(client: Any, org: Any) -> dict[str, dict[str, Any]]:
    jetzt = timezone.now()
    fenster = {"start": (jetzt - timedelta(days=5)).isoformat(), "end": (jetzt + timedelta(days=40)).isoformat()}
    eintraege = client.get(_url("meetings_calendar_events", org), {**fenster, "arten": "termin"}).json()
    return {eintrag["title"]: eintrag for eintrag in eintraege}


def test_anlegen_ansehen_aendern_und_loeschen(org: Any, vera: Any, client_for: Any) -> None:
    client = client_for(vera.user)
    seite = client.get(_url("termin_neu", org) + "?tag=2027-02-03").content.decode()
    assert 'data-testid="termin-formular"' in seite and 'value="2027-02-03"' in seite and "Neuer Termin" in seite

    antwort = client.post(_url("termin_neu", org), _formular())
    assert antwort.status_code == 302 and antwort.url == _url("meetings_calendar", org)
    termin: Any = Termin.objects.get()
    assert (termin.title, termin.location, termin.created_by) == ("Ortstermin Brücke Mühlenweg", "Mühlenweg 4", vera)
    assert timezone.localtime(termin.start).strftime("%H:%M") == "17:30" and not termin.all_day
    # Die Beschreibung liegt verschlüsselt und kommt lesbar zurück
    assert b"Sanierung" not in bytes(termin.description_encrypted or b"")
    assert termin.get_description_decrypted().startswith("Zustand vor der Sanierung")

    eintrag = _im_kalender(client, org)["Ortstermin Brücke Mühlenweg"]
    assert eintrag["url"] == _url("termin", org, termin_id=termin.pk) and eintrag["classNames"] == ["termin-termin"]
    assert eintrag["allDay"] is False

    bearbeiten = client.get(_url("termin", org, termin_id=termin.pk)).content.decode()
    assert 'value="17:30"' in bearbeiten and "Mühlenweg 4" in bearbeiten and "17:30 bis 18:30 Uhr" in bearbeiten

    # Ende vor Beginn: Die Seite kommt mit dem Fehler und den Eingaben zurück, gespeichert wird nichts
    falsch = client.post(_url("termin", org, termin_id=termin.pk), _formular(ende="16:00", title="Geändert"))
    assert falsch.status_code == 422 and "Das Ende liegt nicht nach dem Beginn." in falsch.content.decode()
    assert 'value="16:00"' in falsch.content.decode() and Termin.objects.get().title != "Geändert"

    # Ganztägig über zwei Tage: Im Kalender endet der Eintrag am Folgetag (wie in Kalenderprogrammen)
    tag = timezone.localdate() + timedelta(days=2)
    client.post(
        _url("termin", org, termin_id=termin.pk),
        _formular(title="Klausur", all_day="on", tag=tag.isoformat(), bis_tag=(tag + timedelta(days=1)).isoformat()),
    )
    klausur = _im_kalender(client, org)["Klausur"]
    assert klausur["allDay"] is True and klausur["start"] == tag.isoformat()
    assert klausur["end"] == (tag + timedelta(days=2)).isoformat()
    assert termine.zeitangabe(Termin.objects.get()).endswith("ganztägig")

    assert client.post(_url("termin_loeschen", org, termin_id=termin.pk)).status_code == 302
    assert not Termin.objects.exists()


def test_sichtbarkeit_und_rechte(org: Any, vera: Any, karim: Any, make_member: Any, client_for: Any) -> None:
    verwaltung = make_member(org, [*LESEN, "calendar.manage"])
    arbeitskreis = FactionMeetingType.objects.create(organization=org, name="AK Bauen", participants_mode="persons")
    arbeitskreis.participants.add(vera)

    def anlegen(titel: str, **felder: Any) -> Termin:
        form = termine.TerminForm(_formular(title=titel, **felder), membership=vera)
        assert form.is_valid(), form.errors
        return form.speichern(termine.neu(org, vera))

    offen = anlegen("Für alle")
    privat = anlegen("Nur ich", visibility=Termin.PRIVAT)
    gruppe = anlegen("Im Arbeitskreis", visibility=Termin.ART, meeting_type=str(arbeitskreis.pk))

    def sieht(mitglied: Any) -> set[str]:
        return set(termine.sichtbare(mitglied).values_list("title", flat=True))

    assert sieht(vera) == {"Für alle", "Nur ich", "Im Arbeitskreis"}
    assert sieht(karim) == {"Für alle"}
    # Wer alle Termine verwaltet, sieht auch die der Gruppe, aber keine privaten Termine anderer
    assert sieht(verwaltung) == {"Für alle", "Im Arbeitskreis"}
    arbeitskreis.participants.add(karim)
    assert sieht(karim) == {"Für alle", "Im Arbeitskreis"}
    gast = cast(Any, factories).MembershipFactory(organization=org, is_guest=True)
    assert not termine.sichtbare(gast).exists()

    # Ohne die Art zu nennen, lässt sich „Teilnehmende einer Sitzungsart“ nicht speichern
    ohne_art = termine.TerminForm(_formular(visibility=Termin.ART), membership=vera)
    assert not ohne_art.is_valid() and "meeting_type" in ohne_art.errors

    # Lesen ja, ändern nein: Karim bekommt die Angaben ohne Formular und darf weder speichern noch löschen
    kunde = client_for(karim.user)
    seite = kunde.get(_url("termin", org, termin_id=offen.pk)).content.decode()
    assert 'data-testid="termin-angaben"' in seite and 'data-testid="termin-formular"' not in seite
    assert "Zustand vor der Sanierung" in seite and "Termin löschen" not in seite
    assert kunde.post(_url("termin", org, termin_id=offen.pk), _formular(title="Übernommen")).status_code == 403
    assert kunde.post(_url("termin_loeschen", org, termin_id=offen.pk)).status_code == 403
    assert kunde.get(_url("termin", org, termin_id=privat.pk)).status_code == 404
    assert kunde.get(_url("termin_neu", org)).status_code == 403
    assert Termin.objects.get(pk=offen.pk).title == "Für alle"

    # Verwalten: fremde Termine ändern, private anderer nicht einmal sehen
    leitung = client_for(verwaltung.user)
    assert leitung.post(_url("termin", org, termin_id=gruppe.pk), _formular(title="Verlegt")).status_code == 302
    assert Termin.objects.get(pk=gruppe.pk).title == "Verlegt"
    assert leitung.post(_url("termin_loeschen", org, termin_id=privat.pk)).status_code == 404


def test_einladen_benachrichtigen_und_antworten(org: Any, karim: Any, make_member: Any, client_for: Any) -> None:
    vera = make_member(org, [*ANLEGEN, "faction.view_public"], first_name="Vera", last_name="Beispiel")
    lea = make_member(org, [*LESEN, "faction.view_public"], first_name="Lea", last_name="Lorenz")
    sitzung = FactionMeeting.objects.create(
        organization=org, title="Fraktionssitzung", start=timezone.now() + timedelta(days=2), created_by=vera
    )
    client = client_for(vera.user)
    client.post(
        _url("termin_neu", org),
        _formular(
            title="Vorgespräch",
            visibility=Termin.PRIVAT,
            eingeladene=[str(karim.pk), str(lea.pk)],
            faction_meeting=str(sitzung.pk),
        ),
    )
    termin = Termin.objects.get()
    assert termin.faction_meeting == sitzung
    assert set(termin.teilnahmen.values_list("membership_id", "status")) == {(karim.pk, "offen"), (lea.pk, "offen")}
    einladungen = Notification.objects.filter(notification_type=NotificationType.TERMIN_EINLADUNG)
    assert set(einladungen.values_list("recipient_id", flat=True)) == {karim.pk, lea.pk}
    erste = einladungen.first()
    assert erste is not None and "Vorgespräch" in erste.message

    # Eingeladene sehen auch einen privaten Termin und antworten auf seiner Seite
    gast = client_for(karim.user)
    seite = gast.get(_url("termin", org, termin_id=termin.pk)).content.decode()
    assert 'data-testid="termin-antwort"' in seite and "Noch keine Antwort" in seite and "Lea Lorenz" in seite
    # Die verknüpfte Sitzung nennt die Seite nur, wer interne Sitzungen sehen darf (Lea ja, Karim nein)
    zur_sitzung = reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": sitzung.pk})
    angaben = seite[seite.index('data-testid="termin-angaben"') : seite.index('data-testid="termin-teilnahmen"')]
    assert zur_sitzung not in seite and "Fraktionssitzung" not in angaben
    assert zur_sitzung in client_for(lea.user).get(_url("termin", org, termin_id=termin.pk)).content.decode()
    gast.post(_url("termin_antwort", org, termin_id=termin.pk), {"antwort": "zugesagt"})
    teilnahme = TerminTeilnahme.objects.get(termin=termin, membership=karim)
    assert teilnahme.status == "zugesagt" and teilnahme.geantwortet_am is not None
    gast.post(_url("termin_antwort", org, termin_id=termin.pk), {"antwort": "irgendwas"})
    assert TerminTeilnahme.objects.get(pk=teilnahme.pk).status == "zugesagt"
    # Wer anlegt, ist nicht eingeladen und kann deshalb nicht antworten
    assert not termine.antworten(termin, vera, "zugesagt")

    # Lea wird ausgeladen, Karim bleibt: seine Antwort bleibt stehen, niemand wird erneut benachrichtigt
    client.post(
        _url("termin", org, termin_id=termin.pk),
        _formular(title="Vorgespräch", visibility=Termin.PRIVAT, eingeladene=[str(karim.pk)]),
    )
    assert list(termin.teilnahmen.values_list("membership_id", "status")) == [(karim.pk, "zugesagt")]
    assert einladungen.count() == 2
    assert not termine.sichtbare(lea).exists()


def test_verwaltung_aendert_ohne_zuordnungen_zu_verlieren(
    org: Any, karim: Any, make_member: Any, client_for: Any
) -> None:
    """Wer alle Termine verwaltet, ändert einen fremden Termin, ohne Sitzungsart, Sitzung oder Einladungen zu lösen."""
    vera = make_member(org, [*ANLEGEN, "faction.view_public"], first_name="Vera", last_name="Beispiel")
    leitung = make_member(org, [*LESEN, "calendar.manage"], first_name="Lena", last_name="Leitung")
    arbeitskreis = FactionMeetingType.objects.create(
        organization=org, name="AK Bauen", participants_mode="persons", visibility="participants"
    )
    arbeitskreis.participants.add(vera)
    # Eine Sitzung des Arbeitskreises vor sechs Wochen: außerhalb der Auswahl für neue Verknüpfungen
    alt = FactionMeeting.objects.create(
        organization=org,
        title="Vertrauliche Runde",
        start=timezone.now() - timedelta(days=42),
        meeting_type=arbeitskreis,
        created_by=vera,
    )
    form = termine.TerminForm(
        _formular(
            title="Nachbereitung",
            visibility=Termin.ART,
            meeting_type=str(arbeitskreis.pk),
            eingeladene=[str(karim.pk), str(leitung.pk)],
        ),
        membership=vera,
    )
    assert form.is_valid(), form.errors
    termin = form.speichern(termine.neu(org, vera))
    Termin.objects.filter(pk=termin.pk).update(faction_meeting=alt)
    adresse = _url("termin", org, termin_id=termin.pk)

    # Vera ändert den Titel: Die alte Sitzung steht weiter zur Wahl und bleibt verknüpft
    seite = client_for(vera.user).get(adresse).content.decode()
    assert f'value="{alt.pk}" selected' in seite
    eingaben = {"visibility": Termin.ART, "meeting_type": str(arbeitskreis.pk), "faction_meeting": str(alt.pk)}
    eingeladen = [str(karim.pk), str(leitung.pk)]
    client_for(vera.user).post(adresse, _formular(title="Nachbereitung I", eingeladene=eingeladen, **eingaben))
    assert Termin.objects.get(pk=termin.pk).faction_meeting == alt

    # Die Leitung gehört nicht zum Arbeitskreis und sieht dessen Sitzung nicht: Sie bekommt die Sitzungsart
    # trotzdem angeboten, die Sitzung nicht, und ihre eigene Einladung steht in der Liste
    client = client_for(leitung.user)
    seite = client.get(adresse).content.decode()
    assert f'value="{arbeitskreis.pk}" selected' in seite and "Vertrauliche Runde" not in seite
    assert f'id="termin-eingeladen-{leitung.pk}"' in seite
    eingaben.pop("faction_meeting")
    antwort = client.post(adresse, _formular(title="Nachbereitung II", eingeladene=eingeladen, **eingaben))
    assert antwort.status_code == 302, antwort.content.decode()[:2000]
    termin = Termin.objects.get(pk=termin.pk)
    assert (termin.title, termin.meeting_type, termin.faction_meeting) == ("Nachbereitung II", arbeitskreis, alt)
    assert set(termin.teilnahmen.values_list("membership_id", flat=True)) == {karim.pk, leitung.pk}


def test_vertretung_bekommt_nur_sichtbare_termine(org: Any, vera: Any, karim: Any, make_member: Any) -> None:
    """Ist die eingeladene Person abwesend, geht die Einladung nur an eine Vertretung, die den Termin sehen darf."""
    lea = make_member(org, LESEN, first_name="Lea", last_name="Lorenz")
    heute = timezone.localdate()
    MemberAbsence.objects.create(
        organization=org,
        membership=karim,
        deputy=lea,
        start_date=heute - timedelta(days=1),
        end_date=heute + timedelta(days=7),
        notify_deputy=True,
    )

    def einladen(titel: str, sichtbar: str) -> None:
        form = termine.TerminForm(
            _formular(title=titel, visibility=sichtbar, eingeladene=[str(karim.pk)]), membership=vera
        )
        assert form.is_valid(), form.errors
        form.speichern(termine.neu(org, vera))

    einladen("Vertrauliches Vorgespräch", Termin.PRIVAT)
    einladen("Ortstermin für alle", Termin.ALLE)
    an_vertretung = Notification.objects.filter(recipient=lea, notification_type=NotificationType.TERMIN_EINLADUNG)
    assert [("Ortstermin" in meldung.message, "Vertraulich" in meldung.message) for meldung in an_vertretung] == [
        (True, False)
    ]
    assert (
        Notification.objects.filter(recipient=karim, notification_type=NotificationType.TERMIN_EINLADUNG).count() == 2
    )


def test_wege_zum_termin(org: Any, vera: Any, karim: Any, client_for: Any) -> None:
    kalender = client_for(vera.user).get(_url("meetings_calendar", org)).content.decode()
    neu = _url("termin_neu", org)
    assert f'data-neu="{neu}"' in kalender and "Termin anlegen" in kalender
    assert 'id="kalender-art-termin"' in kalender
    # „Neu“ in der Seitenleiste führt ebenfalls dorthin
    leiste = kalender[kalender.index('id="neu-menue"') : kalender.index("</aside>")]
    assert f'href="{neu}"' in leiste

    ohne = client_for(karim.user).get(_url("meetings_calendar", org)).content.decode()
    assert "data-neu=" not in ohne and "Termin anlegen" not in ohne and 'id="kalender-art-termin"' in ohne
    # Der Weg in den eigenen Kalender (Google, Apple, Outlook) steht für alle am Kalender und führt ins Profil
    assert _url("profile", org) + "#profil-kalender" in ohne
    profil = client_for(karim.user).get(_url("profile", org)).content.decode()
    assert 'id="profil-kalender"' in profil and 'data-testid="kalender-abo-wege"' in profil


def test_auskunft_nennt_termine_und_einladungen(org: Any, vera: Any, karim: Any) -> None:
    """DSGVO-Auskunft: selbst angelegte Termine mit Inhalt, Einladungen mit der eigenen Antwort."""
    form = termine.TerminForm(_formular(title="Vorgespräch", eingeladene=[str(karim.pk)]), membership=vera)
    assert form.is_valid(), form.errors
    termin = form.speichern(termine.neu(org, vera))
    assert termine.antworten(termin, karim, "zugesagt")

    eigene = dsgvo_export_service.collect_user_data(user=vera.user, membership=vera, organization=org)["termine"]
    assert [eintrag["title"] for eintrag in eigene["created"]] == ["Vorgespräch"] and not eigene["invitations"]
    assert eigene["created"][0]["description"].startswith("Zustand vor der Sanierung")
    assert eigene["created"][0]["invited_count"] == 1 and eigene["created"][0]["start"].endswith("17:30")

    daten = dsgvo_export_service.collect_user_data(user=karim.user, membership=karim, organization=org)
    assert not daten["termine"]["created"]
    assert [(e["title"], e["status"]) for e in daten["termine"]["invitations"]] == [("Vorgespräch", "Zugesagt")]
    seite = render_to_string(
        "work/profile/export/dsgvo_export.html",
        {"data": dsgvo_export_service.pdf_data(daten), "user": karim.user, "organization": org},
    )
    assert "17. Termine im Kalender" in seite and "Zugesagt" in seite


def test_persoenlicher_abruf_enthaelt_termine(org: Any, vera: Any, karim: Any, make_member: Any) -> None:
    """Der persönliche Abruf (iCal) trägt die sichtbaren Termine in den eigenen Kalender, ohne ihre Inhalte."""
    ohne_recht = make_member(org, ["dashboard.view"])
    gast = cast(Any, factories).MembershipFactory(organization=org, is_guest=True)

    def anlegen(titel: str, **felder: Any) -> Termin:
        form = termine.TerminForm(_formular(title=titel, **felder), membership=vera)
        assert form.is_valid(), form.errors
        return form.speichern(termine.neu(org, vera))

    def abruf(mitglied: Any) -> str:
        # Lange Zeilen sind im Abruf umbrochen (RFC 5545); zum Prüfen wieder zusammensetzen
        return build_personal_feed(mitglied.user).decode().replace("\r\n ", "")

    tag = timezone.localdate() + timedelta(days=3)
    anlegen("Ortstermin Brücke")
    anlegen("Klausur", all_day="on", tag=tag.isoformat(), bis_tag=(tag + timedelta(days=1)).isoformat())
    privat = anlegen("Vorgespräch", visibility=Termin.PRIVAT, eingeladene=[str(karim.pk)])
    anlegen("Nur Vera", visibility=Termin.PRIVAT)

    feed = abruf(karim)
    assert "SUMMARY:Ortstermin Brücke" in feed and "SUMMARY:Vorgespräch" in feed and "Nur Vera" not in feed
    assert "LOCATION:Mühlenweg 4" in feed and f"/work/{org.slug}/termine/{privat.pk}/" in feed
    # Beschreibung und Videolink bleiben in Work
    assert "Sanierung" not in feed and "meet.example.org" not in feed
    # Ganztägig über zwei Tage: Kalendertage, das Ende ist der Tag danach
    assert f"DTSTART;VALUE=DATE:{tag:%Y%m%d}" in feed
    assert f"DTEND;VALUE=DATE:{tag + timedelta(days=2):%Y%m%d}" in feed

    # Wer abgesagt hat, bekommt den Termin nicht in den eigenen Kalender
    assert termine.antworten(privat, karim, "abgesagt")
    assert "Vorgespräch" not in abruf(karim) and "Ortstermin Brücke" in abruf(karim)
    assert "Vorgespräch" in abruf(vera) and "Nur Vera" in abruf(vera)
    # Ohne das Recht auf Sitzungen und als Gast: keine Termine
    assert "Ortstermin" not in abruf(ohne_recht) and "Ortstermin" not in abruf(gast)
