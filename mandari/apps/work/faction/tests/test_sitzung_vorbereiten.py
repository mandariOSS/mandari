# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fraktionssitzung vor der Sitzung nach dem Muster (Issue #1071, Schritt 2): Planung, Einladung, Rückmeldungen.

Geprüft wird:

- Schalter und Zustände: Entwurf, geplant und eingeladen zeigen die neue Seite; laufend bleibt die Sitzungsansicht,
  abgeschlossen und abgesagt zeigen die Seite nach der Sitzung (test_sitzung_protokoll); ``?ansicht=bisher`` führt
  zurück.
- Keine Funktion fällt weg: Jede Aktion der bisherigen Seite (Aktionen, Dialoge, Links) ist für jede Rolle auf der
  neuen Seite erreichbar (Hauptansicht oder „Tagesordnung bearbeiten“). Ausnahmen mit Begründung.
- Rollen: Vorsitz plant und lädt ein, Mitglieder sehen die Einladung und antworten, Nicht-Vereidigte sehen vom
  nichtöffentlichen Teil nur die Zahl (auch in den Antworten der Aktionen).
- Versandergebnis je Person: verschickt, ohne nichtöffentlichen Teil, nicht verschickt mit Grund.
- Erweiterungen der Aktionen: Ende, Videolink aus der Nebenspalte, Zu- und Absagen je Sitzung, Kalenderdatei.
"""

from __future__ import annotations

import json
import re
import smtplib
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.tenants.models import Organization
from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionMeeting
from apps.work.notifications.models import Notification, NotificationType

pytestmark = pytest.mark.django_db

NEU = "work/faction/neu/vorbereiten.html"
ALT = "work/faction/detail.html"
NOE_TITEL = "Grundstücksangelegenheit Flurstück 12"
ERKLAERUNG = (
    "Diese Punkte sehen nur vereidigte Mitglieder mit Zugriff auf den nichtöffentlichen Teil. Zugang gibt der Vorsitz."
)


@pytest.fixture
def welt(org: Organization, make_member: Any) -> dict[str, Any]:
    Organization.objects.filter(pk=org.pk).update(work_new_design=True)
    org.refresh_from_db()
    vorsitz = make_member(org, [], email="vera@example.org", is_admin=True)
    vorsitz.is_sworn_in = True
    vorsitz.save(update_fields=["is_sworn_in"])
    rat = make_member(
        org,
        ["faction.view_public", "faction.view_non_public", "agenda.create", "meetings.view"],
        email="martin@example.org",
    )
    rat.is_sworn_in = True
    rat.save(update_fields=["is_sworn_in"])
    sachkundig = make_member(
        org,
        ["faction.view_public", "faction.view_non_public", "agenda.propose"],
        email="klara@example.org",
    )
    for m, (vor, nach) in (
        (vorsitz, ("Vera", "Beispiel")),
        (rat, ("Martin", "Muster")),
        (sachkundig, ("Klara", "Kopie")),
    ):
        m.user.first_name, m.user.last_name = vor, nach
        m.user.save(update_fields=["first_name", "last_name"])
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung Nr. 24",
        start=timezone.now() + timedelta(days=5),
        status="planned",
        location="Fraktionsbüro",
        is_virtual=True,
        video_link="https://video.example.org/fraktion",
        created_by=vorsitz,
    )
    for m in (vorsitz, rat, sachkundig):
        FactionAttendance.objects.create(meeting=meeting, membership=m, status="invited")
    haupt = FactionAgendaItem.objects.create(
        meeting=meeting, title="Beschlüsse", number="1", visibility="public", order=1
    )
    FactionAgendaItem.objects.create(
        meeting=meeting, title="Feuerwehrbedarfsplan", number="1.1", visibility="public", order=2, parent=haupt
    )
    FactionAgendaItem.objects.create(meeting=meeting, title="Termine", number="2", visibility="public", order=3)
    FactionAgendaItem.objects.create(meeting=meeting, title=NOE_TITEL, number="1", visibility="internal", order=4)
    FactionAgendaItem.objects.create(
        meeting=meeting,
        title="Radweg am Musterweg",
        visibility="public",
        proposal_status="proposed",
        proposed_by=sachkundig,
        proposed_at=timezone.now(),
        order=5,
    )
    return {"org": org, "vorsitz": vorsitz, "rat": rat, "sachkundig": sachkundig, "meeting": meeting, "haupt": haupt}


def _adresse(welt: dict[str, Any]) -> str:
    return reverse("work:faction_detail", kwargs={"org_slug": welt["org"].slug, "meeting_id": welt["meeting"].pk})


def _aktion(welt: dict[str, Any]) -> str:
    return reverse("work:faction_action", kwargs={"org_slug": welt["org"].slug, "meeting_id": welt["meeting"].pk})


def _seite(welt: dict[str, Any], client_for: Any, rolle: str, **anfrage: str) -> Any:
    antwort = client_for(welt[rolle].user).get(_adresse(welt), anfrage)
    assert antwort.status_code == 200
    return antwort


def _html(antwort: Any) -> str:
    return str(antwort.content.decode())


# ---------------------------------------------------------------------------
# Was eine Seite anbietet: Aktionen, Dialoge, Links
# ---------------------------------------------------------------------------

_AKTION_HX = re.compile(r"""["']action["']\s*:\s*["'](\w+)["']""")
_AKTION_FELD = re.compile(r"""name=["']action["'][^>]*value=["'](\w+)["']""")
_AKTION_BIND = re.compile(r"""itemMode === 'add' \? '(\w+)' : '(\w+)'""")
_EREIGNIS = re.compile(r"""\$dispatch\(\s*'(open-[a-z-]+)'""")
_LINK = re.compile(r"""href=["'](/work/[^"'?#]+)""")
#: TOP anlegen je Teil: bisher Dialog mit Sichtbarkeit, neu Formular mit festem Feld oder Auswahl
_ANLEGEN_ALT = re.compile(r"""open-add-item',\s*\{\s*visibility:\s*'(public|internal)'""")
_ANLEGEN_NEU = re.compile(r"""name="visibility" value="(public|internal)"|<option value="(public|internal)">""")


