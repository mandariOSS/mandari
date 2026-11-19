# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Terminumfrage (Issue #1489, Schritt 4): Vorschläge anlegen, antworten, einen Vorschlag festlegen (daraus wird der
Termin), dazu Sichtbarkeit, Kalender, Auskunft.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.faction.models import FactionMeetingType
from apps.work.notifications.models import Notification, NotificationType
from apps.work.notifications.services import NotificationHub
from apps.work.organization.export_service import dsgvo_export_service
from apps.work.organization.models import MemberAbsence
from apps.work.termine import meldungen, termine, umfrage
from apps.work.termine.models import Termin, TerminTeilnahme, Terminumfrage, VorschlagAntwort

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


@pytest.fixture
def lea(org: Any, make_member: Any) -> Any:
    return make_member(org, LESEN, first_name="Lea", last_name="Lorenz")


def _url(name: str, org: Any, **kwargs: Any) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug, **kwargs})


def _tag(nach: int) -> date:
    return timezone.localdate() + timedelta(days=nach)


def _formular(**felder: Any) -> dict[str, Any]:
    return {
        "title": "Klausur im Frühjahr",
        "location": "Rathaus, Raum 2",
        "description": "Haushalt und Arbeitsprogramm.",
        "vorschlag_tag": [_tag(10).isoformat(), _tag(11).isoformat(), _tag(12).isoformat(), ""],
        "vorschlag_beginn": ["18:00", "18:00", "17:00", ""],
        "vorschlag_ende": ["20:00", "", "19:00", ""],
        "wen": umfrage.ALLE,
        **felder,
    }


def _anlegen(org: Any, mitglied: Any, **felder: Any) -> Terminumfrage:
    form = umfrage.UmfrageForm(_als_anfrage(_formular(**felder)), membership=mitglied)
    assert form.is_valid(), form.errors
    return form.speichern(org)


def _als_anfrage(daten: dict[str, Any]) -> Any:
    """Wie die Anfrage: Listen bleiben mehrere Werte desselben Namens (QueryDict)."""
    from django.http import QueryDict

    anfrage = QueryDict(mutable=True)
    for name, wert in daten.items():
        anfrage.setlist(name, wert if isinstance(wert, list) else [wert])
    return anfrage


