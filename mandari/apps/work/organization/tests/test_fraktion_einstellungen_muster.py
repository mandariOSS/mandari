# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstellungen › Fraktionssitzungen nach dem Muster (Issue #1071, Schritt 4).

- Aufbau und Texte wie im Muster (einstellungen.html), alle bisherigen Wege bleiben (Gleichstand prüft
  test_einstellungen_3c.py; hier zusätzlich die Wege je Abschnitt).
- Sofort speichern: Jeder Abschnitt speichert per htmx nur seine Felder (204), ungültige Werte werden mit Grund
  abgelehnt (400) und nichts geändert; die Automatik je Reihe ebenso. Pflicht zum zweiten Faktor: kein Speichern,
  HX-Redirect für die Anzeige. Ohne JavaScript speichert das Formular wie bisher.
- Nur-Lesen für Mitglieder mit faction.view_public: Seite mit Hinweis und gesperrten Feldern, jede Änderung 403.
- Mandantentrennung: Reihen einer anderen Organisation lassen sich nicht ändern.
"""

from __future__ import annotations

import re
from datetime import date, time, timedelta
from typing import Any

import pytest
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse

from apps.common.tests.factories import OrganizationFactory
from apps.work.faction.models import FactionMeetingException, FactionMeetingSchedule, FactionStandardAgendaItem
from apps.work.organization import fraktion_einstellungen, services

pytestmark = pytest.mark.django_db

HTMX = {"HTTP_HX_REQUEST": "true"}


def settings_url(org: Any) -> str:
    return reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug})


@pytest.fixture
def org(org: Any) -> Any:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    return make_member(org, ["faction.manage", "faction.view_public", "protocols.publish"], email="vorsitz@example.org")


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view", "faction.view_public", "agenda.create"], email="mitglied@example.org")


@pytest.fixture
def reihe(org: Any) -> FactionMeetingSchedule:
    reihe = FactionMeetingSchedule.objects.create(
        organization=org,
        name="Wöchentliche Fraktionssitzung",
        weekday=0,
        time=time(18, 0),
        default_location="Fraktionsbüro",
        default_video_link="https://video.example.org/fraktion",
    )
    FactionMeetingException.objects.create(
        schedule=reihe,
        original_date=date.today() + timedelta(days=60),
        end_date=date.today() + timedelta(days=70),
        exception_type="cancelled",
        reason="Weihnachtspause",
    )
    return reihe


def _fraktion(org: Any) -> dict[str, Any]:
    org.refresh_from_db()
    return dict((org.settings or {}).get("faction", {}))


def _sofort(client: Any, org: Any, abschnitt: str, **daten: str) -> Any:
    return client.post(
        settings_url(org),
        {"section": "save_settings", "felder": fraktion_einstellungen.felder(abschnitt), **daten},
        **HTMX,
    )


# ---------------------------------------------------------------------------
# Aufbau wie im Muster
# ---------------------------------------------------------------------------


def test_aufbau_und_texte_wie_im_muster(org: Any, vorsitz: Any, reihe: Any, client_for: Any) -> None:
    FactionStandardAgendaItem.objects.create(organization=org, title="Termine", visibility="public", order=1)
    seite = client_for(vorsitz.user).get(settings_url(org)).content.decode()
    reihenfolge = [
        "Sitzungsreihe",
        "Standard-Tagesordnung",
        "Wer TOPs einbringt",
        "Einladung",
        "Erinnerung zum Eintragen von TOPs",
        "Protokoll versenden",
        "Öffentliche Protokolle",
        "Weitere Regeln",
        "Erster TOP",
    ]
    treffer = [re.search(rf"<h2[^>]*>{titel}[ <]", seite) for titel in reihenfolge]
    stellen = [t.start() if t else -1 for t in treffer]
    assert -1 not in stellen
    assert stellen == sorted(stellen), list(zip(reihenfolge, stellen, strict=True))
    for text in (
        "Work speichert Änderungen sofort.",
        "Gespeichert",
        "Zu einem festen Zeitpunkt einladen, ohne Freigabe",
        "Pausen und Ausfallregeln …",
        "Weihnachtspause",
        "Gelöschte Termine legt Work nicht wieder an.",
        "fest, mit dem Genehmigungsablauf für das letzte Protokoll",
        "Erst nach Freigabe durch den Vorstand",
        "Wer nicht antwortet, gilt als",
        "https://video.example.org/fraktion",
    ):
        assert text in seite, text
    # Kein Speichern-Knopf mehr außer dem Rückweg ohne JavaScript
    assert seite.count("Einstellungen speichern") == 1
    assert (
        '<noscript><p><button type="submit" class="wf-knopf">Einstellungen speichern</button></p></noscript>' in seite
    )
    assert 'role="switch"' in seite
    assert 'data-autosave="einst"' in seite


def test_jeder_abschnitt_speichert_seine_felder_und_alle_felder_sind_erfasst(
    org: Any, vorsitz: Any, client_for: Any
) -> None:
    abschnitte = fraktion_einstellungen.ABSCHNITT_FELDER
    alle = [f for felder in abschnitte.values() for f in felder]
    assert len(alle) == len(set(alle))
    assert set(alle) == services.FACTION_SETTINGS_FIELDS
    seite = client_for(vorsitz.user).get(settings_url(org)).content.decode()
    angaben = set(re.findall(r'"felder": "([a-z_,]+)"', seite))
    assert angaben == {fraktion_einstellungen.felder(a) for a in abschnitte}
    # Jedes Feld steht im Abschnitt, der es speichert
    for abschnitt, felder in abschnitte.items():
        teil = seite.split(f'"felder": "{fraktion_einstellungen.felder(abschnitt)}"', 1)[1].split("</section>", 1)[0]
        for feld in felder:
            assert f'name="{feld}"' in teil, (abschnitt, feld)


# ---------------------------------------------------------------------------
# Sofort speichern
# ---------------------------------------------------------------------------


def test_abschnitt_speichert_nur_seine_felder(org: Any, vorsitz: Any, client_for: Any) -> None:
    client = client_for(vorsitz.user)
    services.save_faction_settings(org, vorsitz, {"invitation_lead_hours": "48", "agenda_reminder_enabled": "on"})
    # Das Formular schickt alle Felder mit; gespeichert wird nur der Abschnitt „Erinnerung“
    antwort = _sofort(client, org, "erinnerung", agenda_reminder_hours="12", invitation_lead_hours="5")
    assert antwort.status_code == 204
    gespeichert = _fraktion(org)
    assert gespeichert["agenda_reminder_enabled"] is False
    assert gespeichert["agenda_reminder_hours"] == 12
    assert gespeichert["invitation_lead_hours"] == 48
    # Schalter, die nicht zum Abschnitt gehören, bleiben, wie sie waren
    assert gespeichert["auto_create_approval_item"] is False
    assert not list(get_messages(antwort.wsgi_request))


@pytest.mark.parametrize(
    "abschnitt,daten,erwartet",
    [
        (
            "einladung",
            {"invitation_dispatch": "approval", "invitation_lead_hours": "72", "invitation_mode": "opt_out"},
            {"invitation_dispatch": "approval", "invitation_lead_hours": 72, "invitation_mode": "opt_out"},
        ),
        (
            "protokoll",
            {
                "protocol_dispatch": "after_approval",
                "protocol_dispatch_delay_hours": "24",
                "protocol_revision_safe": "on",
            },
            {"protocol_dispatch": "after_approval", "protocol_revision_safe": True, "require_protocol_approval": False},
        ),
        (
            "erster_top",
            {"auto_create_approval_item": "on", "first_agenda_title_with_previous": "  Genehmigung {datum}  "},
            {"auto_create_approval_item": True, "first_agenda_title_with_previous": "Genehmigung {datum}"},
        ),
        ("ablauf", {"quorum_rule": "majority"}, {"link_previous_meeting": False, "quorum_rule": "majority"}),
    ],
)
def test_sofort_speichern_je_abschnitt(
    org: Any, vorsitz: Any, client_for: Any, abschnitt: str, daten: dict[str, str], erwartet: dict[str, Any]
) -> None:
    antwort = _sofort(client_for(vorsitz.user), org, abschnitt, **daten)
    assert antwort.status_code == 204
    gespeichert = _fraktion(org)
    assert {k: gespeichert.get(k) for k in erwartet} == erwartet


def test_oeffentliche_protokolle_sofort(org: Any, vorsitz: Any, client_for: Any) -> None:
    assert _sofort(client_for(vorsitz.user), org, "oeffentlich", publish_protocols="on").status_code == 204
    org.refresh_from_db()
    assert org.publish_protocols is True


def test_oeffentliche_protokolle_ohne_recht_abgelehnt(org: Any, make_member: Any, client_for: Any) -> None:
    leitung = make_member(org, ["faction.manage"], email="leitung@example.org")
    seite = client_for(leitung.user).get(settings_url(org)).content.decode()
    assert re.search(r'name="publish_protocols" id="publish_protocols" disabled', seite)
    antwort = _sofort(client_for(leitung.user), org, "oeffentlich", publish_protocols="on")
    assert antwort.status_code == 400
    assert "Protokolle veröffentlichen" in antwort.content.decode()
    org.refresh_from_db()
    assert org.publish_protocols is False


@pytest.mark.parametrize(
    "abschnitt,daten,grund",
    [
        (
            "einladung",
            {"invitation_dispatch": "automatic", "invitation_lead_hours": "0", "invitation_mode": "opt_in"},
            "Einladungsvorlauf",
        ),
        (
            "einladung",
            {"invitation_dispatch": "automatic", "invitation_lead_hours": "abc", "invitation_mode": "opt_in"},
            "Einladungsvorlauf",
        ),
        (
            "einladung",
            {"invitation_dispatch": "irgendwann", "invitation_lead_hours": "72", "invitation_mode": "opt_in"},
            "Ungültige Auswahl",
        ),
        ("erinnerung", {"agenda_reminder_enabled": "on", "agenda_reminder_hours": "999"}, "Erinnerung"),
        ("protokoll", {"protocol_dispatch": "off", "protocol_dispatch_delay_hours": "-1"}, "Protokollversand"),
    ],
)
def test_ungueltige_werte_abgelehnt_und_nichts_geaendert(
    org: Any, vorsitz: Any, client_for: Any, abschnitt: str, daten: dict[str, str], grund: str
) -> None:
    vorher = _fraktion(org)
    antwort = _sofort(client_for(vorsitz.user), org, abschnitt, **daten)
    assert antwort.status_code == 400
    assert grund in antwort.content.decode()
    assert antwort["Content-Type"].startswith("text/plain")
    assert _fraktion(org) == vorher


def test_unbekanntes_feld_abgelehnt(org: Any, vorsitz: Any, client_for: Any) -> None:
    antwort = client_for(vorsitz.user).post(
        settings_url(org), {"section": "save_settings", "felder": "is_active", "is_active": ""}, **HTMX
    )
    assert antwort.status_code == 400
    org.refresh_from_db()
    assert org.is_active is True


def test_ohne_javascript_speichert_das_ganze_formular_wie_bisher(org: Any, vorsitz: Any, client_for: Any) -> None:
    antwort = client_for(vorsitz.user).post(
        settings_url(org), {"invitation_lead_hours": "5000", "agenda_reminder_enabled": "on"}
    )
    assert antwort.status_code == 302
    gespeichert = _fraktion(org)
    # Bisheriges Verhalten: begrenzt statt abgelehnt
    assert gespeichert["invitation_lead_hours"] == 24 * 60
    assert gespeichert["agenda_reminder_enabled"] is True
    assert gespeichert["auto_create_approval_item"] is False


def test_abschnitt_ohne_javascript_mit_meldung(org: Any, vorsitz: Any, client_for: Any) -> None:
    antwort = client_for(vorsitz.user).post(
        settings_url(org),
        {
            "section": "save_settings",
            "felder": "agenda_reminder_enabled,agenda_reminder_hours",
            "agenda_reminder_enabled": "on",
            "agenda_reminder_hours": "6",
        },
    )
    assert antwort.status_code == 302
    assert _fraktion(org)["agenda_reminder_hours"] == 6
    assert [str(m) for m in get_messages(antwort.wsgi_request)] == ["Einstellungen gespeichert."]


def test_automatik_sofort_speichern(org: Any, vorsitz: Any, reihe: Any, client_for: Any) -> None:
    client = client_for(vorsitz.user)
    daten = {"section": "update_schedule_automation", "schedule_id": str(reihe.id), "auto_invite": "on"}
    fehler = client.post(settings_url(org), daten, **HTMX)
    assert fehler.status_code == 400
    assert "Wochentag und Uhrzeit" in fehler.content.decode()
    reihe.refresh_from_db()
    assert reihe.auto_invite is False

    gut = client.post(
        settings_url(org),
        {**daten, "rsvp_enabled": "on", "auto_invite_weekday": "4", "auto_invite_time": "18:00"},
        **HTMX,
    )
    assert gut.status_code == 204
    reihe.refresh_from_db()
    assert (reihe.rsvp_enabled, reihe.auto_invite, reihe.auto_invite_weekday, reihe.auto_invite_time) == (
        True,
        True,
        4,
        time(18, 0),
    )
    assert not list(get_messages(gut.wsgi_request))


def test_automatik_formular_speichert_sofort(org: Any, vorsitz: Any, reihe: Any, client_for: Any) -> None:
    seite = client_for(vorsitz.user).get(settings_url(org)).content.decode()
    formular = re.search(rf'<form method="post" class="fs-einst-unter" id="automatik-{reihe.id}"[^>]*>', seite)
    assert formular
    assert 'data-autosave="einst"' in formular.group(0)
    assert "autosave-erneut" in formular.group(0)


def test_zweiter_faktor_fehlt_nichts_gespeichert(org: Any, make_member: Any, client_for: Any, settings: Any) -> None:
    settings.TWO_FACTOR_ENFORCEMENT = True
    cache.clear()
    admin = make_member(org, [], email="admin@example.org", is_admin=True)
    antwort = _sofort(
        client_for(admin.user), org, "erinnerung", agenda_reminder_enabled="on", agenda_reminder_hours="6"
    )
    assert antwort.status_code == 204
    assert antwort["HX-Redirect"]
    assert "agenda_reminder_hours" not in _fraktion(org)


# ---------------------------------------------------------------------------
# Nur-Lesen für Mitglieder
# ---------------------------------------------------------------------------


def test_mitglied_sieht_die_einstellungen_nur_zur_information(
    org: Any, mitglied: Any, reihe: Any, client_for: Any
) -> None:
    seite = client_for(mitglied.user).get(settings_url(org)).content.decode()
    assert (
        "Diese Einstellungen ändern Vorsitz, Stellvertretung und Geschäftsführung. Sie sehen sie zur Information."
        in seite
    )
    assert re.search(r'<fieldset class="fs-einst-raster" disabled>', seite)
    assert "Wöchentliche Fraktionssitzung" in seite and "Weihnachtspause" in seite
    # Nichts speichert, nichts legt an
    for kein in (
        'data-autosave="einst"',
        "hx-post",
        "Einstellungen speichern",
        'value="add_schedule"',
        'value="add_exception"',
        'value="add_standard_item"',
        "Work speichert Änderungen sofort.",
    ):
        assert kein not in seite, kein
    # Reiter „Fraktionssitzungen“ für Mitglieder mit Einstellungszugang sichtbar
    assert ">Fraktionssitzungen<" in seite


def test_mitglied_bisherige_vorlage_bleibt_gesperrt(org: Any, mitglied: Any, client_for: Any) -> None:
    assert client_for(mitglied.user).get(settings_url(org) + "?ansicht=bisher").status_code == 403
    org.work_new_design = False
    org.save(update_fields=["work_new_design"])
    assert client_for(mitglied.user).get(settings_url(org)).status_code == 403


@pytest.mark.parametrize(
    "daten,htmx",
    [
        ({"agenda_reminder_enabled": "on", "agenda_reminder_hours": "6"}, False),
        (
            {
                "section": "save_settings",
                "felder": "agenda_reminder_enabled,agenda_reminder_hours",
                "agenda_reminder_enabled": "on",
                "agenda_reminder_hours": "6",
            },
            True,
        ),
        ({"section": "add_schedule", "name": "Neue Reihe", "weekday": "1", "time": "19:00"}, False),
        ({"section": "add_standard_item", "title": "Verschiedenes"}, False),
    ],
)
def test_mitglied_post_abgewiesen(org: Any, mitglied: Any, client_for: Any, daten: dict[str, str], htmx: bool) -> None:
    vorher = _fraktion(org)
    antwort = client_for(mitglied.user).post(settings_url(org), daten, **(HTMX if htmx else {}))
    assert antwort.status_code == 403
    assert _fraktion(org) == vorher
    assert not FactionMeetingSchedule.objects.filter(organization=org).exists()
    assert not FactionStandardAgendaItem.objects.filter(organization=org).exists()


def test_mitglied_automatik_abgewiesen(org: Any, mitglied: Any, reihe: Any, client_for: Any) -> None:
    antwort = client_for(mitglied.user).post(
        settings_url(org),
        {"section": "update_schedule_automation", "schedule_id": str(reihe.id), "rsvp_enabled": "on"},
        **HTMX,
    )
    assert antwort.status_code == 403
    reihe.refresh_from_db()
    assert reihe.rsvp_enabled is False


def test_ohne_fraktionsrecht_kein_zugang(org: Any, make_member: Any, client_for: Any) -> None:
    gast = make_member(org, ["dashboard.view", "motions.view"], email="ohne@example.org")
    assert client_for(gast.user).get(settings_url(org)).status_code == 403


# ---------------------------------------------------------------------------
# Wer TOPs einbringt
# ---------------------------------------------------------------------------


def test_wer_tops_einbringt_aus_den_rollen(org: Any, vorsitz: Any, client_for: Any, make_member: Any) -> None:
    from apps.tenants.models import Permission, Role

    # Rechte wie nach der Migration (die Testdatenbank kennt sie je nach Reihenfolge noch nicht)
    Permission.sync_permissions()  # type: ignore[no-untyped-call]
    Role.objects.filter(organization=org).delete()
    Role.create_default_roles(org)  # type: ignore[no-untyped-call]
    rechte = fraktion_einstellungen.top_rechte(org)
    assert "Fraktionsvorsitz" in rechte.genehmigen
    assert "Fraktionsmitglied" in rechte.eintragen
    assert "Fraktionsmitglied" not in rechte.vorschlagen
    neu = make_member(org, ["faction.manage"], email="neu@example.org")
    seite = client_for(neu.user).get(settings_url(org)).content.decode()
    assert rechte.eintragen in seite


# ---------------------------------------------------------------------------
# Mandantentrennung
# ---------------------------------------------------------------------------


def test_mandantentrennung(org: Any, vorsitz: Any, reihe: Any, client_for: Any) -> None:
    andere: Any = OrganizationFactory(  # type: ignore[no-untyped-call]
        name="Andere Fraktion", slug="andere-fraktion", work_new_design=True
    )
    fremde = FactionMeetingSchedule.objects.create(organization=andere, name="Fremd", weekday=2, time=time(19, 0))
    client = client_for(vorsitz.user)
    antwort = client.post(
        settings_url(org),
        {"section": "update_schedule_automation", "schedule_id": str(fremde.id), "rsvp_enabled": "on"},
        **HTMX,
    )
    assert antwort.status_code == 400
    fremde.refresh_from_db()
    assert fremde.rsvp_enabled is False

    assert (
        _sofort(client, org, "erinnerung", agenda_reminder_enabled="on", agenda_reminder_hours="6").status_code == 204
    )
    andere.refresh_from_db()
    assert "faction" not in (andere.settings or {})
    # Fremde Organisation: kein Zugang
    assert client.get(settings_url(andere)).status_code == 403
    assert _sofort(client, andere, "erinnerung", agenda_reminder_enabled="on").status_code == 403
    # Die eigene Seite zeigt die fremde Reihe nicht
    assert "Fremd" not in client.get(settings_url(org)).content.decode()
