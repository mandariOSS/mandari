# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fraktionssitzung nach der Sitzung nach dem Muster (Issue #1071, Schritt 3): Protokoll, Abschluss, abgesagte Sitzung.

Geprüft wird:

- Schalter und Zustände: abgeschlossen und abgesagt zeigen die neue Seite; ``?ansicht=bisher`` führt zurück.
- Keine Funktion fällt weg: Jede Aktion, jeder Dialog und jeder Link der bisherigen Seite (samt Panel eines TOPs) ist
  für jede Rolle und jeden Zustand auf der neuen Seite (samt Panel) erreichbar.
- Rollen und Schritte: Vorsitz bestätigt die Anwesenheit, genehmigt direkt, erfasst Nachträge; Mitglieder lesen und
  sehen ihre Teilnahme; Notizen je TOP bearbeitet nur, wer protokollieren darf.
- Nichtöffentlicher Teil: Nicht-Vereidigte sehen weder Titel noch Notizen, Einträge oder Beschlüsse, auch nicht in der
  Antwort der Aktionen, im PDF-Link oder im internen PDF.
- Bisheriges Protokoll aus Einzeleinträgen: unverändert mit Art, Person und Uhrzeit.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.tenants.models import Organization
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionDecision,
    FactionMeeting,
    FactionProtocolEntry,
)
from apps.work.faction.tests.test_sitzung_vorbereiten import _funktionen

pytestmark = pytest.mark.django_db

NEU = "work/faction/neu/protokoll.html"
ALT = "work/faction/detail.html"
NOE_TITEL = "Grundstücksangelegenheit Flurstück 12"
NOE_NOTIZ = "Kaufpreis 480.000 Euro vertraulich"
NOE_BESCHLUSS = "Ankauf zustimmen vertraulich"


@pytest.fixture
def welt(org: Organization, make_member: Any) -> dict[str, Any]:
    Organization.objects.filter(pk=org.pk).update(work_new_design=True)
    org.refresh_from_db()
    vorsitz = make_member(org, [], email="vera@example.org", is_admin=True)
    vorsitz.is_sworn_in = True
    vorsitz.save(update_fields=["is_sworn_in"])
    rat = make_member(
        org,
        ["faction.view_public", "faction.view_non_public", "protocols.view_public", "protocols.view_full"],
        email="martin@example.org",
    )
    rat.is_sworn_in = True
    rat.save(update_fields=["is_sworn_in"])
    sachkundig = make_member(
        org,
        ["faction.view_public", "faction.view_non_public", "protocols.view_public", "protocols.view_full"],
        email="klara@example.org",
    )
    for m, (vor, nach) in (
        (vorsitz, ("Vera", "Beispiel")),
        (rat, ("Martin", "Muster")),
        (sachkundig, ("Klara", "Kopie")),
    ):
        m.user.first_name, m.user.last_name = vor, nach
        m.user.save(update_fields=["first_name", "last_name"])
    start = timezone.now() - timedelta(days=2)
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung Nr. 24",
        meeting_number=24,
        start=start,
        started_at=start + timedelta(minutes=4),
        end=start + timedelta(hours=1, minutes=51),
        status="completed",
        location="Fraktionsbüro",
        is_virtual=True,
        invitation_sent=True,
        invitation_sent_at=start - timedelta(days=3),
        created_by=vorsitz,
    )
    FactionAttendance.objects.create(meeting=meeting, membership=vorsitz, status="present")
    FactionAttendance.objects.create(meeting=meeting, membership=rat, status="present", participation_type="online")
    FactionAttendance.objects.create(meeting=meeting, membership=sachkundig, status="excused")
    haupt = FactionAgendaItem.objects.create(
        meeting=meeting, title="Trinkwasserbrunnen", number="1", visibility="public", order=1
    )
    cast(Any, haupt).set_notes_encrypted("<p>Die Fraktion regt einen vierten Standort an.</p>")
    haupt.save()
    FactionDecision.objects.create(
        agenda_item=haupt,
        votes_yes=5,
        votes_no=0,
        votes_abstain=1,
        result="accepted",
        decision_text="Zustimmung im Rat.",
    )
    noe = FactionAgendaItem.objects.create(meeting=meeting, title=NOE_TITEL, number="1", visibility="internal", order=2)
    cast(Any, noe).set_notes_encrypted(f"<p>{NOE_NOTIZ}</p>")
    noe.save()
    FactionDecision.objects.create(
        agenda_item=noe, votes_yes=4, votes_no=1, votes_abstain=0, result="accepted", decision_text=NOE_BESCHLUSS
    )
    return {"org": org, "vorsitz": vorsitz, "rat": rat, "sachkundig": sachkundig, "meeting": meeting, "haupt": haupt}


