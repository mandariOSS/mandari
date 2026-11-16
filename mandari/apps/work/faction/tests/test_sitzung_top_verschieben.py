# SPDX-License-Identifier: AGPL-3.0-or-later
"""
TOP in der laufenden Fraktionssitzung verschieben und Dopplungen beim Hinzufügen melden (Issue #1436).

In einer Sitzung zeigte sich, dass ein Punkt in den nichtöffentlichen Teil gehört. Verschieben ging nur in der
Vorbereitung; in der Sitzungsansicht entstanden stattdessen neue TOPs mit dem Titel des vorhandenen („TOP 3.2 …“,
„TOP 7 TOP 3.2 …“).

- Verschieben am TOP: nach oben, nach unten, in den nichtöffentlichen und zurück in den öffentlichen Teil, auf
  demselben Weg wie in der Vorbereitung (``agenda.place_item``). Die Antwort ist ein TOP-Wechsel auf denselben TOP.
- Rechte: wer die Tagesordnung verwaltet, dazu die Schriftführung; Nichtöffentliches nur für Vereidigte.
- „TOP hinzufügen“ warnt bei einem Titel, den es schon gibt (auch mit vorangestellter TOP-Nummer), und legt erst mit
  Bestätigung an. Nichtöffentliche Titel verrät die Warnung nicht.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.faction import sitzung
from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionMeeting

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

    genehmigung = top("1", "Tagesordnung festlegen", "public", 0, is_approval_item=True)
    arbeit = top("2", "Politische Arbeit", "public", 1)
    return {
        "genehmigung": genehmigung,
        "arbeit": arbeit,
        "halle": top("2.1", "Sechsfachsporthalle (V/0522/2026)", "public", 2, parent=arbeit),
        "klassen": top("2.2", "Fertigbauklassen", "public", 3, parent=arbeit),
        "termine": top("3", "Termine", "public", 4),
        "sonstiges": top("4", "Sonstiges", "public", 5),
        "noe": top("NÖ 1", "Grundstücksangelegenheit Flurstück 12/3", "internal", 6),
    }


def aktion_url(org: Any, meeting: FactionMeeting) -> str:
    return reverse("work:faction_session_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})


def seite(org: Any, meeting: FactionMeeting) -> str:
    return reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})


def verschieben(client: Any, org: Any, meeting: FactionMeeting, item: FactionAgendaItem, richtung: str) -> Any:
    werte = {"action": "top_verschieben", "item_id": str(item.pk), "richtung": richtung}
    return client.post(aktion_url(org, meeting), werte, HTTP_HX_REQUEST="true")


def neu(client: Any, org: Any, meeting: FactionMeeting, **werte: Any) -> Any:
    return client.post(aktion_url(org, meeting), {"action": "top_neu", **werte}, HTTP_HX_REQUEST="true")


def nummern(meeting: FactionMeeting) -> dict[str, str]:
    return {i.title: i.number for i in FactionAgendaItem.objects.filter(meeting=meeting)}


# ---- Verschieben -------------------------------------------------------------------------------------------


def test_nach_unten_und_oben_tauscht_die_nummern(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    antwort = verschieben(client, org, sitzung_laeuft, tops["termine"], "down")
    assert antwort.status_code == 200
    stand = nummern(sitzung_laeuft)
    assert (stand["Sonstiges"], stand["Termine"], stand["Politische Arbeit"]) == ("3", "4", "2")

    html = antwort.content.decode()
    # Wie ein TOP-Wechsel auf denselben TOP: neue Nummer im Kopf, Tagesordnung und Leiste unten kommen mit
    assert f'data-top="{tops["termine"].pk}"' in html and "TOP 4</span> Termine" in html
    assert 'id="fs-tagesordnung" hx-swap-oob="innerHTML"' in html and 'id="fs-unten"' in html
    assert antwort["HX-Push-Url"] == f"{seite(org, sitzung_laeuft)}?top={tops['termine'].pk}"
    assert json.loads(antwort["HX-Trigger"])["showToast"]["message"] == "TOP 4 verschoben"

    assert verschieben(client, org, sitzung_laeuft, tops["termine"], "up").status_code == 200
    assert nummern(sitzung_laeuft)["Termine"] == "3"

    # Unterpunkte tauschen unter ihrem TOP
    assert verschieben(client, org, sitzung_laeuft, tops["halle"], "down").status_code == 200
    stand = nummern(sitzung_laeuft)
    assert (stand["Fertigbauklassen"], stand["Sechsfachsporthalle (V/0522/2026)"]) == ("2.1", "2.2")


def test_am_rand_und_am_genehmigungs_top_geht_nichts(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    vorher = nummern(sitzung_laeuft)
    # Der erste nummerierte TOP steht direkt unter dem Genehmigungs-TOP und geht nicht darüber
    assert verschieben(client, org, sitzung_laeuft, tops["arbeit"], "up").status_code == 400
    assert verschieben(client, org, sitzung_laeuft, tops["sonstiges"], "down").status_code == 400
    assert verschieben(client, org, sitzung_laeuft, tops["genehmigung"], "down").status_code == 400
    assert verschieben(client, org, sitzung_laeuft, tops["termine"], "seitwaerts").status_code == 400
    assert verschieben(client, org, sitzung_laeuft, tops["termine"], "public").status_code == 400
    assert nummern(sitzung_laeuft) == vorher


def test_unterpunkt_geht_in_den_nichtoeffentlichen_teil_und_wird_eigener_top(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    halle = tops["halle"]
    antwort = verschieben(client_for(vorsitz.user), org, sitzung_laeuft, halle, "internal")
    assert antwort.status_code == 200
    halle.refresh_from_db()
    assert (halle.number, halle.visibility, halle.parent_id) == ("NÖ 2", "internal", None)
    stand = nummern(sitzung_laeuft)
    # Der verbliebene Unterpunkt rückt auf, die übrigen Nummern bleiben
    assert (stand["Fertigbauklassen"], stand["Termine"], stand["Grundstücksangelegenheit Flurstück 12/3"]) == (
        "2.1",
        "3",
        "NÖ 1",
    )
    assert json.loads(antwort["HX-Trigger"])["showToast"]["message"] == "TOP NÖ 2 verschoben"

    # Nicht-Vereidigte sehen den TOP danach nicht mehr, auch nicht seinen Titel in der Tagesordnung
    html = client_for(mitglied.user).get(seite(org, sitzung_laeuft)).content.decode()
    assert "Sechsfachsporthalle" not in html
    # Auch nicht über die Adresse des TOPs: Die Seite zeigt dann einen TOP, den das Mitglied sehen darf
    html = client_for(mitglied.user).get(f"{seite(org, sitzung_laeuft)}?top={halle.pk}").content.decode()
    assert "Sechsfachsporthalle" not in html and f'data-top="{halle.pk}"' not in html


def test_top_mit_unterpunkten_nimmt_sie_mit_und_kommt_zurueck(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    assert verschieben(client, org, sitzung_laeuft, tops["arbeit"], "internal").status_code == 200
    stand = nummern(sitzung_laeuft)
    assert (stand["Politische Arbeit"], stand["Sechsfachsporthalle (V/0522/2026)"], stand["Termine"]) == (
        "NÖ 2",
        "NÖ 2.1",
        "2",
    )
    assert set(
        FactionAgendaItem.objects.filter(pk__in=[tops["halle"].pk, tops["klassen"].pk]).values_list(
            "visibility", flat=True
        )
    ) == {"internal"}

    # Zurück in den öffentlichen Teil: ans Ende; die Unterpunkte bleiben nichtöffentlich, bis jemand sie einzeln freigibt
    assert verschieben(client, org, sitzung_laeuft, tops["arbeit"], "public").status_code == 200
    arbeit = FactionAgendaItem.objects.get(pk=tops["arbeit"].pk)
    assert (arbeit.number, arbeit.visibility) == ("4", "public")
    halle = FactionAgendaItem.objects.get(pk=tops["halle"].pk)
    assert (halle.visibility, halle.parent_id) == ("internal", arbeit.pk)

    # Ein nichtöffentlicher Unterpunkt eines öffentlichen TOPs wird an seiner Stelle wieder öffentlich
    assert verschieben(client, org, sitzung_laeuft, halle, "public").status_code == 200
    halle.refresh_from_db()
    assert (halle.visibility, halle.parent_id, halle.number) == ("public", arbeit.pk, "4.1")


def test_menue_am_top_bietet_nur_was_geht(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    client = client_for(vorsitz.user)

    def angebot(item: FactionAgendaItem) -> list[str]:
        html = client.get(f"{seite(org, sitzung_laeuft)}?top={item.pk}").content.decode()
        return [r for r in ("up", "down", "internal", "public") if f'"richtung": "{r}"' in html]

    assert angebot(tops["termine"]) == ["up", "down", "internal"]
    assert angebot(tops["arbeit"]) == ["down", "internal"]
    assert angebot(tops["sonstiges"]) == ["up", "internal"]
    assert angebot(tops["noe"]) == ["public"]
    assert angebot(tops["genehmigung"]) == []

    # Ohne das Recht gibt es das Menü nicht
    html = client_for(mitglied.user).get(f"{seite(org, sitzung_laeuft)}?top={tops['termine'].pk}").content.decode()
    assert 'data-testid="fs-top-verschieben"' not in html


def test_rechte_schriftfuehrung_ja_mitglied_nein_nichtoeffentlich_nur_vereidigt(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    client = client_for(mitglied.user)
    assert verschieben(client, org, sitzung_laeuft, tops["termine"], "down").status_code == 403
    assert nummern(sitzung_laeuft)["Termine"] == "3"

    # Schriftführung ohne faction.manage ordnet um, aber nicht in den nichtöffentlichen Teil (nicht vereidigt)
    sitzung_laeuft.minute_taker = mitglied
    sitzung_laeuft.save(update_fields=["minute_taker"])
    assert verschieben(client, org, sitzung_laeuft, tops["termine"], "down").status_code == 200
    assert nummern(sitzung_laeuft)["Termine"] == "4"
    assert verschieben(client, org, sitzung_laeuft, tops["termine"], "internal").status_code == 403
    # Nichtöffentliche TOPs gibt es für sie nicht
    assert verschieben(client, org, sitzung_laeuft, tops["noe"], "public").status_code in (403, 404)
    tops["noe"].refresh_from_db()
    assert tops["noe"].visibility == "internal"
    assert sitzung.verschiebbar(sitzung_laeuft, mitglied, tops["termine"])["internal"] is False


def test_nur_in_der_laufenden_sitzung_und_nicht_nach_der_genehmigung(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    sitzung_laeuft.protocol_approved = True
    sitzung_laeuft.save(update_fields=["protocol_approved"])
    assert sitzung.darf_top_verschieben(vorsitz, sitzung_laeuft) is False
    assert verschieben(client, org, sitzung_laeuft, tops["termine"], "down").status_code == 403

    sitzung_laeuft.protocol_approved = False
    sitzung_laeuft.status = "completed"
    sitzung_laeuft.save(update_fields=["protocol_approved", "status"])
    assert verschieben(client, org, sitzung_laeuft, tops["termine"], "down").status_code in (404, 409)
    assert nummern(sitzung_laeuft)["Termine"] == "3"


def test_genehmigung_zwischen_klick_und_sperre_verschiebt_nichts(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any
) -> None:
    """Die Prüfung des Aufrufers lief mit dem geladenen Stand; maßgeblich ist der Stand nach der Sperre."""
    assert sitzung.darf_top_verschieben(vorsitz, sitzung_laeuft) is True
    FactionMeeting.objects.filter(pk=sitzung_laeuft.pk).update(protocol_approved=True)
    with pytest.raises(sitzung.TopVerschiebenError) as abgelehnt:
        sitzung.top_verschieben(sitzung_laeuft, vorsitz, tops["termine"], "down")
    assert abgelehnt.value.status == 409
    assert nummern(sitzung_laeuft)["Termine"] == "3"

    FactionMeeting.objects.filter(pk=sitzung_laeuft.pk).update(protocol_approved=False, status="completed")
    with pytest.raises(sitzung.TopVerschiebenError) as abgelehnt:
        sitzung.top_verschieben(sitzung_laeuft, vorsitz, tops["termine"], "down")
    assert abgelehnt.value.status == 409
    assert nummern(sitzung_laeuft)["Termine"] == "3"


# ---- Dopplung beim Hinzufügen ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "titel",
    [
        "Sechsfachsporthalle (V/0522/2026)",
        "  sechsfachsporthalle   (v/0522/2026) ",
        "TOP 2.1 Sechsfachsporthalle (V/0522/2026)",
        "TOP 7 TOP 2.1 Sechsfachsporthalle (V/0522/2026)",
    ],
)
def test_gleicher_titel_warnt_erst_und_legt_mit_bestaetigung_an(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any, titel: str
) -> None:
    client = client_for(vorsitz.user)
    vorher = FactionAgendaItem.objects.count()
    antwort = neu(client, org, sitzung_laeuft, title=titel, visibility="public")
    assert antwort.status_code == 409 and antwort["X-Top-Doppelt"] == "1"
    meldung = antwort.content.decode()
    assert "TOP 2.1" in meldung and "Verschieben" in meldung
    assert "Sechsfachsporthalle" not in meldung
    assert FactionAgendaItem.objects.count() == vorher

    assert neu(client, org, sitzung_laeuft, title=titel, visibility="public", doppelt="1").status_code == 200
    assert FactionAgendaItem.objects.count() == vorher + 1


def test_anderer_titel_legt_ohne_warnung_an(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    for titel in ("Sechsfachsporthalle Nachtrag", "TOP 9 Bericht aus dem Vorstand", "2026 Haushalt"):
        assert neu(client, org, sitzung_laeuft, title=titel, visibility="public").status_code == 200
    assert sitzung.gleichnamige_tops(sitzung_laeuft, vorsitz, "TOP 4") == []


def test_warnung_verraet_keine_nichtoeffentlichen_titel(
    org: Any, vorsitz: Any, mitglied: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    titel = "Grundstücksangelegenheit Flurstück 12/3"
    # Vereidigte werden gewarnt
    assert neu(client_for(vorsitz.user), org, sitzung_laeuft, title=titel, visibility="internal").status_code == 409
    # Die Schriftführung ohne Vereidigung darf TOPs ergänzen, erfährt aber nichts vom nichtöffentlichen TOP
    sitzung_laeuft.minute_taker = mitglied
    sitzung_laeuft.save(update_fields=["minute_taker"])
    assert sitzung.gleichnamige_tops(sitzung_laeuft, mitglied, titel) == []
    assert neu(client_for(mitglied.user), org, sitzung_laeuft, title=titel, visibility="public").status_code == 200


def test_ueberlange_titel_werden_nicht_verglichen(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    """Die Längengrenze gilt vor dem Vergleich: Ein Titel aus sehr vielen Nummern kostet keine Rechenzeit."""
    riesig = "TOP 1 " * 100_000 + "Termine"
    assert sitzung.gleichnamige_tops(sitzung_laeuft, vorsitz, riesig) == []
    antwort = neu(client_for(vorsitz.user), org, sitzung_laeuft, title=riesig, visibility="public")
    assert antwort.status_code == 400 and "zu lang" in antwort.content.decode()
    # Viele Nummern unterhalb der Grenze werden in einem Durchgang gelöst
    assert sitzung.gleichnamige_tops(sitzung_laeuft, vorsitz, "TOP 1 " * 80 + "Termine") == [tops["termine"]]
    # Nur Nummern: die letzte bleibt stehen und gleicht keinem Titel
    assert sitzung.gleichnamige_tops(sitzung_laeuft, vorsitz, "TOP 1 TOP 2") == []


def test_dialog_zeigt_warnung_und_bestaetigung(
    org: Any, vorsitz: Any, sitzung_laeuft: Any, tops: Any, client_for: Any
) -> None:
    html = client_for(vorsitz.user).get(seite(org, sitzung_laeuft)).content.decode()
    assert 'x-data="topNeu"' in html and '@htmx:before-on-load="antwort($event)"' in html
    assert '@htmx:before-request="merken()"' in html
    assert 'name="doppelt"' in html and 'data-testid="fs-top-neu-doppelt"' in html
