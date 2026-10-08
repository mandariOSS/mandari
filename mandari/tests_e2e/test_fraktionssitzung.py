# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsansicht der laufenden Fraktionssitzung im Browser (Issue #874).

Vorsitz: Notizen formatiert schreiben, automatisch speichern, neu laden; TOP wechseln (Tagesordnung, Taste J);
Beschluss in der Leiste unten erfassen; Aufgabe für eine Personengruppe anlegen; Anwesenheit online setzen.
Zwei Personen schreiben gleichzeitig Notizen zum selben TOP: Keine Eingabe geht verloren.
Dazu axe-core und Bildschirmfotos (390, 1280, 1440, 1920, 2560) für die Sichtprüfung, mit Vorlage aus dem RIS,
„Im Beratungsverlauf“ und einem bisherigen Wortbeitrag.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, cast

import pytest
from django.utils import timezone

from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionDecision, FactionMeeting
from apps.work.faction.tests.test_sitzungsansicht import schalter_an
from apps.work.tasks.models import Task
from tests_e2e.conftest import login_via_form, wait_for_bundle

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "E2e-Passwort-123456"
VORSITZ = [
    "dashboard.view",
    "faction.view_public",
    "faction.view_non_public",
    "faction.manage",
    "faction.start",
    "protocols.create",
    "protocols.edit",
    "voting.participate",
    "tasks.view",
    "meetings.prepare",
]
NOTIZ = "#fs-notiz .ProseMirror"
NOTIZEN = "Alpine.$data(document.querySelector('[x-data=sitzungsNotizen]'))"

VORLAGE_TEXT = (
    "Beschlussvorschlag\n"
    "1. Der Rat beschließt den Feuerwehrbedarfsplan der Stadt Musterstadt für die Jahre 2026 bis 2031 in der "
    "Fassung der Anlage 1.\n"
    "2. Die Verwaltung wird beauftragt, die Maßnahmen aus Abschnitt 7 des Plans umzusetzen.\n\n"
    "Sachverhalt\n"
    "Die Stadt muss einen Bedarfsplan für ihre Feuerwehr aufstellen und spätestens alle fünf Jahre fortschreiben. "
    "Der Plan legt fest, wie schnell Hilfe ankommen soll: Die ersten Einsatzkräfte sollen innerhalb von zehn "
    "Minuten nach der Alarmierung am Einsatzort sein, und zwar in 90 Prozent der Einsätze."
)


