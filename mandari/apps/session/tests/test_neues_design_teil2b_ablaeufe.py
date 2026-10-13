# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes, Teil 2b: Abläufe (Issue #944).

Geprüft: Umlaufbeschlüsse, Umlaufbeschluss, Anträge, Antrag bearbeiten, In Vorlage umwandeln, Ladung und Rückmeldungen
haben mit eingeschaltetem Schalter eine eigene neue Vorlage, ohne Schalter bleiben die bisherigen. Keine Funktion
entfällt und keine kommt hinzu: Jede Aktion der bisherigen Seite (Links, Formulare mit Methode, Feldern und Optionen,
Rückfragen, Sperren) steht auch auf der neuen, für jede Standardrolle und in jedem Zustand des Bestands: Umläufe offen
mit Stimmen, abgelaufen und nichtöffentlich, angenommen mit Vorlage, abgebrochen; Anträge in allen Status mit Filtern,
Suche ohne Treffer und zweiter Seite; Sitzungen vor dem Versand (Frist offen, verstrichen), nach Ladung und Nachladung
mit Zusage, Absage mit Grund und Vertretung, offener Antwort, Brief und Fehlschlag, abgesagt und ohne Empfänger;
Erinnerungen an und aus. Die Fehleransicht von „Antrag bearbeiten“ nach POST steht ebenfalls im neuen Erscheinungsbild.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionApplication,
    SessionCircularResolution,
    SessionCircularVote,
    SessionInvitationDispatch,
    SessionInvitationRecipient,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionTenant,
    SessionUser,
)
from apps.session.services import invitation_response_service, invitation_service
from apps.session.tests.gleichstand import aktionen, fehlende, gezeigt, neue, reiter_ausnahmen, vergleichen
from apps.session.tests.test_neues_design import _client, _mandant, _volle_welt, _vorlagen

pytestmark = pytest.mark.django_db

#: Seiten dieses Teils: Name im Bestand → neue Vorlage
SEITEN = {
    "umlaufbeschluesse": "session/neu/umlaufbeschluesse.html",
    "umlauf_offen": "session/neu/umlaufbeschluss.html",
    "antraege": "session/neu/antraege.html",
    "antrag_bearbeiten": "session/neu/antrag_bearbeiten.html",
    "antrag_umwandeln": "session/neu/antrag_umwandeln.html",
    "ladung_versandt": "session/neu/ladung.html",
    "rueckmeldungen_versandt": "session/neu/rueckmeldungen.html",
}
#: Seiten, deren Hauptaktion das Absenden eines Formulars im Inhalt ist (wie Vertretungen): ein gefüllter Knopf dort
FORMULAR_ALS_HAUPTAKTION = ("umlaufbeschluesse", "antrag_bearbeiten", "antrag_umwandeln", "ladung_")
#: Neue Vorlagen dieses Teils (für die Prüfung der Schriftgrößen)
DATEIEN = (
    "umlaufbeschluesse.html",
    "umlaufbeschluss.html",
    "_umlauf_stand.html",
    "antraege.html",
    "antrag_bearbeiten.html",
    "antrag_umwandeln.html",
    "ladung.html",
    "rueckmeldungen.html",
)


def _person(tenant: SessionTenant, vorname: str, nachname: str, email: str = "", **extra: Any) -> SessionPerson:
    return SessionPerson.objects.create(
        tenant=tenant, given_name=vorname, family_name=nachname, email=email, start_date=date(2025, 1, 1), **extra
    )


def _sitzung(tenant: SessionTenant, gremium: SessionOrganization, name: str, tage: int, **extra: Any) -> SessionMeeting:
    start = (timezone.now() + timedelta(days=tage)).replace(hour=17, minute=0, second=0, microsecond=0)
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, organization=gremium, name=name, start=start, meeting_state="scheduled", **extra
    )
    SessionAgendaItem.objects.create(meeting=sitzung, number="1", order=1, name="Eröffnung")
    return sitzung


