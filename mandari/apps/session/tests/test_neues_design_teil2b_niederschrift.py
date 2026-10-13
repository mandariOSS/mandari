# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes, Teil 2b: Niederschrift und Beschlussregister (Issue #944).

Geprüft: Niederschrift ansehen, bearbeiten und berichtigen sowie das Beschlussregister haben mit eingeschaltetem
Schalter eine eigene neue Vorlage, ohne Schalter bleiben die bisherigen. Keine Funktion entfällt und keine kommt hinzu:
Für jede der sechs Standardrollen und jeden Zustand der Niederschrift (keine, Entwurf, in Prüfung mit Genehmigungsvermerk,
ohne Genehmigungsschritt, mit Vier-Augen-Sperre, genehmigt, veröffentlicht mit Berichtigungen in allen Ständen) sowie
für das Register mit Erledigungsvermerken, Übergaben und Filtern bietet die neue Seite dieselben Aktionen mit denselben
Merkmalen wie die bisherige (session/tests/gleichstand.py). Der Lesezugriff auf nichtöffentliche Teile wird weiter
protokolliert. Gefüllte Knöpfe stehen nur dort, wo die Seite ihre eine Hauptaktion hat; keine Schrift unter 13 px.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionAuditLog,
    SessionMeeting,
    SessionOrganizationMembership,
    SessionPerson,
    SessionProtocol,
    SessionProtocolCorrection,
    SessionResolutionForwarding,
    SessionTenant,
    SessionTextBlock,
    SessionUser,
)
from apps.session.tests.gleichstand import _gleich, gezeigt, reiter_ausnahmen, rollen, vergleichen
from apps.session.tests.test_neues_design import _client, _mandant, _volle_welt, _vorlagen

pytestmark = pytest.mark.django_db

#: Seiten dieses Helfers: Art → neue Vorlage
VORLAGEN = {
    "niederschrift": "session/neu/niederschrift.html",
    "bearbeiten": "session/neu/niederschrift_bearbeiten.html",
    "berichtigen": "session/neu/niederschrift_berichtigen.html",
    "register": "session/neu/beschlussregister.html",
}
#: Neue Vorlagen dieses Helfers (für die Prüfung der Schriftgrößen)
NEUE_DATEIEN = (
    "niederschrift.html",
    "niederschrift_bearbeiten.html",
    "niederschrift_berichtigen.html",
    "beschlussregister.html",
    "_niederschrift_arbeitsschritt.html",
    "_niederschrift_berichtigungen.html",
    "_niederschrift_seite.html",
    "_niederschrift_top.html",
    "_niederschrift_top_formular.html",
    "_niederschrift_textbaustein.html",
    "_beschluss_eintrag.html",
)


def _person(tenant: SessionTenant, name: str) -> SessionPerson:
    return SessionPerson.objects.create(tenant=tenant, given_name="P", family_name=name, start_date=date(2025, 1, 1))


def _sitzung(welt: dict[str, Any], name: str, tage: int) -> SessionMeeting:
    rat = welt["sitzung_obj"].organization
    return SessionMeeting.objects.create(
        tenant=rat.tenant,
        organization=rat,
        name=name,
        start=timezone.now() - timedelta(days=tage),
        meeting_state="completed",
    )


def _tops(sitzung: SessionMeeting, *, einzelstimmen: bool = False) -> dict[str, SessionAgendaItem]:
    """Öffentlicher TOP mit Unterpunkt, nichtöffentlicher TOP mit verschlüsseltem Text, ein Nachtrag."""
    beschluss = SessionAgendaItem.objects.create(
        meeting=sitzung,
        number="1",
        order=1,
        name="Radweg Hauptstraße",
        resolution_text="Der Rat beschließt den Radweg.",
        protocol_note="Aussprache zum Radweg.",
        vote_result="approved",
        votes_yes=2,
        votes_no=1,
        voting_method="roll_call" if einzelstimmen else "summary",
        start_time=timezone.now().time().replace(microsecond=0),
    )
    unter = SessionAgendaItem.objects.create(
        meeting=sitzung, number="1.1", order=2, name="Markierung", parent=beschluss, vote_result="noted"
    )
    nachtrag = SessionAgendaItem.objects.create(
        meeting=sitzung, number="2", order=3, name="Bänke", is_supplementary=True, vote_result="rejected"
    )
    noe = SessionAgendaItem(
        meeting=sitzung, number="3", order=4, name="Grundstück", is_public=False, vote_result="approved", votes_yes=3
    )
    cast(Any, noe).set_protocol_note_encrypted("Vertraulicher Wortbeitrag")
    noe.save()
    return {"beschluss": beschluss, "unter": unter, "nachtrag": nachtrag, "noe": noe}


