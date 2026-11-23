# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Grenze der TOP-Beschreibung (``agenda.BESCHREIBUNG_MAX``).

Die Beschreibung ordnet einen TOP kurz ein. Ein eingefügter Antrag mit fast 3.000 Zeichen machte Tagesordnung und
Sitzungsansicht unlesbar; lange Texte gehören als Anhang an den TOP. Alle Wege, auf denen Menschen eine Beschreibung
eingeben, lehnen zu lange Texte mit derselben Meldung ab: sofort speichern in der Vorbereitung, Panel, Dialog
„TOP anlegen/bearbeiten“, Vorschlag und „TOP hinzufügen“ in der laufenden Sitzung. Schon gespeicherte längere
Beschreibungen bleiben, solange niemand sie ändert. Unter den Feldern steht die Grenze mit dem Weg zu den Anhängen.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.faction import agenda, sitzung
from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionMeeting

pytestmark = pytest.mark.django_db

VORSITZ = ["faction.view_public", "faction.view_non_public", "faction.manage", "faction.start", "voting.participate"]
ZU_LANG = "x" * (agenda.BESCHREIBUNG_MAX + 1)
GENAU = "y" * agenda.BESCHREIBUNG_MAX


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    m = make_member(org, VORSITZ, email="vorsitz@example.org")
    m.is_sworn_in = True
    m.save(update_fields=["is_sworn_in"])
    return m


@pytest.fixture
def sitzung_geplant(org: Any, vorsitz: Any) -> FactionMeeting:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() + timedelta(days=2),
        status="planned",
        created_by=vorsitz,
    )


@pytest.fixture
def top(sitzung_geplant: FactionMeeting) -> FactionAgendaItem:
    item = FactionAgendaItem.objects.create(
        meeting=sitzung_geplant, number="2", title="Eigenfinanzierung Zoo", visibility="public", order=1
    )
    cast(Any, item).set_description_encrypted("Kurze Einordnung")
    item.save()
    return item


def _beschreibung(item: FactionAgendaItem) -> str:
    item.refresh_from_db()
    return cast(Any, item).get_description_decrypted() or ""


def _aktion(org: Any, meeting: FactionMeeting) -> str:
    return reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})


# ---- Regel ------------------------------------------------------------------------------------------------


def test_regel_grenze_genau_und_unveraenderter_altbestand() -> None:
    assert agenda.beschreibung_zu_lang(GENAU) == ""
    assert agenda.beschreibung_zu_lang(f"  {GENAU}  ") == ""
    meldung = agenda.beschreibung_zu_lang(ZU_LANG)
    assert "1.000 Zeichen" in meldung and "1.001" in meldung and "Anhang" in meldung
    # Höchstens 200 Zeichen, sonst zeigt das automatische Speichern im Panel die Meldung nicht (autosave.ts)
    assert len(agenda.beschreibung_zu_lang("z" * 12_345)) <= 200
    # Eine schon gespeicherte längere Beschreibung bleibt, solange sie unverändert ist
    assert agenda.beschreibung_zu_lang(ZU_LANG, bisher=f"{ZU_LANG}\n") == ""
    assert agenda.beschreibung_zu_lang(ZU_LANG + "!", bisher=ZU_LANG) != ""


# ---- Wege der Eingabe ---------------------------------------------------------------------------------------


def test_sofort_speichern_lehnt_zu_lang_ab(org: Any, vorsitz: Any, top: FactionAgendaItem, client_for: Any) -> None:
    url = reverse(
        "work:faction_item_fields", kwargs={"org_slug": org.slug, "meeting_id": top.meeting_id, "item_id": top.pk}
    )
    client = client_for(vorsitz.user)

    antwort = client.post(url, data=json.dumps({"description": ZU_LANG}), content_type="application/json")
    assert antwort.status_code == 400
    assert "Anhang" in antwort.json()["error"]
    assert _beschreibung(top) == "Kurze Einordnung"

    antwort = client.post(url, data=json.dumps({"description": GENAU}), content_type="application/json")
    assert antwort.status_code == 200
    assert _beschreibung(top) == GENAU


def test_panel_lehnt_zu_lang_ab_und_laesst_altbestand_stehen(
    org: Any, vorsitz: Any, top: FactionAgendaItem, client_for: Any
) -> None:
    url = reverse(
        "work:faction_item_panel_action",
        kwargs={"org_slug": org.slug, "meeting_id": top.meeting_id, "item_id": top.id},
    )
    client = client_for(vorsitz.user)

    antwort = client.post(url, {"action": "update", "description": ZU_LANG})
    assert antwort.status_code == 400
    assert "Anhang" in antwort.content.decode()
    assert _beschreibung(top) == "Kurze Einordnung"

    # Altbestand über der Grenze: Speichern derselben Beschreibung (und des Titels) scheitert nicht
    cast(Any, top).set_description_encrypted(ZU_LANG)
    top.save()
    antwort = client.post(url, {"action": "update", "description": ZU_LANG})
    assert antwort.status_code == 204
    assert client.post(url, {"action": "update", "title": "Zoo"}).status_code == 204
    assert _beschreibung(top) == ZU_LANG