def _inhalt(html: str) -> str:
    anfang = html.index("<main")
    return html[anfang : html.index("</main>", anfang)]


def _funktionen(html: str) -> set[str]:
    """Aktionen (Formulare, hx-vals, Dialoge der Komponente), geöffnete Dialoge und Links der Seite."""
    teil = _inhalt(html).replace("&quot;", '"').replace("&#x27;", "'").replace("&#39;", "'")
    funktionen = {f"aktion:{a}" for a in _AKTION_HX.findall(teil)}
    funktionen |= {f"aktion:{a}" for a in _AKTION_FELD.findall(teil)}
    for paar in _AKTION_BIND.findall(teil):
        funktionen |= {f"aktion:{a}" for a in paar}
    funktionen |= {f"dialog:{e}" for e in _EREIGNIS.findall(teil)}
    funktionen |= {f"link:{pfad}" for pfad in _LINK.findall(teil)}
    funktionen |= {f"anlegen:{v}" for v in _ANLEGEN_ALT.findall(teil)}
    funktionen |= {f"anlegen:{a or b}" for a, b in _ANLEGEN_NEU.findall(teil)}
    return funktionen


#: Was die bisherige Seite anders anbot, mit Begründung
AUSNAHMEN = {
    # TOP ändern direkt in der Liste (Aktion edit_item) statt über den Dialog
    "dialog:open-edit-item",
    # TOP vorschlagen im Formular über der Tagesordnung (Aktion propose) statt über den Dialog
    "dialog:open-proposal",
    # Mitglieder: die lesende Tagesordnung lädt sich nach Panel-Aktionen per GET neu statt per refresh_agenda
    "aktion:refresh_agenda",
}


def _vergleich(welt: dict[str, Any], client_for: Any, rolle: str) -> list[str]:
    client = client_for(welt[rolle].user)
    alt = client.get(_adresse(welt), {"ansicht": "bisher"})
    neu = client.get(_adresse(welt))
    bearbeiten = client.get(_adresse(welt), {"ansicht": "tagesordnung"})
    assert alt.status_code == neu.status_code == 200
    assert ALT in [t.name for t in alt.templates]
    assert NEU in [t.name for t in neu.templates]
    vorher = _funktionen(_html(alt))
    nachher = _funktionen(_html(neu)) | _funktionen(_html(bearbeiten))
    ausnahmen = set(AUSNAHMEN)
    # TOP anlegen: Verwaltende über die Zeile „TOP hinzufügen“ je Teil statt über den Dialog. Für Mitglieder gilt die
    # Ausnahme nur, wenn jeder Teil (öffentlich, nichtöffentlich), den der Dialog bot, als Formular da ist.
    if rolle == "vorsitz" or {f for f in vorher if f.startswith("anlegen:")} <= nachher:
        ausnahmen.add("dialog:open-add-item")
    return sorted(vorher - nachher - ausnahmen)


def test_funktionen_erkennen() -> None:
    html = (
        '<main><form><input type="hidden" name="action" value="invite"></form>'
        '<button hx-vals=\'{"action": "move_item"}\'></button>'
        "<input x-bind:value=\"itemMode === 'add' ? 'add_item' : 'edit_item'\">"
        "<button @click=\"$dispatch('open-item-panel', { id: '1' })\"></button>"
        '<a href="/work/x/faction/?time=all">x</a></main>'
    )
    assert _funktionen(html) == {
        "aktion:invite",
        "aktion:move_item",
        "aktion:add_item",
        "aktion:edit_item",
        "dialog:open-item-panel",
        "link:/work/x/faction/",
    }


@pytest.mark.parametrize("rolle", ["vorsitz", "rat", "sachkundig"])
@pytest.mark.parametrize("zustand", ["draft", "planned", "invited"])
def test_keine_funktion_der_bisherigen_seite_entfaellt(
    welt: dict[str, Any], client_for: Any, rolle: str, zustand: str
) -> None:
    meeting = welt["meeting"]
    meeting.status = zustand
    meeting.invitation_sent = zustand == "invited"
    meeting.invitation_sent_at = timezone.now() if zustand == "invited" else None
    meeting.rsvp_enabled = True
    meeting.save()
    fehlt = _vergleich(welt, client_for, rolle)
    assert not fehlt, f"{rolle}/{zustand}: {fehlt}"


def test_bestand_zeigt_was_der_vergleich_pruefen_soll(welt: dict[str, Any], client_for: Any) -> None:
    """Die bisherige Seite bietet im Bestand wirklich Einladen, Verschieben, Vorschläge und Löschen an."""
    alt = _funktionen(_html(_seite(welt, client_for, "vorsitz", ansicht="bisher")))
    erwartet = {
        "aktion:invite",
        "aktion:move_item",
        "aktion:accept_proposal",
        "aktion:reject_proposal",
        "aktion:cancel",
        "aktion:update",
        "dialog:open-delete-item",
        "dialog:open-delete-meeting",
        "dialog:open-item-panel",
        "dialog:open-add-sub",
    }
    assert erwartet <= alt, erwartet - alt


# ---------------------------------------------------------------------------
# Zustände und Schalter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,vorlage",
    [
        ("draft", NEU),
        ("planned", NEU),
        ("invited", NEU),
        ("ongoing", "work/faction/sitzung/seite.html"),
        ("completed", "work/faction/neu/protokoll.html"),
        ("cancelled", "work/faction/neu/protokoll.html"),
    ],
)
def test_zustaende(welt: dict[str, Any], client_for: Any, status: str, vorlage: str) -> None:
    FactionMeeting.objects.filter(pk=welt["meeting"].pk).update(status=status)
    namen = [t.name for t in _seite(welt, client_for, "vorsitz").templates]
    assert vorlage in namen