def test_anlegen_antworten_und_festlegen(org: Any, vera: Any, karim: Any, lea: Any, client_for: Any) -> None:
    client = client_for(vera.user)
    seite = client.get(_url("terminumfrage_neu", org)).content.decode()
    assert 'data-testid="umfrage-formular"' in seite and seite.count('name="vorschlag_tag"') == umfrage.ZEILEN + 1

    antwort = client.post(_url("terminumfrage_neu", org), _formular())
    neue: Any = Terminumfrage.objects.get()
    assert antwort.status_code == 302 and antwort.url == _url("terminumfrage", org, umfrage_id=neue.pk)
    assert neue.created_by == vera and neue.abgeschlossen_am is None
    assert b"Haushalt" not in bytes(neue.description_encrypted or b"")
    erster, zweiter, dritter = neue.vorschlaege.order_by("start")
    assert timezone.localtime(erster.start).strftime("%H:%M") == "18:00" and zweiter.end is None
    # Gefragt sind alle Mitglieder, auch wer fragt; benachrichtigt werden nur die anderen
    gefragt = set(neue.teilnahmen.values_list("membership_id", flat=True))
    assert {vera.pk, karim.pk, lea.pk} <= gefragt
    meldungen = Notification.objects.filter(notification_type=NotificationType.TERMIN_UMFRAGE)
    empfaenger = set(meldungen.values_list("recipient_id", flat=True))
    assert {karim.pk, lea.pk} <= empfaenger and vera.pk not in empfaenger

    # Karim antwortet je Vorschlag, Lea nur auf einen; eine unbekannte Antwort zählt nicht
    adresse = _url("terminumfrage", org, umfrage_id=neue.pk)
    gast = client_for(karim.user)
    seite = gast.get(adresse).content.decode()
    assert f'name="wahl-{erster.pk}"' in seite and 'data-testid="umfrage-festlegen"' not in seite
    gast.post(adresse, {f"wahl-{erster.pk}": "ja", f"wahl-{zweiter.pk}": "nein", f"wahl-{dritter.pk}": "notfalls"})
    client_for(lea.user).post(adresse, {f"wahl-{erster.pk}": "ja", f"wahl-{zweiter.pk}": "egal"})
    assert VorschlagAntwort.objects.count() == 4
    gast.post(adresse, {f"wahl-{zweiter.pk}": "notfalls"})
    assert VorschlagAntwort.objects.get(vorschlag=zweiter, membership=karim).wahl == "notfalls"

    stand = umfrage.uebersicht(neue, vera)
    assert stand["beantwortet"] == 2 and "Vera Beispiel" in stand["ohne_antwort"]
    assert [zeile["beste"] for zeile in stand["zeilen"]] == [True, False, False]
    assert stand["zeilen"][0]["ja"] == ["Karim Kaya", "Lea Lorenz"] and stand["zeilen"][2]["notfalls"] == ["Karim Kaya"]
    seite = client.get(adresse).content.decode()
    assert 'data-testid="umfrage-festlegen"' in seite and "2 Ja (Karim Kaya, Lea Lorenz)" in seite
    assert (
        "bisher die meisten Zusagen" in seite and "Rathaus, Raum 2" in seite and "Haushalt und Arbeitsprogramm" in seite
    )

    # Festlegen darf nur, wer angelegt hat: Daraus wird der Termin, die Antworten stehen schon daran
    festlegen = _url("terminumfrage_festlegen", org, umfrage_id=neue.pk)
    assert gast.post(festlegen, {"vorschlag": str(erster.pk)}).status_code == 403
    # Ohne oder mit unbekanntem Vorschlag passiert nichts
    assert client.post(festlegen, {"vorschlag": "kein-vorschlag"}).status_code == 302
    assert client.post(festlegen).status_code == 302 and not Termin.objects.exists()
    antwort = client.post(festlegen, {"vorschlag": str(erster.pk)})
    termin: Any = Termin.objects.get()
    assert antwort.status_code == 302 and antwort.url == _url("termin", org, termin_id=termin.pk)
    assert (termin.title, termin.start, termin.end, termin.location) == (
        neue.title,
        erster.start,
        erster.end,
        neue.location,
    )
    assert termin.visibility == Termin.PRIVAT and termin.created_by == vera
    assert termin.get_description_decrypted() == "Haushalt und Arbeitsprogramm."
    antworten = dict(termin.teilnahmen.values_list("membership_id", "status"))
    assert antworten[karim.pk] == TerminTeilnahme.ZUGESAGT and antworten[lea.pk] == TerminTeilnahme.ZUGESAGT
    assert vera.pk not in antworten
    fest = Notification.objects.filter(notification_type=NotificationType.TERMIN_EINLADUNG, title="Termin steht fest")
    assert {karim.pk, lea.pk} <= set(fest.values_list("recipient_id", flat=True))
    neue.refresh_from_db()
    assert neue.abgeschlossen_am is not None and neue.termin == termin
    assert list(neue.vorschlaege.filter(festgelegt=True)) == [erster]

    # Abgeschlossen: keine Antworten mehr, kein zweites Festlegen, die Seite nennt das Ergebnis
    gast.post(adresse, {f"wahl-{dritter.pk}": "ja"})
    assert VorschlagAntwort.objects.get(vorschlag=dritter, membership=karim).wahl == "notfalls"
    client.post(festlegen, {"vorschlag": str(zweiter.pk)})
    assert Termin.objects.count() == 1
    seite = gast.get(adresse).content.decode()
    assert 'data-testid="umfrage-ergebnis"' in seite and _url("termin", org, termin_id=termin.pk) in seite
    assert f'name="wahl-{erster.pk}"' not in seite and "(festgelegt)" in seite
    assert termine.sichtbare(karim).filter(pk=termin.pk).exists()


