# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsansicht der laufenden Fraktionssitzung (Issue #874).

Schalter je Organisation (Standard aus), bisherige Ansicht bleibt erreichbar, Nichtöffentliches nur für Vereidigte,
Notizen je TOP mit Bereinigung und Konflikterkennung, Schriftführung, Beschluss ohne Verlust vorhandener Angaben,
Aufgaben für Personen und Personengruppen, Unterlagen anhängen, Anwesenheit vor Ort/online, „Im Beratungsverlauf“
aus der Sitzungsvorbereitung und bisherige Protokolleinträge (auch Wortbeiträge) unverändert lesbar.
"""

from __future__ import annotations

import datetime
from datetime import timedelta
from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.work.faction import sitzung
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAgendaItemAttachment,
    FactionAttendance,
    FactionDecision,
    FactionMeeting,
    FactionProtocolEntry,
)
from apps.work.tasks.models import Task

pytestmark = pytest.mark.django_db

VORSITZ = [
    "faction.view_public",
    "faction.view_non_public",
    "faction.manage",
    "faction.start",
    "protocols.create",
    "protocols.edit",
    "voting.participate",
    "motions.view",
    "meetings.prepare",
]
MITGLIED = ["faction.view_public", "voting.participate"]


def schalter_an(org: Any) -> None:
    """Neues Erscheinungsbild für die Organisation einschalten (Schalter der Spur „Rahmen“, #852)."""
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    m = make_member(org, VORSITZ, email="vorsitz@example.org")
    m.is_sworn_in = True
    m.save(update_fields=["is_sworn_in"])
    return m


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, MITGLIED, email="mitglied@example.org")


@pytest.fixture
def sitzung_laeuft(org: Any, vorsitz: Any, mitglied: Any) -> FactionMeeting:
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung Nr. 24",
        start=timezone.now() - timedelta(minutes=10),
        status="ongoing",
        location="Fraktionsbüro",
        created_by=vorsitz,
    )
    for m in (vorsitz, mitglied):
        FactionAttendance.objects.create(meeting=meeting, membership=m, status="invited")
    return meeting


@pytest.fixture
def tops(sitzung_laeuft: FactionMeeting) -> dict[str, FactionAgendaItem]:
    oe = FactionAgendaItem.objects.create(
        meeting=sitzung_laeuft, number="2", title="Feuerwehrbedarfsplan", visibility="public", order=1
    )
    oe2 = FactionAgendaItem.objects.create(
        meeting=sitzung_laeuft, number="3", title="Sonstiges", visibility="public", order=2
    )
    noe = FactionAgendaItem.objects.create(
        meeting=sitzung_laeuft,
        number="NÖ 1",
        title="Grundstücksangelegenheit Flurstück 12/3",
        visibility="internal",
        order=3,
    )
    return {"oe": oe, "oe2": oe2, "noe": noe}


def seite(org: Any, meeting: FactionMeeting) -> str:
    return reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})


def aktion_url(org: Any, meeting: FactionMeeting) -> str:
    return reverse("work:faction_session_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})


def top_url(org: Any, meeting: FactionMeeting, item: FactionAgendaItem) -> str:
    return reverse(
        "work:faction_session_item", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk, "item_id": item.pk}
    )


# ---- Schalter und bisherige Ansicht ------------------------------------------------------------------