def test_rueckweg_und_schalter(welt: dict[str, Any], client_for: Any) -> None:
    assert ALT in [t.name for t in _seite(welt, client_for, "vorsitz", ansicht="bisher").templates]
    Organization.objects.filter(pk=welt["org"].pk).update(work_new_design=False)
    assert ALT in [t.name for t in _seite(welt, client_for, "vorsitz").templates]


# ---------------------------------------------------------------------------
# Rollen
# ---------------------------------------------------------------------------


def test_vorsitz_planung(welt: dict[str, Any], client_for: Any) -> None:
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert 'data-gestaltung="neu"' in html and 'data-modus="planung"' in html
    assert "Noch nicht eingeladen." in html and "Jetzt einladen …" in html
    assert "Tagesordnung" in html and "4 TOPs, davon 1 nichtöffentlich" in html
    assert "Radweg am Musterweg" in html and "Aufnehmen" in html
    assert NOE_TITEL in html
    assert "Termin und Ort" in html and 'name="end_time"' in html
    assert "Zu- und Absagen abfragen" in html
    # Dialog „Jetzt einladen“: Empfänger, nicht vereidigt, Vorschau je Fassung
    assert 'id="fs-einladen"' in html
    assert "Klara Kopie ist nicht vereidigt" in html
    assert html.count('data-testid="fs-mail-vorschau"') == 2
    assert "Einladung an 3 Mitglieder senden" in html


def test_top_aufklappen_mit_angaben(welt: dict[str, Any], client_for: Any) -> None:
    haupt = welt["haupt"]
    haupt.set_description_encrypted("Hilfsfrist im Norden")
    haupt.save()
    html = _html(_seite(welt, client_for, "vorsitz", top=str(haupt.pk)))
    assert f'id="d-{haupt.pk}"' in html
    assert "Hilfsfrist im Norden" in html and 'value="edit_item"' in html
    assert "Unterlagen" in html and "Hinzufügen …" in html


def test_ratsmitglied_liest_und_traegt_ein(welt: dict[str, Any], client_for: Any) -> None:
    html = _html(_seite(welt, client_for, "rat"))
    assert 'data-modus="lesen"' in html
    assert "In Planung." in html and "Einladung folgt" in html
    assert "TOP eintragen" in html and 'value="add_item"' in html
    for verwaltung in ("Jetzt einladen", "Sitzung löschen", "Termin und Ort", 'value="move_item"', "Aufnehmen"):
        assert verwaltung not in html, verwaltung
    assert NOE_TITEL in html  # vereidigt


def test_nicht_vereidigt_sieht_vom_noe_teil_nur_die_zahl(welt: dict[str, Any], client_for: Any) -> None:
    html = _html(_seite(welt, client_for, "sachkundig"))
    assert NOE_TITEL not in html
    assert 'data-testid="fs-gesperrt"' in html and "Nichtöffentlicher Teil, 1 TOP" in html
    assert "TOP vorschlagen" in html and 'value="propose"' in html
    assert "Ihr Vorschlag „Radweg am Musterweg“ wartet auf Genehmigung." in html


def test_bisherige_ansicht_erklaert_den_gesperrten_teil(welt: dict[str, Any], client_for: Any) -> None:
    """Wie die neue Seite: warum gesperrt und wer Zugang gibt (Issue #855); Vereidigte brauchen die Erklärung nicht."""
    gesperrt = _html(_seite(welt, client_for, "sachkundig", ansicht="bisher"))
    assert "Gesperrte Information" in gesperrt and ERKLAERUNG in gesperrt
    assert NOE_TITEL not in gesperrt
    vereidigt = _html(_seite(welt, client_for, "rat", ansicht="bisher"))
    assert NOE_TITEL in vereidigt and ERKLAERUNG not in vereidigt
    # Dieselbe Erklärung auf der neuen Seite
    assert ERKLAERUNG in _html(_seite(welt, client_for, "sachkundig"))


@pytest.mark.parametrize(
    ("vereidigt", "rechte"),
    [
        pytest.param(False, ["faction.view_public", "faction.view_non_public"], id="ohne-vereidigung"),
        pytest.param(True, ["faction.view_public"], id="ohne-recht"),
    ],
)
def test_erklaerung_stimmt_bei_jedem_grund_der_sperre(
    welt: dict[str, Any], client_for: Any, make_member: Any, vereidigt: bool, rechte: list[str]
) -> None:
    """Gesperrt ist, wem die Vereidigung oder das Recht fehlt; der Satz darf keinen der beiden Gründe behaupten."""
    person = make_member(welt["org"], rechte, email="gesperrt@example.org")
    person.is_sworn_in = vereidigt
    person.save(update_fields=["is_sworn_in"])
    client = client_for(person.user)
    for anfrage in ({}, {"ansicht": "bisher"}):
        html = _html(client.get(_adresse(welt), anfrage))
        assert ERKLAERUNG in html and NOE_TITEL not in html, anfrage
        assert "keine Vereidigung" not in html and "nachtragen" not in html, anfrage


def test_tagesordnung_vererbt_ihren_formularen_keine_aktion(welt: dict[str, Any], client_for: Any) -> None:
    """
    htmx vererbt hx-vals und lässt es vor Formularfeldern gelten: Am Container schickte „TOP hinzufügen“ refresh_agenda
    statt add_item, der TOP ging verloren (Issue #855). Den Ablauf im Browser prüft tests_e2e/test_fraktion_einladung_ablauf.py.
    """
    client = client_for(welt["vorsitz"].user)
    for ansicht in ("tagesordnung", "bisher"):
        html = _html(client.get(_adresse(welt), {"ansicht": ansicht}))
        container = re.search(r'<div id="agenda-container"[^>]*>', html)
        assert container is not None and "hx-" not in container.group(), ansicht
        ausloeser = re.search(r'<div hidden[^>]*hx-trigger="agenda-refresh from:body"[^>]*>', html)
        assert ausloeser is not None and 'hx-target="#agenda-container"' in ausloeser.group(), ansicht