def _niederschrift(sitzung: SessionMeeting, status: str, **felder: Any) -> SessionProtocol:
    protokoll = SessionProtocol(
        meeting=sitzung,
        content="Allgemeiner Teil der Sitzung.",
        status=status,
        chair_name="Vera Vorsitz",
        recorder_name="Paul Protokoll",
        **felder,
    )
    cast(Any, protokoll).set_content_encrypted("Vertraulicher allgemeiner Teil")
    if status in ("approved", "published"):
        protokoll.approved_at = timezone.now()
    if status == "published":
        protokoll.published_at = timezone.now()
    protokoll.save()
    return protokoll


def _welt_2b(tenant: SessionTenant) -> dict[str, Any]:
    """
    Bestand auf der Grundlage von ``_volle_welt``: Sitzungen mit Niederschrift in jedem Zustand (keine, Entwurf mit
    Textbausteinen und Einzelabstimmung, in Prüfung mit TOP „Genehmigung der Niederschrift“ in der Folgesitzung,
    genehmigt, veröffentlicht mit beantragter, wirksamer und abgelehnter Berichtigung, öffentlich und nichtöffentlich),
    Anwesenheit (anwesend, entschuldigt, abwesend) und Beschlüsse mit Nummer, Erledigungsvermerk (überfällig, erledigt)
    und Übergabe.
    """
    welt = _volle_welt(tenant)
    slug = tenant.slug
    rat = welt["sitzung_obj"].organization
    # Folgesitzung (die kommende Sitzung) mit dem TOP für den Genehmigungsvermerk
    SessionAgendaItem.objects.create(
        meeting=welt["sitzung_obj"], number="5", order=9, name="Genehmigung der Niederschrift"
    )
    SessionTextBlock.objects.create(
        tenant=tenant, title="Eröffnung", content="Der Vorsitz eröffnet {sitzung}.", category="protocol"
    )

    personen = [_person(tenant, name) for name in ("Amsel", "Buche", "Carl", "Dachs")]
    for person in personen:
        SessionOrganizationMembership.objects.create(organization=rat, person=person, has_voting_rights=True)

    ohne = _sitzung(welt, "Sitzung ohne Niederschrift", 40)
    entwurf = _sitzung(welt, "Sitzung im Entwurf", 35)
    pruefung = _sitzung(welt, "Sitzung in Prüfung", 30)
    veroeffentlicht = _sitzung(welt, "Veröffentlichte Sitzung", 25)
    tops = {
        "entwurf": _tops(entwurf, einzelstimmen=True),
        "pruefung": _tops(pruefung),
        "veroeffentlicht": _tops(veroeffentlicht, einzelstimmen=True),
    }
    for sitzung in (entwurf, pruefung, veroeffentlicht):
        for person, status in zip(personen, ("present", "present", "excused", "absent"), strict=True):
            SessionAttendance.objects.create(meeting=sitzung, person=person, status=status)

    protokolle = {
        "entwurf": _niederschrift(entwurf, "draft"),
        "pruefung": _niederschrift(pruefung, "review", review_requested_at=timezone.now(), approval_note="Vermerk"),
        "veroeffentlicht": _niederschrift(veroeffentlicht, "published"),
    }
    genehmigt = SessionProtocol.objects.get(meeting=welt["vergangen_obj"])
    protokolle["genehmigt"] = genehmigt
    genehmigt.approval_meeting = welt["sitzung_obj"]
    genehmigt.approval_agenda_item = SessionAgendaItem.objects.get(meeting=welt["sitzung_obj"], number="5")
    genehmigt.save()

    # Berichtigungen der veröffentlichten Niederschrift: beantragt (öffentlich), wirksam (nichtöffentlich), abgelehnt
    antragsteller = SessionUser.objects.create(user=cast(Any, UserFactory)(), tenant=tenant)
    beantragt = SessionProtocolCorrection.objects.create(
        protocol=protokolle["veroeffentlicht"],
        target=SessionProtocolCorrection.TARGET_ITEM,
        agenda_item=tops["veroeffentlicht"]["beschluss"],
        is_public=True,
        reason="Zahlendreher",
        changes=[{"feld": "Ja-Stimmen", "alt": "2", "neu": "3"}],
        requested_by=antragsteller,
    )
    wirksam = SessionProtocolCorrection(
        protocol=protokolle["veroeffentlicht"],
        target=SessionProtocolCorrection.TARGET_GENERAL_NP,
        is_public=False,
        changes=[{"feld": "Text", "geaendert": True}],
        status=SessionProtocolCorrection.STATUS_APPLIED,
        requested_by=antragsteller,
    )
    cast(Any, wirksam).set_reason_encrypted("Vertraulicher Grund")
    wirksam.save()
    SessionProtocolCorrection.objects.create(
        protocol=protokolle["veroeffentlicht"],
        target=SessionProtocolCorrection.TARGET_GENERAL,
        is_public=True,
        reason="Tippfehler",
        status=SessionProtocolCorrection.STATUS_REJECTED,
        requested_by=antragsteller,
        decision_note="kein Fehler",
    )

    # Beschlussregister: Nummer, Erledigungsvermerke (überfällig, erledigt), Übergabe
    heute = timezone.localdate()
    SessionAgendaItem.objects.filter(pk=tops["pruefung"]["beschluss"].pk).update(
        resolution_number="B/2026/0001",
        implementation_status="in_progress",
        implementation_recipient="Tiefbauamt",
        implementation_deadline=heute - timedelta(days=3),
        implementation_note="Planung läuft",
        implementation_public_note="In Planung",
        implementation_public=True,
        implementation_updated_at=timezone.now(),
        implementation_updated_by=antragsteller,
    )
    SessionAgendaItem.objects.filter(pk=tops["veroeffentlicht"]["beschluss"].pk).update(
        resolution_number="B/2026/0002", implementation_status="done"
    )
    SessionResolutionForwarding.objects.create(
        agenda_item=tops["pruefung"]["beschluss"], recipient="Tiefbauamt", method="email", sent_by=antragsteller
    )

    def adresse(name: str, **kw: Any) -> str:
        return reverse(f"session:{name}", kwargs={"tenant_slug": slug, **kw})

    def niederschrift(sitzung: SessionMeeting) -> str:
        return adresse("meeting_protocol", meeting_id=sitzung.pk)

    berichtigen = adresse("meeting_protocol_correction", meeting_id=veroeffentlicht.pk)
    bearbeiten = adresse("meeting_protocol_edit", meeting_id=entwurf.pk)
    welt.update(
        {
            "sitzungen_2b": {
                "ohne": ohne,
                "entwurf": entwurf,
                "pruefung": pruefung,
                "genehmigt": welt["vergangen_obj"],
                "veroeffentlicht": veroeffentlicht,
            },
            "protokolle": protokolle,
            "tops": tops,
            "beantragt": beantragt,
            "antragsteller": antragsteller,
            "seiten_niederschrift": {
                "ohne": niederschrift(ohne),
                "entwurf": niederschrift(entwurf),
                "pruefung": niederschrift(pruefung),
                "pruefung_direkt": niederschrift(pruefung),
                "pruefung_vier_augen": niederschrift(pruefung),
                "genehmigt": niederschrift(welt["vergangen_obj"]),
                "veroeffentlicht": niederschrift(veroeffentlicht),
                "veroeffentlicht_vier_augen": niederschrift(veroeffentlicht),
                "bearbeiten_entwurf": bearbeiten,
                "bearbeiten_pruefung": adresse("meeting_protocol_edit", meeting_id=pruefung.pk),
                "berichtigen_top": f"{berichtigen}?top={tops['veroeffentlicht']['beschluss'].pk}",
                "berichtigen_top_noe": f"{berichtigen}?top={tops['veroeffentlicht']['noe'].pk}",
                "berichtigen_allgemein": f"{berichtigen}?teil=allgemein",
                "berichtigen_noe": f"{berichtigen}?teil=allgemein-noe",
            },
            "seiten_register": {
                "register": adresse("resolutions"),
                "register_buergerportal": adresse("resolutions"),
                "register_gefiltert": adresse("resolutions")
                + f"?status=in_progress&organization={rat.pk}&year={heute.year}",
                "register_ueberfaellig": adresse("resolutions") + "?overdue=1&result=approved",
            },
        }
    )
    aktion = adresse("meeting_protocol_action", meeting_id=pruefung.pk, action="approve")
    welt["erwartet_niederschrift"] = {
        ("POST", adresse("meeting_protocol_create", meeting_id=ohne.pk)),
        ("POST", adresse("meeting_protocol_action", meeting_id=entwurf.pk, action="submit")),
        ("GET", bearbeiten),
        ("POST", aktion),
        ("POST", adresse("meeting_protocol_action", meeting_id=pruefung.pk, action="reject")),
        ("POST", adresse("meeting_protocol_action", meeting_id=pruefung.pk, action="publish")),
        ("POST", adresse("meeting_protocol_action", meeting_id=welt["vergangen_obj"].pk, action="publish")),
        ("POST", adresse("meeting_protocol_action", meeting_id=veroeffentlicht.pk, action="unpublish")),
        ("GET", f"{berichtigen}?teil=allgemein"),
        ("GET", f"{berichtigen}?teil=allgemein-noe"),
        ("GET", f"{berichtigen}?top={tops['veroeffentlicht']['noe'].pk}"),
        ("POST", adresse("meeting_protocol_correction_decision", meeting_id=veroeffentlicht.pk, correction_id=beantragt.pk, decision="confirm")),
        ("POST", adresse("meeting_protocol_correction_decision", meeting_id=veroeffentlicht.pk, correction_id=beantragt.pk, decision="reject")),
        ("GET", adresse("meeting_protocol_pdf", meeting_id=veroeffentlicht.pk)),
        ("GET", adresse("meeting_protocol_pdf", meeting_id=veroeffentlicht.pk) + "?fassung=intern"),
        _gleich(("GET", adresse("voting_capture", item_id=tops["entwurf"]["beschluss"].pk) + "?next=" + quote(bearbeiten), ()))[:2],
    }  # fmt: skip
    welt["erwartet_register"] = {
        ("POST", adresse("resolution_tracking_update", item_id=tops["pruefung"]["beschluss"].pk)),
        ("POST", adresse("resolution_forwarding_add", item_id=tops["pruefung"]["nachtrag"].pk)),
        ("GET", adresse("resolution_extract_pdf", item_id=tops["pruefung"]["noe"].pk)),
        ("GET", adresse("resolutions_export_csv")),
        ("GET", "?overdue=1"),
        ("GET", "?status=done"),
    }  # fmt: skip
    return welt