def test_formular_prueft_vorschlaege_und_auswahl(org: Any, vera: Any, karim: Any, lea: Any, client_for: Any) -> None:
    client = client_for(vera.user)
    adresse = _url("terminumfrage_neu", org)

    def fehler(**felder: Any) -> str:
        antwort = client.post(adresse, _formular(**felder))
        assert antwort.status_code == 422
        return str(antwort.content.decode())

    ein_tag = {"vorschlag_tag": [_tag(10).isoformat()], "vorschlag_beginn": ["18:00"], "vorschlag_ende": [""]}
    seite = fehler(**ein_tag)
    # Die Eingaben stehen wieder da, aufgefüllt auf die übliche Zahl an Zeilen
    assert "Bitte mindestens zwei Zeitpunkte vorschlagen." in seite and f'value="{_tag(10).isoformat()}"' in seite
    assert "Jeder Vorschlag braucht einen Tag" in fehler(vorschlag_beginn=["18:00", "", "17:00", ""])
    assert "liegt das Ende nicht nach dem Beginn" in fehler(vorschlag_ende=["17:00", "", "", ""])
    assert "Bitte mindestens ein Mitglied auswählen." in fehler(wen=umfrage.AUSWAHL)
    assert "Bitte die Sitzungsart wählen" in fehler(wen=umfrage.ART)
    assert not Terminumfrage.objects.exists()

    # Derselbe Zeitpunkt zweimal zählt einmal; gefragt sind die Auswahl und wer fragt
    doppelt = _anlegen(
        org,
        vera,
        vorschlag_tag=[_tag(10).isoformat()] * 2 + [_tag(11).isoformat()],
        vorschlag_beginn=["18:00"] * 3,
        vorschlag_ende=[""] * 3,
        wen=umfrage.AUSWAHL,
        gefragte=[str(karim.pk)],
    )
    assert doppelt.vorschlaege.count() == 2
    assert set(doppelt.teilnahmen.values_list("membership_id", flat=True)) == {vera.pk, karim.pk}

    # Teilnehmende einer Sitzungsart
    arbeitskreis = FactionMeetingType.objects.create(organization=org, name="AK Bauen", participants_mode="persons")
    arbeitskreis.participants.add(vera, lea)
    im_kreis = _anlegen(org, vera, wen=umfrage.ART, meeting_type=str(arbeitskreis.pk))
    assert set(im_kreis.teilnahmen.values_list("membership_id", flat=True)) == {vera.pk, lea.pk}

    # Ohne das Recht zum Anlegen gibt es das Formular nicht
    assert client_for(karim.user).get(adresse).status_code == 403