def test_nicht_vereidigter_vorsitz_auch_in_der_antwort_der_aktion(
    welt: dict[str, Any], client_for: Any, make_member: Any
) -> None:
    verwaltung = make_member(welt["org"], ["faction.view_public", "faction.manage"], email="gf@example.org")
    client = client_for(verwaltung.user)
    seite = _html(client.get(_adresse(welt)))
    assert 'data-modus="planung"' in seite and NOE_TITEL not in seite and 'data-testid="fs-gesperrt"' in seite
    antwort = client.post(
        _aktion(welt), {"action": "refresh_agenda"}, HTTP_HX_REQUEST="true", HTTP_X_WORK_GESTALTUNG="neu"
    )
    assert antwort.status_code == 200
    teil = _html(antwort)
    assert 'class="fs-tops"' in teil and NOE_TITEL not in teil and 'data-testid="fs-gesperrt"' in teil


def test_aktion_antwortet_je_seite(welt: dict[str, Any], client_for: Any) -> None:
    client = client_for(welt["vorsitz"].user)
    neu = client.post(
        _aktion(welt),
        {"action": "add_item", "title": "Presse", "visibility": "public", "offen": str(welt["haupt"].pk)},
        HTTP_HX_REQUEST="true",
        HTTP_X_WORK_GESTALTUNG="neu",
    )
    assert neu.status_code == 200 and "Presse" in _html(neu) and 'class="fs-tops"' in _html(neu)
    assert f'id="d-{welt["haupt"].pk}"' in _html(neu)  # der offene TOP bleibt offen
    alt = client.post(_aktion(welt), {"action": "refresh_agenda"}, HTTP_HX_REQUEST="true")
    assert 'class="fs-tops"' not in _html(alt) and "Öffentlicher TOP" in _html(alt)


# ---------------------------------------------------------------------------
# Einladung, Versandergebnis, Rückmeldungen
# ---------------------------------------------------------------------------


def _einladen(welt: dict[str, Any], client_for: Any) -> None:
    antwort = client_for(welt["vorsitz"].user).post(_aktion(welt), {"action": "invite"})
    assert antwort.status_code == 302


def test_versandergebnis_je_person(welt: dict[str, Any], client_for: Any) -> None:
    welt["rat"].user.email = ""
    welt["rat"].user.save(update_fields=["email"])
    _einladen(welt, client_for)
    teilnahmen = {a.membership_id: a for a in welt["meeting"].attendances.all()}
    vorsitz, rat, sachkundig = (teilnahmen[welt[r].pk] for r in ("vorsitz", "rat", "sachkundig"))
    assert vorsitz.invitation_mailed_at and not vorsitz.invitation_error and not vorsitz.invitation_without_internal
    assert sachkundig.invitation_mailed_at and sachkundig.invitation_without_internal
    assert rat.invitation_mailed_at is None and rat.invitation_error == "Keine E-Mail-Adresse hinterlegt."

    html = _html(_seite(welt, client_for, "vorsitz"))
    assert 'data-modus="einladung"' in html and 'data-testid="fs-tln"' in html
    assert "Nicht versendet." in html and "Keine E-Mail-Adresse hinterlegt." in html
    assert "ohne nichtöffentlichen Teil" in html
    assert "2 von 3 Einladungen versendet, eine nicht." in html
    assert 'data-testid="fs-versandliste"' in html and "Sitzung starten" in html


def test_versandfehler_mit_grund(welt: dict[str, Any], client_for: Any) -> None:
    fehler = smtplib.SMTPRecipientsRefused({"klara@example.org": (550, b"unbekannt")})

    def senden(**kwargs: Any) -> bool:
        if kwargs["to"] == ["klara@example.org"]:
            raise fehler
        return True

    with mock.patch("apps.common.mail.send", side_effect=senden):
        _einladen(welt, client_for)
    a = welt["meeting"].attendances.get(membership=welt["sachkundig"])
    assert a.invitation_error == "Der Mailserver hat die Adresse klara@example.org abgelehnt."
    assert a.invitation_mailed_at is None
    # Das Ergebnis steht an der Teilnahme, die Seite zeigt es auch später; „versendet“ verspricht keine Zustellung
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert "2 von 3 Einladungen versendet, eine nicht." in html and "2 von 3 versendet, 1 nicht." in html
    assert "Nicht versendet" in html and "Der Mailserver hat die Adresse klara@example.org abgelehnt." in html
    assert "zugestellt" not in html.lower() and "nicht verschickt" not in html.lower()


@pytest.mark.parametrize("schalter", ["ansicht-bisher", "schalter-aus"])
def test_bisherige_seite_zeigt_versandergebnis_mit_grund(welt: dict[str, Any], client_for: Any, schalter: str) -> None:
    """
    Neu angelegte Organisationen starten mit dem Schalter aus (Issue #855): Dort steht dasselbe Ergebnis, auch nach dem
    Neuladen, und die Meldung verschweigt die abgelehnte Adresse nicht. Mitglieder sehen weder Ergebnis noch Adresse.
    """
    anfrage = {"ansicht": "bisher"} if schalter == "ansicht-bisher" else {}
    if schalter == "schalter-aus":
        Organization.objects.filter(pk=welt["org"].pk).update(work_new_design=False)
    fehler = smtplib.SMTPRecipientsRefused({"klara@example.org": (550, b"unbekannt")})

    def senden(**kwargs: Any) -> bool:
        if kwargs["to"] == ["klara@example.org"]:
            raise fehler
        return True

    client = client_for(welt["vorsitz"].user)
    with mock.patch("apps.common.mail.send", side_effect=senden):
        erstversand = client.post(_aktion(welt), {"action": "invite"})
        aktualisierung = client.post(_aktion(welt), {"action": "invite"})
    assert [str(m) for m in get_messages(erstversand.wsgi_request)] == [
        "Einladungen an 2 von 3 Mitgliedern versendet, 1 nicht. Den Grund nennt das Versandergebnis."
    ]
    assert [str(m) for m in get_messages(aktualisierung.wsgi_request)][-1] == (
        "Aktualisierte Einladung an 2 von 3 Mitgliedern versendet, 1 nicht. Den Grund nennt das Versandergebnis."
    )

    seite = client.get(_adresse(welt), anfrage)
    assert ALT in [t.name for t in seite.templates]
    html = _html(seite)
    assert 'data-testid="fs-versand-bisher"' in html and "2 von 3 versendet, 1 nicht." in html
    assert "Klara Kopie: Nicht versendet." in html
    assert "Der Mailserver hat die Adresse klara@example.org abgelehnt." in html
    assert "ohne nichtöffentlichen Teil" not in html.split("fs-versand-fehler")[1].split("</ul>")[0]
    assert "zugestellt" not in html.lower()

    mitglied = _html(_seite(welt, client_for, "rat", **anfrage))
    assert "fs-versand-bisher" not in mitglied and "klara@example.org" not in mitglied