def ris_unterlagen(org: Any, item: FactionAgendaItem, mitglied: Any, vorsitz: Any) -> None:
    """
    Vorlage aus dem RIS mit Hauptdatei (Text) und Anlage, Position der Fraktion aus der Vorberatung im Ausschuss
    („Im Beratungsverlauf“) und ein bisheriger Wortbeitrag, der in der neuen Ansicht lesbar bleibt.
    """
    from apps.work.faction.models import FactionProtocolEntry
    from apps.work.meetings.models import AgendaItemPosition
    from insight_core.models import (
        OParlAgendaItem,
        OParlBody,
        OParlConsultation,
        OParlFile,
        OParlMeeting,
        OParlOrganization,
        OParlPaper,
        OParlSource,
    )

    ris = "https://ris.example.org"
    source = OParlSource.objects.create(name="Test-RIS", url=f"{ris}/system")
    body = OParlBody.objects.create(external_id=f"{ris}/body/1", source=source, name="Musterstadt")
    ausschuss = OParlOrganization.objects.create(external_id=f"{ris}/org/1", body=body, name="Hauptausschuss")
    rat = OParlOrganization.objects.create(external_id=f"{ris}/org/2", body=body, name="Rat")
    jetzt = timezone.now()
    vorberatung = OParlMeeting.objects.create(
        external_id=f"{ris}/meeting/1", body=body, name="Hauptausschuss", start=jetzt - timedelta(days=2)
    )
    vorberatung.organizations.add(ausschuss)
    ratssitzung = OParlMeeting.objects.create(
        external_id=f"{ris}/meeting/2", body=body, name="Rat", start=jetzt + timedelta(days=9)
    )
    ratssitzung.organizations.add(rat)
    top_ausschuss = OParlAgendaItem.objects.create(
        external_id=f"{ris}/agenda/1", meeting=vorberatung, number="4", order=4
    )
    top_rat = OParlAgendaItem.objects.create(external_id=f"{ris}/agenda/2", meeting=ratssitzung, number="6", order=6)
    vorlage = OParlPaper.objects.create(
        external_id=f"{ris}/paper/1",
        body=body,
        name="Feuerwehrbedarfsplan 2026–2031",
        reference="V/2026/D-012",
        paper_type="Beschlussvorlage",
        date=(jetzt - timedelta(days=18)).date(),
        raw_json={"mainFile": {"id": f"{ris}/file/1"}},
    )
    for nr, (name, text) in enumerate(
        (("Vorlage", VORLAGE_TEXT), ("Langfassung", "Langfassung des Bedarfsplans.")), start=1
    ):
        OParlFile.objects.create(
            external_id=f"{ris}/file/{nr}",
            body=body,
            paper=vorlage,
            name=name,
            mime_type="application/pdf",
            text_content=text,
        )
    for nr, top in enumerate((top_ausschuss, top_rat), start=1):
        OParlConsultation.objects.create(
            external_id=f"{ris}/consultation/{nr}", body=body, paper=vorlage, agenda_item_external_id=top.external_id
        )
    position = AgendaItemPosition(
        organization=org, agenda_item=top_ausschuss, position="amended", outcome="accepted", is_final=True
    )
    cast(Any, position).set_reasoning_encrypted("Gerätehaus Nord vorziehen; die Verwaltung berichtet jährlich.")
    position.save()
    item.related_papers.add(vorlage)
    item.related_agenda_item = top_rat
    item.save(update_fields=["related_agenda_item"])
    eintrag: Any = FactionProtocolEntry(
        meeting=item.meeting, agenda_item=item, entry_type="speech", created_by=vorsitz, speaker=mitglied
    )
    eintrag.save()
    eintrag.set_content_encrypted("Wir tragen den Plan mit und fragen nach dem Gerätehaus Nord.")
    eintrag.save()


@pytest.fixture
def sitzung(db: Any, org: Any, make_member: Any) -> tuple[Any, FactionMeeting, list[FactionAgendaItem]]:
    schalter_an(org)
    vorsitz = make_member(org, VORSITZ, email="vorsitz-e2e@example.org")
    vorsitz.is_sworn_in = True
    vorsitz.save(update_fields=["is_sworn_in"])
    vorsitz.user.set_password(PASSWORD)
    vorsitz.user.save(update_fields=["password"])
    mitglied = make_member(org, ["faction.view_public", "voting.participate"], email="mitglied-e2e@example.org")
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung Nr. 24",
        start=timezone.now() - timedelta(minutes=5),
        started_at=timezone.now() - timedelta(minutes=4),
        status="ongoing",
        location="Fraktionsbüro",
        video_link="https://video.example.org/raum",
        created_by=vorsitz,
        minute_taker=vorsitz,
    )
    for m in (vorsitz, mitglied):
        FactionAttendance.objects.create(meeting=meeting, membership=m, status="present" if m == vorsitz else "invited")
    titel = ["Tagesordnung und Protokoll beschließen", "Feuerwehrbedarfsplan 2026–2031", "Termine", "Sonstiges"]
    tops = [
        FactionAgendaItem.objects.create(meeting=meeting, number=str(i + 1), title=t, visibility="public", order=i)
        for i, t in enumerate(titel)
    ]
    cast(Any, tops[1]).set_description_encrypted("Hilfsfrist im Norden, Gerätehaus Nord, sechs neue Stellen.")
    tops[1].reference_links = [{"label": "Bedarfsplan (Entwurf)", "url": "https://www.example.org/bedarfsplan"}]
    tops[1].save()
    ris_unterlagen(org, tops[1], mitglied, vorsitz)
    return vorsitz, meeting, tops