def _mit_konten(konten: dict[str, SessionUser]) -> Any:
    """``client_fuer`` für ``vergleichen``, das sich das Konto je Rolle merkt (Vier-Augen-Zustände)."""

    def client_fuer(tenant: SessionTenant, rolle: str) -> Client:
        client = _client(tenant, rolle)
        konten[rolle] = SessionUser.objects.get(tenant=tenant, user_id=client.session["_auth_user_id"])
        return client

    return client_fuer


# =============================================================================
# Vorlagen
# =============================================================================


def test_seiten_waehlen_ihre_vorlage_nach_dem_schalter() -> None:
    tenant = _mandant("vorlagen2bn", neu=False)
    welt = _welt_2b(tenant)
    seiten = {
        "niederschrift": welt["seiten_niederschrift"]["entwurf"],
        "bearbeiten": welt["seiten_niederschrift"]["bearbeiten_entwurf"],
        "berichtigen": welt["seiten_niederschrift"]["berichtigen_top"],
        "register": welt["seiten_register"]["register"],
    }
    client = _client(tenant, "Administrator")
    for name, adresse in seiten.items():
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
        alt = client.get(adresse)
        assert alt.status_code == 200, name
        assert VORLAGEN[name] not in _vorlagen(alt) and "session/base_session_alt.html" in _vorlagen(alt), name
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        neu = client.get(adresse)
        assert neu.status_code == 200, name
        assert {VORLAGEN[name], "session/base_session_neu.html"} <= _vorlagen(neu), name
        assert 'data-gestaltung="neu"' in neu.content.decode(), name