def _adresse(welt: dict[str, Any]) -> str:
    return reverse("work:faction_detail", kwargs={"org_slug": welt["org"].slug, "meeting_id": welt["meeting"].pk})


def _aktion(welt: dict[str, Any]) -> str:
    return reverse("work:faction_action", kwargs={"org_slug": welt["org"].slug, "meeting_id": welt["meeting"].pk})


def _seite(welt: dict[str, Any], client_for: Any, rolle: str, **anfrage: str) -> str:
    antwort = client_for(welt[rolle].user).get(_adresse(welt), anfrage)
    assert antwort.status_code == 200
    return str(antwort.content.decode())


def _panel(client: Any, welt: dict[str, Any]) -> str:
    url = reverse(
        "work:faction_item_panel",
        kwargs={"org_slug": welt["org"].slug, "meeting_id": welt["meeting"].pk, "item_id": welt["haupt"].pk},
    )
    antwort = client.get(url, HTTP_HX_REQUEST="true")
    assert antwort.status_code == 200
    return f"<main>{antwort.content.decode()}</main>"


def _setze(welt: dict[str, Any], zustand: str) -> None:
    meeting = welt["meeting"]
    jetzt = timezone.now()
    if zustand == "cancelled":
        meeting.status = "cancelled"
        meeting.cancellation_reason = "Haushaltsklausur"
    if zustand in ("bestaetigt", "genehmigt"):
        meeting.attendance_confirmed_at = jetzt
        meeting.attendance_confirmed_by = welt["vorsitz"]
    if zustand == "genehmigt":
        meeting.protocol_approved = True
        meeting.protocol_status = "approved"
        meeting.protocol_approved_at = jetzt
        meeting.protocol_approved_by = welt["vorsitz"]
    meeting.save()


# ---------------------------------------------------------------------------
# Zustände und Schalter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_zustaende_und_rueckweg(welt: dict[str, Any], client_for: Any, status: str) -> None:
    FactionMeeting.objects.filter(pk=welt["meeting"].pk).update(status=status)
    client = client_for(welt["vorsitz"].user)
    neu = client.get(_adresse(welt))
    assert NEU in [t.name for t in neu.templates]
    assert 'data-gestaltung="neu"' in neu.content.decode()
    assert ALT in [t.name for t in client.get(_adresse(welt), {"ansicht": "bisher"}).templates]
    Organization.objects.filter(pk=welt["org"].pk).update(work_new_design=False)
    assert ALT in [t.name for t in client.get(_adresse(welt)).templates]


# ---------------------------------------------------------------------------
# Keine Funktion fällt weg
# ---------------------------------------------------------------------------

#: Was die bisherige Seite anders anbot, mit Begründung
AUSNAHMEN: set[str] = set()


@pytest.mark.parametrize("rolle", ["vorsitz", "rat", "sachkundig"])
@pytest.mark.parametrize("zustand", ["offen", "bestaetigt", "genehmigt", "cancelled"])
def test_keine_funktion_der_bisherigen_seite_entfaellt(
    welt: dict[str, Any], client_for: Any, rolle: str, zustand: str
) -> None:
    # Ein bisheriger Eintrag am TOP: die bisherige Seite bietet dann Löschen an (vor der Genehmigung angelegt)
    FactionProtocolEntry.objects.create(
        meeting=welt["meeting"], agenda_item=welt["haupt"], entry_type="note", created_by=welt["vorsitz"], order=1
    )
    _setze(welt, zustand)
    client = client_for(welt[rolle].user)
    alt = client.get(_adresse(welt), {"ansicht": "bisher"})
    neu = client.get(_adresse(welt))
    assert ALT in [t.name for t in alt.templates] and NEU in [t.name for t in neu.templates]
    panel = _panel(client, welt)
    vorher = _funktionen(alt.content.decode()) | _funktionen(panel)
    nachher = _funktionen(neu.content.decode()) | _funktionen(panel)
    fehlt = sorted(vorher - nachher - AUSNAHMEN)
    assert not fehlt, f"{rolle}/{zustand}: {fehlt}"


def test_bestand_zeigt_was_der_vergleich_pruefen_soll(welt: dict[str, Any], client_for: Any) -> None:
    alt = _funktionen(_seite(welt, client_for, "vorsitz", ansicht="bisher"))
    erwartet = {"aktion:confirm_attendance", "aktion:add_entry", "dialog:open-item-panel", "dialog:open-decision"}
    assert erwartet <= alt, erwartet - alt