def test_sichtbarkeit_kalender_und_loeschen(
    org: Any, vera: Any, karim: Any, lea: Any, make_member: Any, client_for: Any
) -> None:
    verwaltung = make_member(org, [*LESEN, "calendar.manage"])
    neue = _anlegen(org, vera, wen=umfrage.AUSWAHL, gefragte=[str(karim.pk)])
    adresse = _url("terminumfrage", org, umfrage_id=neue.pk)

    def sieht(mitglied: Any) -> bool:
        return umfrage.sichtbare(mitglied).filter(pk=neue.pk).exists()

    assert sieht(vera) and sieht(karim) and sieht(verwaltung) and not sieht(lea)
    assert client_for(lea.user).get(adresse).status_code == 404
    # Wer nur verwaltet, sieht die Umfrage, ist aber nicht gefragt
    leitung = client_for(verwaltung.user)
    seite = leitung.get(adresse).content.decode()
    assert "Antwort speichern" not in seite and 'data-testid="umfrage-festlegen"' in seite

    # Im Kalender: die Vorschläge als eigene Art, darüber die offene Umfrage mit dem Hinweis auf die fehlende Antwort
    fenster = {"start": timezone.now().isoformat(), "end": (timezone.now() + timedelta(days=40)).isoformat()}

    def vorschlaege(mitglied: Any) -> list[dict[str, Any]]:
        antwort = client_for(mitglied.user).get(_url("meetings_calendar_events", org), {**fenster, "arten": "umfrage"})
        return list(antwort.json())

    eintraege = vorschlaege(karim)
    assert len(eintraege) == 3 and not vorschlaege(lea)
    assert eintraege[0]["title"] == "Vorschlag: Klausur im Frühjahr" and eintraege[0]["url"] == adresse
    assert eintraege[0]["classNames"] == ["termin-umfrage"]
    kalender = client_for(karim.user).get(_url("meetings_calendar", org)).content.decode()
    assert 'data-testid="kalender-umfragen"' in kalender and "Ihre Antwort fehlt" in kalender and adresse in kalender
    assert 'id="kalender-art-umfrage"' in kalender and "Termin abstimmen" not in kalender
    assert "Termin abstimmen" in client_for(vera.user).get(_url("meetings_calendar", org)).content.decode()
    assert (
        'data-testid="kalender-umfragen"'
        not in client_for(lea.user).get(_url("meetings_calendar", org)).content.decode()
    )

    erster = neue.vorschlaege.order_by("start")[0]
    client_for(karim.user).post(adresse, {f"wahl-{erster.pk}": "ja"})
    assert "Ihre Antwort fehlt" not in client_for(karim.user).get(_url("meetings_calendar", org)).content.decode()

    # Legt die Verwaltung fest, gehört der Termin trotzdem den Gefragten: Sie bleibt auf der Seite der Umfrage und
    # bekommt keinen Link zu einem Termin, den sie nicht sehen darf
    antwort = leitung.post(_url("terminumfrage_festlegen", org, umfrage_id=neue.pk), {"vorschlag": str(erster.pk)})
    termin = Termin.objects.get()
    assert antwort.status_code == 302 and antwort.url == adresse
    assert not termine.sichtbare(verwaltung).filter(pk=termin.pk).exists()
    zum_termin = _url("termin", org, termin_id=termin.pk)
    seite = leitung.get(adresse).content.decode()
    assert 'data-testid="umfrage-ergebnis"' in seite and zum_termin not in seite and "Festgelegt ist" in seite
    assert zum_termin in client_for(karim.user).get(adresse).content.decode()

    # Löschen: nicht die Gefragten, wohl aber wer angelegt hat oder alle Termine verwaltet; der Termin bleibt
    loeschen = _url("terminumfrage_loeschen", org, umfrage_id=neue.pk)
    assert client_for(karim.user).post(loeschen).status_code == 403
    assert leitung.post(loeschen).status_code == 302
    assert not Terminumfrage.objects.exists() and not VorschlagAntwort.objects.exists()
    assert not vorschlaege(karim) and Termin.objects.filter(pk=termin.pk).exists()


def test_vertretung_bekommt_die_umfrage_nur_wenn_sie_selbst_gefragt_ist(
    org: Any, vera: Any, karim: Any, make_member: Any
) -> None:
    # Die Vertretung verwaltet alle Termine und sähe deshalb jede Umfrage; weitergeleitet wird trotzdem nur,
    # wenn sie selbst gefragt ist
    lea = make_member(org, [*LESEN, "calendar.manage"], first_name="Lea", last_name="Lorenz")
    heute = timezone.localdate()
    MemberAbsence.objects.create(
        organization=org,
        membership=karim,
        deputy=lea,
        start_date=heute - timedelta(days=1),
        end_date=heute + timedelta(days=7),
        notify_deputy=True,
    )
    an_vertretung = Notification.objects.filter(recipient=lea, title__startswith="[Vertretung]")

    vorstand = _anlegen(org, vera, title="Vorstandsklausur", wen=umfrage.AUSWAHL, gefragte=[str(karim.pk)])
    assert umfrage.sichtbare(lea).filter(pk=vorstand.pk).exists() and not umfrage.beteiligt(lea, vorstand.pk)
    assert not an_vertretung.exists()
    # Ist die Vertretung selbst gefragt, sieht sie die Umfrage und bekommt auch die weitergeleitete Meldung
    _anlegen(org, vera, title="Sommerfest")
    assert [meldung.notification_type for meldung in an_vertretung] == [NotificationType.TERMIN_UMFRAGE]