def oeffnen(page: Any, live_server: Any, org: Any, meeting: FactionMeeting, top: FactionAgendaItem) -> None:
    page.goto(f"{live_server.url}/work/{org.slug}/faction/{meeting.pk}/?top={top.pk}")
    wait_for_bundle(page)
    page.wait_for_selector(NOTIZ, timeout=15000)


def test_sitzungsansicht_kernpfade(
    page: Any, live_server: Any, login: Any, org: Any, sitzung: Any, axe: Any, screenshot: Any
) -> None:
    vorsitz, meeting, tops = sitzung
    page.set_viewport_size({"width": 1440, "height": 900})
    login(vorsitz.user.email, PASSWORD)
    oeffnen(page, live_server, org, meeting, tops[1])
    expect(page.locator("#fs-top-titel")).to_contain_text("Feuerwehrbedarfsplan")
    expect(page.locator(".fs-kopf")).to_contain_text("Schriftführung")
    # Unterlagen groß: Hauptdatei der Vorlage als erster Reiter mit ihrem Text, „Im Beratungsverlauf“ im Kopf des TOPs
    expect(page.get_by_role("tab", name="V/2026/D-012")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#fs-dok")).to_contain_text("Der Rat beschließt den Feuerwehrbedarfsplan")
    expect(page.locator(".fs-verlauf")).to_contain_text("Mit Änderungsantrag")
    # Bisherige Wortbeiträge bleiben im Protokoll sichtbar
    expect(page.locator(".fs-eintraege")).to_contain_text("Wir tragen den Plan mit")

    # Notizen formatiert schreiben: Fett über die Werkzeugleiste, danach automatisch gespeichert
    page.locator(NOTIZ).click()
    page.get_by_role("button", name="Fett (Strg+B)").click()
    page.keyboard.type("Hilfsfrist")
    page.get_by_role("button", name="Fett (Strg+B)").click()
    page.keyboard.type(" im Norden: 84 statt 90 Prozent.")
    expect(page.locator(".fs-status")).to_contain_text(
        re.compile(r"Automatisch gespeichert um \d{2}:\d{2}"), timeout=10000
    )
    tops[1].refresh_from_db()
    gespeichert = cast(Any, tops[1]).get_notes_decrypted()
    assert "<strong>Hilfsfrist</strong>" in gespeichert and "84 statt 90 Prozent" in gespeichert

    result = axe()
    assert not result.failing, result.describe()
    screenshot("fraktionssitzung-1440")
    page.set_viewport_size({"width": 1280, "height": 800})
    screenshot("fraktionssitzung-1280")
    page.set_viewport_size({"width": 1920, "height": 1080})
    screenshot("fraktionssitzung-1920")
    page.set_viewport_size({"width": 1440, "height": 900})

    # Neu laden: Notizen sind da
    page.reload()
    wait_for_bundle(page)
    expect(page.locator(NOTIZ)).to_contain_text("84 statt 90 Prozent")

    # Beschluss in der Leiste unten
    leiste = page.locator("#fs-unten")
    leiste.get_by_role("button", name="Dafür: eins mehr").click()
    leiste.get_by_role("button", name="Dafür: eins mehr").click()
    leiste.get_by_role("button", name="Erfassen").click()
    expect(page.locator("#fs-unten")).to_contain_text("2 dafür, 0 dagegen")
    assert FactionDecision.objects.get(agenda_item=tops[1]).votes_yes == 2

    # Aufgabe für eine Gruppe: Suche und Auswahl kombiniert
    page.fill("#fs-auf-titel", "Pressemitteilung vorbereiten")
    eingabe = page.locator("[role=combobox]")
    eingabe.click()
    eingabe.type("Alle")
    page.locator("[role=option]", has_text="Alle Mitglieder").click()
    page.locator("#fs-aufgaben").get_by_role("button", name="Anlegen").click()
    expect(page.locator("#fs-aufgaben")).to_contain_text("Pressemitteilung vorbereiten")
    assert Task.objects.filter(related_faction_agenda_item=tops[1]).count() == 2

    # Ohne Auswahl übernimmt die anlegende Person die Aufgabe; abhaken wie im Aufgabenboard
    page.fill("#fs-auf-titel", "Rückfrage an die Verwaltung")
    page.locator("#fs-aufgaben").get_by_role("button", name="Anlegen").click()
    haken = page.get_by_role("checkbox", name="Erledigt: Rückfrage an die Verwaltung")
    expect(haken).not_to_be_checked()
    haken.click()
    # Erst die Antwort des Servers zeichnet die Zeile als erledigt (Haken allein setzt schon der Browser)
    expect(page.locator("#fs-aufgaben li.erledigt")).to_contain_text("Rückfrage an die Verwaltung")
    expect(page.get_by_role("checkbox", name="Erledigt: Rückfrage an die Verwaltung")).to_be_checked()
    assert Task.objects.get(related_faction_agenda_item=tops[1], title="Rückfrage an die Verwaltung").is_completed

    # TOP wechseln: Notizen werden vorher gesichert, die Adresse merkt den TOP
    page.locator(NOTIZ).click()
    page.keyboard.press("End")
    page.keyboard.type(" Ergänzung vor dem Wechsel.")
    page.locator("#fs-tagesordnung").get_by_text("Termine").click()
    expect(page.locator("#fs-top-titel")).to_contain_text("Termine")
    assert re.search(rf"top={tops[2].pk}", page.url)
    # Die Auswahl „Person oder Gruppe“ des neuen TOPs bleibt zu (htmx übertrug sonst den Stil der alten Liste)
    page.wait_for_timeout(300)
    expect(page.get_by_role("listbox")).to_be_hidden()
    tops[1].refresh_from_db()
    assert "Ergänzung vor dem Wechsel." in cast(Any, tops[1]).get_notes_decrypted()

    # Taste J: nächster TOP
    page.locator("#fs-top-titel").click()
    page.keyboard.press("j")
    expect(page.locator("#fs-top-titel")).to_contain_text("Sonstiges")

    # Anwesenheit: Mitglied online anwesend
    page.locator(".fs-kopf-aktionen").get_by_role("button", name=re.compile("von 2")).click()
    anw = page.locator("#fs-anwesenheit")
    anw.get_by_role("checkbox").nth(1).check()
    expect(anw.get_by_role("button", name="online").nth(1)).to_be_visible()
    anw.get_by_role("button", name="online").nth(1).click()
    expect(anw.get_by_role("button", name="online").nth(1)).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".fs-kopf-aktionen")).to_contain_text("2 von 2")
    mitglied_teilnahme = meeting.attendances.exclude(membership=vorsitz).get()
    assert (mitglied_teilnahme.status, mitglied_teilnahme.participation_type) == ("present", "online")