def _welt_2b(tenant: SessionTenant) -> dict[str, Any]:
    """
    Bestand für die Abläufe auf der Grundlage von ``_volle_welt``: Besetzung des Rats mit E-Mail, Brief, ohne E-Mail
    und einer Vertretung ohne eigenen Sitz; vier Umläufe (offen mit Ja und Nein, abgelaufen und nichtöffentlich,
    angenommen mit freigegebener Vorlage, abgebrochen); Anträge in allen sieben Status (dringend, mit Gremium, mit
    Begründung und Beschlussvorschlag, umgewandelt mit Vorlage) und so vielen weiteren, dass es eine zweite Seite gibt;
    Sitzungen vor dem Versand (Frist offen, Frist verstrichen), nach Ladung und Nachladung (Zusage, Absage mit Grund
    und Vertretung, offene Antwort, Brief teils versandt, ein Fehlschlag), abgesagt und ohne Besetzung.
    """
    welt = _volle_welt(tenant)
    slug = tenant.slug
    sitzung = welt["sitzung_obj"]
    rat = sitzung.organization
    heute = timezone.localdate()

    # Besetzung des Rats: E-Mail, Brief (mit Anschrift), ohne E-Mail und eine Vertretung ohne eigenen Sitz
    anna = _person(tenant, "Anna", "Amberg", "anna@example.org")
    bernd = _person(tenant, "Bernd", "Birk", "bernd@example.org")
    clara = _person(tenant, "Clara", "Conrad", delivery_channel="letter")
    cast(Any, clara).set_address_encrypted("Postweg 1\n12345 Musterstadt")  # Accessor zur Laufzeit
    clara.save()
    dora = _person(tenant, "Dora", "Dahl", "dora@example.org")
    emil = _person(tenant, "Emil", "Ohne")
    vera = _person(tenant, "Vera", "Vertretung", "vera@example.org")
    for person in (anna, bernd, clara, dora, emil):
        SessionOrganizationMembership.objects.create(organization=rat, person=person, start_date=date(2025, 1, 1))
    SessionOrganizationMembership.objects.create(
        organization=rat, person=vera, start_date=date(2025, 1, 1), substitute_for=anna, has_voting_rights=False
    )

    # Umläufe: offen mit Stimmen, abgelaufen und nichtöffentlich, angenommen mit Vorlage, abgebrochen
    sportstaetten = SessionPaper.objects.create(
        tenant=tenant, name="Sportstätten", reference="V/2026/020", status="approved", main_organization=rat
    )
    offen = SessionCircularResolution.objects.create(
        tenant=tenant,
        organization=rat,
        reference="U/2026/0001",
        title="Zuschuss Sportverein",
        resolution_text="Der Zuschuss wird gewährt.",
        deadline=heute + timedelta(days=7),
    )
    SessionCircularVote.objects.create(circular=offen, person=anna, vote="yes", received_at=heute)
    SessionCircularVote.objects.create(circular=offen, person=bernd, vote="no", received_at=heute)
    abgelaufen = SessionCircularResolution.objects.create(
        tenant=tenant,
        organization=rat,
        reference="U/2026/0002",
        title="Grundstückstausch",
        resolution_text="Der Tausch wird genehmigt.",
        deadline=heute - timedelta(days=2),
        is_public=False,
    )
    angenommen = SessionCircularResolution.objects.create(
        tenant=tenant,
        organization=rat,
        reference="U/2026/0003",
        title="Sportstätten",
        resolution_text="Die Sportstätten werden saniert.",
        deadline=heute - timedelta(days=10),
        paper=sportstaetten,
        status="adopted",
        result_note="Einstimmig",
        decided_at=timezone.now() - timedelta(days=9),
    )
    abgebrochen = SessionCircularResolution.objects.create(
        tenant=tenant,
        organization=rat,
        title="Alte Eilsache",
        resolution_text="Hinfällig.",
        deadline=heute - timedelta(days=20),
        status="cancelled",
    )

    # Anträge in allen Status; der offene mit Begründung und Beschlussvorschlag, einer dringend, einer mit Gremium
    antrag = welt["antrag_obj"]
    SessionApplication.objects.filter(pk=antrag.pk).update(justification="Weil", resolution_proposal="Bänke")
    antraege = {"submitted": antrag}
    for status, extra in (
        ("received", {"is_urgent": True, "urgency_reason": "Frist"}),
        ("in_review", {"target_organization": rat, "application_type": "inquiry"}),
        ("accepted", {}),
        ("rejected", {}),
        ("converted", {}),
        ("withdrawn", {}),
    ):
        antraege[status] = SessionApplication.objects.create(
            tenant=tenant,
            title=f"Antrag {status}",
            submitter_name="Fraktion",
            submitter_email="f@example.org",
            status=status,
            **extra,
        )
    SessionPaper.objects.create(
        tenant=tenant, name="Antrag umgewandelt", reference="V/2026/021", source_application=antraege["converted"]
    )
    for nummer in range(16):
        SessionApplication.objects.create(
            tenant=tenant, title=f"Weiterer Antrag {nummer}", submitter_name="F", submitter_email="f@example.org"
        )

    # Sitzungen: vor dem Versand, Frist verstrichen, nach Ladung und Nachladung, abgesagt, Gremium ohne Besetzung
    sitzungsdienst = SessionUser.objects.create(user=cast(Any, UserFactory)(), tenant=tenant)
    ueberfaellig = _sitzung(tenant, rat, "Eilsitzung", 3)
    versandt = _sitzung(tenant, rat, "Ratssitzung Juni", 14)
    abgesagt = _sitzung(tenant, rat, "Ausgefallene Sitzung", 12, cancelled=True)
    leer = _sitzung(tenant, SessionOrganization.objects.create(tenant=tenant, name="Ältestenrat"), "Ältestenrat", 20)
    mail.outbox = []
    ladung = invitation_service.send_invitations(versandt, sent_by=sitzungsdienst)
    SessionAgendaItem.objects.create(meeting=versandt, number="2", order=2, name="Nachtrag", is_supplementary=True)
    nachladung = invitation_service.send_invitations(versandt, sent_by=sitzungsdienst, dispatch_type="supplementary")
    invitation_response_service.record_response(versandt, bernd, decision="confirm", source="staff")
    invitation_response_service.record_response(
        versandt, anna, decision="decline", source="staff", reason="Dienstreise", substitute_requested=True
    )
    invitation_response_service.mark_letters_sent(ladung)
    invitation_response_service.acknowledge_meeting(clara, versandt, via="staff")
    SessionInvitationRecipient.objects.filter(dispatch=nachladung, person=dora).update(status="failed")

    def adresse(name: str, **kw: Any) -> str:
        return reverse(f"session:{name}", kwargs={"tenant_slug": slug, **kw})

    def sitzung_kw(meeting: SessionMeeting) -> dict[str, Any]:
        return {"meeting_id": meeting.pk}

    liste = adresse("applications")
    welt.update(
        {
            "umlaufbeschluesse": adresse("circulars"),
            "umlauf_offen": adresse("circular_detail", circular_id=offen.pk),
            "umlauf_abgelaufen": adresse("circular_detail", circular_id=abgelaufen.pk),
            "umlauf_angenommen": adresse("circular_detail", circular_id=angenommen.pk),
            "umlauf_abgebrochen": adresse("circular_detail", circular_id=abgebrochen.pk),
            "antraege": liste,
            "antraege_seite_2": f"{liste}?page=2",
            "antraege_gefiltert": f"{liste}?status=in_review&type=inquiry",
            "antraege_ohne_treffer": f"{liste}?q=Gibtsnicht",
            "antrag_bearbeiten": adresse("application_process", application_id=antrag.pk),
            "antrag_bearbeiten_umgewandelt": adresse("application_process", application_id=antraege["converted"].pk),
            "antrag_umwandeln": adresse("application_convert", application_id=antraege["in_review"].pk),
            "antrag_umwandeln_offen": adresse("application_convert", application_id=antrag.pk),
            "ladung_vorher": adresse("meeting_invitation", **sitzung_kw(sitzung)),
            "ladung_ueberfaellig": adresse("meeting_invitation", **sitzung_kw(ueberfaellig)),
            "ladung_versandt": adresse("meeting_invitation", **sitzung_kw(versandt)),
            "ladung_abgesagt": adresse("meeting_invitation", **sitzung_kw(abgesagt)),
            "ladung_ohne_empfaenger": adresse("meeting_invitation", **sitzung_kw(leer)),
            "rueckmeldungen_vorher": adresse("meeting_invitation_status", **sitzung_kw(sitzung)),
            "rueckmeldungen_versandt": adresse("meeting_invitation_status", **sitzung_kw(versandt)),
            "rueckmeldungen_ohne_erinnerung": adresse("meeting_invitation_status", **sitzung_kw(versandt)),
            "antraege_obj": antraege,
            "versandt_obj": versandt,
            "nachladung_obj": nachladung,
        }
    )
    serienbrief = {"meeting_id": versandt.pk, "dispatch_id": nachladung.pk}
    welt["erwartet_2b"] = {
        ("POST", adresse("circular_create")),
        ("GET", adresse("circular_detail", circular_id=offen.pk)),
        ("POST", adresse("circular_vote", circular_id=offen.pk)),
        ("POST", adresse("circular_close", circular_id=offen.pk)),
        ("GET", adresse("paper_detail", paper_id=sportstaetten.pk)),
        ("GET", adresse("application_detail", application_id=antraege["withdrawn"].pk)),
        ("GET", "?page=2"),
        ("GET", "?page=1"),
        ("GET", ""),  # Filter der Anträge
        ("POST", ""),  # Bearbeiten, Umwandeln und Versand senden an die eigene Adresse
        ("GET", adresse("meeting_invitation_status", **sitzung_kw(versandt))),
        ("GET", adresse("meeting_agenda_pdf", **sitzung_kw(versandt))),
        ("GET", adresse("meeting_agenda_pdf", **sitzung_kw(versandt)) + "?variante=nachtrag"),
        ("GET", adresse("meeting_ics", **sitzung_kw(versandt))),
        ("POST", adresse("meeting_invitation_remind", **sitzung_kw(versandt))),
        ("POST", adresse("meeting_response_entry", meeting_id=versandt.pk, person_id=dora.pk)),
        ("GET", adresse("meeting_invitation_proof", **sitzung_kw(versandt))),
        ("GET", adresse("meeting_serial_letter", fmt="pdf", **serienbrief)),
        ("GET", adresse("meeting_serial_letter", fmt="csv", **serienbrief)),
        ("POST", adresse("meeting_letters_sent", **serienbrief)),
    }
    return welt