def test_bisherige_seite_ohne_erfasstes_ergebnis_ohne_abschnitt(welt: dict[str, Any], client_for: Any) -> None:
    """Versand vor dieser Erfassung: keine Zahlen, die so aussehen, als sei nichts rausgegangen."""
    FactionMeeting.objects.filter(pk=welt["meeting"].pk).update(invitation_sent=True, status="invited")
    assert "fs-versand-bisher" not in _html(_seite(welt, client_for, "vorsitz", ansicht="bisher"))


def test_bisherige_seite_weist_vor_der_ersten_einladung_auf_die_testmail_hin(
    welt: dict[str, Any], client_for: Any, make_member: Any
) -> None:
    html = _html(_seite(welt, client_for, "vorsitz", ansicht="bisher"))
    assert 'data-testid="fs-testmail-bisher"' in html and "Ihre erste Einladung über diesen Versandweg." in html
    assert "Einstellungen › E-Mail-Versand" in html and 'value="test_invitation"' in html
    # Darf einladen, sieht die Einstellungen nicht: Hinweis ohne den Weg dorthin
    einladend = make_member(welt["org"], ["faction.view_public", "faction.invite"], email="einladend@example.org")
    html = _html(client_for(einladend.user).get(_adresse(welt), {"ansicht": "bisher"}))
    assert "Ihre erste Einladung über diesen Versandweg." in html and "Einstellungen › E-Mail-Versand" not in html
    # Darf nicht einladen: kein Hinweis
    assert "fs-testmail-bisher" not in _html(_seite(welt, client_for, "rat", ansicht="bisher"))
    # Nach der ersten Einladung der Organisation entfällt er, auch für die nächste Sitzung
    _einladen(welt, client_for)
    naechste = FactionMeeting.objects.create(
        organization=welt["org"], title="Nächste Sitzung", start=timezone.now() + timedelta(days=9), status="planned"
    )
    adresse = reverse("work:faction_detail", kwargs={"org_slug": welt["org"].slug, "meeting_id": naechste.pk})
    assert "fs-testmail-bisher" not in _html(client_for(welt["vorsitz"].user).get(adresse, {"ansicht": "bisher"}))


