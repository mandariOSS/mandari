# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sichtschutz für den nichtöffentlichen Teil der laufenden Sitzung (Issue #1166).

Auch wer den nichtöffentlichen Teil sehen darf, bekommt ihn in der Sitzungsansicht zunächst verdeckt: Die Seite und
jede HTMX-Antwort kommen mit der Klasse ``noe-verdeckt`` am Container, NÖ-Titel und -Beschreibung weichgezeichnet und
für Vorleser ersetzt, Unterlagen, Protokoll und Beschluss hinter einer Abdeckung. Erst der Browser blendet den Teil
auf Wunsch ein (frontend/alpine/faction-session.ts). Die Rechteprüfung des Servers bleibt unverändert: Wer keine Rechte
hat, bekommt den Teil wie bisher gar nicht.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionMeeting

pytestmark = pytest.mark.django_db

VORSITZ = [
    "faction.view_public",
    "faction.view_non_public",
    "faction.manage",
    "faction.start",
    "protocols.create",
    "protocols.edit",
    "voting.participate",
]
TITEL = "Anfrage Hitzeschutz an Schulen"
BESCHREIBUNG = "Messwerte aus drei Grundschulen, Antwort der Verwaltung liegt vor."
#: So steht ein NÖ-Titel überall in der Ansicht: weichgezeichnet, für Vorleser ausgeblendet und ersetzt
VERDECKT = (
    f'<span class="fs-noe" aria-hidden="true" :aria-hidden="!noeSichtbar">{TITEL}</span>'
    '<span class="sr-only fs-noe-ersatz">Nichtöffentlicher TOP, ausgeblendet</span>'
)


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    m = make_member(org, VORSITZ, email="vorsitz-noe@example.org")
    m.is_sworn_in = True
    m.save(update_fields=["is_sworn_in"])
    return m


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["faction.view_public", "voting.participate"], email="mitglied-noe@example.org")


@pytest.fixture
def sitzung(org: Any, vorsitz: Any, mitglied: Any) -> tuple[FactionMeeting, dict[str, FactionAgendaItem]]:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung Nr. 25",
        start=timezone.now() - timedelta(minutes=10),
        status="ongoing",
        created_by=vorsitz,
    )
    for m in (vorsitz, mitglied):
        FactionAttendance.objects.create(meeting=meeting, membership=m, status="invited")
    oe = FactionAgendaItem.objects.create(
        meeting=meeting, number="1", title="Haushalt 2027", visibility="public", order=1
    )
    noe = FactionAgendaItem.objects.create(meeting=meeting, number="NÖ 1", title=TITEL, visibility="internal", order=2)
    cast(Any, noe).set_description_encrypted(BESCHREIBUNG)
    noe.save()
    return meeting, {"oe": oe, "noe": noe}


def _seite(org: Any, meeting: FactionMeeting, item: FactionAgendaItem) -> str:
    url = reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})
    return f"{url}?top={item.pk}"


def _top_url(org: Any, meeting: FactionMeeting, item: FactionAgendaItem) -> str:
    return reverse(
        "work:faction_session_item", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk, "item_id": item.pk}
    )


def _ohne_verdecktes(html: str) -> str:
    """HTML ohne die verdeckten Stellen und die Kurzinfo zum Einblenden: Was übrig bleibt, wäre lesbar."""
    html = html.replace(VERDECKT, "")
    html = re.sub(r'<p class="fs-top-zeile2 fs-noe"[^>]*aria-hidden="true"[^>]*>.*?</p>', "", html, flags=re.S)
    # Auswahl „Als Unterpunkt von“ im Dialog „TOP hinzufügen“: sichtbar nur die Nummer, der Titel erst eingeblendet
    html = re.sub(r'data-voll="[^"]*"', "", html)
    return re.sub(r'data-noe-titel="[^"]*"', "", html)


def test_nichtoeffentlicher_top_kommt_verdeckt(org: Any, vorsitz: Any, sitzung: Any, client_for: Any) -> None:
    meeting, tops = sitzung
    html = client_for(vorsitz.user).get(_seite(org, meeting, tops["noe"])).content.decode()

    # Ohne Klick verdeckt: Klasse am Container, Alpine nimmt sie erst nach dem Einblenden weg
    assert 'class="fs-sitzung wf noe-verdeckt"' in html
    assert ":class=\"{ 'noe-verdeckt': !noeSichtbar }\"" in html
    assert f'data-sitzung="{meeting.pk}"' in html
    # Tagesordnung: NÖ-Titel verdeckt, Nummer lesbar, Schalter an der Überschrift; öffentliche Titel unverändert
    zeile = re.search(rf'<li id="fs-tl-{tops["noe"].pk}">.*?</li>', html, flags=re.S)
    assert zeile and '<span class="nr">NÖ 1</span>' in zeile.group(0) and VERDECKT in zeile.group(0)
    oe_zeile = re.search(rf'<li id="fs-tl-{tops["oe"].pk}">.*?</li>', html, flags=re.S)
    assert oe_zeile and re.search(r'<span class="titel">Haushalt 2027\s*</span>', oe_zeile.group(0))
    assert re.search(
        r'fs-noe-gruppe">Nichtöffentlicher Teil\s*<button[^>]*fs-noe-schalter[^>]*aria-pressed="false"', html
    )
    # Kopf des TOPs: Nummer lesbar, Titel und Beschreibung verdeckt, Schalter daneben
    assert f'<span class="wf-top-nr">TOP NÖ 1</span> {VERDECKT}' in html
    assert re.search(
        rf'<p class="fs-top-zeile2 fs-noe" x-ref="zeile" aria-hidden="true" :aria-hidden="!noeSichtbar">'
        rf"\s*{re.escape(BESCHREIBUNG)}</p>",
        html,
    )
    assert html.count("fs-noe-schalter") == 2
    # Unterlagen und Protokoll hinter der Abdeckung, der Beschluss in der Leiste unten ebenso
    assert html.index("fs-noe-abdeckung") < html.index('<div class="fs-noe-inhalt">') < html.index('id="fs-notiz"')
    assert "Nichtöffentlichen Teil einblenden</button>" in html
    assert '<div class="fs-beschluss fs-noe-ersatz">' in html
    # Nirgends sonst lesbar
    rest = _ohne_verdecktes(html)
    assert TITEL not in rest and BESCHREIBUNG not in rest