def test_anwesenheit_als_spalte_auf_breiten_bildschirmen(
    page: Any, live_server: Any, login: Any, org: Any, sitzung: Any, axe: Any, screenshot: Any
) -> None:
    """Ab 2200 px steht die Anwesenheit dauerhaft rechts (Prototyp Fassung 2), darunter klappt sie auf."""
    vorsitz, meeting, tops = sitzung
    page.set_viewport_size({"width": 2560, "height": 1440})
    login(vorsitz.user.email, PASSWORD)
    oeffnen(page, live_server, org, meeting, tops[1])
    anw = page.locator("#fs-anwesenheit")
    expect(anw).to_be_visible()
    expect(page.locator(".fs-kopf-aktionen [aria-controls=fs-anwesenheit]")).to_be_hidden()
    # Die Spalte liegt neben dem TOP, nicht darüber
    spalte = anw.bounding_box()
    fokus = page.locator("#fs-fokus").bounding_box()
    assert spalte and fokus and spalte["x"] >= fokus["x"] + fokus["width"] - 1
    # Klick in die Notizen schließt die Spalte nicht; Bedienen tauscht sie aus und sie bleibt stehen
    page.locator(NOTIZ).click()
    expect(anw).to_be_visible()
    anw.get_by_role("checkbox").nth(1).check()
    expect(page.locator("#fs-anwesenheit").get_by_role("button", name="online").nth(1)).to_be_visible()
    result = axe()
    assert not result.failing, result.describe()
    screenshot("fraktionssitzung-2560")

    # Schmaler: wieder als aufklappendes Feld über den Knopf im Kopf
    page.set_viewport_size({"width": 1920, "height": 1080})
    expect(page.locator("#fs-anwesenheit")).to_be_hidden()
    page.locator(".fs-kopf-aktionen").get_by_role("button", name=re.compile("von 2")).click()
    expect(page.locator("#fs-anwesenheit")).to_be_visible()