def _erinnerung(tenant: SessionTenant) -> Any:
    """``vorher`` für ``vergleichen``: Erinnerungen nur auf der Seite „ohne Erinnerung“ abgeschaltet."""

    def vorher(rolle: str, name: str) -> None:
        einstellungen = {"rsvp_enabled": False} if name == "rueckmeldungen_ohne_erinnerung" else {}
        SessionTenant.objects.filter(pk=tenant.pk).update(reminder_settings=einstellungen)

    return vorher


#: Alle Seiten des Bestands für den Gleichstand (Name → Schlüssel in der Welt)
VERGLEICH = (
    "umlaufbeschluesse",
    "umlauf_offen",
    "umlauf_abgelaufen",
    "umlauf_angenommen",
    "umlauf_abgebrochen",
    "antraege",
    "antraege_seite_2",
    "antraege_gefiltert",
    "antraege_ohne_treffer",
    "antrag_bearbeiten",
    "antrag_bearbeiten_umgewandelt",
    "antrag_umwandeln",
    "antrag_umwandeln_offen",
    "ladung_vorher",
    "ladung_ueberfaellig",
    "ladung_versandt",
    "ladung_abgesagt",
    "ladung_ohne_empfaenger",
    "rueckmeldungen_vorher",
    "rueckmeldungen_versandt",
    "rueckmeldungen_ohne_erinnerung",
)