def test_oeffentlicher_top_verdeckt_nur_den_nachbarn(org: Any, vorsitz: Any, sitzung: Any, client_for: Any) -> None:
    meeting, tops = sitzung
    html = client_for(vorsitz.user).get(_seite(org, meeting, tops["oe"])).content.decode()
    assert re.search(r'<span class="wf-top-nr">TOP 1</span> Haushalt 2027\s*</h2>', html)
    # Öffentlicher TOP: keine Abdeckung, kein Schalter im Kopf; nur der an der Überschrift der Tagesordnung
    assert "fs-noe-abdeckung" not in html and "fs-noe-inhalt" not in html
    assert html.count("fs-noe-schalter") == 1
    # „Nächster TOP“: Titel weichgezeichnet, die Kurzinfo nennt ihn erst nach dem Einblenden
    assert 'title="Nächster TOP: NÖ 1 (Taste J)"' in html
    assert f'data-noe-titel="Nächster TOP: NÖ 1 {TITEL} (Taste J)"' in html and ':title="noeTitel($el)"' in html
    assert f"<b>NÖ 1 {VERDECKT}" in html
    assert TITEL not in _ohne_verdecktes(html)


def test_htmx_antworten_kommen_verdeckt(org: Any, vorsitz: Any, sitzung: Any, client_for: Any) -> None:
    meeting, tops = sitzung
    client = client_for(vorsitz.user)
    # TOP-Wechsel per HTMX: TOP und Leiste unten verdeckt (der Zustand im Browser gilt weiter)
    html = client.get(_top_url(org, meeting, tops["noe"]), HTTP_HX_REQUEST="true").content.decode()
    assert VERDECKT in html and "fs-noe-abdeckung" in html and '<div class="fs-beschluss fs-noe-ersatz">' in html
    assert TITEL not in _ohne_verdecktes(html)
    # Beschluss erfassen: Leiste und ausgetauschte Zeile der Tagesordnung bleiben verdeckt
    aktion = reverse("work:faction_session_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})
    antwort = client.post(
        aktion, {"action": "beschluss", "item_id": tops["noe"].pk, "votes_yes": "3", "result": "accepted"}
    )
    html = antwort.content.decode()
    assert antwort.status_code == 200
    assert '<div class="fs-beschluss fs-noe-ersatz">' in html and '<div class="fs-noe-inhalt">' in html
    assert f'<li id="fs-tl-{tops["noe"].pk}" hx-swap-oob="true">' in html and VERDECKT in html
    assert TITEL not in _ohne_verdecktes(html)


def test_ohne_rechte_bleibt_der_teil_wie_bisher_weg(org: Any, mitglied: Any, sitzung: Any, client_for: Any) -> None:
    meeting, tops = sitzung
    client = client_for(mitglied.user)
    html = client.get(_seite(org, meeting, tops["oe"])).content.decode()
    assert TITEL not in html and BESCHREIBUNG not in html
    assert "Ein TOP, nur für vereidigte Mitglieder" in html
    # Nichts zum Einblenden: kein Schalter, keine verdeckten Titel
    assert "fs-noe-schalter" not in html and 'class="fs-noe"' not in html
    assert client.get(_top_url(org, meeting, tops["noe"]), HTTP_HX_REQUEST="true").status_code == 403


def test_beratungsverlauf_eines_noe_tops_ist_verdeckt_und_nicht_bedienbar(org: Any, sitzung: Any) -> None:
    """Die Links im Verlauf eines verdeckten NÖ-TOPs sind weichgezeichnet und per inert weder fokussier- noch klickbar."""
    from django.template.loader import render_to_string

    from apps.work.faction.sitzung import VerlaufEintrag

    _meeting, tops = sitzung
    eintrag = VerlaufEintrag(
        wo="Schulausschuss, 12.10.2026", position="Zustimmung", punkt="zustimmung", meeting_id=str(uuid.uuid4())
    )
    kontext = {"item": tops["noe"], "verlauf": [eintrag], "org_slug": org.slug, "beschreibung": BESCHREIBUNG}
    html = render_to_string("work/faction/sitzung/_top_kopf.html", kontext | {"intern": True})
    assert '<ul x-ref="verlauf" class="fs-noe" inert x-bind:inert="!noeSichtbar">' in html
    assert "Schulausschuss, 12.10.2026" in html and "fs-noe-schalter" in html
    # Öffentlicher TOP: unverändert, ohne Schalter
    html = render_to_string("work/faction/sitzung/_top_kopf.html", kontext | {"intern": False})
    assert '<ul x-ref="verlauf">' in html and "fs-noe" not in html
