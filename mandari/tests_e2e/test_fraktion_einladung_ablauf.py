# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fraktionssitzung im Browser von der Einladung bis zum Protokoll (Issue #855).

Der ganze Ablauf mit echten Klicks: Sitzung planen, TOPs eintragen, Versand freigeben, der Zeitplan lädt ein (Mail mit
Kalenderdatei und Tagesordnung), Zusage und Absage, der Zeitplan erinnert, Sitzung starten, Notiz und Beschluss, Protokoll.
Dazu: Eine vom Mailserver abgelehnte Adresse wird angezeigt, die übrigen Mails gehen raus und das Ergebnis bleibt nach
dem Neuladen stehen. Nicht Vereidigte sehen vom nichtöffentlichen Teil nur die Zahl und die Erklärung, wer Zugang gibt.

Gewartet wird nur auf Zustände (``expect``, ``wait_for_*``), nie auf eine feste Zeit. Die Zeitpläne laufen als die
Aufgaben aus ``apps/work/schedules.py`` im Testprozess; der Zeitpunkt kommt als Argument, die Uhr bleibt echt.
"""

from __future__ import annotations

import io
import re
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from unittest import mock

import pytest
from django.core import mail
from django.utils import timezone
from playwright.sync_api import expect
from pypdf import PdfReader

from apps.common.mail import delivery
from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.events.schedule import registry
from apps.work import schedules
from apps.work.faction.invitations import invitation_dispatch_at
from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionMeeting
from apps.work.faction.tests.test_sitzungsansicht import schalter_an
from tests_e2e.conftest import ADMIN_PASSWORD as PASSWORT
from tests_e2e.conftest import BrowserProblems, login_via_form, wait_for_bundle, wait_for_component
from tests_e2e.test_fraktionssitzung import NOTIZ, oeffnen

TITEL = "Fraktionssitzung November"
ORT = "Fraktionsbüro"
TOP_HAUSHALT = "Haushalt 2027"
TOP_RADWEG = "Radwegkonzept"
TOP_NOE = "Ankauf Bauland Nord"
NOTIZTEXT = "Die Fraktion trägt den Entwurf mit."
ERKLAERUNG = "Diese Punkte sehen nur vereidigte Mitglieder mit Zugriff auf den nichtöffentlichen Teil."
ZUGANG = "Zugang gibt der Vorsitz."
#: Lesen mit Protokoll; den nichtöffentlichen Teil sieht trotzdem nur, wer auch vereidigt ist
MITGLIED = ["faction.view_public", "faction.view_non_public", "protocols.view_public", "protocols.view_full"]
BREIT = {"width": 1440, "height": 900}


@dataclass
class Fraktion:
    """Organisation im Freigabe-Modus: Vorsitz und Rat vereidigt, Klara nicht, dazu ein Gastzugang."""

    org: Any
    vorsitz: Any
    rat: Any
    sachkundig: Any
    gast: Any


def _person(make_member: Any, org: Any, email: str, vorname: str, nachname: str, *, vereidigt: bool) -> Any:
    person = make_member(org, MITGLIED, email=email)
    return _einrichten(person, vorname, nachname, vereidigt=vereidigt)


def _einrichten(person: Any, vorname: str, nachname: str, *, vereidigt: bool) -> Any:
    person.is_sworn_in = vereidigt
    person.save(update_fields=["is_sworn_in"])
    person.user.first_name, person.user.last_name = vorname, nachname
    person.user.set_password(PASSWORT)
    person.user.save(update_fields=["password", "first_name", "last_name"])
    return person


@pytest.fixture
def fraktion(org: Any, make_member: Any, admin: Any) -> Fraktion:
    schalter_an(org)
    org.settings = {**(org.settings or {}), "faction": {"invitation_dispatch": "approval"}}
    org.save(update_fields=["settings"])
    konto = UserFactory(email="gast-e2e@example.org")  # type: ignore[no-untyped-call]
    return Fraktion(
        org=org,
        vorsitz=_einrichten(admin, "Vera", "Beispiel", vereidigt=True),
        rat=_person(make_member, org, "martin-e2e@example.org", "Martin", "Muster", vereidigt=True),
        sachkundig=_person(make_member, org, "klara-e2e@example.org", "Klara", "Kopie", vereidigt=False),
        gast=MembershipFactory(user=konto, organization=org, is_guest=True),  # type: ignore[no-untyped-call]
    )


def _geplante_sitzung(fraktion: Fraktion) -> FactionMeeting:
    """Sitzung in zwei Tagen mit öffentlichem und nichtöffentlichem TOP: Der geplante Versand ist schon vorbei."""
    meeting = FactionMeeting.objects.create(
        organization=fraktion.org,
        title=TITEL,
        start=timezone.now() + timedelta(days=2),
        status="planned",
        location=ORT,
        created_by=fraktion.vorsitz,
    )
    for person in (fraktion.vorsitz, fraktion.rat, fraktion.sachkundig):
        FactionAttendance.objects.create(meeting=meeting, membership=person, status="invited")
    FactionAgendaItem.objects.create(meeting=meeting, number="1", title=TOP_HAUSHALT, visibility="public", order=1)
    FactionAgendaItem.objects.create(meeting=meeting, number="1", title=TOP_NOE, visibility="internal", order=2)
    return meeting


def _sitzung_url(fraktion: Fraktion, meeting: FactionMeeting) -> str:
    return f"/work/{fraktion.org.slug}/faction/{meeting.pk}/"


def _als(new_context: Any, live_server: Any, person: Any) -> Any:
    """Weitere Person in einem eigenen Browserkontext (eigene Anmeldung)."""
    seite = new_context().new_page()
    seite.set_viewport_size(BREIT)
    login_via_form(seite, live_server.url, person.user.email, PASSWORT)
    return seite


def _oeffnen(seite: Any, live_server: Any, fraktion: Fraktion, meeting: FactionMeeting) -> None:
    seite.goto(f"{live_server.url}{_sitzung_url(fraktion, meeting)}")
    wait_for_bundle(seite)


def _neu_laden(seite: Any) -> None:
    seite.reload()
    wait_for_bundle(seite)


def _sitzung_planen(page: Any, goto: Any, fraktion: Fraktion) -> FactionMeeting:
    """Dialog „Fraktionssitzung planen“: in fünf Tagen, mit Zu- und Absagen. Der geplante Versand liegt noch vor uns."""
    goto(f"/work/{fraktion.org.slug}/faction/")
    wait_for_component(page, "{ showCreateModal")
    page.get_by_role("button", name="Fraktionssitzung planen").click()
    dialog = page.get_by_test_id("neue-sitzung")
    expect(dialog).to_be_visible()
    dialog.locator("#neue-sitzung-titel-feld").fill(TITEL)
    dialog.locator("#neue-sitzung-datum").fill((timezone.localdate() + timedelta(days=5)).isoformat())
    dialog.locator("#neue-sitzung-uhrzeit").fill("18:00")
    dialog.locator("#neue-sitzung-ort").fill(ORT)
    dialog.locator("#neue-sitzung-zusagen").check()
    dialog.get_by_role("button", name="Planen", exact=True).click()
    expect(page.get_by_test_id("fs-vorbereiten")).to_have_attribute("data-modus", "planung")
    wait_for_component(page, "factionDetail")
    return FactionMeeting.objects.get(organization=fraktion.org, title=TITEL)


def _top_eintragen(page: Any, feld: str, titel: str) -> None:
    """Zeile „TOP hinzufügen“; htmx tauscht die Tagesordnung, danach ist die nächste Zeile bereit."""
    page.locator(feld).fill(titel)
    page.locator(feld).press("Enter")
    expect(page.locator(".fs-top-titel", has_text=titel)).to_be_visible()
    # Erst nach dem Beruhigen hat htmx die neuen Formulare verdrahtet; vorher ginge Enter als normales Absenden raus
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-swapping, .htmx-settling')")


def _zeitplan(aufgabe: Any, lauf: str, zeitpunkt: datetime) -> None:
    """Führt einen Zeitplan aus wie der Worker, als wäre es ``zeitpunkt`` (nur der Lauf bekommt ihn als Argument)."""
    eintrag = registry.get(aufgabe.module_path)
    assert eintrag is not None and eintrag.task is aufgabe, aufgabe.module_path
    with mock.patch.object(schedules, lauf, partial(getattr(schedules, lauf), now=zeitpunkt)):
        aufgabe.call()


def _einladungslauf(zeitpunkt: datetime) -> None:
    _zeitplan(schedules.fraktionseinladungen_senden, "run_faction_invitation_pass", zeitpunkt)


def _erinnerungslauf(zeitpunkt: datetime) -> None:
    _zeitplan(schedules.fraktionserinnerungen_senden, "run_faction_reminder_pass", zeitpunkt)


def _anhaenge(nachricht: Any) -> dict[str, Any]:
    return {name: inhalt for name, inhalt, _typ in nachricht.attachments}


def _pdf_text(inhalt: Any) -> str:
    return "\n".join(seite.extract_text() or "" for seite in PdfReader(io.BytesIO(bytes(inhalt))).pages)


def _ics_text(inhalt: Any) -> str:
    return inhalt if isinstance(inhalt, str) else bytes(inhalt).decode("utf-8")


def _empfaenger(nachrichten: list[Any]) -> list[str]:
    return sorted(adresse for nachricht in nachrichten for adresse in nachricht.to)


def _einladungsmails() -> dict[str, Any]:
    """Einladungen je Empfänger; Testmails zählen nicht."""
    return {m.to[0]: m for m in mail.outbox if m.subject == f"Einladung: {TITEL}"}


def _mailserver_lehnt_ab(monkeypatch: pytest.MonkeyPatch, abgelehnt: str) -> None:
    """Der Mailserver lehnt eine Adresse ab, alles andere geht durch den echten Versandweg."""
    echter_versand = delivery.deliver

    def versand(nachricht: Any, weg: Any, *, kind: str) -> str:
        if nachricht.to == (abgelehnt,):
            raise smtplib.SMTPRecipientsRefused({abgelehnt: (550, b"unbekannt")})
        return echter_versand(nachricht, weg, kind=kind)

    monkeypatch.setattr(delivery, "deliver", versand)


def _einladung_pruefen(fraktion: Fraktion, meeting: FactionMeeting) -> None:
    """Alle Eingeladenen haben Kalenderdatei und Tagesordnung, der nichtöffentliche Teil nur für Vereidigte."""
    mails = _einladungsmails()
    assert sorted(mails) == sorted(p.user.email for p in (fraktion.vorsitz, fraktion.rat, fraktion.sachkundig))
    assert fraktion.gast.user.email not in mails

    beginn = meeting.start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    for adresse, nachricht in mails.items():
        anhaenge = _anhaenge(nachricht)
        assert sorted(anhaenge) == ["sitzung.ics", "tagesordnung.pdf"], adresse
        ics = _ics_text(anhaenge["sitzung.ics"])
        assert "BEGIN:VEVENT" in ics and f"SUMMARY:{TITEL}" in ics and f"DTSTART:{beginn}" in ics, adresse
        assert f"faction-meeting-{meeting.pk}@mandari" in ics and ORT in ics, adresse
        assert TOP_HAUSHALT in nachricht.body and TOP_RADWEG in nachricht.body, adresse
        pdf = _pdf_text(anhaenge["tagesordnung.pdf"])
        assert TOP_HAUSHALT in pdf and TOP_RADWEG in pdf, adresse

    for person, sieht_noe in ((fraktion.vorsitz, True), (fraktion.rat, True), (fraktion.sachkundig, False)):
        nachricht = mails[person.user.email]
        assert (TOP_NOE in nachricht.body) is sieht_noe, person.user.email
        assert (TOP_NOE in _pdf_text(_anhaenge(nachricht)["tagesordnung.pdf"])) is sieht_noe, person.user.email


def test_von_der_einladung_bis_zum_protokoll(
    page: Any,
    live_server: Any,
    goto: Any,
    login: Any,
    new_context: Any,
    fraktion: Fraktion,
    problems: BrowserProblems,
    screenshot: Any,
) -> None:
    page.set_viewport_size(BREIT)
    login(fraktion.vorsitz.user.email, PASSWORT)

    # Sitzung anlegen und Tagesordnung eintragen
    meeting = _sitzung_planen(page, goto, fraktion)
    assert meeting.rsvp_enabled is True and meeting.status == "planned"
    assert set(meeting.attendances.values_list("membership_id", flat=True)) == {
        fraktion.vorsitz.pk,
        fraktion.rat.pk,
        fraktion.sachkundig.pk,
    }, "alle Mitglieder eingeladen, der Gastzugang nicht"
    _top_eintragen(page, "#neu-oe", TOP_HAUSHALT)
    _top_eintragen(page, "#neu-oe", TOP_RADWEG)
    _top_eintragen(page, "#neu-noe", TOP_NOE)
    expect(page.get_by_test_id("fs-to-zahl")).to_contain_text("davon 1 nichtöffentlich")

    # Freigeben: Solange der geplante Zeitpunkt nicht da ist, geht nichts raus
    _neu_laden(page)
    info = page.get_by_test_id("fs-info")
    expect(info).to_contain_text("nach Freigabe durch den Vorsitz")
    page.once("dialog", lambda dialog: dialog.accept())
    info.get_by_role("button", name="Versand freigeben").click()
    expect(info).to_contain_text("Versand freigegeben.")
    meeting.refresh_from_db()
    assert meeting.invitation_released_by == fraktion.vorsitz and meeting.invitation_sent is False
    versand_am = invitation_dispatch_at(meeting)
    _einladungslauf(versand_am - timedelta(minutes=1))
    assert mail.outbox == []

    # Der Zeitplan versendet zum geplanten Zeitpunkt, genau einmal
    _einladungslauf(versand_am)
    meeting.refresh_from_db()
    assert meeting.invitation_sent is True and meeting.status == "invited"
    _einladung_pruefen(fraktion, meeting)
    _einladungslauf(versand_am + timedelta(minutes=15))
    assert len(mail.outbox) == 3

    # Das Ergebnis steht an der Sitzung, je Person, und sagt „versendet“
    _neu_laden(page)
    expect(page.get_by_test_id("fs-vorbereiten")).to_have_attribute("data-modus", "einladung")
    expect(info).to_contain_text("Alle Einladungen sind versendet.")
    expect(page.get_by_test_id("fs-rm-satz")).to_have_text("0 Zusagen, 0 Absagen und 3 offen")
    page.get_by_role("button", name="Versandergebnis").click()
    versandliste = page.get_by_test_id("fs-versandliste")
    expect(versandliste.locator("li")).to_have_count(3)
    expect(versandliste.locator("li", has_text="Klara Kopie")).to_contain_text("ohne nichtöffentlichen Teil")
    expect(versandliste.locator("li", has_text="Martin Muster")).to_contain_text(
        re.compile(r"versendet \d{2}\.\d{2}\.")
    )
    screenshot("fraktion_einladung_versandergebnis_1440")
    page.keyboard.press("Escape")
    expect(versandliste).to_be_hidden()

    # Rückmeldungen: der Rat sagt zu, die Sachkundige sagt ab
    rat = _als(new_context, live_server, fraktion.rat)
    _oeffnen(rat, live_server, fraktion, meeting)
    expect(rat.get_by_test_id("fs-to-lesen")).to_contain_text(TOP_HAUSHALT)
    rat.get_by_test_id("fs-antwort").get_by_role("button", name="Zusagen").click()
    expect(rat.locator(".fs-antwort-stand")).to_contain_text("Sie haben zugesagt.")
    expect(rat.get_by_test_id("fs-antwort").get_by_role("button", name="Zusagen")).to_have_attribute(
        "aria-pressed", "true"
    )
    sachkundig = _als(new_context, live_server, fraktion.sachkundig)
    _oeffnen(sachkundig, live_server, fraktion, meeting)
    sachkundig.get_by_test_id("fs-antwort").get_by_role("button", name="Absagen").click()
    expect(sachkundig.locator(".fs-antwort-stand")).to_contain_text("Sie haben abgesagt.")
    _neu_laden(page)
    expect(page.get_by_test_id("fs-rm-satz")).to_have_text("1 Zusage, 1 Absage und 1 offen")

    # Der Zeitplan erinnert genau 48 Stunden vorher, eine Minute früher noch nicht: Ein anderer Vorlauf fiele auf.
    # Mit Zu- und Absagen geht die Erinnerung nur an die Zusage, und nur einmal
    mail.outbox.clear()
    _erinnerungslauf(meeting.start - timedelta(hours=48, minutes=1))
    assert mail.outbox == []
    _erinnerungslauf(meeting.start - timedelta(hours=48))
    assert _empfaenger(mail.outbox) == [fraktion.rat.user.email]
    assert mail.outbox[0].subject == f"Erinnerung: {TITEL} in 48 Stunden"
    assert _sitzung_url(fraktion, meeting) in mail.outbox[0].body
    _erinnerungslauf(meeting.start - timedelta(hours=47))
    assert len(mail.outbox) == 1
    _neu_laden(page)
    expect(page.get_by_test_id("fs-tln").locator("tr", has_text="Martin Muster")).to_contain_text(
        "an die Sitzung erinnert"
    )

    # Der Termin ist da: Sitzung starten, Notiz und Beschluss erfassen, beenden
    FactionMeeting.objects.filter(pk=meeting.pk).update(start=timezone.now() + timedelta(minutes=10))
    _neu_laden(page)
    info.get_by_role("button", name="Sitzung starten").click()
    page.wait_for_selector(NOTIZ, timeout=15000)
    meeting.refresh_from_db()
    assert meeting.status == "ongoing"
    haushalt = FactionAgendaItem.objects.get(meeting=meeting, title=TOP_HAUSHALT)
    oeffnen(page, live_server, fraktion.org, meeting, haushalt)
    page.locator(NOTIZ).click()
    page.keyboard.type(NOTIZTEXT)
    expect(page.locator(".fs-status")).to_contain_text(re.compile(r"gespeichert um"), timeout=10000)
    leiste = page.locator("#fs-unten")
    leiste.get_by_role("button", name="Dafür: eins mehr").click()
    leiste.get_by_role("button", name="Erfassen").click()
    expect(page.locator("#fs-unten")).to_contain_text("1 dafür, 0 dagegen")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name=re.compile("Sitzung beenden")).click()

    # Protokoll: mit Notiz und Beschluss, der nichtöffentliche Teil nur für Vereidigte
    expect(page.get_by_test_id("fs-protokoll")).to_have_attribute("data-zustand", "completed")
    wait_for_bundle(page)
    dokument = page.get_by_test_id("fs-prot-dok")
    expect(dokument).to_contain_text(NOTIZTEXT)
    expect(dokument).to_contain_text("Angenommen mit 1 Stimme dafür")
    expect(dokument).to_contain_text(TOP_NOE)
    screenshot("fraktion_einladung_protokoll_1440")
    _oeffnen(sachkundig, live_server, fraktion, meeting)
    dokument_klara = sachkundig.get_by_test_id("fs-prot-dok")
    expect(dokument_klara).to_contain_text(TOP_HAUSHALT)
    expect(dokument_klara).to_contain_text(NOTIZTEXT)
    expect(sachkundig.get_by_test_id("fs-gesperrt")).to_contain_text(ERKLAERUNG)
    assert TOP_NOE not in sachkundig.content()

    problems.assert_clean("Ablauf der Fraktionssitzung")


def test_abgelehnte_adresse_wird_angezeigt_und_bleibt_nach_dem_neuladen(
    page: Any,
    goto: Any,
    login: Any,
    fraktion: Fraktion,
    monkeypatch: pytest.MonkeyPatch,
    problems: BrowserProblems,
    screenshot: Any,
) -> None:
    meeting = _geplante_sitzung(fraktion)
    abgelehnt = fraktion.sachkundig.user.email
    _mailserver_lehnt_ab(monkeypatch, abgelehnt)

    page.set_viewport_size(BREIT)
    login(fraktion.vorsitz.user.email, PASSWORT)
    goto(_sitzung_url(fraktion, meeting))

    # Vor der ersten Einladung: Hinweis auf die Testmail und, wer darf, der Weg zu den Einstellungen
    page.get_by_test_id("fs-info").get_by_role("button", name="Jetzt einladen").click()
    dialog = page.locator("#fs-einladen")
    expect(dialog).to_be_visible()
    hinweis = dialog.get_by_test_id("fs-testmail")
    expect(hinweis).to_contain_text("Ihre erste Einladung über diesen Versandweg.")
    expect(hinweis.get_by_role("link", name="Einstellungen › E-Mail-Versand")).to_be_visible()
    hinweis.get_by_role("button", name="Testmail an mich senden").click()
    expect(dialog.locator("#fs-testmail-erg")).to_contain_text(f"Testmail an {fraktion.vorsitz.user.email} versendet")

    # Versand: Die eine Adresse lehnt der Mailserver ab, die übrigen Mails gehen raus
    dialog.get_by_role("button", name="Einladung an 3 Mitglieder senden").click()
    expect(page.get_by_test_id("fs-vorbereiten")).to_have_attribute("data-modus", "einladung")
    wait_for_bundle(page)
    assert sorted(_einladungsmails()) == sorted(p.user.email for p in (fraktion.vorsitz, fraktion.rat))
    for nachricht in _einladungsmails().values():
        assert sorted(_anhaenge(nachricht)) == ["sitzung.ics", "tagesordnung.pdf"]

    grund = f"Der Mailserver hat die Adresse {abgelehnt} abgelehnt."

    def ergebnis_pruefen() -> None:
        expect(page.get_by_test_id("fs-info")).to_contain_text("2 von 3 Einladungen versendet, eine nicht.")
        zeile = page.get_by_test_id("fs-tln").locator("tr", has_text="Klara Kopie")
        expect(zeile).to_contain_text("Nicht versendet.")
        expect(zeile).to_contain_text(grund)
        page.get_by_role("button", name="Versandergebnis").click()
        liste = page.get_by_test_id("fs-versandliste")
        expect(liste).to_be_visible()
        expect(page.locator("#fs-versand")).to_contain_text("2 von 3 versendet, 1 nicht.")
        expect(liste.locator("li", has_text="Klara Kopie")).to_contain_text(grund)
        expect(liste.locator("li", has_text="Vera Beispiel")).to_contain_text("versendet")
        # textContent, also auch verborgene Teile der Seite
        expect(page.locator("body")).not_to_contain_text(re.compile("zugestellt", re.IGNORECASE))

    ergebnis_pruefen()
    screenshot("fraktion_einladung_abgelehnt_1440")
    _neu_laden(page)
    ergebnis_pruefen()
    problems.assert_clean("Abgelehnte Adresse")


def test_bisherige_ansicht_zeigt_testmail_und_abgelehnte_adresse(
    page: Any,
    goto: Any,
    login: Any,
    fraktion: Fraktion,
    monkeypatch: pytest.MonkeyPatch,
    problems: BrowserProblems,
    screenshot: Any,
) -> None:
    """Neu angelegte Organisationen starten mit dem Schalter aus: Sie brauchen dasselbe Versandergebnis (Issue #855)."""
    fraktion.org.work_new_design = False
    fraktion.org.save(update_fields=["work_new_design"])
    meeting = _geplante_sitzung(fraktion)
    abgelehnt = fraktion.sachkundig.user.email
    _mailserver_lehnt_ab(monkeypatch, abgelehnt)
    page.set_viewport_size(BREIT)
    login(fraktion.vorsitz.user.email, PASSWORT)
    goto(_sitzung_url(fraktion, meeting))

    hinweis = page.get_by_test_id("fs-testmail-bisher")
    expect(hinweis).to_contain_text("Ihre erste Einladung über diesen Versandweg.")
    expect(hinweis.get_by_role("link", name="Einstellungen › E-Mail-Versand")).to_be_visible()
    hinweis.get_by_role("button", name="Testmail an mich senden").click()
    expect(hinweis.locator("#fs-testmail-erg")).to_contain_text(f"Testmail an {fraktion.vorsitz.user.email} versendet")

    page.get_by_role("button", name="Einladen", exact=True).click()
    page.get_by_role("button", name="Bestätigen").click()  # hx-confirm: eigener Dialog statt confirm()
    ergebnis = page.get_by_test_id("fs-versand-bisher")
    expect(ergebnis).to_contain_text("2 von 3 versendet, 1 nicht.")
    wait_for_bundle(page)
    assert sorted(_einladungsmails()) == sorted(p.user.email for p in (fraktion.vorsitz, fraktion.rat))

    def ergebnis_pruefen() -> None:
        expect(ergebnis).to_contain_text("2 von 3 versendet, 1 nicht.")
        fehler = page.get_by_test_id("fs-versand-fehler")
        expect(fehler).to_contain_text("Klara Kopie: Nicht versendet.")
        expect(fehler).to_contain_text(f"Der Mailserver hat die Adresse {abgelehnt} abgelehnt.")
        expect(hinweis).to_have_count(0)
        expect(page.locator("body")).not_to_contain_text(re.compile("zugestellt", re.IGNORECASE))

    ergebnis_pruefen()
    ergebnis.get_by_text("Alle Empfänger").click()
    expect(ergebnis.locator("li", has_text="Vera Beispiel")).to_contain_text("versendet")
    screenshot("fraktion_versandergebnis_bisherige_ansicht_1440")
    _neu_laden(page)
    ergebnis_pruefen()
    problems.assert_clean("Versandergebnis in der bisherigen Ansicht")


def test_gesperrte_tops_mit_erklaerung_auch_in_der_bisherigen_ansicht(
    page: Any,
    goto: Any,
    login: Any,
    fraktion: Fraktion,
    axe: Any,
    problems: BrowserProblems,
    screenshot: Any,
) -> None:
    meeting = _geplante_sitzung(fraktion)
    url = _sitzung_url(fraktion, meeting)
    page.set_viewport_size(BREIT)
    login(fraktion.sachkundig.user.email, PASSWORT)

    goto(url)
    gesperrt = page.get_by_test_id("fs-gesperrt")
    expect(gesperrt).to_contain_text("Nichtöffentlicher Teil, 1 TOP")
    expect(gesperrt).to_contain_text(ERKLAERUNG)
    expect(gesperrt).to_contain_text(ZUGANG)
    assert TOP_NOE not in page.content()
    result = axe()
    assert not result.failing, result.describe()

    goto(f"{url}?ansicht=bisher")
    expect(page.locator("#agenda-container")).to_contain_text("Gesperrte Information")
    erklaerung = page.get_by_test_id("fs-gesperrt-bisher")
    expect(erklaerung).to_contain_text(ERKLAERUNG)
    expect(erklaerung).to_contain_text(ZUGANG)
    assert TOP_NOE not in page.content()
    screenshot("fraktion_gesperrt_bisherige_ansicht_1440")
    problems.assert_clean("Gesperrte TOPs")


@pytest.mark.parametrize("ansicht", ["tagesordnung", "bisher"])
def test_tagesordnung_laedt_sich_nach_aktionen_im_panel_neu(
    page: Any, goto: Any, login: Any, fraktion: Fraktion, problems: BrowserProblems, ansicht: str
) -> None:
    """Der Auslöser sitzt neben dem Container (Issue #855); das Ereignis des Panels muss ihn weiter erreichen."""
    meeting = _geplante_sitzung(fraktion)
    page.set_viewport_size(BREIT)
    login(fraktion.vorsitz.user.email, PASSWORT)
    goto(f"{_sitzung_url(fraktion, meeting)}?ansicht={ansicht}")
    tagesordnung = page.locator("#agenda-container")
    expect(tagesordnung).to_contain_text(TOP_HAUSHALT)
    FactionAgendaItem.objects.create(meeting=meeting, number="2", title=TOP_RADWEG, visibility="public", order=3)
    page.evaluate("() => window.htmx.trigger(document.body, 'agenda-refresh')")
    expect(tagesordnung).to_contain_text(TOP_RADWEG)
    problems.assert_clean("Neu laden der Tagesordnung")


def test_bisherige_ansicht_nimmt_vorschlag_auf(
    page: Any, goto: Any, login: Any, fraktion: Fraktion, problems: BrowserProblems
) -> None:
    """Formulare in der Tagesordnung senden ihre eigene Aktion, nicht die des Neuladens (Issue #855)."""
    meeting = _geplante_sitzung(fraktion)
    vorschlag = FactionAgendaItem.objects.create(
        meeting=meeting,
        title=TOP_RADWEG,
        visibility="public",
        proposal_status="proposed",
        proposed_by=fraktion.sachkundig,
        proposed_at=timezone.now(),
        order=3,
    )
    page.set_viewport_size(BREIT)
    login(fraktion.vorsitz.user.email, PASSWORT)
    goto(f"{_sitzung_url(fraktion, meeting)}?ansicht=bisher")
    page.locator("#agenda-container").get_by_role("button", name="Annehmen").click()
    expect(page.locator("#agenda-container").get_by_role("button", name="Annehmen")).to_have_count(0)
    vorschlag.refresh_from_db()
    assert vorschlag.proposal_status == "active"
    problems.assert_clean("Vorschlag aufnehmen")