# =============================================================================
# Vorlagen
# =============================================================================


def test_seiten_waehlen_ihre_vorlage_nach_dem_schalter() -> None:
    tenant = _mandant("ablaeufe2b", neu=False)
    welt = _welt_2b(tenant)
    client = _client(tenant, "Administrator")
    for name, vorlage in SEITEN.items():
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
        alt = client.get(welt[name])
        assert alt.status_code == 200, name
        assert vorlage not in _vorlagen(alt) and "session/base_session_alt.html" in _vorlagen(alt), name
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        neu = client.get(welt[name])
        assert neu.status_code == 200, name
        assert {vorlage, "session/base_session_neu.html"} <= _vorlagen(neu), name
        assert 'data-gestaltung="neu"' in neu.content.decode(), name


# =============================================================================
# Keine Funktion entfällt, keine kommt hinzu
# =============================================================================


def test_gleichstand_der_ablaeufe_fuer_alle_rollen() -> None:
    """
    Jede Aktion der bisherigen Seiten steht mit denselben Merkmalen (Rückfrage, Sperre, Optionen) auf der neuen, für
    alle sechs Standardrollen und jeden Zustand des Bestands; die neue Seite bietet nichts an, was die bisherige nicht
    hatte (außer erreichbaren Links wie „Filter zurücksetzen“ und dem Weg zurück). Einzige Ausnahme wie in Teil 1 und
    2a: die vier Seiten, die die bisherige Seitenleiste direkt verlinkte und die jetzt als Reiter über ihrer Liste
    stehen – eine Änderung des Rahmens, nicht der Seiten.
    """
    tenant = _mandant("gleich2b", neu=False)
    welt = _welt_2b(tenant)
    fehlt, gesehen = vergleichen(
        tenant,
        {name: welt[name] for name in VERGLEICH},
        ausnahmen=reiter_ausnahmen(tenant),
        vorher=_erinnerung(tenant),
    )
    assert not fehlt, "\n".join(fehlt)
    # Der Bestand zeigt wirklich, was er prüfen soll (sonst wäre der Vergleich leer)
    assert welt["erwartet_2b"] <= gezeigt(gesehen), sorted(welt["erwartet_2b"] - gezeigt(gesehen))
    # … samt den Merkmalen: Rückfrage beim Abbrechen und Erinnern, gesperrter Versand ohne Empfänger
    merkmale = {(methode, adresse): m for (methode, adresse, _), m in gesehen.items()}
    offen = SessionCircularResolution.objects.get(tenant=tenant, title="Zuschuss Sportverein")
    abschluss = reverse("session:circular_close", kwargs={"tenant_slug": tenant.slug, "circular_id": offen.pk})
    assert "data-confirm=Umlauf wirklich abbrechen?" in merkmale[("POST", abschluss)]
    erinnern = reverse(
        "session:meeting_invitation_remind", kwargs={"tenant_slug": tenant.slug, "meeting_id": welt["versandt_obj"].pk}
    )
    assert any(m.startswith("data-confirm=Erinnerung an ") for m in merkmale[("POST", erinnern)])
    versand = [m for (methode, adresse, felder), m in gesehen.items() if (methode, adresse) == ("POST", "") and felder]
    assert any("disabled" in m for m in versand), "Versand ohne zustellbare Empfänger muss gesperrt bleiben"