# =============================================================================
# Keine Funktion entfällt, keine kommt hinzu
# =============================================================================


def _zustand(tenant: SessionTenant, welt: dict[str, Any], konten: dict[str, SessionUser]) -> Any:
    """
    ``vorher`` für ``vergleichen``: Genehmigungsweg und Vier-Augen-Prinzip des Mandanten je Seite. Bei
    ``…_vier_augen`` hat die abrufende Person die Niederschrift erstellt bzw. die Berichtigung beantragt.
    """
    pruefung, beantragt = welt["protokolle"]["pruefung"], welt["beantragt"]

    def vorher(rolle: str, name: str) -> None:
        SessionTenant.objects.filter(pk=tenant.pk).update(
            protocol_approval_mode="direct" if name == "pruefung_direkt" else None,
            four_eyes_protocols=name.endswith("_vier_augen"),
            implementation_publish=name == "register_buergerportal",
        )
        selbst = konten.get(rolle) if name.endswith("_vier_augen") else None
        SessionProtocol.objects.filter(pk=pruefung.pk).update(created_by=selbst)
        SessionProtocolCorrection.objects.filter(pk=beantragt.pk).update(requested_by=selbst or welt["antragsteller"])

    return vorher


def test_keine_funktion_der_niederschrift_entfaellt() -> None:
    """
    Niederschrift ansehen, bearbeiten und berichtigen: Jede Aktion der bisherigen Seite steht mit denselben Merkmalen
    auf der neuen, und die neue bietet nichts an, was die bisherige nicht hatte – für jede Standardrolle und jeden
    Zustand. Ausnahme wie in Teil 1 und 2a nur die Reiter des Rahmens (reiter_ausnahmen).
    """
    tenant = _mandant("niederschrift2b", neu=False)
    welt = _welt_2b(tenant)
    konten: dict[str, SessionUser] = {}
    fehlt, gesehen = vergleichen(
        tenant,
        welt["seiten_niederschrift"],
        ausnahmen=reiter_ausnahmen(tenant),
        client_fuer=_mit_konten(konten),
        vorher=_zustand(tenant, welt, konten),
    )
    assert not fehlt, "\n".join(fehlt)
    erwartet = welt["erwartet_niederschrift"]
    assert erwartet <= gezeigt(gesehen), sorted(erwartet - gezeigt(gesehen))
    # Formulare ohne Ziel: Niederschrift speichern (mit „weiter bearbeiten“) und Berichtigung mit Grund
    formulare = [felder for methode, ziel, felder in gesehen if methode == "POST" and not ziel]
    assert any("continue=1" in felder and "content_np" in felder for felder in formulare)
    assert any("reason" in felder and any(f.startswith("vote_") for f in felder) for felder in formulare)