def test_rueckmeldungen_nach_muster(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    FactionMeeting.objects.filter(pk=meeting.pk).update(rsvp_enabled=True)
    _einladen(welt, client_for)
    client_for(welt["rat"].user).post(_aktion(welt), {"action": "respond", "status": "confirmed"})
    client_for(welt["sachkundig"].user).post(
        _aktion(welt), {"action": "respond", "status": "declined", "response_message": "Dienstreise"}
    )
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert "Rückmeldungen" in html and "1 Zusage, 1 Absage und 1 offen" in html
    assert "Noch offen, 1" in html and "Zugesagt, 1" in html and "Abgesagt, 1" in html
    assert "„Dienstreise“" in html
    mitglied = _html(_seite(welt, client_for, "rat"))
    assert (
        "Können Sie teilnehmen?" in mitglied and 'aria-pressed="true"' in mitglied and "Sie haben zugesagt." in mitglied
    )
    assert "Zugesagt: Martin Muster." in mitglied


def test_aktualisierung_nach_aenderung(welt: dict[str, Any], client_for: Any) -> None:
    _einladen(welt, client_for)
    FactionAgendaItem.objects.create(meeting=welt["meeting"], title="Neu", number="3", visibility="public", order=9)
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert "Tagesordnung geändert." in html and 'id="fs-aktualisieren"' in html


# ---------------------------------------------------------------------------
# TOP-Vorschläge nach dem Versand (Issue #1160)
# ---------------------------------------------------------------------------

OFFENER_VORSCHLAG = "Radweg am Musterweg"


def _aufnehmen(
    welt: dict[str, Any], client_for: Any, titel: str = OFFENER_VORSCHLAG, *, teil: str = "", **kopf: str
) -> Any:
    vorschlag = welt["meeting"].agenda_items.get(title=titel)
    daten = {"action": "accept_proposal", "item_id": str(vorschlag.pk)}
    if teil:
        daten["visibility"] = teil
    return client_for(welt["vorsitz"].user).post(_aktion(welt), daten, **kopf)


def test_offene_vorschlaege_stehen_nach_dem_versand_auf_der_ersten_seite(welt: dict[str, Any], client_for: Any) -> None:
    _einladen(welt, client_for)
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert 'data-modus="einladung"' in html
    assert 'data-testid="fs-vorschlaege"' in html and OFFENER_VORSCHLAG in html
    assert 'value="accept_proposal"' in html and 'value="reject_proposal"' in html


def test_vorschlag_nach_dem_versand_wird_gespeichert_und_aufgenommen(welt: dict[str, Any], client_for: Any) -> None:
    _einladen(welt, client_for)
    titel = "Spielplatz im Südpark"
    antwort = client_for(welt["sachkundig"].user).post(
        _aktion(welt), {"action": "propose", "title": titel, "description": "Bitte beraten"}
    )
    assert antwort.status_code == 302
    vorschlag = welt["meeting"].agenda_items.get(title=titel)
    assert vorschlag.proposal_status == "proposed" and vorschlag.get_description_decrypted() == "Bitte beraten"
    assert f"Ihr Vorschlag „{titel}“ wartet auf Genehmigung." in _html(_seite(welt, client_for, "sachkundig"))

    assert titel in _html(_seite(welt, client_for, "vorsitz"))
    assert _aufnehmen(welt, client_for, titel).status_code == 302
    vorschlag.refresh_from_db()
    assert vorschlag.proposal_status == "active" and vorschlag.number == "3"
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert "Tagesordnung geändert." in html and 'id="fs-aktualisieren"' in html
    assert f'data-top-titel="{titel}"' in _html(_seite(welt, client_for, "rat"))


def test_vor_dem_versand_eingegangener_vorschlag_zaehlt_nach_der_aufnahme_als_aenderung(
    welt: dict[str, Any], client_for: Any
) -> None:
    _einladen(welt, client_for)
    assert "Tagesordnung geändert." not in _html(_seite(welt, client_for, "vorsitz"))

    assert _aufnehmen(welt, client_for).status_code == 302
    meeting = welt["meeting"]
    meeting.refresh_from_db()
    vorschlag = meeting.agenda_items.get(title=OFFENER_VORSCHLAG)
    # Die Annahme lässt updated_at stehen: Nur reviewed_at zeigt, dass sich die Tagesordnung geändert hat
    assert vorschlag.updated_at < meeting.invitation_sent_at < vorschlag.reviewed_at
    assert "Tagesordnung geändert." in _html(_seite(welt, client_for, "vorsitz"))

    # Nach der Aktualisierung ist die Fassung wieder aktuell
    _einladen(welt, client_for)
    assert "Tagesordnung geändert." not in _html(_seite(welt, client_for, "vorsitz"))


def test_abgelehnter_vorschlag_aendert_die_tagesordnung_nicht(welt: dict[str, Any], client_for: Any) -> None:
    _einladen(welt, client_for)
    vorschlag = welt["meeting"].agenda_items.get(title=OFFENER_VORSCHLAG)
    client_for(welt["vorsitz"].user).post(_aktion(welt), {"action": "reject_proposal", "item_id": str(vorschlag.pk)})
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert "Tagesordnung geändert." not in html and 'data-testid="fs-vorschlaege"' not in html


def test_entscheidung_auf_der_tagesordnung_bleibt_auf_der_tagesordnung(welt: dict[str, Any], client_for: Any) -> None:
    _einladen(welt, client_for)
    adresse = _adresse(welt)
    antwort = _aufnehmen(welt, client_for, HTTP_REFERER=f"https://mandari.example{adresse}?ansicht=tagesordnung")
    assert antwort["Location"] == f"{adresse}?ansicht=tagesordnung"


def test_entscheidung_auf_der_einladung_bleibt_auf_der_einladung(welt: dict[str, Any], client_for: Any) -> None:
    _einladen(welt, client_for)
    adresse = _adresse(welt)
    antwort = _aufnehmen(welt, client_for, HTTP_REFERER=f"https://mandari.example{adresse}")
    assert antwort["Location"] == adresse


def test_vereidigte_schlagen_auch_nichtoeffentlich_vor(welt: dict[str, Any], client_for: Any) -> None:
    assert 'id="tf-sicht"' not in _html(_seite(welt, client_for, "sachkundig"))

    sachkundig = welt["sachkundig"]
    sachkundig.is_sworn_in = True
    sachkundig.save(update_fields=["is_sworn_in"])
    assert 'id="tf-sicht"' in _html(_seite(welt, client_for, "sachkundig"))

    titel = "Grundstücksangelegenheit"
    client_for(sachkundig.user).post(_aktion(welt), {"action": "propose", "title": titel, "visibility": "internal"})
    vorschlag = welt["meeting"].agenda_items.get(title=titel)
    assert vorschlag.visibility == "internal" and vorschlag.proposal_status == "proposed"


def test_bisherige_ansicht_bietet_den_nichtoeffentlichen_teil_nur_vereidigten(
    welt: dict[str, Any], client_for: Any
) -> None:
    assert 'id="vorschlag-teil"' not in _html(_seite(welt, client_for, "sachkundig", ansicht="bisher"))

    sachkundig = welt["sachkundig"]
    sachkundig.is_sworn_in = True
    sachkundig.save(update_fields=["is_sworn_in"])
    html = _html(_seite(welt, client_for, "sachkundig", ansicht="bisher"))
    assert 'id="vorschlag-teil"' in html and 'name="visibility"' in html


def test_aufnehmen_legt_den_vorschlag_in_den_gewaehlten_teil(welt: dict[str, Any], client_for: Any) -> None:
    """Wer aufnimmt, ordnet ein: Der TOP steht nie, auch nicht kurz, im öffentlichen Teil (Issue #1160)."""
    _einladen(welt, client_for)
    assert 'id="vs-teil-' in _html(_seite(welt, client_for, "vorsitz"))

    assert _aufnehmen(welt, client_for, teil="internal").status_code == 302

    vorschlag = welt["meeting"].agenda_items.get(title=OFFENER_VORSCHLAG)
    assert vorschlag.visibility == "internal" and vorschlag.proposal_status == "active"
    assert vorschlag.number == "NÖ 2"
    assert OFFENER_VORSCHLAG not in _html(_seite(welt, client_for, "sachkundig"))
    vorsitz = _html(_seite(welt, client_for, "vorsitz"))
    assert "Tagesordnung geändert." in vorsitz
    # Die Nummer trägt „NÖ“ schon selbst: in der Nebenspalte nicht doppelt
    assert '<span class="nr">NÖ 2</span>' in vorsitz and "NÖ NÖ" not in vorsitz
    meldung = Notification.objects.get(
        recipient=welt["sachkundig"], notification_type=NotificationType.FACTION_PROPOSAL_DECIDED
    )
    assert OFFENER_VORSCHLAG not in meldung.title and OFFENER_VORSCHLAG not in meldung.message


def test_aufnehmen_ohne_auswahl_behaelt_den_teil_des_vorschlags(welt: dict[str, Any], client_for: Any) -> None:
    assert _aufnehmen(welt, client_for).status_code == 302
    vorschlag = welt["meeting"].agenda_items.get(title=OFFENER_VORSCHLAG)
    assert vorschlag.visibility == "public" and vorschlag.number == "3"


def test_nichtoeffentlicher_vorschlag_ist_beim_aufnehmen_vorausgewaehlt(welt: dict[str, Any], client_for: Any) -> None:
    welt["meeting"].agenda_items.filter(title=OFFENER_VORSCHLAG).update(visibility="internal")
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert '<option value="internal" selected>nichtöffentlich</option>' in html


def test_nicht_vereidigte_nehmen_nicht_in_den_nichtoeffentlichen_teil_auf(
    welt: dict[str, Any], client_for: Any, make_member: Any
) -> None:
    genehmigend = make_member(welt["org"], ["faction.view_public", "agenda.approve"], email="gerda@example.org")
    vorschlag = welt["meeting"].agenda_items.get(title=OFFENER_VORSCHLAG)
    html = _html(client_for(genehmigend.user).get(_adresse(welt)))
    assert OFFENER_VORSCHLAG in html and 'id="vs-teil-' not in html

    antwort = client_for(genehmigend.user).post(
        _aktion(welt), {"action": "accept_proposal", "item_id": str(vorschlag.pk), "visibility": "internal"}
    )
    assert antwort.status_code == 403
    vorschlag.refresh_from_db()
    assert vorschlag.proposal_status == "proposed" and vorschlag.visibility == "public"


def test_wechsel_des_teils_im_panel_nummeriert_beide_teile_neu(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    termine = meeting.agenda_items.get(title="Beschlüsse")
    url = reverse(
        "work:faction_item_panel_action",
        kwargs={"org_slug": welt["org"].slug, "meeting_id": meeting.pk, "item_id": termine.pk},
    )
    antwort = client_for(welt["vorsitz"].user).post(url, {"action": "update", "visibility": "internal"})
    assert antwort.status_code == 204

    nummern = {i.title: i.number for i in meeting.agenda_items.filter(parent__isnull=True, proposal_status="active")}
    # Ohne gewählte Stelle zählt die bisherige Reihenfolge der Sitzung
    assert nummern == {"Beschlüsse": "NÖ 1", "Termine": "1", NOE_TITEL: "NÖ 2"}


def test_formular_sichert_eingaben_im_browser(welt: dict[str, Any], client_for: Any) -> None:
    html = _html(_seite(welt, client_for, "sachkundig"))
    assert 'x-data="fsTopEntwurf"' in html
    assert f'data-sitzung="{welt["meeting"].pk}"' in html and f'data-organisation="{welt["org"].pk}"' in html
    assert f'data-konto="{welt["sachkundig"].user.pk}"' in html
    # Der Titel eigener Vorschläge steht an der Zeile: Daran erkennt die Sicherung, dass der Server ihn angenommen hat
    assert f'data-top-titel="{OFFENER_VORSCHLAG}"' in html


def test_aktionsadresse_fuehrt_nach_der_anmeldung_zur_sitzung(welt: dict[str, Any], client_for: Any) -> None:
    antwort = client_for(welt["sachkundig"].user).get(_aktion(welt))
    assert antwort.status_code == 302 and antwort["Location"] == _adresse(welt)


# ---------------------------------------------------------------------------
# Erweiterte Aktionen
# ---------------------------------------------------------------------------


def test_termin_aus_der_nebenspalte(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    lokal = timezone.localtime(meeting.start)
    antwort = client_for(welt["vorsitz"].user).post(
        _aktion(welt),
        {
            "action": "update",
            "title": meeting.title,
            "description": "",
            "start_date": f"{lokal:%Y-%m-%d}",
            "start_time": "18:00",
            "end_time": "20:00",
            "location": "Rathaus",
            "video_link": "",
            "video_aus_link": "1",
        },
    )
    assert antwort.status_code == 302
    meeting.refresh_from_db()
    assert f"{timezone.localtime(meeting.end):%H:%M}" == "20:00" and meeting.location == "Rathaus"
    assert meeting.is_virtual is False and meeting.video_link == ""


def test_ende_vor_beginn_wird_abgewiesen(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    lokal = timezone.localtime(meeting.start)
    client_for(welt["vorsitz"].user).post(
        _aktion(welt),
        {
            "action": "update",
            "title": meeting.title,
            "start_date": f"{lokal:%Y-%m-%d}",
            "start_time": "18:00",
            "end_time": "17:00",
        },
    )
    meeting.refresh_from_db()
    assert meeting.end is None


def test_zu_und_absagen_schalten_nur_mit_recht(welt: dict[str, Any], client_for: Any) -> None:
    client_for(welt["rat"].user).post(_aktion(welt), {"action": "set_rsvp", "rsvp_enabled": "on"})
    welt["meeting"].refresh_from_db()
    assert welt["meeting"].rsvp_enabled is False
    client_for(welt["vorsitz"].user).post(_aktion(welt), {"action": "set_rsvp", "rsvp_enabled": "on"})
    welt["meeting"].refresh_from_db()
    assert welt["meeting"].rsvp_enabled is True


def test_kalenderdatei(welt: dict[str, Any], client_for: Any) -> None:
    adresse = reverse("work:faction_ics", kwargs={"org_slug": welt["org"].slug, "meeting_id": welt["meeting"].pk})
    antwort = client_for(welt["sachkundig"].user).get(adresse)
    assert antwort.status_code == 200 and antwort["Content-Type"].startswith("text/calendar")
    inhalt = antwort.content.decode()
    assert "BEGIN:VEVENT" in inhalt and NOE_TITEL not in inhalt


def test_hx_kopfzeile_ist_json(welt: dict[str, Any], client_for: Any) -> None:
    html = _html(_seite(welt, client_for, "vorsitz"))
    kopf = re.search(r"hx-headers='([^']+)'", html)
    assert kopf and json.loads(kopf.group(1)) == {"X-Work-Gestaltung": "neu"}


# ---------------------------------------------------------------------------
# Nachbesserungen aus der Prüfung
# ---------------------------------------------------------------------------


def test_vereidigtes_mitglied_traegt_nichtoeffentlichen_top_ein(welt: dict[str, Any], client_for: Any) -> None:
    html = _html(_seite(welt, client_for, "rat"))
    assert 'name="visibility"' in html and '<option value="internal">nichtöffentlich</option>' in html
    assert "unter NÖ 1" in html
    antwort = client_for(welt["rat"].user).post(
        _aktion(welt), {"action": "add_item", "title": "Vertrauliches", "visibility": "internal"}
    )
    assert antwort.status_code == 302
    assert welt["meeting"].agenda_items.get(title="Vertrauliches").visibility == "internal"
    # Nicht vereidigt: keine Auswahl
    assert '<option value="internal">' not in _html(_seite(welt, client_for, "sachkundig"))


def test_starten_ab_dreissig_minuten_auch_ohne_einladung(
    welt: dict[str, Any], client_for: Any, make_member: Any
) -> None:
    FactionMeeting.objects.filter(pk=welt["meeting"].pk).update(start=timezone.now() + timedelta(minutes=10))
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert "Noch nicht eingeladen." in html and 'value="start"' in html and "Sitzung starten" in html
    # Nach dem Versand auch für Rollen mit faction.start ohne Verwaltung
    leitung = make_member(welt["org"], ["faction.view_public", "faction.start"], email="leitung@example.org")
    FactionMeeting.objects.filter(pk=welt["meeting"].pk).update(
        status="invited", invitation_sent=True, invitation_sent_at=timezone.now()
    )
    seite = _html(client_for(leitung.user).get(_adresse(welt)))
    assert 'data-modus="lesen"' in seite and 'value="start"' in seite
    # Ohne das Recht kein Start
    assert 'value="start"' not in _html(_seite(welt, client_for, "rat"))


def test_teilnehmende_in_der_planung_mit_gaesten(welt: dict[str, Any], client_for: Any) -> None:
    client = client_for(welt["vorsitz"].user)
    client.post(_aktion(welt), {"action": "add_attendee", "attendee_type": "guest", "guest_name": "Gerda Gast"})
    html = _html(client.get(_adresse(welt)))
    assert 'data-modus="planung"' in html and 'data-testid="fs-teilnehmende"' in html
    assert "Martin Muster" in html and "Gäste: Gerda Gast." in html


def _termin(meeting: FactionMeeting, **felder: str) -> dict[str, str]:
    lokal = timezone.localtime(meeting.start)
    return {
        "action": "update",
        "title": meeting.title,
        "start_date": f"{lokal:%Y-%m-%d}",
        "start_time": f"{lokal:%H:%M}",
        **felder,
    }


def test_videolink_nur_http(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    for felder in ({"video_aus_link": "1"}, {"is_virtual": "on"}):
        client_for(welt["vorsitz"].user).post(
            _aktion(welt), _termin(meeting, video_link="javascript:alert(1)", **felder)
        )
        meeting.refresh_from_db()
        assert meeting.video_link == "https://video.example.org/fraktion"


def test_online_bleibt_ohne_link(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    FactionMeeting.objects.filter(pk=meeting.pk).update(video_link="", is_virtual=True)
    meeting.refresh_from_db()
    client_for(welt["vorsitz"].user).post(
        _aktion(welt), _termin(meeting, location="Rathaus", video_link="", video_aus_link="1")
    )
    meeting.refresh_from_db()
    assert meeting.is_virtual is True and meeting.location == "Rathaus"


def test_vorschau_ohne_noe_unterpunkte(welt: dict[str, Any], client_for: Any) -> None:
    FactionAgendaItem.objects.create(
        meeting=welt["meeting"],
        title="Vertraulicher Unterpunkt",
        number="1.2",
        visibility="internal",
        order=2,
        parent=welt["haupt"],
    )
    html = _html(_seite(welt, client_for, "vorsitz"))
    vorschauen = re.findall(r'data-testid="fs-mail-vorschau">(.*?)</div>', html, flags=re.S)
    assert len(vorschauen) == 2
    assert "Vertraulicher Unterpunkt" in vorschauen[0] and "Vertraulicher Unterpunkt" not in vorschauen[1]


def test_abfragen_wachsen_nicht_mit_den_tops(welt: dict[str, Any], client_for: Any) -> None:
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.work.faction.models import FactionAgendaItemAttachment

    def tops(n: int, start: int) -> None:
        for i in range(start, start + n):
            item = FactionAgendaItem.objects.create(
                meeting=welt["meeting"], title=f"Zusatz {i}", number=str(10 + i), visibility="public", order=20 + i
            )
            FactionAgendaItemAttachment.objects.create(
                agenda_item=item, file=SimpleUploadedFile(f"a{i}.pdf", b"%PDF-1.4"), filename=f"a{i}.pdf"
            )

    def zaehlen() -> list[int]:
        ergebnis = []
        for rolle in ("vorsitz", "rat"):
            client = client_for(welt[rolle].user)
            client.get(_adresse(welt))
            with CaptureQueriesContext(connection) as abfragen:
                assert client.get(_adresse(welt)).status_code == 200
            ergebnis.append(len(abfragen))
        return ergebnis

    tops(2, 0)
    vorher = zaehlen()
    tops(6, 2)
    nachher = zaehlen()
    assert nachher == vorher, f"{vorher} → {nachher}"