def test_fehleransicht_nach_post_im_neuen_erscheinungsbild() -> None:
    """
    „Antrag bearbeiten“ mit ungültigem Status (umwandeln geht nur über die Umwandlung) zeigt das Formular mit dem
    Fehler am Feld – im neuen Erscheinungsbild mit denselben Aktionen wie bisher und den eingegebenen Werten.
    """
    tenant = _mandant("fehler2b", neu=False)
    welt = _welt_2b(tenant)
    antrag = welt["antraege_obj"]["submitted"]
    client = _client(tenant, "Sachbearbeiter")
    daten = {"status": "converted", "target_organization": "", "processing_notes": "Rückfrage beim Einreicher"}
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
    alt = client.post(welt["antrag_bearbeiten"], daten)
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    neu = client.post(welt["antrag_bearbeiten"], daten)
    assert alt.status_code == neu.status_code == 200
    assert "session/neu/antrag_bearbeiten.html" in _vorlagen(neu)
    html = neu.content.decode()
    assert 'aria-invalid="true"' in html and "id_status-error" in html
    assert "Rückfrage beim Einreicher" in html
    bisher, jetzt = aktionen(alt), aktionen(neu)
    ausnahmen = reiter_ausnahmen(tenant)
    assert not fehlende(bisher, jetzt, ausnahmen)
    assert not [a for a in neue(bisher, jetzt, ausnahmen) if a[0] != "GET"]
    assert SessionApplication.objects.get(pk=antrag.pk).status == "submitted"


# =============================================================================
# Darstellung
# =============================================================================


def test_zaehler_werden_saetze_und_listen_ersetzen_tabellen() -> None:
    tenant = _mandant("saetze2b", neu=True)
    welt = _welt_2b(tenant)
    client = _client(tenant, "Administrator")

    rueckmeldungen = client.get(welt["rueckmeldungen_versandt"]).content.decode()
    # Empfänger: Anna, Bernd, Dora (E-Mail), Clara (Brief) und Vera (Vertretungsanfrage); Emil ohne E-Mail nicht
    assert "2 von 5 haben geantwortet (1 Zusage, 1 Absage)" in rueckmeldungen
    assert "Dienstreise" in rueckmeldungen and "Vertretung für Anna Amberg" in rueckmeldungen
    assert "<table" not in rueckmeldungen
    assert 'data-testid="erinnerung"' in rueckmeldungen

    umlauf = client.get(welt["umlauf_offen"]).content.decode()
    assert "Ja 1, Nein 1, Enthaltung 0. 2 von 5 Stimmberechtigten haben geantwortet" in umlauf
    # Das Auszählungsergebnis ist nur ein Vorschlag; festgestellt wird es erst mit einem der Knöpfe
    assert "Vorschlag nach der Auszählung:" in umlauf
    assert 'Ja</span><span class="tabular-nums">, eingegangen am' in umlauf

    SessionTenant.objects.filter(pk=tenant.pk).update(reminder_settings={"rsvp_enabled": False})
    assert 'data-testid="erinnerung-aus"' in client.get(welt["rueckmeldungen_versandt"]).content.decode()

    gesperrt = client.get(welt["ladung_abgesagt"]).content.decode()
    assert 'data-testid="ladung-gesperrt"' in gesperrt and 'name="dispatch_type"' not in gesperrt
    # Der Stand steht direkt vor dem Doppelpunkt, ohne Leerzeichen aus dem Zeilenende der Teilvorlage
    assert "Ladungsfrist verstrichen</span></span>:" in client.get(welt["ladung_ueberfaellig"]).content.decode()
    assert "Noch nicht versandt</span>:" in client.get(welt["ladung_vorher"]).content.decode()