def test_dialog_anlegen_und_bearbeiten_lehnen_zu_lang_ab(
    org: Any, vorsitz: Any, top: FactionAgendaItem, client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    url = _aktion(org, top.meeting)

    antwort = client.post(
        url,
        {"action": "add_item", "title": "Neuer TOP", "visibility": "public", "description": ZU_LANG},
        HTTP_HX_REQUEST="true",
    )
    assert antwort.status_code == 400 and "Anhang" in antwort.content.decode()
    assert not FactionAgendaItem.objects.filter(meeting=top.meeting, title="Neuer TOP").exists()

    antwort = client.post(
        url,
        {"action": "edit_item", "item_id": str(top.pk), "title": "Geändert", "description": ZU_LANG},
        HTTP_HX_REQUEST="true",
    )
    assert antwort.status_code == 400
    top.refresh_from_db()
    assert top.title == "Eigenfinanzierung Zoo"
    assert _beschreibung(top) == "Kurze Einordnung"


def test_vorschlag_lehnt_zu_lang_ab(
    org: Any, make_member: Any, sitzung_geplant: FactionMeeting, client_for: Any
) -> None:
    sachkundig = make_member(org, ["faction.view_public", "agenda.propose"], email="sachkundig@example.org")
    antwort = client_for(sachkundig.user).post(
        _aktion(org, sitzung_geplant),
        {"action": "propose", "title": "Antragsvorschlag", "visibility": "public", "description": ZU_LANG},
    )
    assert antwort.status_code == 302
    assert not FactionAgendaItem.objects.filter(meeting=sitzung_geplant, title="Antragsvorschlag").exists()


def test_laufende_sitzung_top_neu_lehnt_zu_lang_ab(org: Any, vorsitz: Any, client_for: Any) -> None:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() - timedelta(minutes=10),
        status="ongoing",
        created_by=vorsitz,
    )
    FactionAttendance.objects.create(meeting=meeting, membership=vorsitz, status="present")
    with pytest.raises(sitzung.TopNeuError) as abgelehnt:
        sitzung.top_hinzufuegen(meeting, vorsitz, titel="Zoo", teil="public", beschreibung=ZU_LANG)
    assert "Anhang" in abgelehnt.value.meldung
    assert not FactionAgendaItem.objects.filter(meeting=meeting, title="Zoo").exists()

    antwort = client_for(vorsitz.user).post(
        reverse("work:faction_session_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk}),
        {"action": "top_neu", "title": "Zoo", "visibility": "public", "description": ZU_LANG},
        HTTP_HX_REQUEST="true",
    )
    assert antwort.status_code == 400 and "Anhang" in antwort.content.decode()


# ---- Hinweis unter dem Feld -------------------------------------------------------------------------------------


def test_vorbereitung_zeigt_grenze_mit_weg_zu_den_anhaengen(
    org: Any, vorsitz: Any, top: FactionAgendaItem, client_for: Any
) -> None:
    seite = reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": top.meeting_id})
    html = client_for(vorsitz.user).get(f"{seite}?ansicht=tagesordnung&top={top.pk}").content.decode()

    assert f'aria-describedby="b-{top.pk}-grenze"' in html
    assert f'id="b-{top.pk}-grenze"' in html
    assert "Höchstens 1.000 Zeichen." in html
    assert f"$dispatch('open-item-panel', {{ id: '{top.pk}' }})\">Anhang hinzufügen</button>" in html
    # Kein maxlength am Feld: Der Browser schnitte einen eingefügten Antrag still ab
    feld = html[html.index(f'id="b-{top.pk}"') :]
    assert "maxlength" not in feld[: feld.index(">")]


def test_panel_springt_zu_den_anhaengen(org: Any, vorsitz: Any, top: FactionAgendaItem, client_for: Any) -> None:
    url = reverse(
        "work:faction_item_panel", kwargs={"org_slug": org.slug, "meeting_id": top.meeting_id, "item_id": top.id}
    )
    html = client_for(vorsitz.user).get(url, HTTP_HX_REQUEST="true").content.decode()

    assert f'id="panel-anhaenge-{top.id}"' in html
    assert f"getElementById('panel-anhaenge-{top.id}')" in html
    assert "zu den Anhängen</button>" in html