def test_meldungen_gehen_erst_nach_dem_commit(
    org: Any, vera: Any, karim: Any, django_capture_on_commit_callbacks: Any
) -> None:
    """Umfrage, weiterer Vorschlag, Festlegen und Einladung zum Termin benachrichtigen erst nach dem Speichern."""
    an_karim = Notification.objects.filter(recipient=karim)

    with django_capture_on_commit_callbacks(execute=False) as nach_commit:
        neue = _anlegen(org, vera, wen=umfrage.AUSWAHL, gefragte=[str(karim.pk)])
    assert not an_karim.exists() and len(nach_commit) == 1
    nach_commit[0]()
    # ``.all()``: Die Iteration füllte sonst den Zwischenspeicher von ``an_karim``, und die Zählungen unten
    # läsen nicht mehr aus der Datenbank
    assert [meldung.notification_type for meldung in an_karim.all()] == [NotificationType.TERMIN_UMFRAGE]

    with django_capture_on_commit_callbacks(execute=False) as nach_commit:
        assert umfrage.vorschlag_ergaenzen(neue, {"tag": _tag(14).isoformat(), "beginn": "19:00"}, vera) == ""
    assert an_karim.all().count() == 1 and len(nach_commit) == 1

    erster = neue.vorschlaege.order_by("start")[0]
    with django_capture_on_commit_callbacks(execute=False) as nach_commit:
        assert umfrage.festlegen(neue, erster.pk, vera) is not None
    assert an_karim.all().count() == 1 and len(nach_commit) == 1

    with django_capture_on_commit_callbacks(execute=False) as nach_commit:
        form = termine.TerminForm(
            {
                "title": "Ortstermin",
                "tag": _tag(3).isoformat(),
                "beginn": "17:00",
                "visibility": Termin.PRIVAT,
                "eingeladene": [str(karim.pk)],
            },
            membership=vera,
        )
        assert form.is_valid(), form.errors
        form.speichern(termine.neu(org, vera))
    assert an_karim.all().count() == 1 and len(nach_commit) == 1
    nach_commit[0]()
    assert an_karim.filter(notification_type=NotificationType.TERMIN_EINLADUNG).count() == 1


def test_mit_auftragsjournal_entstehen_meldungen_mit_der_aenderung(
    org: Any,
    vera: Any,
    karim: Any,
    lea: Any,
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Any,
) -> None:
    """
    Mit dem Auftragsjournal entstehen Benachrichtigung und Mailauftrag in der Transaktion der Änderung (der Runner
    versendet erst nach dem Commit). Ein Fehler bei einer Person hält die übrigen nicht auf und lässt die Umfrage stehen.
    """
    monkeypatch.setattr(meldungen, "_im_journal", lambda: True)
    echtes_senden = NotificationHub.send

    def senden(**angaben: Any) -> Any:
        if angaben["recipient"].pk == karim.pk:
            raise RuntimeError("Störung bei einer Person")
        return echtes_senden(**angaben)

    monkeypatch.setattr(NotificationHub, "send", senden)

    with django_capture_on_commit_callbacks(execute=False):
        neue = _anlegen(org, vera, wen=umfrage.AUSWAHL, gefragte=[str(karim.pk), str(lea.pk)])
        # Schon vor dem Commit angelegt, nicht erst in einem Rückruf danach
        an_lea = Notification.objects.filter(recipient=lea, notification_type=NotificationType.TERMIN_UMFRAGE)
        assert an_lea.count() == 1
    assert not Notification.objects.filter(recipient=karim).exists()
    assert Terminumfrage.objects.filter(pk=neue.pk).exists() and neue.teilnahmen.count() == 3