def test_nichtoeffentlicher_umlauf_nur_mit_recht() -> None:
    tenant = _mandant("noe2b", neu=True)
    welt = _welt_2b(tenant)
    lesen = _client(tenant, "Lesezugriff")
    assert "Grundstückstausch" not in lesen.get(welt["umlaufbeschluesse"]).content.decode()
    assert lesen.get(welt["umlauf_abgelaufen"]).status_code == 404
    liste = _client(tenant, "Sachbearbeiter").get(welt["umlaufbeschluesse"]).content.decode()
    assert "Grundstückstausch" in liste and "Nichtöffentlich" in liste and "Frist verstrichen" in liste


def test_hoechstens_ein_gefuellter_knopf_und_nur_wo_vorgesehen() -> None:
    """
    Auf den neuen Seiten trägt höchstens ein Knopf die Füllfarbe: die Hauptaktion im Kopfband oder, wo das Absenden
    eines Formulars die Hauptaktion ist (Umlauf starten, Speichern, Umwandeln, Versenden), dessen Knopf.
    """
    tenant = _mandant("knoepfe2b", neu=True)
    welt = _welt_2b(tenant)
    client = _client(tenant, "Administrator")
    for name in VERGLEICH:
        html = client.get(welt[name]).content.decode()
        seite = html[html.index('data-gestaltung="neu"') : html.index("</main>")]
        inhalt = html[html.index('class="work-page-content"') : html.index("</main>")]
        assert seite.count("bg-primary-600 hover:bg-primary-700") <= 1, f"{name}: mehr als ein gefüllter Knopf"
        erlaubt = 1 if name.startswith(FORMULAR_ALS_HAUPTAKTION) else 0
        assert inhalt.count("bg-primary-600 hover:bg-primary-700") <= erlaubt, f"{name}: gefüllter Knopf im Inhalt"
        for farbe in ("green", "blue", "indigo", "amber", "red"):
            assert f"bg-{farbe}-600 hover:bg-{farbe}-700" not in seite, f"{name}: Knopf in {farbe}"


def test_listen_nicht_in_12_px() -> None:
    neu = Path(__file__).resolve().parents[3] / "templates" / "session" / "neu"
    for datei in DATEIEN:
        text = (neu / datei).read_text(encoding="utf-8")
        for klein in ("text-xs", "text-[11px]", "text-[10px]", "text-[0.75rem]"):
            assert klein not in text, f"{datei}: {klein}"
        assert "c-ui.kpi-tile" not in text and "c-ui.badge" not in text and "c-ui.card" not in text, datei


def test_versand_und_rueckmeldung_wirken_wie_bisher() -> None:
    """Die neuen Seiten senden an dieselben Ziele: Versand der Ladung und Eintrag einer Rückmeldung wirken."""
    tenant = _mandant("wirkung2b", neu=True)
    welt = _welt_2b(tenant)
    client = _client(tenant, "Administrator")
    vorher = SessionInvitationDispatch.objects.filter(meeting=welt["sitzung_obj"]).count()
    antwort = client.post(welt["ladung_vorher"], {"dispatch_type": "invitation", "subject": "Ladung", "message": ""})
    assert antwort.status_code == 302
    assert SessionInvitationDispatch.objects.filter(meeting=welt["sitzung_obj"]).count() == vorher + 1
    dora = SessionPerson.objects.get(tenant=tenant, family_name="Dahl")
    eintrag = reverse(
        "session:meeting_response_entry",
        kwargs={"tenant_slug": tenant.slug, "meeting_id": welt["versandt_obj"].pk, "person_id": dora.pk},
    )
    assert client.post(eintrag, {"action": "decline", "reason": "Urlaub"}).status_code == 302
    assert "Urlaub" in client.get(welt["rueckmeldungen_versandt"]).content.decode()