def test_sitzungsansicht_am_handy(
    page: Any, live_server: Any, login: Any, org: Any, sitzung: Any, screenshot: Any
) -> None:
    vorsitz, meeting, tops = sitzung
    page.set_viewport_size({"width": 390, "height": 844})
    login(vorsitz.user.email, PASSWORD)
    oeffnen(page, live_server, org, meeting, tops[1])
    expect(page.locator("#fs-top-titel")).to_contain_text("Feuerwehrbedarfsplan")
    screenshot("fraktionssitzung-390")
    # Tagesordnung als Blatt
    page.locator("#fs-unten").get_by_role("button", name=re.compile("TOP 2")).click()
    expect(page.locator("#fs-tagesordnung")).to_be_visible()
    page.locator("#fs-tagesordnung").get_by_text("Sonstiges").click()
    expect(page.locator("#fs-top-titel")).to_contain_text("Sonstiges")
    expect(page.locator("#fs-tagesordnung")).to_be_hidden()


@pytest.fixture
def zwei_browser(new_context: Any) -> Iterator[tuple[Any, Any]]:
    """Zwei unabhängige Browserkontexte (eigene Anmeldung), je eine Seite."""
    yield new_context().new_page(), new_context().new_page()


def test_zwei_personen_schreiben_gleichzeitig_ohne_verlust(
    live_server: Any, org: Any, make_member: Any, sitzung: Any, zwei_browser: Any
) -> None:
    """Vorsitz und Schriftführung schreiben zur selben Zeit in die Notizen eines TOPs; beide Fassungen bleiben."""
    vorsitz, meeting, tops = sitzung
    schrift = make_member(org, ["faction.view_public", "protocols.create"], email="schrift-e2e@example.org")
    schrift.user.set_password(PASSWORD)
    schrift.user.save(update_fields=["password"])
    meeting.minute_taker = schrift
    meeting.save(update_fields=["minute_taker"])
    seite_a, seite_b = zwei_browser
    for seite, person in ((seite_a, vorsitz), (seite_b, schrift)):
        seite.set_viewport_size({"width": 1440, "height": 900})
        login_via_form(seite, live_server.url, person.user.email, PASSWORD)
        oeffnen(seite, live_server, org, meeting, tops[1])

    # Beide haben den TOP mit leeren Notizen geöffnet; die Vorsitzende speichert zuerst
    seite_a.locator(NOTIZ).click()
    seite_a.keyboard.type("Fassung der Sitzungsleitung.")
    expect(seite_a.locator(".fs-status")).to_contain_text(re.compile(r"gespeichert um"), timeout=10000)

    # Die Schriftführung schreibt auf dem älteren Stand weiter: Der Server lehnt ab, der Browser führt zusammen
    seite_b.locator(NOTIZ).click()
    seite_b.keyboard.type("Fassung der Schriftführung.")
    expect(seite_b.locator(".fs-hinweis")).to_be_visible(timeout=10000)
    expect(seite_b.locator(".fs-status")).to_contain_text(re.compile(r"gespeichert um"), timeout=10000)

    tops[1].refresh_from_db()
    gespeichert = cast(Any, tops[1]).get_notes_decrypted()
    assert "Fassung der Sitzungsleitung." in gespeichert
    assert "Fassung der Schriftführung." in gespeichert
