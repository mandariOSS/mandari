# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes, Teil 2b: Cockpit, Abstimmung erfassen und Leitstelle (Issue #944).

Geprüft:

- Cockpit und Abstimmung haben mit eingeschaltetem Schalter eigene neue Vorlagen, ohne Schalter die bisherigen. Der
  Stand des Cockpits (Polling, WebSocket-Hinweis, Aktionen) kommt im Erscheinungsbild des Mandanten zurück, mit
  denselben Ids, data-testid und Formularschlüsseln (data-cockpit-form), an denen meetingCockpit hängt.
- Keine Funktion entfällt, keine kommt hinzu: Jede Aktion der bisherigen Seiten (Links, Formulare mit Feldern, festen
  Werten und Optionen, htmx mit Ziel und Rückfrage) steht für jede Standardrolle auch auf der neuen – das Cockpit in
  jedem Stand (vor Beginn, laufend mit offener Abstimmung als Summe bzw. mit Einzelstimmen, mit Ergebnis, geschlossen,
  gesperrt, nichtöffentlicher TOP mit Abstimmung), die Abstimmung mit Summen, Einzelstimmen, Gesperrt, Beratenden und
  früheren Stimmen ohne Stimmrecht.
- Die Leitstelle folgt ihren Mandanten: im neuen Rahmen erst, wenn alle aktiven Mandanten der Gruppe ihn eingeschaltet
  haben. Dann zeigt sie Übersicht und Suche mit allen Aktionen der bisherigen Seiten (Links in die Mandanten, weitere
  Leitstellen, Suche) – für beide Gruppenrollen.
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionAttendanceDisruption,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionProtocol,
    SessionRole,
    SessionTenant,
    SessionTenantGroup,
    SessionTenantGroupMembership,
    SessionTenantGroupTenant,
    SessionUser,
    SessionVote,
)
from apps.session.rahmen import neues_design_gruppe
from apps.session.services import cockpit_service, meeting_format_service
from apps.session.tests.gleichstand import aktionen, fehlende, gezeigt, neue, reiter_ausnahmen, vergleichen
from apps.session.tests.test_neues_design import _client, _mandant, _vorlagen

pytestmark = pytest.mark.django_db

#: Rechte, mit denen der Bestand die Sitzungen über das Cockpit in ihren Stand bringt
STEUERN = {"view_meetings", "conduct_meetings", "view_non_public_meetings", "edit_protocols"}


def _person(tenant: SessionTenant, name: str) -> SessionPerson:
    return SessionPerson.objects.create(tenant=tenant, given_name="P", family_name=name)


def _sitzung(tenant: SessionTenant, rat: SessionOrganization, name: str, *, oeffentlich: bool = True) -> SessionMeeting:
    """Hybride Sitzung mit drei TOPs (einer nichtöffentlich) und drei Anwesenden, eine Person zugeschaltet."""
    meeting = SessionMeeting.objects.create(
        tenant=tenant,
        organization=rat,
        name=name,
        start=timezone.now(),
        is_public=oeffentlich,
        format=SessionMeeting.FORMAT_HYBRID,
    )
    for order, (nummer, titel, public) in enumerate(
        [("1", "Eröffnung", True), ("2", "Radweg", True), ("N1", "Grundstück", False)]
    ):
        SessionAgendaItem.objects.create(meeting=meeting, number=nummer, name=titel, order=order, is_public=public)
    for nachname in ("Amsel", "Buche", "Carl"):
        person = SessionPerson.objects.get(tenant=tenant, family_name=nachname)
        SessionAttendance.objects.create(
            meeting=meeting,
            person=person,
            status="present",
            participation_mode="remote" if nachname == "Carl" else "in_person",
        )
    return meeting


def _tun(meeting: SessionMeeting, leitung: SessionUser, aktion: str, **daten: Any) -> None:
    meeting.refresh_from_db()
    cockpit_service.perform(
        meeting, aktion, {k: str(v) for k, v in daten.items()}, permissions=STEUERN, session_user=leitung
    )


