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
from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.tenants.models import Organization
from apps.work.faction import ratssitzung, vorbereiten
from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionMeeting
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


def test_ziehen_nur_mit_recht_und_nur_oberste_ebene(welt: dict[str, Any], client_for: Any) -> None:
    meeting, haupt = welt["meeting"], welt["haupt"]
    termine = FactionAgendaItem.objects.get(meeting=meeting, title="Termine")
    antwort = _post(
        welt, client_for, "rat", {"action": "move_item", "item_id": str(termine.pk), "direction": "to", "position": "0"}
    )
    assert antwort.status_code == 403
    unter = FactionAgendaItem.objects.get(meeting=meeting, parent=haupt)
    _post(
        welt,
        client_for,
        "vorsitz",
        {"action": "move_item", "item_id": str(unter.pk), "direction": "to", "position": "0"},
        **_neu(),
    )
    assert _oben(meeting) == ["Beschlüsse", "Termine"]


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

    fremde = OrganizationFactory(name="Andere Fraktion", slug="andere-fraktion")
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
    meeting = welt["meeting"]
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
