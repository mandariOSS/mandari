# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Funktionen der Seite vor der Sitzung aus dem Muster (Issue #1071): Ziehen, sofort speichern, aus Ratssitzung
übernehmen, Offene erinnern, Testmail der Einladung.

Geprüft werden jeweils Rechte, Mandantentrennung, Dubletten bzw. Mehrfachklicks und Fehlerfälle; die Seite selbst nur
so weit, dass die Bedienelemente für die richtigen Rollen da sind.
"""

from __future__ import annotations

import json
import smtplib
from datetime import timedelta
from typing import Any, cast
from unittest import mock

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.tenants.models import Organization
from apps.work.faction import ratssitzung, vorbereiten
from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionAuditLog, FactionMeeting
from insight_core.models import OParlAgendaItem, OParlBody, OParlConsultation, OParlMeeting, OParlPaper, OParlSource

from . import test_sitzung_vorbereiten as basis
from .test_sitzung_vorbereiten import _aktion, _html, _seite

pytestmark = pytest.mark.django_db

#: Organisation mit Vorsitz, Ratsmitglied, sachkundiger Bürgerin und einer geplanten Sitzung (wie die Seitentests)
welt = basis.welt


@pytest.fixture(autouse=True)
def _leerer_cache() -> None:
    cache.clear()


def _post(welt: dict[str, Any], client_for: Any, rolle: str, daten: dict[str, Any], **kopf: str) -> Any:
    return client_for(welt[rolle].user).post(_aktion(welt), daten, **kopf)


def _neu() -> dict[str, str]:
    return {"HTTP_HX_REQUEST": "true", "HTTP_X_WORK_GESTALTUNG": "neu"}


def _oben(meeting: FactionMeeting) -> list[str]:
    return list(
        FactionAgendaItem.objects.filter(
            meeting=meeting, visibility="public", parent__isnull=True, proposal_status="active"
        )
        .order_by("order")
        .values_list("title", flat=True)
    )


# ---------------------------------------------------------------------------
# 1. Ziehen (move_item mit direction=to)
# ---------------------------------------------------------------------------


def test_ziehen_setzt_top_an_die_stelle_und_nummeriert_neu(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    FactionAgendaItem.objects.create(meeting=meeting, title="Presse", number="3", visibility="public", order=6)
    presse = FactionAgendaItem.objects.get(meeting=meeting, title="Presse")
    antwort = _post(
        welt,
        client_for,
        "vorsitz",
        {"action": "move_item", "item_id": str(presse.pk), "direction": "to", "position": "0"},
        **_neu(),
    )
    assert antwort.status_code == 200
    assert _oben(meeting) == ["Presse", "Beschlüsse", "Termine"]
    nummern = dict(FactionAgendaItem.objects.filter(meeting=meeting).values_list("title", "number"))
    assert nummern["Presse"] == "1" and nummern["Beschlüsse"] == "2" and nummern["Feuerwehrbedarfsplan"] == "2.1"
    # Antwort ist die neue Tagesordnung mit Griff zum Ziehen
    assert 'data-griff="' in _html(antwort) and "data-ziehbar" in _html(antwort)


def _in_teil(
    welt: dict[str, Any], client_for: Any, rolle: str, item: FactionAgendaItem, teil: str, pos: str = "0"
) -> Any:
    daten = {"action": "move_item", "item_id": str(item.pk), "direction": "to", "position": pos, "teil": teil}
    return _post(welt, client_for, rolle, daten, **_neu())


def test_ziehen_in_den_nichtoeffentlichen_teil_nummeriert_beide_teile(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    termine = meeting.agenda_items.get(title="Termine")
    antwort = _in_teil(welt, client_for, "vorsitz", welt["haupt"], "internal", "1")
    assert antwort.status_code == 200

    nummern = {i.title: i.number for i in meeting.agenda_items.filter(parent__isnull=True, proposal_status="active")}
    assert nummern == {"Termine": "1", basis.NOE_TITEL: "NÖ 1", "Beschlüsse": "NÖ 2"}
    # Die Antwort ist die neue Tagesordnung, der TOP steht im nichtöffentlichen Teil
    html = _html(antwort)
    assert (
        html.index(f'id="top-{termine.pk}"')
        < html.index('data-teil="internal"')
        < html.index(f'id="top-{welt["haupt"].pk}"')
    )


def test_ziehen_in_den_nichtoeffentlichen_teil_an_die_gewaehlte_stelle(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    _in_teil(welt, client_for, "vorsitz", welt["haupt"], "internal", "0")
    reihenfolge = meeting.agenda_items.filter(visibility="internal", parent__isnull=True).order_by("order")
    assert [i.title for i in reihenfolge] == ["Beschlüsse", basis.NOE_TITEL]
    assert [i.number for i in reihenfolge] == ["NÖ 1", "NÖ 2"]


def test_unterpunkte_gehen_in_den_nichtoeffentlichen_teil_mit_und_bleiben_dort(
    welt: dict[str, Any], client_for: Any
) -> None:
    """Die Einladung listet öffentliche Unterpunkte auch unter einem nichtöffentlichen TOP (Issue #1160)."""
    meeting, haupt = welt["meeting"], welt["haupt"]
    unter = meeting.agenda_items.get(title="Feuerwehrbedarfsplan")

    _in_teil(welt, client_for, "vorsitz", haupt, "internal")
    unter.refresh_from_db()
    assert unter.visibility == "internal" and unter.number == "NÖ 1.1"
    assert "Feuerwehrbedarfsplan" not in _html(_seite(welt, client_for, "sachkundig"))

    # Zurück im öffentlichen Teil gibt erst eine eigene Entscheidung den Unterpunkt frei
    haupt.refresh_from_db()
    _in_teil(welt, client_for, "vorsitz", haupt, "public")
    unter.refresh_from_db()
    assert unter.visibility == "internal"
    haupt.refresh_from_db()
    assert haupt.visibility == "public" and haupt.number == "1"


def test_unterpunkt_eines_nichtoeffentlichen_tops_wird_nicht_oeffentlich(welt: dict[str, Any], client_for: Any) -> None:
    meeting, haupt = welt["meeting"], welt["haupt"]
    unter = meeting.agenda_items.get(title="Feuerwehrbedarfsplan")
    _in_teil(welt, client_for, "vorsitz", haupt, "internal")
    url = reverse(
        "work:faction_item_panel_action",
        kwargs={"org_slug": welt["org"].slug, "meeting_id": meeting.pk, "item_id": unter.pk},
    )
    client_for(welt["vorsitz"].user).post(url, {"action": "update", "visibility": "public"})
    unter.refresh_from_db()
    assert unter.visibility == "internal"


def test_ziehen_in_den_nichtoeffentlichen_teil_nur_fuer_vereidigte(
    welt: dict[str, Any], client_for: Any, make_member: Any
) -> None:
    verwaltend = make_member(welt["org"], ["faction.view_public", "faction.manage"], email="nina@example.org")
    welt["verwaltend"] = verwaltend
    haupt = welt["haupt"]
    assert _in_teil(welt, client_for, "verwaltend", haupt, "internal").status_code == 403
    haupt.refresh_from_db()
    assert haupt.visibility == "public"
    # Der nichtöffentliche Teil wird dieser Person gar nicht erst angeboten
    assert 'data-teil="internal"' not in _html(client_for(verwaltend.user).get(basis._adresse(welt)))


def test_nichtoeffentlichen_top_ziehen_nur_fuer_vereidigte(
    welt: dict[str, Any], client_for: Any, make_member: Any
) -> None:
    welt["verwaltend"] = make_member(welt["org"], ["faction.view_public", "faction.manage"], email="nina@example.org")
    geheim = welt["meeting"].agenda_items.get(title=basis.NOE_TITEL)
    assert _in_teil(welt, client_for, "verwaltend", geheim, "public").status_code == 403
    geheim.refresh_from_db()
    assert geheim.visibility == "internal"


def test_ziehen_in_den_anderen_teil_nur_mit_recht_und_solange_die_sitzung_offen_ist(
    welt: dict[str, Any], client_for: Any
) -> None:
    meeting, haupt = welt["meeting"], welt["haupt"]
    assert _in_teil(welt, client_for, "rat", haupt, "internal").status_code == 403

    FactionMeeting.objects.filter(pk=meeting.pk).update(status="completed", protocol_approved=True)
    assert _in_teil(welt, client_for, "vorsitz", haupt, "internal").status_code == 403
    haupt.refresh_from_db()
    assert haupt.visibility == "public"


def test_vorschlag_laesst_sich_nicht_in_den_anderen_teil_ziehen(welt: dict[str, Any], client_for: Any) -> None:
    vorschlag = welt["meeting"].agenda_items.get(title="Radweg am Musterweg")
    _in_teil(welt, client_for, "vorsitz", vorschlag, "internal")
    vorschlag.refresh_from_db()
    assert vorschlag.visibility == "public" and vorschlag.proposal_status == "proposed"


def test_ziehen_in_den_anderen_teil_gilt_nach_dem_versand_als_aenderung(welt: dict[str, Any], client_for: Any) -> None:
    basis._einladen(welt, client_for)
    assert "Tagesordnung geändert." not in _html(_seite(welt, client_for, "vorsitz"))
    _in_teil(welt, client_for, "vorsitz", welt["haupt"], "internal")
    assert "Tagesordnung geändert." in _html(_seite(welt, client_for, "vorsitz"))


def test_aenderungshistorie_sperrt_top_nach_dem_wechsel_in_den_nichtoeffentlichen_teil(
    welt: dict[str, Any], client_for: Any, make_member: Any
) -> None:
    lesend = make_member(welt["org"], ["faction.view_public", "faction.view_audit"], email="olga@example.org")
    url = reverse("work:faction_audit", kwargs={"org_slug": welt["org"].slug})
    titel = "Grundstück am Nordring"
    top = FactionAgendaItem.objects.create(
        meeting=welt["meeting"], title=titel, number="3", visibility="public", order=9
    )
    assert titel in _html(client_for(lesend.user).get(url))

    _in_teil(welt, client_for, "vorsitz", top, "internal")

    html = _html(client_for(lesend.user).get(url))
    assert titel not in html and "Gesperrte Information" in html
    # Vereidigte lesen den Verlauf weiter
    assert titel in _html(client_for(welt["vorsitz"].user).get(url))


def test_neuer_titel_beim_wechsel_im_panel_steht_gesperrt_im_verlauf(welt: dict[str, Any], client_for: Any) -> None:
    meeting, termine = welt["meeting"], welt["meeting"].agenda_items.get(title="Termine")
    url = reverse(
        "work:faction_item_panel_action",
        kwargs={"org_slug": welt["org"].slug, "meeting_id": meeting.pk, "item_id": termine.pk},
    )
    titel = "Vertrauliche Personalsache"
    client_for(welt["vorsitz"].user).post(url, {"action": "update", "title": titel, "visibility": "internal"})

    termine.refresh_from_db()
    assert termine.title == titel and termine.visibility == "internal" and termine.number == "NÖ 1"
    eintraege = list(FactionAuditLog.objects.filter(object_id=termine.pk, object_repr__contains=titel))
    assert eintraege and all(eintrag.is_internal for eintrag in eintraege)


def test_tagesordnung_nennt_den_teil_je_liste(welt: dict[str, Any], client_for: Any) -> None:
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert 'data-teil="public" data-ziehbar' in html and 'data-teil="internal" data-ziehbar' in html


def test_ziehen_ans_ende_und_ungueltige_stelle(welt: dict[str, Any], client_for: Any) -> None:
    meeting, haupt = welt["meeting"], welt["haupt"]
    for position in ("99", "abc"):
        _post(
            welt,
            client_for,
            "vorsitz",
            {"action": "move_item", "item_id": str(haupt.pk), "direction": "to", "position": position},
            **_neu(),
        )
    assert _oben(meeting) == ["Termine", "Beschlüsse"]


def test_ziehen_nur_mit_recht(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    termine = FactionAgendaItem.objects.get(meeting=meeting, title="Termine")
    antwort = _post(
        welt, client_for, "rat", {"action": "move_item", "item_id": str(termine.pk), "direction": "to", "position": "0"}
    )
    assert antwort.status_code == 403
    assert _oben(meeting) == ["Beschlüsse", "Termine"]


# ---------------------------------------------------------------------------
# 1a. Ebene: TOP zum Unterpunkt, Unterpunkt zum eigenen TOP (Issue #1285)
# ---------------------------------------------------------------------------


def _ziehen(
    welt: dict[str, Any],
    client_for: Any,
    item: FactionAgendaItem,
    eltern: Any = None,
    pos: str = "0",
    rolle: str = "vorsitz",
    **mehr: str,
) -> Any:
    daten = {
        "action": "move_item",
        "item_id": str(item.pk),
        "direction": "to",
        "position": pos,
        "parent_id": str(eltern.pk) if eltern is not None else "",
        **mehr,
    }
    return _post(welt, client_for, rolle, daten, **_neu())


def _nummern(meeting: FactionMeeting) -> dict[str, str]:
    return dict(meeting.agenda_items.filter(proposal_status="active").values_list("title", "number"))


def _richtung(welt: dict[str, Any], client_for: Any, item: FactionAgendaItem, richtung: str) -> Any:
    daten = {"action": "move_item", "item_id": str(item.pk), "direction": richtung}
    return _post(welt, client_for, "vorsitz", daten, **_neu())


def test_ziehen_macht_top_zum_unterpunkt(welt: dict[str, Any], client_for: Any) -> None:
    meeting, haupt = welt["meeting"], welt["haupt"]
    termine = meeting.agenda_items.get(title="Termine")

    assert _ziehen(welt, client_for, termine, haupt, "0").status_code == 200
    termine.refresh_from_db()
    assert termine.parent_id == haupt.pk and termine.visibility == "public"
    nummern = _nummern(meeting)
    assert (nummern["Beschlüsse"], nummern["Termine"], nummern["Feuerwehrbedarfsplan"]) == ("1", "1.1", "1.2")
    assert _oben(meeting) == ["Beschlüsse"]

    # Hinter den bestehenden Unterpunkt
    _ziehen(welt, client_for, termine, haupt, "1")
    nummern = _nummern(meeting)
    assert (nummern["Feuerwehrbedarfsplan"], nummern["Termine"]) == ("1.1", "1.2")
    # Die Seite zeigt ihn als Unterpunkt mit Griff
    html = _html(_seite(welt, client_for, "vorsitz", ansicht="tagesordnung"))
    assert f'data-eltern="{haupt.pk}"' in html and f'data-griff="{termine.pk}"' in html


def test_unterpunkt_wird_eigener_top_oder_unterpunkt_eines_anderen(welt: dict[str, Any], client_for: Any) -> None:
    meeting, haupt = welt["meeting"], welt["haupt"]
    unter = meeting.agenda_items.get(title="Feuerwehrbedarfsplan")
    termine = meeting.agenda_items.get(title="Termine")

    _ziehen(welt, client_for, unter, None, "1")
    unter.refresh_from_db()
    assert unter.parent_id is None
    assert _oben(meeting) == ["Beschlüsse", "Feuerwehrbedarfsplan", "Termine"]
    assert _nummern(meeting)["Termine"] == "3"

    _ziehen(welt, client_for, unter, termine, "0")
    unter.refresh_from_db()
    assert unter.parent_id == termine.pk
    nummern = _nummern(meeting)
    assert (nummern["Termine"], nummern["Feuerwehrbedarfsplan"]) == ("2", "2.1")
    assert not haupt.children.exists()


def test_nur_eine_ebene_und_nicht_unter_sich_selbst(welt: dict[str, Any], client_for: Any) -> None:
    meeting, haupt = welt["meeting"], welt["haupt"]
    termine = meeting.agenda_items.get(title="Termine")
    unter = meeting.agenda_items.get(title="Feuerwehrbedarfsplan")

    # Ein TOP mit Unterpunkten wird kein Unterpunkt
    antwort = _ziehen(welt, client_for, haupt, termine)
    assert antwort.status_code == 400 and "eigene Unterpunkte" in antwort.content.decode()
    # Kein Unterpunkt unter einem Unterpunkt, keiner unter sich selbst
    assert _ziehen(welt, client_for, termine, unter).status_code == 400
    assert _ziehen(welt, client_for, termine, termine).status_code == 400
    # Unter einen offenen Vorschlag auch nicht
    vorschlag = meeting.agenda_items.get(title="Radweg am Musterweg")
    assert _ziehen(welt, client_for, termine, vorschlag).status_code == 403
    # Unter den Genehmigungs-TOP kommt nichts
    genehmigung = FactionAgendaItem.objects.create(
        meeting=meeting, title="Tagesordnung festlegen", number="1", order=0, is_approval_item=True
    )
    assert _ziehen(welt, client_for, termine, genehmigung).status_code == 400
    termine.refresh_from_db()
    haupt.refresh_from_db()
    assert termine.parent_id is None and haupt.parent_id is None


def test_unterpunkt_eines_noe_tops_wird_nichtoeffentlich(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    termine = meeting.agenda_items.get(title="Termine")
    geheim = meeting.agenda_items.get(title=basis.NOE_TITEL)

    # Auch ohne Angabe des Teils: Unter einem NÖ-TOP ist der Unterpunkt nichtöffentlich
    assert _ziehen(welt, client_for, termine, geheim).status_code == 200
    termine.refresh_from_db()
    assert termine.visibility == "internal" and termine.parent_id == geheim.pk and termine.number == "NÖ 1.1"
    assert "Termine" not in _html(_seite(welt, client_for, "sachkundig"))


def test_unterpunkt_unter_noe_top_nur_fuer_vereidigte(welt: dict[str, Any], client_for: Any, make_member: Any) -> None:
    welt["verwaltend"] = make_member(welt["org"], ["faction.view_public", "faction.manage"], email="nina@example.org")
    meeting = welt["meeting"]
    termine = meeting.agenda_items.get(title="Termine")
    geheim = meeting.agenda_items.get(title=basis.NOE_TITEL)
    assert _ziehen(welt, client_for, termine, geheim, rolle="verwaltend").status_code == 403
    termine.refresh_from_db()
    assert termine.parent_id is None and termine.visibility == "public"


def test_noe_unterpunkt_eines_oeffentlichen_tops_bleibt_nichtoeffentlich(welt: dict[str, Any], client_for: Any) -> None:
    meeting, haupt = welt["meeting"], welt["haupt"]
    personal = FactionAgendaItem.objects.create(
        meeting=meeting, title="Personalie", number="1.2", visibility="internal", order=7, parent=haupt
    )
    termine = meeting.agenda_items.get(title="Termine")
    # Unter einen anderen öffentlichen TOP gezogen: bleibt nichtöffentlich
    _ziehen(welt, client_for, personal, termine)
    personal.refresh_from_db()
    assert personal.parent_id == termine.pk and personal.visibility == "internal" and personal.number == "2.1"
    # Ausgerückt ohne Wahl des öffentlichen Teils: eigener TOP am Ende des nichtöffentlichen Teils
    assert _richtung(welt, client_for, personal, "out").status_code == 200
    personal.refresh_from_db()
    assert personal.parent_id is None and personal.visibility == "internal" and personal.number == "NÖ 2"
    # Mit ausdrücklicher Wahl (Rückfrage im Browser) wird er öffentlich
    _ziehen(welt, client_for, personal, None, "1", teil="public")
    personal.refresh_from_db()
    assert personal.visibility == "public" and personal.number == "2"


def test_alter_oeffentlich_gespeicherter_unterpunkt_eines_noe_tops_bleibt_nichtoeffentlich(
    welt: dict[str, Any], client_for: Any
) -> None:
    """Altbestand: Unterpunkte eines NÖ-TOPs gelten als nichtöffentlich, auch wenn sie öffentlich gespeichert sind."""
    meeting = welt["meeting"]
    geheim = meeting.agenda_items.get(title=basis.NOE_TITEL)
    alt = FactionAgendaItem.objects.create(
        meeting=meeting, title="Kaufpreis", number="NÖ 1.1", visibility="public", order=7, parent=geheim
    )
    assert _richtung(welt, client_for, alt, "out").status_code == 200
    alt.refresh_from_db()
    assert alt.parent_id is None and alt.visibility == "internal" and alt.number == "NÖ 2"
    assert "Kaufpreis" not in _html(_seite(welt, client_for, "sachkundig"))

    # Gezogen innerhalb des NÖ-Teils, ohne Wahl des öffentlichen Teils: bleibt nichtöffentlich
    alt.parent = geheim
    alt.visibility = "public"
    alt.save()
    _ziehen(welt, client_for, alt, None, "0")
    alt.refresh_from_db()
    assert alt.parent_id is None and alt.visibility == "internal" and alt.number == "NÖ 1"


def test_ungueltige_stelle_verschiebt_nichts(welt: dict[str, Any], client_for: Any) -> None:
    meeting, haupt = welt["meeting"], welt["haupt"]
    termine = meeting.agenda_items.get(title="Termine")
    _ziehen(welt, client_for, termine, haupt, "abc")
    termine.refresh_from_db()
    assert termine.parent_id is None and termine.number == "2"


def test_pfeiltasten_ruecken_ein_und_aus(welt: dict[str, Any], client_for: Any) -> None:
    meeting, haupt = welt["meeting"], welt["haupt"]
    termine = meeting.agenda_items.get(title="Termine")
    presse = FactionAgendaItem.objects.create(meeting=meeting, title="Presse", number="3", visibility="public", order=8)

    # Rechts: letzter Unterpunkt des TOPs darüber
    assert _richtung(welt, client_for, termine, "in").status_code == 200
    termine.refresh_from_db()
    assert termine.parent_id == haupt.pk and termine.number == "1.2"
    assert _nummern(meeting)["Presse"] == "2"

    # Unterpunkte hoch und runter unter ihresgleichen
    _richtung(welt, client_for, termine, "up")
    nummern = _nummern(meeting)
    assert (nummern["Termine"], nummern["Feuerwehrbedarfsplan"]) == ("1.1", "1.2")
    _richtung(welt, client_for, termine, "down")
    assert _nummern(meeting)["Termine"] == "1.2"

    # Links: eigener TOP direkt hinter dem bisherigen
    assert _richtung(welt, client_for, termine, "out").status_code == 200
    assert _oben(meeting) == ["Beschlüsse", "Termine", "Presse"]
    assert _nummern(meeting)["Presse"] == "3"

    # Der erste TOP hat keinen darüber; ein TOP mit Unterpunkten rückt nicht ein
    antwort = _richtung(welt, client_for, haupt, "in")
    assert antwort.status_code == 400 and "keiner" in antwort.content.decode()
    FactionAgendaItem.objects.create(
        meeting=meeting, title="Pressemitteilung", number="3.1", visibility="public", order=9, parent=presse
    )
    assert _richtung(welt, client_for, presse, "in").status_code == 400
    presse.refresh_from_db()
    assert presse.parent_id is None


def test_menue_bietet_ein_und_ausruecken(welt: dict[str, Any], client_for: Any) -> None:
    html = _html(_seite(welt, client_for, "vorsitz", ansicht="tagesordnung"))
    assert "Zum Unterpunkt von TOP 1 machen" in html
    assert "Zum eigenen TOP machen" in html
    # Der erste TOP und einer mit Unterpunkten rücken nicht ein: nur „Termine“ bietet es an
    assert html.count("Zum Unterpunkt von TOP") == 1
    assert "Zum eigenen TOP" not in _html(_seite(welt, client_for, "rat", ansicht="tagesordnung"))


def test_ebene_wechseln_gilt_nach_dem_versand_als_aenderung(welt: dict[str, Any], client_for: Any) -> None:
    basis._einladen(welt, client_for)
    assert "Tagesordnung geändert." not in _html(_seite(welt, client_for, "vorsitz"))
    _richtung(welt, client_for, welt["meeting"].agenda_items.get(title="Termine"), "in")
    assert "Tagesordnung geändert." in _html(_seite(welt, client_for, "vorsitz"))


def test_stelle_zaehlt_nur_sichtbare_geschwister() -> None:
    """Nicht-Vereidigte sehen nichtöffentliche Unterpunkte nicht; ihre Stelle zählt ohne sie."""
    from apps.work.faction.agenda import _insert_index

    geschwister = [mock.Mock(sichtbar=True), mock.Mock(sichtbar=False), mock.Mock(sichtbar=True)]

    def sichtbar(s: Any) -> bool:
        return bool(s.sichtbar)

    assert _insert_index(geschwister, 0, sichtbar) == 0
    assert _insert_index(geschwister, 1, sichtbar) == 2
    assert _insert_index(geschwister, 2, sichtbar) == 3
    assert _insert_index(geschwister, 9, None) == 3
    assert _insert_index(geschwister, None, sichtbar) == 3
    assert _insert_index([mock.Mock(sichtbar=False)], 0, sichtbar) == 1


def test_griff_nur_fuer_die_verwaltung(welt: dict[str, Any], client_for: Any) -> None:
    assert 'data-griff="' in _html(_seite(welt, client_for, "vorsitz"))
    assert "griff-hilfe" in _html(_seite(welt, client_for, "vorsitz"))
    assert 'data-griff="' not in _html(_seite(welt, client_for, "rat"))


# ---------------------------------------------------------------------------
# 2. Sofort speichern (JSON an faction_item_fields)
# ---------------------------------------------------------------------------


def _felder_url(welt: dict[str, Any], item: Any, org: Organization | None = None) -> str:
    return reverse(
        "work:faction_item_fields",
        kwargs={"org_slug": (org or welt["org"]).slug, "meeting_id": item.meeting_id, "item_id": item.pk},
    )


def _json(client: Any, url: str, daten: Any) -> Any:
    return client.post(
        url,
        data=json.dumps(daten),
        content_type="application/json",
        HTTP_ACCEPT="application/json",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )


def test_sofort_speichern_titel_und_beschreibung(welt: dict[str, Any], client_for: Any) -> None:
    haupt = welt["haupt"]
    client = client_for(welt["vorsitz"].user)
    antwort = _json(client, _felder_url(welt, haupt), {"title": "  Beschlüsse und Anträge "})
    assert antwort.status_code == 200 and antwort.json() == {
        "ok": True,
        "title": "Beschlüsse und Anträge",
        "number": "1",
    }
    haupt.refresh_from_db()
    assert haupt.title == "Beschlüsse und Anträge"
    _json(client, _felder_url(welt, haupt), {"description": "Hilfsfrist im Norden"})
    haupt.refresh_from_db()
    assert haupt.get_description_decrypted() == "Hilfsfrist im Norden"
    assert haupt.title == "Beschlüsse und Anträge"
    _json(client, _felder_url(welt, haupt), {"description": ""})
    haupt.refresh_from_db()
    assert not haupt.description_encrypted


@pytest.mark.parametrize(
    "daten,meldung",
    [
        ({"title": "   "}, "Der Titel darf nicht leer sein."),
        ({"title": "x" * 501}, "Der Titel darf höchstens 500 Zeichen lang sein."),
        ([1, 2], "Ungültige Eingabe"),
    ],
)
def test_sofort_speichern_lehnt_ungueltiges_ab(welt: dict[str, Any], client_for: Any, daten: Any, meldung: str) -> None:
    antwort = _json(client_for(welt["vorsitz"].user), _felder_url(welt, welt["haupt"]), daten)
    assert antwort.status_code == 400 and antwort.json() == {"error": meldung}
    welt["haupt"].refresh_from_db()
    assert welt["haupt"].title == "Beschlüsse"


def test_sofort_speichern_nur_mit_recht(welt: dict[str, Any], client_for: Any, make_member: Any) -> None:
    meeting = welt["meeting"]
    # Mitglied ohne Verwaltung
    antwort = _json(client_for(welt["rat"].user), _felder_url(welt, welt["haupt"]), {"title": "Neu"})
    assert antwort.status_code == 403 and "error" in antwort.json()
    # Verwaltung ohne Vereidigung: kein nichtöffentlicher TOP
    verwaltung = make_member(welt["org"], ["faction.view_public", "faction.manage"], email="nv@example.org")
    noe = FactionAgendaItem.objects.get(meeting=meeting, visibility="internal")
    antwort = _json(client_for(verwaltung.user), _felder_url(welt, noe), {"title": "Neu"})
    assert antwort.status_code == 403
    noe.refresh_from_db()
    assert noe.title != "Neu"
    # Genehmigungs-TOP behält seinen Titel
    genehmigung = FactionAgendaItem.objects.create(
        meeting=meeting, title="Tagesordnung festlegen", number="1", visibility="public", order=0, is_approval_item=True
    )
    antwort = _json(client_for(welt["vorsitz"].user), _felder_url(welt, genehmigung), {"title": "Neu"})
    assert antwort.status_code == 403
    # Abgeschlossene Sitzung
    FactionMeeting.objects.filter(pk=meeting.pk).update(status="completed")
    antwort = _json(client_for(welt["vorsitz"].user), _felder_url(welt, welt["haupt"]), {"title": "Neu"})
    assert antwort.status_code == 403


def test_sofort_speichern_mandantentrennung(welt: dict[str, Any], client_for: Any, make_member: Any) -> None:
    from apps.common.tests.factories import OrganizationFactory

    fremde: Organization = cast(Any, OrganizationFactory)(name="Andere Fraktion", slug="andere-fraktion")
    fremd = make_member(fremde, [], email="fremd@example.org", is_admin=True)
    # Über die eigene Organisation auf die Sitzung der anderen: nicht gefunden
    antwort = _json(client_for(fremd.user), _felder_url(welt, welt["haupt"], org=fremde), {"title": "Fremd"})
    assert antwort.status_code == 404
    welt["haupt"].refresh_from_db()
    assert welt["haupt"].title == "Beschlüsse"


def test_sofort_speichern_seite_hat_felder_und_konfiguration(welt: dict[str, Any], client_for: Any) -> None:
    html = _html(_seite(welt, client_for, "vorsitz", ansicht="tagesordnung", top=str(welt["haupt"].pk)))
    assert 'x-data="fsTagesordnung"' in html and 'id="fs-to-config"' in html
    assert 'data-top-feld="title"' in html and 'data-top-feld="description"' in html
    assert 'data-testid="fs-speicherstand"' in html
    # Konfiguration nennt Kennungen, keine Namen
    assert str(welt["vorsitz"].user.pk) in html and 'vera@example.org"' not in html.split('id="fs-to-config"')[1][:400]


# ---------------------------------------------------------------------------
# 3. Aus Ratssitzung übernehmen
# ---------------------------------------------------------------------------


@pytest.fixture
def ris(welt: dict[str, Any]) -> dict[str, Any]:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    eigene = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt A")
    fremde = OParlBody.objects.create(external_id="https://ris.example.org/body/2", source=source, name="Stadt B")
    org = welt["org"]
    org.body = eigene
    org.save(update_fields=["body"])
    rat = OParlMeeting.objects.create(
        external_id="https://ris.example.org/meeting/1",
        body=eigene,
        name="Rat der Stadt",
        start=welt["meeting"].start + timedelta(days=2),
    )
    punkte = {}
    for nr, name, oeffentlich in (
        ("1", "Eröffnung", True),
        ("2", "Jugendbeirat", True),
        ("3", "Grundstücksverkauf Hafen", False),
    ):
        punkte[nr] = OParlAgendaItem.objects.create(
            external_id=f"https://ris.example.org/item/{nr}",
            meeting=rat,
            number=nr,
            order=int(nr),
            name=name,
            public=oeffentlich,
        )
    vorlage = OParlPaper.objects.create(
        external_id="https://ris.example.org/paper/1", body=eigene, name="Jugendbeirat", reference="A/2026/011"
    )
    OParlConsultation.objects.create(
        external_id="https://ris.example.org/consultation/1",
        body=eigene,
        paper=vorlage,
        agenda_item_external_id=punkte["2"].external_id,
    )
    fremde_sitzung = OParlMeeting.objects.create(
        external_id="https://ris.example.org/meeting/2", body=fremde, name="Fremder Rat", start=rat.start
    )
    fremder_punkt = OParlAgendaItem.objects.create(
        external_id="https://ris.example.org/item/f", meeting=fremde_sitzung, number="1", name="Fremd"
    )
    return {"rat": rat, "punkte": punkte, "vorlage": vorlage, "fremde_sitzung": fremde_sitzung, "fremd": fremder_punkt}


def _uebernehmen(
    welt: dict[str, Any], client_for: Any, rolle: str, ris_meeting: Any, punkte: list[Any], **extra: str
) -> Any:
    return client_for(welt[rolle].user).post(
        _aktion(welt),
        {
            "action": "ris_uebernehmen",
            "ris_meeting_id": str(ris_meeting.pk),
            "punkt": [str(p.pk) for p in punkte],
            **extra,
        },
        **_neu(),
    )


def _toast(antwort: Any) -> str:
    return str(json.loads(antwort["HX-Trigger"])["show-toast"]["message"])


def test_ris_auswahl_zeigt_punkte_mit_vorlage(welt: dict[str, Any], client_for: Any, ris: dict[str, Any]) -> None:
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert "Aus Ratssitzung übernehmen …" in html and 'id="fs-ris"' in html
    antwort = _post(welt, client_for, "vorsitz", {"action": "ris_auswahl"}, **_neu())
    inhalt = _html(antwort)
    assert antwort.status_code == 200
    assert "Rat der Stadt" in inhalt and "Jugendbeirat" in inhalt and "Vorlage A/2026/011" in inhalt
    assert "Fremder Rat" not in inhalt
    # Vorsitz ist vereidigt: nichtöffentlicher Punkt sichtbar, mit Hinweis
    assert "Grundstücksverkauf Hafen" in inhalt and "kommt in den nichtöffentlichen Teil" in inhalt
    # Vorschlag zum Einordnen: unter „Beschlüsse“
    assert f'value="{welt["haupt"].pk}" selected' in inhalt


def test_ris_uebernehmen_als_unterpunkte_ohne_dubletten(
    welt: dict[str, Any], client_for: Any, ris: dict[str, Any]
) -> None:
    meeting, haupt = welt["meeting"], welt["haupt"]
    punkte = ris["punkte"]
    antwort = _uebernehmen(welt, client_for, "vorsitz", ris["rat"], [punkte["2"]], parent_id=str(haupt.pk))
    assert antwort.status_code == 200
    assert _toast(antwort) == "1 TOP aus der Ratssitzung übernommen."
    neu = FactionAgendaItem.objects.get(meeting=meeting, related_agenda_item=punkte["2"])
    assert neu.parent_id == haupt.pk and neu.number == "1.2" and neu.visibility == "public"
    assert list(neu.related_papers.all()) == [ris["vorlage"]]
    meeting.refresh_from_db()
    assert meeting.related_meeting_id == ris["rat"].pk
    # Erneut übernehmen: keine Dublette
    antwort = _uebernehmen(welt, client_for, "vorsitz", ris["rat"], [punkte["2"], punkte["1"]])
    assert FactionAgendaItem.objects.filter(meeting=meeting, related_agenda_item=punkte["2"]).count() == 1
    assert FactionAgendaItem.objects.filter(meeting=meeting, related_agenda_item=punkte["1"]).count() == 1
    # Auswahl zeigt den übernommenen als gesperrt mit Nummer
    inhalt = _html(_post(welt, client_for, "vorsitz", {"action": "ris_auswahl"}, **_neu()))
    assert "schon übernommen als TOP 1.2" in inhalt
    # Nur Dubletten: freundliche Meldung, nichts angelegt
    antwort = _uebernehmen(welt, client_for, "vorsitz", ris["rat"], [punkte["2"]])
    assert "stehen schon auf der Tagesordnung" in _toast(antwort)


def test_ris_nichtoeffentlich_nur_fuer_vereidigte(
    welt: dict[str, Any], client_for: Any, ris: dict[str, Any], make_member: Any
) -> None:
    meeting = welt["meeting"]
    noe = ris["punkte"]["3"]
    antwort = _uebernehmen(welt, client_for, "vorsitz", ris["rat"], [noe])
    assert antwort.status_code == 200
    item = FactionAgendaItem.objects.get(meeting=meeting, related_agenda_item=noe)
    assert item.visibility == "internal" and item.parent_id is None and item.number == "NÖ 2"

    verwaltung = make_member(welt["org"], ["faction.view_public", "faction.manage"], email="nv@example.org")
    welt["nv"] = verwaltung
    inhalt = _html(_post(welt, client_for, "nv", {"action": "ris_auswahl"}, **_neu()))
    assert "Grundstücksverkauf Hafen" not in inhalt and "1 nichtöffentlicher Punkt" in inhalt
    item.delete()
    antwort = _uebernehmen(welt, client_for, "nv", ris["rat"], [noe])
    assert antwort.status_code == 400
    assert not FactionAgendaItem.objects.filter(meeting=meeting, related_agenda_item=noe).exists()


def test_ris_nur_mit_recht_und_eigener_kommune(welt: dict[str, Any], client_for: Any, ris: dict[str, Any]) -> None:
    assert _post(welt, client_for, "rat", {"action": "ris_auswahl"}, **_neu()).status_code == 403
    assert _uebernehmen(welt, client_for, "rat", ris["rat"], [ris["punkte"]["2"]]).status_code == 403
    assert "Aus Ratssitzung übernehmen" not in _html(_seite(welt, client_for, "rat"))
    # Sitzung einer fremden Kommune bzw. Punkt einer anderen Sitzung: nichts angelegt
    assert _uebernehmen(welt, client_for, "vorsitz", ris["fremde_sitzung"], [ris["fremd"]]).status_code == 400
    assert _uebernehmen(welt, client_for, "vorsitz", ris["rat"], [ris["fremd"]]).status_code == 400
    assert not FactionAgendaItem.objects.filter(related_agenda_item=ris["fremd"]).exists()
    # Fremde Sitzung in der Auswahl angefragt: es bleibt bei der eigenen
    inhalt = _html(
        _post(
            welt,
            client_for,
            "vorsitz",
            {"action": "ris_auswahl", "ris_meeting_id": str(ris["fremde_sitzung"].pk)},
            **_neu(),
        )
    )
    assert "Fremd" not in inhalt.replace("Fremder", "")


def test_ris_ohne_kommune_kein_knopf(welt: dict[str, Any], client_for: Any) -> None:
    assert not ratssitzung.moeglich(welt["meeting"])
    assert "Aus Ratssitzung übernehmen" not in _html(_seite(welt, client_for, "vorsitz"))


# ---------------------------------------------------------------------------
# 4. Offene erinnern
# ---------------------------------------------------------------------------


def _eingeladen(welt: dict[str, Any]) -> FactionMeeting:
    meeting: FactionMeeting = welt["meeting"]
    meeting.status = "invited"
    meeting.invitation_sent = True
    meeting.invitation_sent_at = timezone.now() - timedelta(hours=1)
    meeting.rsvp_enabled = True
    meeting.save()
    FactionAttendance.objects.filter(meeting=meeting, membership=welt["vorsitz"]).update(
        status="confirmed", responded_at=timezone.now()
    )
    return meeting


def test_offene_erinnern_nur_an_offene_mit_ergebnis_je_person(
    welt: dict[str, Any], client_for: Any, mailoutbox: Any
) -> None:
    meeting = _eingeladen(welt)
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert 'data-testid="fs-offene-erinnern"' in html and 'value="remind_open"' in html
    antwort = _post(welt, client_for, "vorsitz", {"action": "remind_open"})
    assert antwort.status_code == 302
    empfaenger = sorted(adresse for mail in mailoutbox for adresse in mail.to)
    assert empfaenger == ["klara@example.org", "martin@example.org"]
    assert all(mail.subject == "Bitte um Rückmeldung: Fraktionssitzung Nr. 24" for mail in mailoutbox)
    offen = FactionAttendance.objects.filter(meeting=meeting, status="invited")
    assert all(a.rsvp_reminder_mailed_at and not a.rsvp_reminder_error for a in offen)
    assert not FactionAttendance.objects.get(meeting=meeting, membership=welt["vorsitz"]).rsvp_reminder_mailed_at
    meeting.refresh_from_db()
    assert meeting.rsvp_reminder_sent_at is not None
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert "An alle ohne Rückmeldung erinnert am" in html and "erinnert " in html
    assert "Erneut erinnern können Sie ab" in html


def test_offene_erinnern_sperrfrist_gegen_mehrfachklick(welt: dict[str, Any], client_for: Any, mailoutbox: Any) -> None:
    _eingeladen(welt)
    _post(welt, client_for, "vorsitz", {"action": "remind_open"})
    _post(welt, client_for, "vorsitz", {"action": "remind_open"})
    assert len(mailoutbox) == 2
    with pytest.raises(vorbereiten.ErinnernNichtMoeglichError, match="Erneut erinnern können Sie ab"):
        vorbereiten.offene_erinnern(FactionMeeting.objects.get(pk=welt["meeting"].pk))
    # Nach Ablauf der Sperrfrist wieder möglich
    spaeter = timezone.now() + vorbereiten.ERINNERN_SPERRE + timedelta(minutes=1)
    ok, nicht = vorbereiten.offene_erinnern(FactionMeeting.objects.get(pk=welt["meeting"].pk), now=spaeter)
    assert (ok, nicht) == (2, 0)


def test_offene_erinnern_nur_mit_recht_und_zustand(welt: dict[str, Any], client_for: Any, mailoutbox: Any) -> None:
    meeting = _eingeladen(welt)
    assert _post(welt, client_for, "rat", {"action": "remind_open"}, HTTP_HX_REQUEST="true").status_code == 403
    assert 'data-testid="fs-offene-erinnern"' not in _html(_seite(welt, client_for, "rat"))
    # Ohne Zu- und Absagen bzw. vor der Einladung: nichts verschickt
    FactionMeeting.objects.filter(pk=meeting.pk).update(rsvp_enabled=False)
    _post(welt, client_for, "vorsitz", {"action": "remind_open"})
    FactionMeeting.objects.filter(pk=meeting.pk).update(rsvp_enabled=True, invitation_sent=False, status="planned")
    _post(welt, client_for, "vorsitz", {"action": "remind_open"})
    # Alle haben geantwortet
    FactionMeeting.objects.filter(pk=meeting.pk).update(invitation_sent=True, status="invited")
    FactionAttendance.objects.filter(meeting=meeting).update(status="declined")
    _post(welt, client_for, "vorsitz", {"action": "remind_open"})
    assert mailoutbox == []
    assert FactionMeeting.objects.get(pk=meeting.pk).rsvp_reminder_sent_at is None


def test_offene_erinnern_fehler_je_person(welt: dict[str, Any], client_for: Any) -> None:
    meeting = _eingeladen(welt)
    with mock.patch("apps.common.mail.send", side_effect=smtplib.SMTPRecipientsRefused({"x": (550, b"no")})):
        _post(welt, client_for, "vorsitz", {"action": "remind_open"})
    a = FactionAttendance.objects.get(meeting=meeting, membership=welt["rat"])
    assert a.rsvp_reminder_mailed_at is None and "abgelehnt" in a.rsvp_reminder_error
    assert "Erinnerung nicht verschickt." in _html(_seite(welt, client_for, "vorsitz"))


# ---------------------------------------------------------------------------
# 5. Testmail der Einladung
# ---------------------------------------------------------------------------


def test_testmail_mit_echtem_einladungstext_an_die_eigene_adresse(
    welt: dict[str, Any], client_for: Any, mailoutbox: Any
) -> None:
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert "Testmail an mich senden" in html and "Ihre erste Einladung über diesen Versandweg." in html
    antwort = _post(welt, client_for, "vorsitz", {"action": "test_invitation"}, **_neu())
    assert antwort.status_code == 200 and "Testmail an vera@example.org versendet" in _html(antwort)
    assert len(mailoutbox) == 1
    mail = mailoutbox[0]
    assert mail.to == ["vera@example.org"]
    assert mail.subject == "Testmail – Einladung: Fraktionssitzung Nr. 24"
    text = mail.body
    assert "Termine" in text and "Beschlüsse" in text
    # Vereidigt: mit nichtöffentlichem Teil, wie die echte Einladung
    assert "Grundstücksangelegenheit Flurstück 12" in text
    assert {a[0] for a in mail.attachments} == {"tagesordnung.pdf", "sitzung.ics"}
    # Die Sitzung bleibt unverändert, kein Versandergebnis
    meeting = FactionMeeting.objects.get(pk=welt["meeting"].pk)
    assert not meeting.invitation_sent
    assert not FactionAttendance.objects.filter(meeting=meeting, invitation_mailed_at__isnull=False).exists()


def test_testmail_nicht_vereidigt_ohne_noe_und_nur_mit_recht(
    welt: dict[str, Any], client_for: Any, make_member: Any, mailoutbox: Any
) -> None:
    assert _post(welt, client_for, "rat", {"action": "test_invitation"}, **_neu()).status_code == 403
    einlader = make_member(welt["org"], ["faction.view_public", "faction.invite"], email="ein@example.org")
    welt["einlader"] = einlader
    antwort = _post(welt, client_for, "einlader", {"action": "test_invitation"}, **_neu())
    assert antwort.status_code == 200
    assert "Grundstücksangelegenheit Flurstück 12" not in mailoutbox[0].body


def test_testmail_mehrfachklick_und_fehler(welt: dict[str, Any], client_for: Any, mailoutbox: Any) -> None:
    _post(welt, client_for, "vorsitz", {"action": "test_invitation"}, **_neu())
    zweite = _post(welt, client_for, "vorsitz", {"action": "test_invitation"}, **_neu())
    assert "gerade unterwegs" in _html(zweite) and len(mailoutbox) == 1
    cache.clear()
    with mock.patch("apps.common.mail.send", side_effect=OSError("down")):
        antwort = _post(welt, client_for, "vorsitz", {"action": "test_invitation"}, **_neu())
    assert 'class="fehler"' in _html(antwort) and "nicht erreichbar" in _html(antwort)
