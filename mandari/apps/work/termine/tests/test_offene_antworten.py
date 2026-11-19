# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Offene Antworten (Issue #1489): Einladungen zu Terminen und Terminumfragen ohne Antwort stehen auf der Startseite
unter „Für Sie“; vor der Antwortfrist einer Umfrage erinnert ein Zeitplan die Gefragten ohne Antwort, einmal.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.http import QueryDict
from django.urls import reverse
from django.utils import timezone

from apps.work import schedules
from apps.work.dashboard import selectors
from apps.work.notifications.models import Notification, NotificationType
from apps.work.termine import termine, umfrage
from apps.work.termine.models import Termin, Terminumfrage

pytestmark = pytest.mark.django_db

LESEN = ["dashboard.view", "meetings.view"]


@pytest.fixture
def org(org: Any) -> Any:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


@pytest.fixture
def vera(org: Any, make_member: Any) -> Any:
    return make_member(org, [*LESEN, "calendar.create"], first_name="Vera", last_name="Beispiel")


@pytest.fixture
def karim(org: Any, make_member: Any) -> Any:
    return make_member(org, LESEN, first_name="Karim", last_name="Kaya")


def _tag(nach: int) -> str:
    return (timezone.localdate() + timedelta(days=nach)).isoformat()


def _termin(org: Any, vera: Any, titel: str, tag: int, eingeladen: list[Any]) -> Termin:
    form = termine.TerminForm(
        {
            "title": titel,
            "tag": _tag(tag),
            "beginn": "18:00",
            "visibility": Termin.PRIVAT,
            "eingeladene": [str(person.pk) for person in eingeladen],
        },
        membership=vera,
    )
    assert form.is_valid(), form.errors
    return form.speichern(termine.neu(org, vera))


def _umfrage(org: Any, vera: Any, titel: str, *, bis: int | None = None, gefragt: list[Any]) -> Terminumfrage:
    daten = QueryDict(mutable=True)
    for name, wert in {
        "title": [titel],
        "vorschlag_tag": [_tag(10), _tag(11)],
        "vorschlag_beginn": ["18:00", "18:00"],
        "vorschlag_ende": ["", ""],
        "wen": [umfrage.AUSWAHL],
        "gefragte": [str(person.pk) for person in gefragt],
        "antwort_bis": [_tag(bis) if bis is not None else ""],
    }.items():
        daten.setlist(name, wert)
    form = umfrage.UmfrageForm(daten, membership=vera)
    assert form.is_valid(), form.errors
    return form.speichern(org)


def test_fuer_sie_nennt_offene_einladungen_und_umfragen(org: Any, vera: Any, karim: Any, client_for: Any) -> None:
    ortstermin = _termin(org, vera, "Ortstermin Brücke", 3, [karim])
    _termin(org, vera, "Pressegespräch", 5, [karim])
    vorbei = _termin(org, vera, "Vorbei", 2, [karim])
    Termin.objects.filter(pk=vorbei.pk).update(start=timezone.now() - timedelta(days=2))
    klausur = _umfrage(org, vera, "Klausur im Frühjahr", bis=4, gefragt=[karim])

    eintraege = selectors.fuer_sie(org, karim)
    titel = [eintrag["titel"] for eintrag in eintraege]
    # Nächster Termin zuerst, vergangene nicht, dann die Umfrage mit ihrer Frist
    assert titel[:3] == ["Ortstermin Brücke", "Pressegespräch", "Klausur im Frühjahr"] and "Vorbei" not in titel
    assert eintraege[0]["url"] == reverse("work:termin", kwargs={"org_slug": org.slug, "termin_id": ortstermin.pk})
    assert eintraege[0]["zeile"].endswith("Bitte sagen Sie zu oder ab.")
    assert eintraege[2]["art"].startswith("Terminumfrage, Antwort bis ")
    assert eintraege[2]["url"] == reverse("work:terminumfrage", kwargs={"org_slug": org.slug, "umfrage_id": klausur.pk})

    # Wer geantwortet hat, findet den Eintrag nicht mehr; wer anlegt, ist nicht eingeladen
    assert termine.antworten(ortstermin, karim, "abgesagt")
    erster = klausur.vorschlaege.order_by("start")[0]
    assert umfrage.antworten(klausur, karim, {f"wahl-{erster.pk}": "ja"}) == 1
    titel = [eintrag["titel"] for eintrag in selectors.fuer_sie(org, karim)]
    assert "Ortstermin Brücke" not in titel and "Klausur im Frühjahr" not in titel and "Pressegespräch" in titel
    assert not [eintrag for eintrag in selectors.fuer_sie(org, vera) if eintrag["art"] == "Einladung zu einem Termin"]

    # Auf der Startseite
    seite = client_for(karim.user).get(reverse("work:dashboard", kwargs={"org_slug": org.slug})).content.decode()
    assert "Pressegespräch" in seite and "Einladung zu einem Termin" in seite


def test_ohne_recht_auf_sitzungen_keine_termine_auf_dem_start(org: Any, vera: Any, make_member: Any) -> None:
    ohne = make_member(org, ["dashboard.view"])
    _termin(org, vera, "Ortstermin Brücke", 3, [ohne])
    assert not [eintrag for eintrag in selectors.fuer_sie(org, ohne) if eintrag["titel"] == "Ortstermin Brücke"]


def test_erinnerung_vor_der_frist_einmal_und_nur_ohne_antwort(
    org: Any, vera: Any, karim: Any, make_member: Any
) -> None:
    lea = make_member(org, LESEN, first_name="Lea", last_name="Lorenz")
    morgen = _umfrage(org, vera, "Klausur", bis=1, gefragt=[karim, lea])
    _umfrage(org, vera, "Später", bis=5, gefragt=[karim])
    _umfrage(org, vera, "Ohne Frist", gefragt=[karim])
    abgeschlossen = _umfrage(org, vera, "Schon entschieden", bis=0, gefragt=[karim])
    assert umfrage.festlegen(abgeschlossen, abgeschlossen.vorschlaege.order_by("start")[0].pk, vera) is not None
    erster = morgen.vorschlaege.order_by("start")[0]
    assert umfrage.antworten(morgen, lea, {f"wahl-{erster.pk}": "nein"}) == 1

    erinnerungen = Notification.objects.filter(
        notification_type=NotificationType.TERMIN_UMFRAGE, message__startswith="Erinnerung:"
    )
    assert umfrage.erinnern() == {"umfragen": 1, "erinnert": 1}
    # Nur Karim: Lea hat geantwortet, Vera hat gefragt; die übrigen Umfragen sind nicht fällig oder abgeschlossen
    assert [(meldung.recipient_id, "endet morgen" in meldung.message) for meldung in erinnerungen] == [(karim.pk, True)]
    morgen.refresh_from_db()
    assert morgen.erinnert_am is not None

    # Ein zweiter Lauf erinnert nicht noch einmal, auch nicht am Tag der Frist
    assert umfrage.erinnern() == {"umfragen": 0, "erinnert": 0}
    assert umfrage.erinnern(heute=timezone.localdate() + timedelta(days=1)) == {"umfragen": 0, "erinnert": 0}
    assert erinnerungen.count() == 1


def test_zeitplan_ist_eingetragen() -> None:
    from apps.events.schedule import registry

    assert registry.get(schedules.terminumfragen_erinnern.module_path) is not None