def test_keine_funktion_des_beschlussregisters_entfaellt() -> None:
    """Beschlussregister mit Filtern, Beschlusskontrolle, Erledigungsvermerk, Übergabe, Auszug und CSV-Export."""
    tenant = _mandant("register2b", neu=False)
    welt = _welt_2b(tenant)
    fehlt, gesehen = vergleichen(
        tenant,
        welt["seiten_register"],
        ausnahmen=reiter_ausnahmen(tenant),
        vorher=_zustand(tenant, welt, {}),
    )
    assert not fehlt, "\n".join(fehlt)
    erwartet = welt["erwartet_register"]
    assert erwartet <= gezeigt(gesehen), sorted(erwartet - gezeigt(gesehen))
    # Die öffentliche Statusmeldung erscheint nur, wenn der Mandant sie anbietet (register_buergerportal)
    vermerke = [felder for methode, ziel, felder in gesehen if methode == "POST" and "tracking" in ziel]
    assert any("public_note" in felder for felder in vermerke) and any("public_note" not in f for f in vermerke)


# =============================================================================
# Zustände im Einzelnen
# =============================================================================


def _seite(client: Client, adresse: str) -> str:
    antwort = client.get(adresse)
    assert antwort.status_code == 200, adresse
    return antwort.content.decode()


