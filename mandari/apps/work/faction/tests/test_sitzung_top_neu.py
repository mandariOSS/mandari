# SPDX-License-Identifier: AGPL-3.0-or-later
"""
TOP in der laufenden Fraktionssitzung hinzufügen (Issue #1166).

Wie „TOP eintragen“ in der Vorbereitung (#855, Teil wählbar seit #1160): öffentlich, nichtöffentlich oder als
Unterpunkt, am Ende seines Teils bzw. seines TOPs nummeriert, mit Änderungsverlauf. Rechte wie in der Vorbereitung
(Verwaltung der Sitzung oder ``agenda.create``), Nichtöffentliches nur für Vereidigte, nach dem Beenden nicht mehr.
Die Antwort öffnet den neuen TOP wie ein TOP-Wechsel; andere sehen ihn beim nächsten TOP-Wechsel.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionAuditLog, FactionMeeting

pytestmark = pytest.mark.django_db

VORSITZ = ["faction.view_public", "faction.view_non_public", "faction.manage", "faction.start", "voting.participate"]
MITGLIED = ["faction.view_public", "voting.participate"]


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
        FactionAttendance.objects.create(meeting=meeting, membership=m, status="present")
    return meeting


@pytest.fixture
def tops(sitzung_laeuft: FactionMeeting) -> dict[str, FactionAgendaItem]:
    def top(nummer: str, titel: str, teil: str, order: int, **extra: Any) -> FactionAgendaItem:
        return FactionAgendaItem.objects.create(
            meeting=sitzung_laeuft, number=nummer, title=titel, visibility=teil, order=order, **extra
        )

    return {
        "genehmigung": top("1", "Tagesordnung festlegen", "public", 0, is_approval_item=True),
        "oe": top("2", "Feuerwehrbedarfsplan", "public", 1),
        "oe2": top("3", "Sonstiges", "public", 2),
        "noe": top("NÖ 1", "Grundstücksangelegenheit Flurstück 12/3", "internal", 3),
    }


def aktion_url(org: Any, meeting: FactionMeeting) -> str:
    return reverse("work:faction_session_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})


def seite(org: Any, meeting: FactionMeeting) -> str:
    return reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})


def neu(client: Any, org: Any, meeting: FactionMeeting, **werte: Any) -> Any:
    return client.post(aktion_url(org, meeting), {"action": "top_neu", **werte}, HTTP_HX_REQUEST="true")


def angelegt(meeting: FactionMeeting, titel: str) -> FactionAgendaItem:
    return FactionAgendaItem.objects.get(meeting=meeting, title=titel)


# ---- Anlegen ---------------------------------------------------------------------------------------------


def test_oeffentlicher_top_kommt_ans_ende_und_oeffnet_sich(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    antwort = neu(
        client_for(vorsitz.user),
        org,
        sitzung_laeuft,
        title="Dringlichkeitsantrag Schulbau",
        visibility="public",
        description="Unter TOP 1 neu aufgenommen",
    )
    assert antwort.status_code == 200
    item = angelegt(sitzung_laeuft, "Dringlichkeitsantrag Schulbau")
    assert (item.number, item.visibility, item.parent_id, item.proposal_status) == ("4", "public", None, "active")
    assert item.order > tops["noe"].order
    assert cast(Any, item).get_description_decrypted() == "Unter TOP 1 neu aufgenommen"
    # Bestehende Nummern bleiben, wie sie sind
    assert [FactionAgendaItem.objects.get(pk=t.pk).number for t in tops.values()] == ["1", "2", "3", "NÖ 1"]

    html = antwort.content.decode()
    # Wie ein TOP-Wechsel: der neue TOP in der Mitte, Leiste unten und Tagesordnung kommen mit
    assert f'data-top="{item.pk}"' in html and "TOP 4</span> Dringlichkeitsantrag Schulbau" in html
    assert 'id="fs-unten"' in html and 'id="fs-tagesordnung" hx-swap-oob="innerHTML"' in html
    assert f'id="fs-tl-{item.pk}"' in html
    # Die Auswahl „Als Unterpunkt von“ im Dialog kennt den neuen TOP; der Dialog selbst steht nicht in der Tagesordnung
    assert 'id="fs-top-neu-eltern" hx-swap-oob="true"' in html and f'<option value="{item.pk}">4 ' in html
    assert 'id="fs-top-neu"' not in html
    assert 'id="fs-kopf" hx-swap-oob="true"' in html and "5 TOPs" in html
    assert antwort["HX-Push-Url"] == f"{seite(org, sitzung_laeuft)}?top={item.pk}"
    assert json.loads(antwort["HX-Trigger"])["showToast"]["message"] == "TOP 4 hinzugefügt"

    # Änderungsverlauf wie beim Eintragen in der Vorbereitung
    eintrag = FactionAuditLog.objects.get(model_name="FactionAgendaItem", object_id=item.pk, action="create")
    assert eintrag.membership_id == vorsitz.pk and eintrag.meeting_id_ref == sitzung_laeuft.pk
    assert eintrag.is_internal is False


def test_nichtoeffentlicher_top_kommt_ans_ende_des_nichtoeffentlichen_teils(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    assert neu(client, org, sitzung_laeuft, title="Personalangelegenheit", visibility="internal").status_code == 200
    item = angelegt(sitzung_laeuft, "Personalangelegenheit")
    assert (item.number, item.visibility) == ("NÖ 2", "internal")
    assert FactionAuditLog.objects.get(object_id=item.pk, action="create").is_internal is True
    # Danach wieder ein öffentlicher: die Teile zählen getrennt
    assert neu(client, org, sitzung_laeuft, title="Presse", visibility="public").status_code == 200
    assert angelegt(sitzung_laeuft, "Presse").number == "4"


def test_unterpunkte_am_ende_ihres_tops(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    for titel in ("Stellungnahme Feuerwehr", "Haushaltsmittel"):
        antwort = neu(client, org, sitzung_laeuft, title=titel, visibility="public", parent_id=str(tops["oe"].pk))
        assert antwort.status_code == 200
    erster, zweiter = angelegt(sitzung_laeuft, "Stellungnahme Feuerwehr"), angelegt(sitzung_laeuft, "Haushaltsmittel")
    assert (erster.number, erster.parent_id, erster.visibility) == ("2.1", tops["oe"].pk, "public")
    assert zweiter.number == "2.2"
    # Die nummerierten TOPs verschieben sich nicht
    assert FactionAgendaItem.objects.get(pk=tops["oe2"].pk).number == "3"
    # Unter einem nichtöffentlichen TOP ist der Unterpunkt immer nichtöffentlich
    neu(client, org, sitzung_laeuft, title="Kaufpreis", visibility="public", parent_id=str(tops["noe"].pk))
    kaufpreis = angelegt(sitzung_laeuft, "Kaufpreis")
    assert (kaufpreis.number, kaufpreis.visibility) == ("NÖ 1.1", "internal")
    # Wer „nichtöffentlich“ wählt, bekommt auch unter einem öffentlichen TOP keinen öffentlichen Unterpunkt
    neu(client, org, sitzung_laeuft, title="Vertrauliche Zahlen", visibility="internal", parent_id=str(tops["oe"].pk))
    vertraulich = angelegt(sitzung_laeuft, "Vertrauliche Zahlen")
    assert (vertraulich.number, vertraulich.visibility, vertraulich.parent_id) == ("2.3", "internal", tops["oe"].pk)


def test_ungueltige_eingaben_legen_nichts_an(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    vorher = FactionAgendaItem.objects.count()
    assert neu(client, org, sitzung_laeuft, title="  ", visibility="public").status_code == 400
    assert neu(client, org, sitzung_laeuft, title="x" * 501, visibility="public").status_code == 400
    assert neu(client, org, sitzung_laeuft, title="Neu", visibility="geheim").status_code == 400
    # Nicht unter den Genehmigungs-TOP, nicht unter einen Unterpunkt, nicht unter fremde oder unbekannte TOPs
    unter = FactionAgendaItem.objects.create(
        meeting=sitzung_laeuft, number="2.1", title="Unterpunkt", visibility="public", order=5, parent=tops["oe"]
    )
    for eltern in (tops["genehmigung"].pk, unter.pk, "kein-top"):
        antwort = neu(client, org, sitzung_laeuft, title="Neu", visibility="public", parent_id=str(eltern))
        assert antwort.status_code == 400 and "gibt es nicht mehr" in antwort.content.decode()
    assert FactionAgendaItem.objects.count() == vorher + 1


def test_leere_tagesordnung_laedt_die_seite_mit_dem_neuen_top(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    html = client.get(seite(org, sitzung_laeuft)).content.decode()
    assert 'data-testid="fs-top-neu-oeffnen"' in html and 'name="neu_laden"' in html
    antwort = neu(client, org, sitzung_laeuft, title="Begrüßung", visibility="public", neu_laden="1")
    item = angelegt(sitzung_laeuft, "Begrüßung")
    assert antwort["HX-Redirect"] == f"{seite(org, sitzung_laeuft)}?top={item.pk}"
    assert item.number == "1"


# ---- Rechte und Zustand ------------------------------------------------------------------------------------


def test_ohne_recht_403_und_kein_eintrag(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    client = client_for(mitglied.user)
    assert 'data-testid="fs-top-neu-oeffnen"' not in client.get(seite(org, sitzung_laeuft)).content.decode()
    vorher = FactionAgendaItem.objects.count()
    antwort = neu(client, org, sitzung_laeuft, title="Eigener Punkt", visibility="public")
    assert antwort.status_code == 403
    assert FactionAgendaItem.objects.count() == vorher
    # Die Schriftführung allein gibt das Recht nicht (wie in der Vorbereitung)
    sitzung_laeuft.minute_taker = mitglied
    sitzung_laeuft.save(update_fields=["minute_taker"])
    assert neu(client, org, sitzung_laeuft, title="Eigener Punkt", visibility="public").status_code == 403
    # Wer die Sitzung verwaltet, sieht den Eintrag
    assert (
        'data-testid="fs-top-neu-oeffnen"' in client_for(vorsitz.user).get(seite(org, sitzung_laeuft)).content.decode()
    )


def test_agenda_create_darf_eintragen_nichtoeffentlich_nur_vereidigt(
    org: Any, sitzung_laeuft: Any, tops: Any, make_member: Any, client_for: Any
) -> None:
    rat = make_member(org, ["faction.view_public", "agenda.create"], email="rat@example.org")
    client = client_for(rat.user)
    html = client.get(seite(org, sitzung_laeuft)).content.decode()
    # Nicht vereidigt: kein Teil zur Wahl, keine nichtöffentlichen TOPs als Eltern
    assert 'data-testid="fs-top-neu-oeffnen"' in html and 'name="visibility" value="public"' in html
    assert "Grundstücksangelegenheit" not in html and 'value="internal"' not in html
    assert neu(client, org, sitzung_laeuft, title="Radweg", visibility="public").status_code == 200
    assert angelegt(sitzung_laeuft, "Radweg").number == "4"
    vorher = FactionAgendaItem.objects.count()
    assert neu(client, org, sitzung_laeuft, title="Geheim", visibility="internal").status_code == 403
    antwort = neu(client, org, sitzung_laeuft, title="Geheim", visibility="public", parent_id=str(tops["noe"].pk))
    assert antwort.status_code == 403
    assert FactionAgendaItem.objects.count() == vorher


def test_beendete_sitzung_lehnt_ab(org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any) -> None:
    client = client_for(vorsitz.user)
    vorher = FactionAgendaItem.objects.count()
    for status in ("completed", "planned", "cancelled"):
        sitzung_laeuft.status = status
        sitzung_laeuft.save(update_fields=["status"])
        antwort = neu(client, org, sitzung_laeuft, title="Zu spät", visibility="public")
        assert antwort.status_code == 409, status
        assert "nur während der Sitzung" in antwort.content.decode()
    assert FactionAgendaItem.objects.count() == vorher


def test_genehmigtes_protokoll_lehnt_ab(org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any) -> None:
    from apps.work.faction import sitzung

    sitzung_laeuft.protocol_approved = True
    assert not sitzung.darf_top_hinzufuegen(vorsitz, sitzung_laeuft)


# ---- Andere Teilnehmende ---------------------------------------------------------------------------------------


def test_andere_sehen_den_neuen_top_beim_naechsten_top_wechsel(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    neu(client_for(vorsitz.user), org, sitzung_laeuft, title="Nachgereichter Punkt", visibility="public")
    neu(client_for(vorsitz.user), org, sitzung_laeuft, title="Vertraulicher Punkt", visibility="internal")
    url = reverse(
        "work:faction_session_item",
        kwargs={"org_slug": org.slug, "meeting_id": sitzung_laeuft.pk, "item_id": tops["oe2"].pk},
    )
    html = client_for(mitglied.user).get(url, HTTP_HX_REQUEST="true").content.decode()
    assert 'id="fs-tagesordnung" hx-swap-oob="innerHTML"' in html
    assert "Nachgereichter Punkt" in html
    # Nichtöffentliches bleibt für Nicht-Vereidigte gesperrt, nur die Zahl
    assert "Vertraulicher Punkt" not in html and "2 TOPs, nur für vereidigte Mitglieder" in html