# ---------------------------------------------------------------------------
# Rollen und Schritte
# ---------------------------------------------------------------------------


def test_vorsitz_nach_dem_ende(welt: dict[str, Any], client_for: Any) -> None:
    html = _seite(welt, client_for, "vorsitz")
    assert "Beendet um" in html and "Anwesenheit bestätigen …" in html
    assert 'id="fs-bestaetigen"' in html and 'value="confirm_attendance"' in html
    assert "Vera Beispiel, Martin Muster (online)" in html and "Entschuldigt: Klara Kopie" in html
    assert 'data-testid="fs-pr-schritte"' in html and "Nach der Sitzung" in html
    assert 'id="fs-genehmigen"' in html and 'value="approve_protocol"' in html
    # Protokoll als Dokument: Angaben, Notiz, Beschluss, Editor je TOP
    assert "Anwesend vor Ort" in html and "Anwesend online" in html
    assert "vierten Standort" in html
    assert "Angenommen mit 5 Stimmen dafür, 0 dagegen, 1 Enthaltung. Zustimmung im Rat." in html
    assert 'x-data="sitzungsNotizen"' in html and f'data-konfig="fs-notiz-{welt["haupt"].pk}"' in html
    assert "Abstimmung ändern …" in html
    assert NOE_TITEL in html and NOE_NOTIZ in html
    assert "Protokoll, vollständig (PDF)" in html


def test_bestaetigen_und_direkt_genehmigen(welt: dict[str, Any], client_for: Any) -> None:
    client = client_for(welt["vorsitz"].user)
    antwort = client.post(_aktion(welt), {"action": "confirm_attendance"})
    assert antwort.status_code == 302 and antwort.url == _adresse(welt)
    html = client.get(_adresse(welt)).content.decode()
    assert "Anwesenheit bestätigt." in html and "Direkt genehmigen …" in html
    assert 'id="fs-bestaetigen"' not in html
    client.post(_aktion(welt), {"action": "approve_protocol"})
    welt["meeting"].refresh_from_db()
    assert welt["meeting"].protocol_approved
    html = client.get(_adresse(welt)).content.decode()
    assert "Abgeschlossen." in html and "Protokoll direkt genehmigt" in html
    assert "Nachtrag erfassen" in html and 'value="addendum"' in html
    assert 'x-data="sitzungsNotizen"' not in html


def test_nachtrag_erscheint_im_protokoll(welt: dict[str, Any], client_for: Any) -> None:
    _setze(welt, "genehmigt")
    client = client_for(welt["vorsitz"].user)
    client.post(
        _aktion(welt),
        {"action": "add_entry", "entry_type": "addendum", "agenda_item_id": welt["haupt"].pk, "content": "Korrektur"},
    )
    html = client.get(_adresse(welt)).content.decode()
    assert "Nachtrag von Vera Beispiel" in html and "Korrektur" in html


def test_mitglied_liest(welt: dict[str, Any], client_for: Any) -> None:
    _setze(welt, "bestaetigt")
    html = _seite(welt, client_for, "rat")
    assert 'data-testid="fs-pr-teilnahme"' in html and "Anwesend, online. Bestätigt am" in html
    assert "Teilnahmenachweis (PDF)" in html
    assert "Protokoll im Entwurf." in html
    for verwaltung in (
        'id="fs-bestaetigen"',
        'id="fs-genehmigen"',
        'x-data="sitzungsNotizen"',
        "Abstimmung erfassen …",
    ):
        assert verwaltung not in html, verwaltung
    assert NOE_TITEL in html  # vereidigt


def test_notizen_nach_dem_ende_speichern(welt: dict[str, Any], client_for: Any) -> None:
    url = reverse(
        "work:faction_session_action", kwargs={"org_slug": welt["org"].slug, "meeting_id": welt["meeting"].pk}
    )
    werte = {"action": "notizen", "item_id": str(welt["haupt"].pk), "html": "<p>Ergänzt</p>"}
    assert client_for(welt["vorsitz"].user).post(url, werte).status_code == 200
    assert client_for(welt["rat"].user).post(url, werte).status_code == 403


# ---------------------------------------------------------------------------
# Nichtöffentlicher Teil
# ---------------------------------------------------------------------------