def test_schalter_aus_zeigt_bisherige_seite(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    html = client_for(vorsitz.user).get(seite(org, sitzung_laeuft)).content.decode()
    assert 'x-data="factionDetail"' in html
    assert "fs-sitzung" not in html
    # Ohne Schalter gibt es auch die Teile der neuen Ansicht nicht
    antwort = client_for(vorsitz.user).get(top_url(org, sitzung_laeuft, tops["oe"]), HTTP_HX_REQUEST="true")
    assert antwort.status_code == 404


def test_schalter_an_zeigt_sitzungsansicht_und_bisherige_bleibt(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    schalter_an(org)
    client = client_for(vorsitz.user)
    antwort = client.get(seite(org, sitzung_laeuft), {"top": str(tops["oe"].pk)})
    html = antwort.content.decode()
    assert antwort.status_code == 200
    assert 'x-data="fraktionssitzung"' in html
    assert "TOP 2</span> Feuerwehrbedarfsplan" in html
    assert "Ablauf der Sitzung" in html and "beschlussfähig" in html
    assert "Fraktionsbüro" in html
    # Kein Inline-JavaScript in den neuen Templates
    for template in antwort.templates:
        if template.name and template.name.startswith("work/faction/sitzung/"):
            assert "<script>" not in template.source
            assert " onclick=" not in template.source
    # Bisherige Ansicht mit allen Funktionen bleibt erreichbar
    alt = client.get(seite(org, sitzung_laeuft), {"ansicht": "bisher"}).content.decode()
    assert 'x-data="factionDetail"' in alt


def test_geplante_sitzung_bleibt_in_bisheriger_ansicht(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, client_for: Any
) -> None:
    schalter_an(org)
    sitzung_laeuft.status = "planned"
    sitzung_laeuft.save(update_fields=["status"])
    html = client_for(vorsitz.user).get(seite(org, sitzung_laeuft)).content.decode()
    assert 'x-data="factionDetail"' in html


def test_top_wechsel_liefert_top_und_leiste(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    schalter_an(org)
    antwort = client_for(vorsitz.user).get(top_url(org, sitzung_laeuft, tops["oe2"]), HTTP_HX_REQUEST="true")
    html = antwort.content.decode()
    assert antwort.status_code == 200
    assert 'id="fs-fokus"' in html and "Sonstiges" in html
    assert 'id="fs-unten"' in html and 'hx-swap-oob="true"' in html
    assert "Voriger TOP" in html and "Feuerwehrbedarfsplan" in html


# ---- Nichtöffentliches ----------------------------------------------------------------------------------


def test_nicht_vereidigte_bekommen_nichts_vom_nichtoeffentlichen_teil(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    schalter_an(org)
    client = client_for(mitglied.user)
    html = client.get(seite(org, sitzung_laeuft)).content.decode()
    assert "Grundstücksangelegenheit" not in html
    assert "Ein TOP, nur für vereidigte Mitglieder" in html
    assert client.get(top_url(org, sitzung_laeuft, tops["noe"]), HTTP_HX_REQUEST="true").status_code == 403
    dok = reverse(
        "work:faction_session_document",
        kwargs={
            "org_slug": org.slug,
            "meeting_id": sitzung_laeuft.pk,
            "item_id": tops["noe"].pk,
            "schluessel": "links",
        },
    )
    assert client.get(dok).status_code == 403
    sitzung_laeuft.minute_taker = mitglied
    sitzung_laeuft.save(update_fields=["minute_taker"])
    antwort = client.post(
        aktion_url(org, sitzung_laeuft), {"action": "notizen", "item_id": tops["noe"].pk, "html": "x"}
    )
    assert antwort.status_code == 403
    # Vereidigte sehen den Teil
    assert "Grundstücksangelegenheit" in client_for(vorsitz.user).get(seite(org, sitzung_laeuft)).content.decode()


# ---- Notizen -----------------------------------------------------------------------------------------------


def test_notizen_werden_bereinigt_gespeichert_und_konflikte_erkannt(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    schalter_an(org)
    client = client_for(vorsitz.user)
    item = tops["oe"]
    erste = client.post(
        aktion_url(org, sitzung_laeuft),
        {
            "action": "notizen",
            "item_id": item.pk,
            "html": "<p><strong>Hilfsfrist</strong> im Norden<script>alert(1)</script></p><ul><li>Rückfrage</li></ul>",
            "stand": sitzung.fingerabdruck(""),
        },
    )
    assert erste.status_code == 200
    stand = erste.json()["stand"]
    item.refresh_from_db()
    gespeichert = sitzung.notizen(item)
    assert "<strong>Hilfsfrist</strong>" in gespeichert and "<li>Rückfrage</li>" in gespeichert
    assert "script" not in gespeichert
    assert item.notes_updated_at is not None
    assert stand == sitzung.fingerabdruck(gespeichert)

    # Jemand speichert mit veraltetem Stand: nichts wird überschrieben, der aktuelle Stand kommt zurück
    konflikt = client.post(
        aktion_url(org, sitzung_laeuft),
        {"action": "notizen", "item_id": item.pk, "html": "<p>Andere Fassung</p>", "stand": sitzung.fingerabdruck("")},
    )
    assert konflikt.status_code == 409
    daten = konflikt.json()
    assert daten["konflikt"] is True and "Hilfsfrist" in daten["html"]
    item.refresh_from_db()
    assert sitzung.notizen(item) == gespeichert

    # Mit aktuellem Stand gelingt das Speichern
    weiter = client.post(
        aktion_url(org, sitzung_laeuft),
        {"action": "notizen", "item_id": item.pk, "html": gespeichert + "<p>Ergänzung</p>", "stand": stand},
    )
    assert weiter.status_code == 200
    item.refresh_from_db()
    assert "Ergänzung" in sitzung.notizen(item)
    # Notizen sind verschlüsselt gespeichert
    assert b"Hilfsfrist" not in bytes(item.notes_encrypted)


def test_schriftfuehrung_darf_protokollieren_ein_mitglied_ohne_rolle_nicht(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    schalter_an(org)
    client = client_for(mitglied.user)
    daten = {"action": "notizen", "item_id": tops["oe"].pk, "html": "<p>Notiz</p>", "stand": sitzung.fingerabdruck("")}
    assert client.post(aktion_url(org, sitzung_laeuft), daten).status_code == 403
    # Der Vorsitz legt die Schriftführung fest
    rollen = client_for(vorsitz.user).post(
        aktion_url(org, sitzung_laeuft), {"action": "rollen", "schriftfuehrung": mitglied.pk, "leitung": vorsitz.pk}
    )
    assert rollen.status_code == 200
    assert "Schriftführung: " + mitglied.user.get_display_name() in rollen.content.decode()
    assert client.post(aktion_url(org, sitzung_laeuft), daten).status_code == 200
    # Die Schriftführung darf keine Rollen vergeben
    assert client.post(aktion_url(org, sitzung_laeuft), {"action": "rollen", "leitung": mitglied.pk}).status_code == 403


def test_rollen_namen_bleiben_nach_entfernen_der_mitgliedschaft(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any
) -> None:
    from apps.work.faction.services import preserve_member_names

    sitzung_laeuft.minute_taker = mitglied
    sitzung_laeuft.chaired_by = vorsitz
    sitzung_laeuft.save()
    name = mitglied.user.get_display_name()
    preserve_member_names(mitglied)
    mitglied.delete()
    sitzung_laeuft.refresh_from_db()
    assert sitzung_laeuft.minute_taker_id is None
    assert sitzung_laeuft.minute_taker_name == name
    assert sitzung_laeuft.chaired_by_name == vorsitz.user.get_display_name()


# ---- Beschluss -----------------------------------------------------------------------------------------------


def test_beschluss_erfassen_behaelt_vorhandene_anmerkungen(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    schalter_an(org)
    item = tops["oe"]
    FactionDecision.objects.create(agenda_item=item, result="accepted", votes_yes=1, notes="Aus der bisherigen Ansicht")
    antwort = client_for(vorsitz.user).post(
        aktion_url(org, sitzung_laeuft),
        {
            "action": "beschluss",
            "item_id": item.pk,
            "votes_yes": "5",
            "votes_no": "1",
            "votes_abstain": "0",
            "result": "modified",
            "decision_text": "Mit Gerätehaus Nord",
        },
    )
    assert antwort.status_code == 200
    html = antwort.content.decode()
    assert "Geändert angenommen" in html and 'id="fs-tl-' in html
    decision = FactionDecision.objects.get(agenda_item=item)
    assert (decision.votes_yes, decision.votes_no, decision.result) == (5, 1, "modified")
    assert decision.notes == "Aus der bisherigen Ansicht"
    item.refresh_from_db()
    assert item.has_decision and item.votes_for == 5


def test_beschluss_nur_mit_protokollrecht(
    org: Any, mitglied: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    schalter_an(org)
    antwort = client_for(mitglied.user).post(
        aktion_url(org, sitzung_laeuft), {"action": "beschluss", "item_id": tops["oe"].pk, "votes_yes": "1"}
    )
    assert antwort.status_code == 403
    assert not FactionDecision.objects.filter(agenda_item=tops["oe"]).exists()


# ---- Aufgaben -------------------------------------------------------------------------------------------------


def test_aufgaben_fuer_personen_und_gruppen(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any, tops: Any, make_member: Any, client_for: Any
) -> None:
    schalter_an(org)
    presse = make_member(org, MITGLIED, email="presse@example.org")
    rolle = presse.roles.first()
    mitglied.roles.add(rolle)
    _personen, gruppen = sitzung.personen_und_gruppen(org, nur_vereidigte=False)
    gruppe = next(g for g in gruppen if g.schluessel == f"rolle:{rolle.pk}")
    assert {m.pk for m in gruppe.mitglieder} == {presse.pk, mitglied.pk}

    antwort = client_for(vorsitz.user).post(
        aktion_url(org, sitzung_laeuft),
        {
            "action": "aufgaben",
            "item_id": tops["oe"].pk,
            "titel": "Pressemitteilung vorbereiten",
            "faellig": "2026-10-20",
            "gruppen": [gruppe.schluessel],
            "personen": [str(mitglied.pk)],
        },
    )
    assert antwort.status_code == 200
    aufgaben = Task.objects.filter(related_faction_agenda_item=tops["oe"])
    # Eine Aufgabe je Person, doppelte Auswahl zählt einmal
    assert sorted(str(a.assigned_to_id) for a in aufgaben) == sorted([str(presse.pk), str(mitglied.pk)])
    assert all(a.related_faction_meeting_id == sitzung_laeuft.pk for a in aufgaben)
    assert all(a.due_date == datetime.date(2026, 10, 20) for a in aufgaben)
    html = antwort.content.decode()
    # Zusammen angelegte Aufgaben stehen in einer Zeile mit allen Zuständigen (ohne Haken: zwei Aufgaben)
    assert html.count('<span class="sr-only">: Pressemitteilung vorbereiten</span>') == 1
    assert "Erledigt: Pressemitteilung vorbereiten" not in html and "0 von 2 erledigt" in html
    assert presse.user.get_display_name() in html and mitglied.user.get_display_name() in html


def test_aufgabe_aus_dem_top_abhaken_wie_im_aufgabenboard(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any, tops: Any, make_member: Any, client_for: Any
) -> None:
    schalter_an(org)
    client = client_for(vorsitz.user)
    client.post(
        aktion_url(org, sitzung_laeuft),
        {"action": "aufgaben", "item_id": tops["oe"].pk, "titel": "Rückfrage stellen", "personen": [str(mitglied.pk)]},
    )
    client.post(
        aktion_url(org, sitzung_laeuft),
        {"action": "aufgaben", "item_id": tops["oe"].pk, "titel": "Termin abstimmen", "gruppen": ["alle"]},
    )
    einzeln = Task.objects.get(related_faction_agenda_item=tops["oe"], title="Rückfrage stellen")
    zeilen = {z.titel: z for z in sitzung.aufgaben_des_tops(tops["oe"], vorsitz)}
    # Eine einzelne Aufgabe hat einen Haken, zusammen angelegte zeigen nur, wie viele erledigt sind
    assert zeilen["Rückfrage stellen"].darf_abhaken
    assert not zeilen["Termin abstimmen"].darf_abhaken and zeilen["Termin abstimmen"].gesamt == 2

    antwort = client.post(
        aktion_url(org, sitzung_laeuft),
        {"action": "aufgabe_erledigt", "item_id": tops["oe"].pk, "task_id": str(einzeln.pk)},
    )
    assert antwort.status_code == 200
    einzeln.refresh_from_db()
    assert einzeln.is_completed and einzeln.status == "done"
    assert 'aria-label="Erledigt: Rückfrage stellen"' in antwort.content.decode()

    # Wieder öffnen wie auf dem Board
    client.post(
        aktion_url(org, sitzung_laeuft),
        {"action": "aufgabe_erledigt", "item_id": tops["oe"].pk, "task_id": str(einzeln.pk)},
    )
    einzeln.refresh_from_db()
    assert not einzeln.is_completed and einzeln.status == "todo"

    # Wer die Aufgabe weder angelegt hat noch zuständig ist (ohne tasks.manage), darf sie nicht abhaken
    fremd = make_member(org, MITGLIED, email="fremd@example.org")
    verboten = client_for(fremd.user).post(
        aktion_url(org, sitzung_laeuft),
        {"action": "aufgabe_erledigt", "item_id": tops["oe"].pk, "task_id": str(einzeln.pk)},
    )
    assert verboten.status_code == 403
    # Eine Aufgabe eines anderen TOPs lässt sich über diesen TOP nicht umschalten
    anderer_top = client.post(
        aktion_url(org, sitzung_laeuft),
        {"action": "aufgabe_erledigt", "item_id": tops["oe2"].pk, "task_id": str(einzeln.pk)},
    )
    assert anderer_top.status_code == 403
    einzeln.refresh_from_db()
    assert not einzeln.is_completed


def test_aufgaben_bei_nichtoeffentlichem_top_nur_fuer_vereidigte(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    schalter_an(org)
    client_for(vorsitz.user).post(
        aktion_url(org, sitzung_laeuft),
        {"action": "aufgaben", "item_id": tops["noe"].pk, "titel": "Kaufvertrag prüfen", "gruppen": ["alle"]},
    )
    zugewiesen = set(
        Task.objects.filter(related_faction_agenda_item=tops["noe"]).values_list("assigned_to_id", flat=True)
    )
    assert zugewiesen == {vorsitz.pk}


# ---- Unterlagen ------------------------------------------------------------------------------------------------


def test_unterlage_in_der_sitzung_anhaengen(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    schalter_an(org)
    datei = SimpleUploadedFile(
        "Tischvorlage.pdf", b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n", "application/pdf"
    )
    antwort = client_for(vorsitz.user).post(
        aktion_url(org, sitzung_laeuft), {"action": "datei", "item_id": tops["oe"].pk, "ziel": "fokus", "file": datei}
    )
    assert antwort.status_code == 200, antwort.content
    html = antwort.content.decode()
    anlage = FactionAgendaItemAttachment.objects.get(agenda_item=tops["oe"])
    assert anlage.filename == "Tischvorlage.pdf"
    assert 'id="fs-unterlagen"' in html and "Tischvorlage.pdf" in html
    # Vorschau eingebettet nur mit ?vorschau, sonst Download wie bisher
    download = reverse(
        "work:faction_attachment_download",
        kwargs={
            "org_slug": org.slug,
            "meeting_id": sitzung_laeuft.pk,
            "item_id": tops["oe"].pk,
            "attachment_id": anlage.pk,
        },
    )
    client = client_for(vorsitz.user)
    assert "attachment" in client.get(download)["Content-Disposition"]
    assert "inline" in client.get(download + "?vorschau")["Content-Disposition"]


# ---- Anwesenheit --------------------------------------------------------------------------------------------------


def test_anwesenheit_vor_ort_und_online(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any, client_for: Any
) -> None:
    schalter_an(org)
    teilnahme = sitzung_laeuft.attendances.get(membership=mitglied)
    client = client_for(vorsitz.user)
    url = aktion_url(org, sitzung_laeuft)
    assert (
        client.post(url, {"action": "anwesenheit", "attendance_id": teilnahme.pk, "status": "present"}).status_code
        == 200
    )
    antwort = client.post(url, {"action": "anwesenheit", "attendance_id": teilnahme.pk, "participation_type": "online"})
    teilnahme.refresh_from_db()
    assert (teilnahme.status, teilnahme.participation_type) == ("present", "online")
    assert teilnahme.checked_in_at is not None
    assert "1 von 2" in antwort.content.decode()
    client.post(url, {"action": "anwesenheit", "attendance_id": teilnahme.pk, "status": "excused"})
    teilnahme.refresh_from_db()
    assert teilnahme.status == "excused"
    # Gäste
    client.post(url, {"action": "gast", "guest_name": "Gast aus der Musterstadt"})
    assert sitzung_laeuft.attendances.filter(
        is_guest=True, guest_name="Gast aus der Musterstadt", status="present"
    ).exists()
    # Ungültige Kennung: Fehlermeldung statt Serverfehler
    assert (
        client.post(url, {"action": "anwesenheit", "attendance_id": "kaputt", "status": "present"}).status_code == 400
    )


def test_starten_merkt_den_beginn(org: Any, vorsitz: Any, sitzung_laeuft: Any, client_for: Any) -> None:
    sitzung_laeuft.status = "planned"
    sitzung_laeuft.start = timezone.now()
    sitzung_laeuft.save()
    client_for(vorsitz.user).post(
        reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": sitzung_laeuft.pk}),
        {"action": "start"},
    )
    sitzung_laeuft.refresh_from_db()
    assert sitzung_laeuft.status == "ongoing" and sitzung_laeuft.started_at is not None


# ---- Bestand ---------------------------------------------------------------------------------------------------------


def test_bisherige_eintraege_bleiben_vollstaendig_lesbar(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    schalter_an(org)
    item = tops["oe"]
    for art, text in (
        ("speech", "Wir tragen den Plan mit."),
        ("note", "Rückfrage zur Hilfsfrist"),
        ("action", "Pressemitteilung"),
    ):
        eintrag: Any = FactionProtocolEntry(
            meeting=sitzung_laeuft,
            agenda_item=item,
            entry_type=art,
            created_by=vorsitz,
            speaker=mitglied if art == "speech" else None,
        )
        eintrag.save()
        eintrag.set_content_encrypted(text)
        eintrag.save()
    vorher = FactionProtocolEntry.objects.count()
    html = client_for(vorsitz.user).get(seite(org, sitzung_laeuft), {"top": str(item.pk)}).content.decode()
    assert "Wortbeitrag von " + mitglied.user.get_display_name() in html
    assert "Wir tragen den Plan mit." in html and "Rückfrage zur Hilfsfrist" in html and "Pressemitteilung" in html
    # Neue Wortbeiträge legt die neue Ansicht nicht an; der Bestand bleibt unverändert
    assert FactionProtocolEntry.objects.count() == vorher


def test_im_beratungsverlauf_aus_der_vorbereitung(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    from apps.work.meetings.models import AgendaItemPosition
    from insight_core.models import (
        OParlAgendaItem,
        OParlBody,
        OParlConsultation,
        OParlMeeting,
        OParlOrganization,
        OParlPaper,
        OParlSource,
    )

    schalter_an(org)
    ris = "https://ris.example.org"
    source = OParlSource.objects.create(name="Test-RIS", url=f"{ris}/system")
    body = OParlBody.objects.create(external_id=f"{ris}/body/1", source=source, name="Musterstadt")
    ausschuss = OParlOrganization.objects.create(external_id=f"{ris}/org/1", body=body, name="Hauptausschuss")
    vorberatung = OParlMeeting.objects.create(
        external_id=f"{ris}/meeting/1",
        body=body,
        name="Hauptausschuss",
        start=datetime.datetime(2026, 10, 12, 15, 0, tzinfo=datetime.UTC),
    )
    vorberatung.organizations.add(ausschuss)
    rat = OParlMeeting.objects.create(external_id=f"{ris}/meeting/2", body=body, name="Rat")
    top_ausschuss = OParlAgendaItem.objects.create(
        external_id=f"{ris}/agenda/1", meeting=vorberatung, number="4", order=4
    )
    top_rat = OParlAgendaItem.objects.create(external_id=f"{ris}/agenda/2", meeting=rat, number="6", order=6)
    vorlage = OParlPaper.objects.create(external_id=f"{ris}/paper/1", body=body, name="Feuerwehrbedarfsplan")
    for nr, top in enumerate((top_ausschuss, top_rat), start=1):
        OParlConsultation.objects.create(
            external_id=f"{ris}/consultation/{nr}", body=body, paper=vorlage, agenda_item_external_id=top.external_id
        )
    position = AgendaItemPosition(
        organization=org, agenda_item=top_ausschuss, position="amended", outcome="accepted", is_final=True
    )
    cast(Any, position).set_reasoning_encrypted("Gerätehaus Nord vorziehen")
    position.save()
    item = tops["oe"]
    item.related_agenda_item = top_rat
    item.save(update_fields=["related_agenda_item"])

    html = client_for(vorsitz.user).get(seite(org, sitzung_laeuft), {"top": str(item.pk)}).content.decode()
    assert "Im Beratungsverlauf" in html
    assert "Hauptausschuss, 12.10.2026" in html
    assert "Mit Änderungsantrag" in html and "Ergebnis: angenommen" in html and "endgültig" in html
    assert "Gerätehaus Nord vorziehen" in html

    # Positionen aus der Vorbereitung nur mit deren Recht (meetings.prepare), wie in der Vorbereitung selbst
    ohne = client_for(mitglied.user).get(seite(org, sitzung_laeuft), {"top": str(item.pk)}).content.decode()
    assert "Im Beratungsverlauf" not in ohne and "Gerätehaus Nord vorziehen" not in ohne


def test_herkunft_mit_datum_endet_mit_einem_punkt(tops: Any) -> None:
    """„Im Rat TOP 6 am 19.10.“ – das Datum trägt den Punkt, kein zweiter am Satzende."""
    from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlOrganization, OParlSource

    ris = "https://ris.example.org"
    source = OParlSource.objects.create(name="Test-RIS", url=f"{ris}/system")
    body = OParlBody.objects.create(external_id=f"{ris}/body/1", source=source, name="Musterstadt")
    rat = OParlOrganization.objects.create(external_id=f"{ris}/org/1", body=body, name="Rat")
    sitzung_rat = OParlMeeting.objects.create(
        external_id=f"{ris}/meeting/1",
        body=body,
        name="Rat",
        start=datetime.datetime(2026, 10, 19, 15, 0, tzinfo=datetime.UTC),
    )
    sitzung_rat.organizations.add(rat)
    item = tops["oe"]
    item.related_agenda_item = OParlAgendaItem.objects.create(
        external_id=f"{ris}/agenda/1", meeting=sitzung_rat, number="6", order=6
    )
    item.save(update_fields=["related_agenda_item"])
    assert sitzung.herkunft(item) == "Im Rat TOP 6 am 19.10."


def test_sitzungsansicht_ohne_berechtigung_nicht_erreichbar(
    org: Any, sitzung_laeuft: Any, tops: Any, make_member: Any, client_for: Any
) -> None:
    schalter_an(org)
    fremd = make_member(org, [], email="ohne-rechte@example.org")
    antwort = client_for(fremd.user).get(top_url(org, sitzung_laeuft, tops["oe"]), HTTP_HX_REQUEST="true")
    assert antwort.status_code in (302, 403)


def test_unterlagen_reiter_mit_vorlage_dokument_und_links(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    from apps.work.motions.models import Motion
    from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlSource

    schalter_an(org)
    ris = "https://ris.example.org"
    source = OParlSource.objects.create(name="Test-RIS", url=f"{ris}/system")
    body = OParlBody.objects.create(external_id=f"{ris}/body/1", source=source, name="Musterstadt")
    vorlage = OParlPaper.objects.create(
        external_id=f"{ris}/paper/1",
        body=body,
        name="Feuerwehrbedarfsplan",
        reference="V/2026/012",
        raw_json={"mainFile": {"id": f"{ris}/file/2"}},
    )
    OParlFile.objects.create(
        external_id=f"{ris}/file/1", body=body, paper=vorlage, name="Anlage Langfassung", text_content="Langfassung"
    )
    OParlFile.objects.create(
        external_id=f"{ris}/file/2",
        body=body,
        paper=vorlage,
        name="Vorlage",
        text_content="Beschlussvorschlag\nDer Rat beschließt.",
    )
    dokument = Motion.objects.create(
        organization=org, author=vorsitz, title="Stellungnahme Löschzug", visibility="organization"
    )
    cast(Any, dokument).set_content_encrypted("<p>Östliche Fläche<script>x</script></p>")
    dokument.save()
    item = tops["oe"]
    item.related_papers.add(vorlage)
    item.related_motions.add(dokument)
    item.reference_links = [
        {"label": "Gut", "url": "https://www.example.org"},
        {"label": "Böse", "url": "javascript:alert(1)"},
    ]
    item.save(update_fields=["reference_links"])

    reiter = sitzung.unterlagen(item, vorsitz)
    assert [u.titel for u in reiter] == ["V/2026/012", "Anlage Langfassung", "Stellungnahme Löschzug", "Links"]
    assert [link["label"] for link in reiter[-1].objekt] == ["Gut"]

    client = client_for(vorsitz.user)
    html = client.get(seite(org, sitzung_laeuft), {"top": str(item.pk)}).content.decode()
    # Hauptdatei der Vorlage ist der erste Reiter und wird gleich gezeigt (Text der Datei)
    assert "Der Rat beschließt." in html and "Original (PDF) öffnen" in html

    def reiter_url(schluessel: str) -> str:
        return reverse(
            "work:faction_session_document",
            kwargs={
                "org_slug": org.slug,
                "meeting_id": sitzung_laeuft.pk,
                "item_id": item.pk,
                "schluessel": schluessel,
            },
        )

    dok = client.get(reiter_url(f"d-{dokument.pk}")).content.decode()
    assert "Östliche Fläche" in dok and "<script>" not in dok
    links = client.get(reiter_url("links")).content.decode()
    assert 'href="https://www.example.org"' in links and "javascript:" not in links
    assert client.get(reiter_url("d-00000000-0000-0000-0000-000000000000")).status_code == 404


def test_dateien_ohne_vorschau_als_dokumentzeile_von_insight(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    """Anlagen und RIS-Dateien, die sich nicht einbetten lassen, zeigt die gemeinsame Dokumentzeile von Insight."""
    from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlSource

    schalter_an(org)
    item = tops["oe"]
    anlage = FactionAgendaItemAttachment.objects.create(
        agenda_item=item,
        file=SimpleUploadedFile("tischvorlage.docx", b"PK\x03\x04", content_type="application/msword"),
        filename="tischvorlage.docx",
        mime_type="application/msword",
        file_size=2048,
        uploaded_by=vorsitz,
    )
    ris = "https://ris.example.org"
    source = OParlSource.objects.create(name="Test-RIS", url=f"{ris}/system")
    body = OParlBody.objects.create(external_id=f"{ris}/body/1", source=source, name="Musterstadt")
    vorlage = OParlPaper.objects.create(external_id=f"{ris}/paper/1", body=body, name="Tabelle", reference="V/7")
    tabelle = OParlFile.objects.create(
        external_id=f"{ris}/file/1",
        body=body,
        paper=vorlage,
        name="Kostenaufstellung",
        mime_type="application/vnd.ms-excel",
        download_url=f"{ris}/file/1.xls",
    )
    item.related_papers.add(vorlage)

    client = client_for(vorsitz.user)

    def reiter(schluessel: str) -> str:
        url = reverse(
            "work:faction_session_document",
            kwargs={
                "org_slug": org.slug,
                "meeting_id": sitzung_laeuft.pk,
                "item_id": item.pk,
                "schluessel": schluessel,
            },
        )
        return str(client.get(url).content.decode())

    download = reverse(
        "work:faction_attachment_download",
        kwargs={"org_slug": org.slug, "meeting_id": sitzung_laeuft.pk, "item_id": item.pk, "attachment_id": anlage.pk},
    )
    html = reiter(f"a-{anlage.pk}")
    assert "tischvorlage.docx" in html and "2.0 KB" in html
    assert f'href="{download}"' in html and ">Herunterladen<" in html
    # Ohne Vorschau-Adresse kein „Ansehen“ und kein Verweis auf den Dateiabruf von Insight
    assert ">Ansehen<" not in html and "/insight/dokumente/" not in html

    # RIS-Datei ohne Text und ohne PDF: Dokumentzeile mit dem Dateiabruf von Insight statt einer leeren Vorschau
    html = reiter(f"r-{tabelle.pk}")
    assert "<iframe" not in html and "Kostenaufstellung" in html
    assert f'data-doc-url="/insight/dokumente/{tabelle.pk}/preview/"' in html
    assert f'href="/insight/dokumente/{tabelle.pk}/preview/?download=1"' in html


def test_dokumentzeile_mit_eigenen_adressen() -> None:
    """Gemeinsame Dokumentzeile: eigene Adressen ersetzen den Dateiabruf von Insight, „dicht“ für Arbeitsflächen."""
    import uuid
    from types import SimpleNamespace

    from django.template import engines
    from django_cotton.compiler_regex import CottonCompiler

    datei = SimpleNamespace(
        id=uuid.uuid4(), filename="plan.pdf", mime_type="application/pdf", file_size=10, size_human="10 B"
    )
    quelle = (
        '<ul><c-insight.document-row :file="datei" titel="plan.pdf" ansehen="/a/" herunterladen="/h/" dicht /></ul>'
    )
    html = engines["django"].from_string(CottonCompiler().process(quelle)).render({"datei": datei})
    assert 'data-doc-url="/a/"' in html and 'href="/h/"' in html
    assert ">plan.pdf<" in html and "10 B" in html
    assert "min-h-8" in html and "min-h-11" not in html and "/insight/" not in html


# ---- Notizen außerhalb der Sitzungsansicht -------------------------------------------------------------------------


def test_genehmigungs_top_zeigt_das_vorige_protokoll(
    org: Any, sitzung_laeuft: Any, make_member: Any, mitglied: Any, client_for: Any, monkeypatch: Any
) -> None:
    """TOP „Tagesordnung und Protokoll“: Protokoll der vorigen Sitzung als erste Unterlage, Fassung nach Recht."""
    from apps.work.faction import services as faction_services

    schalter_an(org)
    leser = make_member(org, [*VORSITZ, "protocols.view_public", "protocols.view_full"], email="leser@example.org")
    leser.is_sworn_in = True
    leser.save(update_fields=["is_sworn_in"])
    vorige = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung Nr. 23",
        start=datetime.datetime(2026, 10, 5, 16, 0, tzinfo=datetime.UTC),
        status="completed",
    )
    item = FactionAgendaItem.objects.create(
        meeting=sitzung_laeuft,
        number="1",
        title="Tagesordnung und Protokoll beschließen",
        visibility="public",
        order=0,
        is_approval_item=True,
        approves_meeting=vorige,
    )
    assert sitzung.herkunft(item).endswith(
        "Das Protokoll von „Fraktionssitzung Nr. 23“ vom 05.10.2026 liegt zur Genehmigung vor."
    )

    reiter = sitzung.unterlagen(item, leser)
    assert (reiter[0].art, reiter[0].titel, reiter[0].fassung) == ("protokoll", "Protokoll vom 05.10.2026", "intern")
    # Ohne Recht auf die vollständige Niederschrift die öffentliche Fassung, ohne Recht auf Protokolle kein Reiter
    nur_oeffentlich = make_member(org, ["faction.view_public", "protocols.view_public"], email="oe@example.org")
    assert sitzung.unterlagen(item, nur_oeffentlich)[0].fassung == "oeffentlich"
    assert sitzung.unterlagen(item, mitglied) == []

    html = client_for(leser.user).get(seite(org, sitzung_laeuft), {"top": str(item.pk)}).content.decode()
    pdf_url = reverse(
        "work:faction_protocol_pdf", kwargs={"org_slug": org.slug, "meeting_id": vorige.pk, "variant": "intern"}
    )
    assert f'src="{pdf_url}?vorschau"' in html and "Protokoll vom 05.10.2026" in html

    # Die Vorschau bettet das PDF ein, der Download bleibt ein Download
    monkeypatch.setattr(faction_services, "build_faction_protocol_pdf", lambda meeting, internal: b"%PDF-")
    client = client_for(leser.user)
    assert client.get(f"{pdf_url}?vorschau")["Content-Disposition"].startswith("inline;")
    assert client.get(pdf_url)["Content-Disposition"].startswith("attachment;")


def test_notizen_stehen_in_bisheriger_ansicht_und_niederschrift(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any, monkeypatch: Any
) -> None:
    from apps.work.faction import services as faction_services

    schalter_an(org)
    item = tops["oe"]
    sitzung.notizen_speichern(item, "<p><strong>Hilfsfrist</strong> im Norden</p>", basis=None, membership=vorsitz)
    sitzung_laeuft.chaired_by = vorsitz
    sitzung_laeuft.minute_taker = vorsitz
    sitzung_laeuft.save(update_fields=["chaired_by", "minute_taker"])

    # Bisherige Ansicht (Panel des TOPs): Notizen lesbar, nicht bearbeitbar
    panel = reverse(
        "work:faction_item_panel", kwargs={"org_slug": org.slug, "meeting_id": sitzung_laeuft.pk, "item_id": item.pk}
    )
    html = client_for(vorsitz.user).get(panel, HTTP_HX_REQUEST="true").content.decode()
    assert "Notizen aus der Sitzung" in html and "<strong>Hilfsfrist</strong> im Norden" in html

    # Niederschrift: Notizen beim TOP, Namen von Sitzungsleitung und Schriftführung bei den Unterschriften
    erzeugt: dict[str, str] = {}

    def pdf(html: str) -> bytes:
        erzeugt["html"] = html
        return b"%PDF-"

    monkeypatch.setattr(faction_services, "html_to_pdf", pdf)
    faction_services.build_faction_protocol_pdf(sitzung_laeuft, internal=False)
    assert "<strong>Hilfsfrist</strong> im Norden" in erzeugt["html"]
    name = vorsitz.user.get_display_name()
    assert f"{name}, Sitzungsleitung" in erzeugt["html"] and f"{name}, Protokollführung" in erzeugt["html"]


def test_datenexport_nennt_sitzungsleitung_und_schriftfuehrung(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any
) -> None:
    from apps.work.organization.export_service import DsgvoExportService

    sitzung_laeuft.chaired_by = vorsitz
    sitzung_laeuft.minute_taker = mitglied
    sitzung_laeuft.save(update_fields=["chaired_by", "minute_taker"])
    daten = DsgvoExportService().collect_user_data(user=mitglied.user, membership=mitglied, organization=org)
    vermerke = [(v["action"], v["object"]) for v in daten["vermerke"]]
    assert ("Schriftführung", sitzung_laeuft.title) in vermerke
    assert ("Sitzungsleitung", sitzung_laeuft.title) not in vermerke