def test_weiteren_vorschlag_ergaenzen(org: Any, vera: Any, karim: Any, client_for: Any) -> None:
    neue = _anlegen(org, vera, wen=umfrage.AUSWAHL, gefragte=[str(karim.pk)])
    adresse = _url("terminumfrage_vorschlag", org, umfrage_id=neue.pk)
    client = client_for(vera.user)
    assert (
        'data-testid="umfrage-vorschlag-neu"'
        in client.get(_url("terminumfrage", org, umfrage_id=neue.pk)).content.decode()
    )
    meldungen = Notification.objects.filter(recipient=karim, notification_type=NotificationType.TERMIN_UMFRAGE)
    assert meldungen.count() == 1

    assert client.post(adresse, {"tag": _tag(14).isoformat(), "beginn": "19:00", "ende": "21:00"}).status_code == 302
    assert neue.vorschlaege.count() == 4
    # Die Gefragten erfahren davon, damit sie ihre Antwort ergänzen
    assert meldungen.count() == 2 and any("Neuer Vorschlag" in meldung.message for meldung in meldungen.all())

    # Derselbe Zeitpunkt, ein unvollständiger und einer mit dem Ende vor dem Beginn ändern nichts
    for eingabe in (
        {"tag": _tag(14).isoformat(), "beginn": "19:00"},
        {"tag": _tag(15).isoformat()},
        {"tag": _tag(15).isoformat(), "beginn": "19:00", "ende": "18:00"},
    ):
        assert client.post(adresse, eingabe).status_code == 302
    assert neue.vorschlaege.count() == 4 and meldungen.all().count() == 2

    # Gefragte ergänzen keine Vorschläge; nach dem Festlegen geht es auch für die anlegende Person nicht mehr
    assert client_for(karim.user).post(adresse, {"tag": _tag(16).isoformat(), "beginn": "19:00"}).status_code == 403
    erster = neue.vorschlaege.order_by("start")[0]
    assert umfrage.festlegen(neue, erster.pk, vera) is not None
    neue.refresh_from_db()
    assert umfrage.vorschlag_ergaenzen(neue, {"tag": _tag(16).isoformat(), "beginn": "19:00"}, vera)
    assert neue.vorschlaege.count() == 4


def test_seite_zeigt_was_am_tag_schon_ansteht(org: Any, vera: Any, karim: Any, client_for: Any) -> None:
    neue = _anlegen(org, vera)
    form = termine.TerminForm(
        {"title": "Ortstermin Brücke", "tag": _tag(10).isoformat(), "beginn": "16:00", "visibility": Termin.ALLE},
        membership=vera,
    )
    assert form.is_valid(), form.errors
    form.speichern(termine.neu(org, vera))
    # Ein ganztägiger Termin über zwei Tage berührt den zweiten und den dritten Vorschlag
    mehrtaegig = termine.TerminForm(
        {
            "title": "Klausurtagung",
            "tag": _tag(11).isoformat(),
            "bis_tag": _tag(12).isoformat(),
            "all_day": "on",
            "visibility": Termin.ALLE,
        },
        membership=vera,
    )
    assert mehrtaegig.is_valid(), mehrtaegig.errors
    mehrtaegig.speichern(termine.neu(org, vera))

    seite = client_for(karim.user).get(_url("terminumfrage", org, umfrage_id=neue.pk)).content.decode()
    assert "Am selben Tag in Ihrem Kalender: 16:00 Ortstermin Brücke" in seite
    assert "Am selben Tag in Ihrem Kalender: Klausurtagung<" in seite
    assert f"Am selben Tag in Ihrem Kalender: Klausurtagung (seit {_tag(11):%d.%m.})" in seite
    assert seite.count("Am selben Tag in Ihrem Kalender") == 3


def test_auskunft_nennt_umfragen_und_eigene_antworten(org: Any, vera: Any, karim: Any) -> None:
    neue = _anlegen(org, vera)
    erster = neue.vorschlaege.order_by("start")[0]
    assert umfrage.antworten(neue, karim, {f"wahl-{erster.pk}": "notfalls"}) == 1

    eigene = dsgvo_export_service.collect_user_data(user=vera.user, membership=vera, organization=org)["termine"]
    assert [eintrag["title"] for eintrag in eigene["polls_created"]] == ["Klausur im Frühjahr"]
    assert eigene["polls_created"][0]["description"] == "Haushalt und Arbeitsprogramm."
    assert len(eigene["polls_created"][0]["proposals"]) == 3

    daten = dsgvo_export_service.collect_user_data(user=karim.user, membership=karim, organization=org)["termine"]
    assert not daten["polls_created"]
    assert [(eintrag["title"], len(eintrag["answers"])) for eintrag in daten["polls_asked"]] == [
        ("Klausur im Frühjahr", 1)
    ]
    assert daten["polls_asked"][0]["answers"][0]["answer"] == "Wenn nötig"