def _top(meeting: SessionMeeting, nummer: str) -> SessionAgendaItem:
    return SessionAgendaItem.objects.get(meeting=meeting, number=nummer)


def _welt_cockpit(tenant: SessionTenant, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """
    Sitzungen in jedem Stand des Cockpits: vor Beginn (mit andauernder Störung einer zugeschalteten Person), laufend mit
    offener Abstimmung als Summe bzw. mit Einzelstimmen, laufend mit Ergebnis und nächstem TOP, geschlossen, gesperrt
    (genehmigte Niederschrift) und mit aufgerufenem nichtöffentlichen TOP samt offener Abstimmung. Dazu die Seiten zum
    Erfassen der Abstimmung: Summe, Einzelstimmen mit Beratenden und früherer Stimme ohne Stimmrecht, gesperrt.
    """
    meeting_format_service.sync_profiles()
    uhr = [time(18, 0)]
    monkeypatch.setattr(cockpit_service, "_now", lambda: uhr[0])
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    for nachname in ("Amsel", "Buche", "Carl", "Dorn"):
        _person(tenant, nachname)
    leitung = SessionUser.objects.create(user=cast(Any, UserFactory)(), tenant=tenant)

    vorher = _sitzung(tenant, rat, "Vor Beginn")
    carl = SessionAttendance.objects.get(meeting=vorher, person__family_name="Carl")
    SessionAttendanceDisruption.objects.create(
        attendance=carl, started_at=time(17, 50), cause="technical", note="Vermerk"
    )
    SessionAttendanceDisruption.objects.create(
        attendance=carl, started_at=time(17, 30), ended_at=time(17, 40), cause="technical"
    )

    summe = _sitzung(tenant, rat, "Abstimmung als Summe")
    _tun(summe, leitung, "sitzung_eroeffnen")
    _tun(summe, leitung, "top_aufrufen", item=_top(summe, "2").pk)
    _tun(summe, leitung, "abstimmung_oeffnen", item=_top(summe, "2").pk, voting_method="summary")

    einzeln = _sitzung(tenant, rat, "Abstimmung mit Einzelstimmen")
    _tun(einzeln, leitung, "sitzung_eroeffnen")
    _tun(einzeln, leitung, "top_aufrufen", item=_top(einzeln, "2").pk)
    _tun(einzeln, leitung, "abstimmung_oeffnen", item=_top(einzeln, "2").pk, voting_method="open")

    ergebnis = _sitzung(tenant, rat, "Mit Ergebnis")
    _tun(ergebnis, leitung, "sitzung_eroeffnen")
    _tun(ergebnis, leitung, "top_aufrufen", item=_top(ergebnis, "1").pk)
    _tun(ergebnis, leitung, "abstimmung_oeffnen", item=_top(ergebnis, "1").pk, voting_method="summary")
    _tun(
        ergebnis, leitung, "abstimmung_schliessen", item=_top(ergebnis, "1").pk,
        votes_yes=2, votes_no=1, votes_abstain=0, vote_result="approved",
    )  # fmt: skip
    SessionAttendance.objects.filter(meeting=ergebnis, person__family_name="Buche").update(status="left_early")

    geschlossen = _sitzung(tenant, rat, "Geschlossen")
    _tun(geschlossen, leitung, "sitzung_eroeffnen")
    _tun(geschlossen, leitung, "sitzung_schliessen")

    gesperrt = _sitzung(tenant, rat, "Gesperrt")
    SessionAgendaItem.objects.filter(pk=_top(gesperrt, "2").pk).update(
        vote_result="approved", votes_yes=3, voting_method="summary"
    )
    SessionProtocol.objects.create(meeting=gesperrt, status="approved", approved_at=timezone.now())

    noe = _sitzung(tenant, rat, "Nichtöffentlicher TOP")
    _tun(noe, leitung, "sitzung_eroeffnen")
    _tun(noe, leitung, "top_aufrufen", item=_top(noe, "N1").pk)
    _tun(noe, leitung, "abstimmung_oeffnen", item=_top(noe, "N1").pk, voting_method="secret")

    # Abstimmung erfassen: Einzelstimmen mit einer beratenden Person und einer früheren Stimme ohne Anwesenheit
    dorn = SessionPerson.objects.get(tenant=tenant, family_name="Dorn")
    SessionAttendance.objects.create(
        meeting=einzeln, person=dorn, status="present", role="expert", has_voting_rights=False
    )
    SessionVote.objects.create(agenda_item=_top(einzeln, "2"), person=dorn, vote="yes")
    SessionVote.objects.create(
        agenda_item=_top(einzeln, "2"), person=SessionPerson.objects.get(tenant=tenant, family_name="Amsel"), vote="no"
    )

    def cockpit(meeting: SessionMeeting) -> str:
        return reverse("session:meeting_cockpit", kwargs={"tenant_slug": tenant.slug, "meeting_id": meeting.pk})

    def erfassen(item: SessionAgendaItem) -> str:
        return reverse("session:voting_capture", kwargs={"tenant_slug": tenant.slug, "item_id": item.pk})

    sitzungen = {
        "vorher": vorher, "summe": summe, "einzeln": einzeln, "ergebnis": ergebnis,
        "geschlossen": geschlossen, "gesperrt": gesperrt, "noe": noe,
    }  # fmt: skip
    seiten = {f"cockpit-{name}": cockpit(m) for name, m in sitzungen.items()}
    seiten.update(
        {
            "erfassen-summe": erfassen(_top(summe, "2")),
            "erfassen-einzeln": erfassen(_top(einzeln, "2")),
            "erfassen-gesperrt": erfassen(_top(gesperrt, "2")),
            "erfassen-geheim": erfassen(_top(noe, "N1")),
        }
    )
    aktion = {
        name: reverse("session:meeting_cockpit_action", kwargs={"tenant_slug": tenant.slug, "meeting_id": m.pk})
        for name, m in sitzungen.items()
    }
    return {"seiten": seiten, "sitzungen": sitzungen, "aktion": aktion, "leitung": leitung, "uhr": uhr}


# =============================================================================
# Vorlagen
# =============================================================================


def test_cockpit_und_abstimmung_waehlen_ihre_vorlage(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = _mandant("cockpit2b", neu=False)
    welt = _welt_cockpit(tenant, monkeypatch)
    client = _client(tenant, "Administrator")
    for name, vorlage in (
        ("cockpit-summe", "session/neu/cockpit.html"),
        ("erfassen-einzeln", "session/neu/abstimmung.html"),
    ):
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
        alt = client.get(welt["seiten"][name])
        assert alt.status_code == 200 and "session/base_session_alt.html" in _vorlagen(alt), name
        assert vorlage not in _vorlagen(alt), name
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        neu = client.get(welt["seiten"][name])
        assert neu.status_code == 200 and {vorlage, "session/base_session_neu.html"} <= _vorlagen(neu), name
        assert 'data-gestaltung="neu"' in neu.content.decode(), name
    html = client.get(welt["seiten"]["cockpit-summe"]).content.decode()
    assert "session/neu/_cockpit_stand.html" in _vorlagen(client.get(welt["seiten"]["cockpit-summe"]))
    for kennung in ('x-data="meetingCockpit"', 'id="cockpit-stand"', 'hx-trigger="cockpit:refresh"',
                    'data-cockpit-form="abstimmung-', 'data-testid="verbindung"', 'data-testid="aktueller-top"'):  # fmt: skip
        assert kennung in html, kennung


def test_stand_und_aktionen_antworten_im_erscheinungsbild_des_mandanten(monkeypatch: pytest.MonkeyPatch) -> None:
    """Polling bzw. Hinweis über den WebSocket und jede Aktion liefern den Stand in der Gestaltung des Mandanten."""
    tenant = _mandant("stand2b", neu=True)
    welt = _welt_cockpit(tenant, monkeypatch)
    meeting = welt["sitzungen"]["vorher"]
    client = _client(tenant, "Administrator")
    stand_url = reverse("session:meeting_cockpit_state", kwargs={"tenant_slug": tenant.slug, "meeting_id": meeting.pk})
    neu = client.get(stand_url, HTTP_HX_REQUEST="true")
    assert neu.status_code == 200 and "session/neu/_cockpit_stand.html" in _vorlagen(neu)
    assert neu.content.decode().lstrip().startswith('<div id="cockpit-stand"')
    antwort = client.post(welt["aktion"]["vorher"], {"aktion": "sitzung_eroeffnen"}, HTTP_HX_REQUEST="true")
    assert antwort.status_code == 200 and "session/neu/_cockpit_stand.html" in _vorlagen(antwort)
    assert "showToast" in antwort["HX-Trigger"] and "Sitzung läuft" in antwort.content.decode()

    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
    alt = client.get(stand_url, HTTP_HX_REQUEST="true")
    assert "session/cockpit/_stand.html" in _vorlagen(alt) and "session/neu/_cockpit_stand.html" not in _vorlagen(alt)


# =============================================================================
# Keine Funktion entfällt, keine kommt hinzu
# =============================================================================


def test_keine_funktion_des_cockpits_und_der_abstimmung_entfaellt(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = _mandant("vergleich2b-cockpit", neu=False)
    welt = _welt_cockpit(tenant, monkeypatch)
    fehlt, gesehen = vergleichen(tenant, welt["seiten"], ausnahmen=reiter_ausnahmen(tenant))
    assert not fehlt, "\n".join(fehlt)
    # Der Bestand zeigt wirklich, was er prüfen soll
    a = welt["aktion"]
    erwartet = {
        ("POST", a["vorher"]), ("POST", a["summe"]), ("POST", a["einzeln"]), ("POST", a["ergebnis"]),
        ("POST", a["geschlossen"]), ("POST", a["noe"]),
    }  # fmt: skip
    assert erwartet <= gezeigt(gesehen), sorted(erwartet - gezeigt(gesehen))
    # Abstimmung erfassen: ein Formular an die Seite selbst mit den Einzelstimmen und den Werten des geladenen Stands
    erfassen = [felder for methode, ziel, felder in gesehen if methode == "POST" and ziel == ""]
    assert any(any(f.startswith("vote_") and "∈" in f for f in felder) and "geladen_voting_method=open" in felder
               for felder in erfassen), erfassen  # fmt: skip
    merkmale = {m for aktion, ms in gesehen.items() for m in ms if aktion[1] == a["summe"]}
    assert any(m.startswith("hx-confirm=Sitzung jetzt schließen?") for m in merkmale), merkmale
    assert "hx-target=#cockpit-stand" in merkmale


def test_nur_der_naechste_schritt_ist_gefuellt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Im Stand trägt höchstens eine Aktion die Füllfarbe: der nächste Schritt der Sitzung."""
    tenant = _mandant("knoepfe2b-cockpit", neu=True)
    welt = _welt_cockpit(tenant, monkeypatch)
    client = _client(tenant, "Administrator")
    for name, adresse in welt["seiten"].items():
        html = client.get(adresse).content.decode()
        inhalt = html[html.index('class="work-page-content"') : html.index("</main>")]
        assert inhalt.count("bg-primary-600 hover:bg-primary-700") <= 1, name
        for farbe in ("green", "blue", "indigo", "amber", "red"):
            assert f"bg-{farbe}-600 hover:bg-{farbe}-700" not in inhalt, f"{name}: Knopf in {farbe}"


# =============================================================================
# Leitstelle
# =============================================================================


def _welt_leitstelle() -> dict[str, Any]:
    """Gruppe mit zwei Mandanten, Konto mit Mitgliedschaft nur in Nord, eine zweite Leitstelle, Arbeitsvorräte."""
    nord = _mandant("lnord", neu=False)
    sued = _mandant("lsued", neu=False)
    gruppe = SessionTenantGroup.objects.create(name="Bezirke", slug="bezirke-2b")
    andere = SessionTenantGroup.objects.create(name="Kreis", slug="kreis-2b")
    for tenant in (nord, sued):
        SessionTenantGroupTenant.objects.create(group=gruppe, tenant=tenant)
    rat = SessionOrganization.objects.create(tenant=sued, name="Rat", invitation_period_days=7)
    SessionPaper.objects.create(tenant=nord, name="Radweg Nord", reference="N-1", status="review")
    SessionPaper.objects.create(tenant=sued, name="Geheim Süd", reference="S-1", status="review", is_public=False)
    SessionPaper.objects.create(
        tenant=sued, name="Markt Süd", reference="S-2", status="review", deadline=timezone.localdate()
    )
    SessionMeeting.objects.create(
        tenant=sued,
        organization=rat,
        name="Rat Süd",
        start=timezone.now() + timedelta(days=3),
        meeting_state="scheduled",
    )
    return {"nord": nord, "sued": sued, "gruppe": gruppe, "andere": andere}


def _leitstellen_client(welt: dict[str, Any], rolle: str) -> Client:
    user = cast(Any, UserFactory)()
    konto = SessionUser.objects.create(user=user, tenant=welt["nord"])
    konto.roles.add(SessionRole.objects.get(tenant=welt["nord"], name="Sachbearbeiter"))
    SessionTenantGroupMembership.objects.create(group=welt["gruppe"], user=user, role=rolle)
    SessionTenantGroupMembership.objects.create(group=welt["andere"], user=user)
    client = Client()
    client.force_login(user)
    return client


def test_leitstelle_folgt_ihren_mandanten() -> None:
    welt = _welt_leitstelle()
    gruppe: SessionTenantGroup = welt["gruppe"]
    assert neues_design_gruppe(gruppe) is False
    SessionTenant.objects.filter(pk=welt["nord"].pk).update(session_new_design=True)
    assert neues_design_gruppe(gruppe) is False, "erst wenn alle Mandanten der Gruppe umgeschaltet haben"
    SessionTenant.objects.filter(pk=welt["sued"].pk).update(session_new_design=True)
    assert neues_design_gruppe(gruppe) is True
    SessionTenant.objects.filter(pk=welt["sued"].pk).update(is_active=False, session_new_design=False)
    assert neues_design_gruppe(gruppe) is True, "inaktive Mandanten zählen nicht"
    leer = SessionTenantGroup.objects.create(name="Leer", slug="leer-2b")
    assert neues_design_gruppe(leer) is False and neues_design_gruppe(None) is False

    client = _leitstellen_client(welt, SessionTenantGroupMembership.ROLE_LEITSTELLE)
    adresse = reverse("session:leitstelle", kwargs={"group_slug": gruppe.slug})
    neu = client.get(adresse)
    assert neu.status_code == 200 and {"session/neu/leitstelle.html", "session/base_session_neu.html"} <= _vorlagen(neu)
    html = neu.content.decode()
    assert 'data-rahmen="neu"' in html and "Leitstelle Bezirke" in html and 'data-gestaltung="neu"' in html
    SessionTenant.objects.filter(pk=welt["nord"].pk).update(session_new_design=False)
    alt = client.get(adresse)
    assert {"session/leitstelle/overview.html", "session/base_session_alt.html"} <= _vorlagen(alt)


def _umschalten(welt: dict[str, Any], an: bool) -> None:
    SessionTenant.objects.filter(pk__in=[welt["nord"].pk, welt["sued"].pk]).update(session_new_design=an)


def test_keine_funktion_der_leitstelle_entfaellt() -> None:
    """Übersicht und Suche für beide Gruppenrollen: alle Aktionen der bisherigen Seiten (inkl. Rahmen) stehen auch neu."""
    welt = _welt_leitstelle()
    slug = welt["gruppe"].slug
    seiten = {
        "uebersicht": reverse("session:leitstelle", kwargs={"group_slug": slug}),
        "suche": reverse("session:leitstelle_search", kwargs={"group_slug": slug}),
        "suche-treffer": reverse("session:leitstelle_search", kwargs={"group_slug": slug}) + "?q=S-",
        "suche-kurz": reverse("session:leitstelle_search", kwargs={"group_slug": slug}) + "?q=x",
    }
    fehlt: list[str] = []
    gesehen: set[tuple[str, str]] = set()
    for rolle in (SessionTenantGroupMembership.ROLE_LEITSTELLE, SessionTenantGroupMembership.ROLE_KENNZAHLEN):
        client = _leitstellen_client(welt, rolle)
        for name, adresse in seiten.items():
            _umschalten(welt, False)
            alt = client.get(adresse)
            _umschalten(welt, True)
            neu = client.get(adresse)
            assert alt.status_code == neu.status_code, f"{rolle}/{name}"
            if alt.status_code != 200:
                continue
            assert "session/base_session_neu.html" in _vorlagen(neu), f"{rolle}/{name}"
            bisher, jetzt = aktionen(alt), aktionen(neu)
            gesehen |= gezeigt(bisher)
            fehlt.extend(f"{rolle}/{name}: {f}" for f in fehlende(bisher, jetzt))
            # Kopfsuche des neuen Rahmens auf der Suchseite: dieselbe Suche wie das Suchfeld der Übersicht (nur mit
            # Suchrecht, Gruppenrolle „Leitstelle“) – eine Änderung des Rahmens, keine neue Funktion
            for methode, ziel, felder in neue(bisher, jetzt, [("GET", seiten["suche"], ("q",))]):
                if methode == "GET" and not felder and client.get(ziel).status_code in (200, 302):
                    continue  # Weg im neuen Rahmen (Mandant wechseln, Start), für die Person erreichbar
                fehlt.append(f"{rolle}/{name}: neu {(methode, ziel, felder)}")
    assert not fehlt, "\n".join(fehlt)
    nord_start = reverse("session:dashboard", kwargs={"tenant_slug": welt["nord"].slug})
    erwartet = {
        ("GET", nord_start),
        ("GET", reverse("session:leitstelle", kwargs={"group_slug": welt["andere"].slug})),
        ("GET", reverse("session:leitstelle_search", kwargs={"group_slug": slug})),
    }
    assert erwartet <= gesehen, sorted(erwartet - gesehen)


def test_leitstelle_im_neuen_rahmen_ohne_suche_fuer_kennzahlen() -> None:
    welt = _welt_leitstelle()
    _umschalten(welt, True)
    slug = welt["gruppe"].slug
    suche = reverse("session:leitstelle_search", kwargs={"group_slug": slug})
    kennzahlen = _leitstellen_client(welt, SessionTenantGroupMembership.ROLE_KENNZAHLEN)
    html = kennzahlen.get(reverse("session:leitstelle", kwargs={"group_slug": slug})).content.decode()
    assert suche not in html and 'id="kopf-suche"' not in html
    assert "Leitstelle · Kennzahlen" in html and "leitstelle-vorlagen" not in html
    assert kennzahlen.get(suche).status_code == 403
    leitstelle = _leitstellen_client(welt, SessionTenantGroupMembership.ROLE_LEITSTELLE)
    html = leitstelle.get(suche + "?q=Markt").content.decode()
    assert 'id="kopf-suche"' in html and f'action="{suche}"' in html and "Markt Süd" in html
    brotkrumen = html[html.index('<nav aria-label="Brotkrumen"') :]
    assert (
        f'href="{reverse("session:leitstelle", kwargs={"group_slug": slug})}"'
        in brotkrumen[: brotkrumen.index("</nav>")]
    )


def test_neue_vorlagen_ohne_12_px() -> None:
    from pathlib import Path

    neu = Path(__file__).resolve().parents[3] / "templates" / "session" / "neu"
    for datei in ("cockpit.html", "_cockpit_stand.html", "_cockpit_top.html", "_cockpit_abstimmung.html",
                  "_cockpit_tagesordnung.html", "_cockpit_sitzung.html", "_cockpit_anwesenheit.html",
                  "_cockpit_stoerungen.html", "_cockpit_aktion.html", "abstimmung.html", "leitstelle.html",
                  "leitstelle_suche.html", "_leitstelle_zeilen.html"):  # fmt: skip
        text = (neu / datei).read_text(encoding="utf-8")
        for klein in ("text-xs", "text-[11px]", "text-[10px]", "text-[0.75rem]"):
            assert klein not in text, f"{datei}: {klein}"