def test_vier_augen_zeigt_hinweis_statt_genehmigen() -> None:
    tenant = _mandant("vieraugen2bn", neu=True)
    welt = _welt_2b(tenant)
    konten: dict[str, SessionUser] = {}
    client = _mit_konten(konten)(tenant, "Administrator")
    _zustand(tenant, welt, konten)("Administrator", "pruefung_vier_augen")
    html = _seite(client, welt["seiten_niederschrift"]["pruefung"])
    assert 'data-testid="vier-augen-hinweis"' in html and 'data-testid="genehmigen-formular"' not in html
    assert "/protocol/reject/" in html, "Zurückweisen bleibt"
    _zustand(tenant, welt, konten)("Administrator", "pruefung")
    html = _seite(client, welt["seiten_niederschrift"]["pruefung"])
    assert 'data-testid="genehmigen-formular"' in html and "Genehmigung der Niederschrift" in html


def test_ohne_ns_recht_kein_nichtoeffentlicher_teil() -> None:
    tenant = _mandant("lesen2bn", neu=True)
    welt = _welt_2b(tenant)
    html = _seite(_client(tenant, "Lesezugriff"), welt["seiten_niederschrift"]["veroeffentlicht"])
    for geheim in ("Vertraulicher", "Grundstück", "fassung=intern", "allgemein-noe"):
        assert geheim not in html, geheim
    assert "Allgemeiner Teil der Sitzung." in html and "Radweg Hauptstraße" in html
    voll = _seite(_client(tenant, "Administrator"), welt["seiten_niederschrift"]["veroeffentlicht"])
    for sichtbar in (
        "Vertraulicher allgemeiner Teil",
        "Vertraulicher Wortbeitrag",
        "Vertraulicher Grund",
        "fassung=intern",
    ):
        assert sichtbar in voll, sichtbar


def test_lesezugriff_auf_nichtoeffentliches_bleibt_protokolliert() -> None:
    tenant = _mandant("audit2bn", neu=True)
    welt = _welt_2b(tenant)
    vorher = SessionAuditLog.objects.filter(tenant=tenant, action="view").count()
    _seite(_client(tenant, "Sachbearbeiter"), welt["seiten_niederschrift"]["entwurf"])
    assert SessionAuditLog.objects.filter(tenant=tenant, action="view").count() == vorher + 1


def test_bearbeiten_speichert_ueber_die_neue_seite() -> None:
    """Das Formular der neuen Seite trägt dieselben Felder: Speichern und weiter bearbeiten führt zurück ins Formular."""
    tenant = _mandant("speichern2bn", neu=True)
    welt = _welt_2b(tenant)
    client = _client(tenant, "Protokollant")
    adresse = welt["seiten_niederschrift"]["bearbeiten_entwurf"]
    html = _seite(client, adresse)
    top = welt["tops"]["entwurf"]["nachtrag"]
    assert f'name="votes_yes_{top.pk}"' in html and 'data-testid="summen-aus-einzelstimmen"' in html
    assert "data-textblock-picker" in html and 'class="tb-select' in html
    antwort = client.post(
        adresse,
        {"content": "Neuer Text", "chair_name": "V", "recorder_name": "R", "continue": "1",
         f"protocol_note_{top.pk}": "Notiz", f"vote_result_{top.pk}": "rejected"},
    )  # fmt: skip
    assert antwort.status_code == 302 and antwort["Location"] == adresse
    assert SessionProtocol.objects.get(pk=welt["protokolle"]["entwurf"].pk).content == "Neuer Text"


# =============================================================================
# Darstellung
# =============================================================================