def test_nicht_vereidigt_sieht_vom_noe_teil_nichts(welt: dict[str, Any], client_for: Any) -> None:
    html = _seite(welt, client_for, "sachkundig")
    for geheim in (NOE_TITEL, NOE_NOTIZ, NOE_BESCHLUSS):
        assert geheim not in html, geheim
    assert 'data-testid="fs-gesperrt"' in html and "Nichtöffentlicher Teil, 1 TOP" in html
    assert "variant=intern" not in html and "niederschrift/intern.pdf" not in html
    assert "Protokoll, öffentlicher Teil (PDF)" in html
    client = client_for(welt["sachkundig"].user)
    teil = client.post(
        _aktion(welt), {"action": "refresh_agenda"}, HTTP_HX_REQUEST="true", HTTP_X_WORK_GESTALTUNG="neu"
    )
    assert teil.status_code == 200 and 'data-testid="fs-prot-dok"' in teil.content.decode()
    for geheim in (NOE_TITEL, NOE_NOTIZ, NOE_BESCHLUSS):
        assert geheim not in teil.content.decode(), geheim
    pdf = reverse(
        "work:faction_protocol_pdf",
        kwargs={"org_slug": welt["org"].slug, "meeting_id": welt["meeting"].pk, "variant": "intern"},
    )
    assert client.get(pdf).status_code == 403


def test_noe_eintraege_ohne_top_nur_fuer_vereidigte(welt: dict[str, Any], client_for: Any) -> None:
    eintrag: Any = FactionProtocolEntry(meeting=welt["meeting"], entry_type="note", created_by=welt["vorsitz"], order=5)
    eintrag.set_content_encrypted("Allgemeiner interner Hinweis")
    eintrag.save()
    assert "Allgemeiner interner Hinweis" in _seite(welt, client_for, "rat")
    assert "Allgemeiner interner Hinweis" not in _seite(welt, client_for, "sachkundig")


# ---------------------------------------------------------------------------
# Bisheriges Protokoll und abgesagte Sitzung
# ---------------------------------------------------------------------------


def test_bisheriges_protokoll_aus_einzeleintraegen(welt: dict[str, Any], client_for: Any) -> None:
    FactionAgendaItem.objects.filter(meeting=welt["meeting"]).update(notes_encrypted=b"")
    eintrag: Any = FactionProtocolEntry(
        meeting=welt["meeting"],
        agenda_item=welt["haupt"],
        entry_type="speech",
        speaker=welt["rat"],
        created_by=welt["vorsitz"],
        order=1,
    )
    eintrag.set_content_encrypted("Ich schlage Enthaltung vor.")
    eintrag.save()
    _setze(welt, "genehmigt")
    html = _seite(welt, client_for, "vorsitz")
    assert "Wortbeitrag von Martin Muster" in html and "Ich schlage Enthaltung vor." in html
    assert 'data-testid="fs-pr-alt"' in html and "Bisheriges Protokoll aus Einzeleinträgen" in html


def test_abgesagte_sitzung(welt: dict[str, Any], client_for: Any) -> None:
    _setze(welt, "cancelled")
    html = _seite(welt, client_for, "vorsitz")
    assert "Abgesagt." in html and "Haushaltsklausur" in html and "Geplante Tagesordnung" in html
    assert "vierten Standort" not in html and 'id="fs-bestaetigen"' not in html


def test_abgeschlossene_sitzung_laesst_sich_nicht_absagen(welt: dict[str, Any], client_for: Any) -> None:
    """Kein „Sitzung absagen …“ nach dem Ende; die Aktion meldet einen Fehler statt Erfolg und ändert nichts."""
    client = client_for(welt["vorsitz"].user)
    assert 'value="cancel"' not in client.get(_adresse(welt)).content.decode()
    antwort = client.post(_aktion(welt), {"action": "cancel"}, follow=True)
    welt["meeting"].refresh_from_db()
    assert welt["meeting"].status == "completed"
    meldungen = [str(m) for m in antwort.context["messages"]]
    assert "Eine abgeschlossene Sitzung lässt sich nicht mehr absagen." in meldungen
    assert "Sitzung abgesagt." not in meldungen


def test_neuladen_wartet_auf_das_speichern_der_notizen(welt: dict[str, Any], client_for: Any) -> None:
    """Das Dokument lädt nach Panel-Aktionen erst neu, wenn alle Notizen gespeichert sind (faction-session.ts)."""
    html = _seite(welt, client_for, "vorsitz")
    anfang = html.index('id="agenda-container"')
    assert "data-sichern-vorher" in html[anfang : html.index(">", anfang)]