def _inhalt(html: str) -> str:
    return html[html.index('class="work-page-content"') : html.index("</main>")]


def test_hoechstens_ein_gefuellter_knopf_und_nur_an_seiner_stelle() -> None:
    """
    Niederschrift und Register: gefüllt höchstens die eine Hauptaktion im Kopfband, nie im Inhalt. Bearbeiten und
    Berichtigen: genau ein gefüllter Knopf, das Absenden am Ende des Formulars (wie „Vertretung eintragen“ in Teil 2a);
    das Kopfband bleibt dort ohne Hauptaktion, weil der Knopf zum Formular gehört.
    """
    tenant = _mandant("knoepfe2bn", neu=True)
    welt = _welt_2b(tenant)
    konten: dict[str, SessionUser] = {}
    client = _mit_konten(konten)(tenant, "Administrator")
    vorher = _zustand(tenant, welt, konten)
    for name, adresse in {**welt["seiten_niederschrift"], **welt["seiten_register"]}.items():
        vorher("Administrator", name)
        html = _seite(client, adresse)
        seite = html[html.index("<main") : html.index("</main>")]
        gesamt = seite.count("bg-primary-600 hover:bg-primary-700")
        im_inhalt = _inhalt(html).count("bg-primary-600 hover:bg-primary-700")
        assert gesamt <= 1, f"{name}: {gesamt} gefüllte Knöpfe"
        assert im_inhalt == (1 if name.startswith(("bearbeiten", "berichtigen")) else 0), (
            f"{name}: {im_inhalt} im Inhalt"
        )
        for farbe in ("green", "blue", "indigo", "amber", "teal", "red", "purple"):
            assert f"bg-{farbe}-600 hover:bg-{farbe}-700" not in seite, f"{name}: Knopf in {farbe}"


#: Farbfläche einer Pille oder eines Chips (bg-…-100), nicht Hover-Zustände
_PILLE = re.compile(r"(?<![\w:-])bg-(?:gray|green|blue|red|amber|indigo|purple|teal)-100")


def test_keine_tabellen_und_keine_pillen() -> None:
    """Listen statt Tabellen, Stand als Punkt mit Text statt farbiger Pille (Ergebnis, Umsetzung, Nummer, NÖ)."""
    tenant = _mandant("listen2bn", neu=True)
    welt = _welt_2b(tenant)
    client = _client(tenant, "Administrator")
    for adresse in (*welt["seiten_niederschrift"].values(), *welt["seiten_register"].values()):
        inhalt = _inhalt(_seite(client, adresse))
        assert "<table" not in inhalt, adresse
        assert not _PILLE.findall(inhalt), (adresse, _PILLE.findall(inhalt))


def test_listen_nicht_in_12_px() -> None:
    """Die neuen Vorlagen setzen Text in 13 bis 15 px, nie in text-xs (12 px) oder kleiner."""
    neu = Path(__file__).resolve().parents[3] / "templates" / "session" / "neu"
    for datei in NEUE_DATEIEN:
        text = (neu / datei).read_text(encoding="utf-8")
        for klein in ("text-xs", "text-[11px]", "text-[10px]", "text-[0.75rem]"):
            assert klein not in text, f"{datei}: {klein}"
        assert "c-ui.badge" not in text and "c-ui.card" not in text and "c-ui.kpi-tile" not in text, datei


def test_rechte_wie_bisher() -> None:
    """Lesezugriff sieht die Niederschrift, bearbeitet und berichtigt aber nicht; Revision sieht sie gar nicht."""
    tenant = _mandant("rechte2bn", neu=True)
    welt = _welt_2b(tenant)
    seiten = welt["seiten_niederschrift"]
    lesen, revision = _client(tenant, "Lesezugriff"), _client(tenant, "Revision")
    assert lesen.get(seiten["entwurf"]).status_code == 200
    assert lesen.get(seiten["bearbeiten_entwurf"]).status_code == 403
    assert lesen.get(seiten["berichtigen_top"]).status_code == 403
    assert revision.get(seiten["entwurf"]).status_code == 403
    assert len(rollen()) == 6
