# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Körperschaften im Mandanten (Issue #756): Oberfläche.

- Mandanten mit einer Körperschaft sehen keine Änderung: kein Filter, keine Auswahl, keine Kachel.
- Ab der zweiten aktiven Körperschaft: Filter „Körperschaft“ mit Gesamtansicht in Sitzungs-, Vorlagen- und
  Gremienlisten, Kalender und Arbeitsvorrat; Auswahl beim Anlegen von Gremium und Vorlage, Gremien nach
  Körperschaft gruppiert beim Anlegen einer Sitzung; Nummernkreise je Körperschaft.
- Verwaltung der Körperschaften (anlegen, bearbeiten, Standard festlegen) mit Eintrag im Prüfprotokoll.
- Keine zusätzlichen Abfragen je Zeile in den Listen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAuditLog,
    SessionBody,
    SessionCosignature,
    SessionMeeting,
    SessionNumberRange,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import body_service

pytestmark = pytest.mark.django_db
JAHR = timezone.localdate().year


@dataclass
class Welt:
    tenant: SessionTenant
    samtgemeinde: SessionBody
    mitglied: SessionBody
    rat_sg: SessionOrganization
    rat_md: SessionOrganization
    client: Client

    def url(self, pfad: str) -> str:
        return f"/session/{self.tenant.slug}{pfad}"

    def seite(self, pfad: str) -> str:
        antwort = self.client.get(self.url(pfad))
        assert antwort.status_code == 200, pfad
        return antwort.content.decode()


def _verwaltung(tenant: SessionTenant) -> Client:
    """Angemeldete Person mit der Standardrolle Administrator."""
    rollen = SessionRole.create_default_roles(tenant)
    user = cast(Any, UserFactory)(email=f"verwaltung@{tenant.slug}.example")
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(rollen["admin"])
    client = Client()
    client.force_login(user)
    return client


def _welt(*, zweite: bool = True) -> Welt:
    tenant = SessionTenant.objects.create(name="Samtgemeinde Muster", slug="sg-muster", short_name="SG")
    samtgemeinde = body_service.default_body(tenant)
    mitglied = SessionBody.objects.create(
        tenant=tenant,
        name="Gemeinde Musterdorf",
        short_name="MD",
        slug="musterdorf",
        body_type="mitgliedsgemeinde",
        parent=samtgemeinde,
        is_active=zweite,
    )
    rat_sg = SessionOrganization.objects.create(tenant=tenant, name="Samtgemeinderat", organization_type="council")
    rat_md = SessionOrganization.objects.create(
        tenant=tenant, name="Rat Musterdorf", organization_type="council", body=mitglied
    )
    return Welt(tenant, samtgemeinde, mitglied, rat_sg, rat_md, _verwaltung(tenant))


#: Pflichtangaben im Gremienformular
GREMIUM = {"organization_type": "committee", "invitation_period_days": "7", "allowance_amount": "0"}


def _sitzung(gremium: SessionOrganization, name: str, tage: int = 3) -> SessionMeeting:
    start = (timezone.now() + timedelta(days=tage)).replace(microsecond=0)
    return SessionMeeting.objects.create(
        tenant=gremium.tenant, organization=gremium, name=name, start=start, meeting_state="scheduled"
    )


def _vorlage(gremium: SessionOrganization, name: str, **felder: Any) -> SessionPaper:
    return SessionPaper.objects.create(
        tenant=gremium.tenant, main_organization=gremium, name=name, paper_type="proposal", **felder
    )


# =============================================================================
# Eine Körperschaft: keine Änderung
# =============================================================================


class TestEineKoerperschaft:
    def test_listen_und_formulare_ohne_koerperschaft(self) -> None:
        welt = _welt(zweite=False)
        _sitzung(welt.rat_sg, "Sitzung Samtgemeinderat")
        _vorlage(welt.rat_sg, "Vorlage Radweg", status="review")
        for pfad in (
            "/meetings/",
            "/papers/",
            "/organizations/",
            "/meetings/calendar/",
            "/papers/review/",
            "/cosignatures/",
            "/organizations/create/",
            "/papers/create/",
            "/meetings/create/",
            "/settings/numbering/",
        ):
            inhalt = welt.seite(pfad)
            assert 'name="body"' not in inhalt, pfad
            assert "Alle Körperschaften" not in inhalt, pfad
            assert "<optgroup" not in inhalt, pfad

    def test_keine_kachel_mit_nur_einer_koerperschaft(self) -> None:
        tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
        antwort = _verwaltung(tenant).get(f"/session/{tenant.slug}/settings/")
        assert antwort.status_code == 200
        assert "settings/koerperschaften/" not in antwort.content.decode()

    def test_anlegen_ohne_auswahl_setzt_standardkoerperschaft(self) -> None:
        welt = _welt(zweite=False)
        antwort = welt.client.post(welt.url("/organizations/create/"), {**GREMIUM, "name": "Bauausschuss"})
        assert antwort.status_code == 302
        assert SessionOrganization.objects.get(tenant=welt.tenant, name="Bauausschuss").body == welt.samtgemeinde

    def test_mehrere_ohne_eigene_abfrage(self) -> None:
        # Die Zahl der Körperschaften lädt der Mandant mit (SessionMixin) – ohne zweite Körperschaft keine Abfrage
        welt = _welt(zweite=False)
        tenant = body_service.annotate_body_count(SessionTenant.objects.all()).get(pk=welt.tenant.pk)
        with CaptureQueriesContext(connection) as abfragen:
            assert body_service.choice(tenant).active is False
            assert body_service.has_multiple(tenant) is False
        assert len(abfragen) == 0


# =============================================================================
# Mehrere Körperschaften: Filter und Gesamtansicht
# =============================================================================


class TestFilter:
    def test_sitzungen_gesamtansicht_und_je_koerperschaft(self) -> None:
        welt = _welt()
        _sitzung(welt.rat_sg, "Sitzung Samtgemeinderat")
        _sitzung(welt.rat_md, "Sitzung Rat Musterdorf")
        alle = welt.seite("/meetings/")
        assert "Alle Körperschaften" in alle
        assert "Sitzung Samtgemeinderat" in alle and "Sitzung Rat Musterdorf" in alle
        # Gremienfilter nach Körperschaft gruppiert
        assert '<optgroup label="Gemeinde Musterdorf">' in alle
        nur_md = welt.seite("/meetings/?body=musterdorf")
        assert "Sitzung Rat Musterdorf" in nur_md and "Sitzung Samtgemeinderat" not in nur_md
        # Unbekannte Kennung: Gesamtansicht statt Fehler
        unbekannt = welt.seite("/meetings/?body=gibt-es-nicht")
        assert "Sitzung Samtgemeinderat" in unbekannt and "Sitzung Rat Musterdorf" in unbekannt

    def test_standardkoerperschaft_umfasst_nachzuegler_ohne_angabe(self) -> None:
        welt = _welt()
        _sitzung(welt.rat_sg, "Sitzung Samtgemeinderat")
        _sitzung(welt.rat_md, "Sitzung Rat Musterdorf")
        # Gremium eines älteren Images ohne Körperschaft zählt zur Standardkörperschaft
        SessionOrganization.objects.filter(pk=welt.rat_sg.pk).update(body=None)
        inhalt = welt.seite(f"/meetings/?body={welt.samtgemeinde.slug}")
        assert "Sitzung Samtgemeinderat" in inhalt and "Sitzung Rat Musterdorf" not in inhalt

    def test_vorlagen_gremien_kalender_und_arbeitsvorrat(self) -> None:
        welt = _welt()
        _sitzung(welt.rat_sg, "Sitzung Samtgemeinderat")
        _sitzung(welt.rat_md, "Sitzung Rat Musterdorf")
        vorlage_sg = _vorlage(welt.rat_sg, "Vorlage Schulträger", status="review")
        vorlage_md = _vorlage(welt.rat_md, "Vorlage Dorfplatz", status="review")
        amt = SessionOrganization.objects.create(tenant=welt.tenant, name="Bauamt", organization_type="department")
        SessionCosignature.objects.create(paper=vorlage_sg, department=amt, order=1)
        SessionCosignature.objects.create(paper=vorlage_md, department=amt, order=1)

        erwartet = {
            "/papers/": ("Vorlage Dorfplatz", "Vorlage Schulträger"),
            "/papers/review/": ("Vorlage Dorfplatz", "Vorlage Schulträger"),
            "/cosignatures/": ("Vorlage Dorfplatz", "Vorlage Schulträger"),
            "/organizations/": ("Rat Musterdorf", "Samtgemeinderat"),
            "/meetings/calendar/": ("Sitzung Rat Musterdorf", "Sitzung Samtgemeinderat"),
        }
        for pfad, (eigen, fremd) in erwartet.items():
            zusatz = ""
            if pfad == "/meetings/calendar/":
                # Der Kalender zeigt den Monat der Sitzungen
                start = timezone.localtime(SessionMeeting.objects.get(name=eigen).start)
                zusatz = f"&year={start.year}&month={start.month}"
            gesamt = welt.seite(f"{pfad}?{zusatz.lstrip('&')}" if zusatz else pfad)
            assert eigen in gesamt and fremd in gesamt, pfad
            assert "Alle Körperschaften" in gesamt, pfad
            gefiltert = welt.seite(f"{pfad}?body=musterdorf{zusatz}")
            assert eigen in gefiltert and fremd not in gefiltert, pfad

    def test_kalender_blaettern_behaelt_den_filter(self) -> None:
        welt = _welt()
        inhalt = welt.seite("/meetings/calendar/?body=musterdorf&year=2031&month=5")
        assert "year=2031&amp;month=4" in inhalt and "body=musterdorf" in inhalt.split("Voriger Monat")[0]

    def test_listen_ohne_abfragen_je_zeile(self) -> None:
        welt = _welt()

        def zaehlen(pfad: str) -> int:
            welt.seite(pfad)  # Aufwärmen: Rechte- und Sitzungs-Cache
            with CaptureQueriesContext(connection) as abfragen:
                welt.seite(pfad)
            return len(abfragen)

        for n in range(2):
            _sitzung(welt.rat_md, f"Sitzung MD {n}", tage=3 + n)
            _vorlage(welt.rat_md, f"Vorlage MD {n}")
            SessionOrganization.objects.create(tenant=welt.tenant, name=f"Ausschuss MD {n}", body=welt.mitglied)
        vorher = {pfad: zaehlen(pfad) for pfad in ("/meetings/", "/papers/", "/organizations/")}
        for n in range(2, 6):
            _sitzung(welt.rat_md, f"Sitzung MD {n}", tage=3 + n)
            _vorlage(welt.rat_md, f"Vorlage MD {n}")
            SessionOrganization.objects.create(tenant=welt.tenant, name=f"Ausschuss MD {n}", body=welt.mitglied)
        for pfad, anzahl in vorher.items():
            assert zaehlen(pfad) == anzahl, pfad


# =============================================================================
# Anlegen: Auswahl der Körperschaft
# =============================================================================


class TestAnlegen:
    def test_gremium_mit_koerperschaft(self) -> None:
        welt = _welt()
        formular = welt.seite("/organizations/create/")
        assert 'name="body"' in formular
        # Vorbelegt mit der Standardkörperschaft
        assert f'value="{welt.samtgemeinde.pk}" selected' in formular
        antwort = welt.client.post(
            welt.url("/organizations/create/"),
            {**GREMIUM, "name": "Verwaltungsausschuss MD", "body": str(welt.mitglied.pk)},
        )
        assert antwort.status_code == 302
        neu = SessionOrganization.objects.get(tenant=welt.tenant, name="Verwaltungsausschuss MD")
        assert neu.body == welt.mitglied

    def test_nachzuegler_gremium_mit_sitzungen_bleibt_in_der_standardkoerperschaft(self) -> None:
        # Gremium ohne Angabe (älteres Image) gehört wirksam zur Standardkörperschaft – auch das sperrt den Wechsel
        welt = _welt()
        _sitzung(welt.rat_sg, "Sitzung Samtgemeinderat")
        SessionOrganization.objects.filter(pk=welt.rat_sg.pk).update(body=None)
        url = welt.url(f"/organizations/{welt.rat_sg.pk}/edit/")
        daten = {**GREMIUM, "name": "Samtgemeinderat", "organization_type": "council"}
        verschoben = welt.client.post(url, {**daten, "body": str(welt.mitglied.pk)})
        assert verschoben.status_code == 200
        assert "lässt sich nicht mehr ändern" in verschoben.content.decode()
        assert SessionOrganization.objects.get(pk=welt.rat_sg.pk).body_id is None
        # Die wirksame Körperschaft ausdrücklich speichern bleibt möglich
        gespeichert = welt.client.post(url, {**daten, "body": str(welt.samtgemeinde.pk)})
        assert gespeichert.status_code == 302
        assert SessionOrganization.objects.get(pk=welt.rat_sg.pk).body == welt.samtgemeinde

    def test_amt_gehoert_zur_standardkoerperschaft(self) -> None:
        welt = _welt()
        assert "Ämter gehören zur Standardkörperschaft „Samtgemeinde Muster“." in welt.seite("/organizations/create/")
        antwort = welt.client.post(
            welt.url("/organizations/create/"),
            {**GREMIUM, "name": "Bauamt", "organization_type": "department", "body": str(welt.mitglied.pk)},
        )
        assert antwort.status_code == 302
        assert SessionOrganization.objects.get(tenant=welt.tenant, name="Bauamt").body == welt.samtgemeinde
        # Ein Gremium der Mitgliedsgemeinde mit Sitzungen wird nicht still zum Amt der Verwaltung
        _sitzung(welt.rat_md, "Sitzung Rat Musterdorf")
        fehler = welt.client.post(
            welt.url(f"/organizations/{welt.rat_md.pk}/edit/"),
            {**GREMIUM, "name": "Rat Musterdorf", "organization_type": "department", "body": str(welt.mitglied.pk)},
        )
        assert fehler.status_code == 200
        assert "Sitzungen oder Vorlagen in einer anderen Körperschaft" in fehler.content.decode()
        welt.rat_md.refresh_from_db()
        assert (welt.rat_md.body, welt.rat_md.organization_type) == (welt.mitglied, "council")

    def test_gremium_mit_sitzungen_bleibt_in_seiner_koerperschaft(self) -> None:
        welt = _welt()
        _sitzung(welt.rat_md, "Sitzung Rat Musterdorf")
        antwort = welt.client.post(
            welt.url(f"/organizations/{welt.rat_md.pk}/edit/"),
            {**GREMIUM, "name": "Rat Musterdorf", "organization_type": "council", "body": str(welt.samtgemeinde.pk)},
        )
        assert antwort.status_code == 200
        assert "lässt sich nicht mehr ändern" in antwort.content.decode()
        welt.rat_md.refresh_from_db()
        assert welt.rat_md.body == welt.mitglied

    def test_fremde_koerperschaft_nicht_waehlbar(self) -> None:
        welt = _welt()
        fremd = SessionTenant.objects.create(name="Andere Verwaltung", slug="andere")
        antwort = welt.client.post(
            welt.url("/organizations/create/"),
            {**GREMIUM, "name": "Untergeschoben", "body": str(body_service.default_body(fremd).pk)},
        )
        assert antwort.status_code == 200
        assert not SessionOrganization.objects.filter(name="Untergeschoben").exists()

    def test_vorlage_folgt_dem_gremium_oder_der_auswahl(self) -> None:
        welt = _welt()
        formular = welt.seite("/papers/create/")
        assert 'name="body"' in formular and '<optgroup label="Gemeinde Musterdorf">' in formular
        basis = {"paper_type": "proposal", "is_public": "on"}
        antwort = welt.client.post(
            welt.url("/papers/create/"), {**basis, "name": "Dorfplatz", "main_organization": str(welt.rat_md.pk)}
        )
        assert antwort.status_code == 302
        assert SessionPaper.objects.get(name="Dorfplatz").body == welt.mitglied
        # Gewählte Körperschaft und federführendes Gremium passen nicht zusammen: Formularfehler
        fehler = welt.client.post(
            welt.url("/papers/create/"),
            {
                **basis,
                "name": "Widerspruch",
                "main_organization": str(welt.rat_md.pk),
                "body": str(welt.samtgemeinde.pk),
            },
        )
        assert fehler.status_code == 200
        assert "gehört zu einer anderen Körperschaft" in fehler.content.decode()
        assert not SessionPaper.objects.filter(name="Widerspruch").exists()
        # Ohne Gremium mit gewählter Körperschaft
        ohne = welt.client.post(
            welt.url("/papers/create/"), {**basis, "name": "Anfrage", "body": str(welt.mitglied.pk)}
        )
        assert ohne.status_code == 302
        assert SessionPaper.objects.get(name="Anfrage").body == welt.mitglied

    def test_vorlage_ohne_gremium_verlangt_die_koerperschaft(self) -> None:
        # Sonst landete sie still bei der Standardkörperschaft und wäre mit der Nummer dort festgelegt
        welt = _welt()
        antwort = welt.client.post(
            welt.url("/papers/create/"), {"paper_type": "proposal", "is_public": "on", "name": "Ohne Zuordnung"}
        )
        assert antwort.status_code == 200
        assert "Bitte die Körperschaft wählen" in antwort.content.decode()
        assert not SessionPaper.objects.filter(name="Ohne Zuordnung").exists()

    def test_nummernhinweis_beim_anlegen_je_koerperschaft(self) -> None:
        welt = _welt()
        # Gleicher Kreis und gleiche nächste Nummer für alle: ein Hinweis wie bisher
        einer = welt.seite("/papers/create/")
        assert "Die Nummer wird beim Speichern vergeben (Nummernkreis „" in einer
        assert "Nummernkreis der Körperschaft" not in einer
        SessionNumberRange.objects.create(
            tenant=welt.tenant, body=welt.mitglied, name="Musterdorf", pattern="MD/{jahr}/{lfd:3}"
        )
        je_koerperschaft = welt.seite("/papers/create/")
        assert "Die Nummer kommt aus dem Nummernkreis der Körperschaft" in je_koerperschaft
        assert "Samtgemeinde Muster: beim Speichern vergeben (Nummernkreis „" in je_koerperschaft
        assert (
            f"Gemeinde Musterdorf: beim Speichern vergeben (Nummernkreis „Musterdorf“, nächste MD/{JAHR}/001)"
            in je_koerperschaft
        )

    def test_unterlage_uebernimmt_die_koerperschaft_der_bezugsvorlage(self) -> None:
        welt = _welt()
        # Körperschaft ausdrücklich gewählt, kein federführendes Gremium
        bezug = SessionPaper.objects.create(
            tenant=welt.tenant, name="Anfrage Dorfplatz", paper_type="proposal", body=welt.mitglied
        )
        assert bezug.reference
        antwort = welt.client.post(welt.url(f"/papers/{bezug.pk}/unternummer/"), {"relation_type": "supplement"})
        assert antwort.status_code == 302
        assert SessionPaper.objects.get(parent_paper=bezug).body == welt.mitglied

    def test_feste_koerperschaft_ohne_angabe_zeigt_die_standardkoerperschaft(self) -> None:
        welt = _welt()
        vorlage = _vorlage(welt.rat_sg, "Haushalt")
        assert vorlage.reference
        # Wie von einem älteren Image angelegt: Nummer, aber keine Körperschaft
        SessionPaper.objects.filter(pk=vorlage.pk).update(body=None)
        assert "Körperschaft: Samtgemeinde Muster <span" in welt.seite(f"/papers/{vorlage.pk}/edit/")

    def test_sitzung_gremien_gruppiert_gemeinsame_nur_in_einer_koerperschaft(self) -> None:
        welt = _welt()
        formular = welt.seite("/meetings/create/")
        assert '<optgroup label="Gemeinde Musterdorf">' in formular
        assert "Die Sitzung gehört zur Körperschaft ihres Gremiums." in formular
        antwort = welt.client.post(
            welt.url("/meetings/create/"),
            {
                "name": "Gemeinsam über Grenzen",
                "organization": str(welt.rat_sg.pk),
                "joint_organizations": [str(welt.rat_md.pk)],
                "start": "2031-03-01T17:00",
                "is_public": "on",
            },
        )
        assert antwort.status_code == 200
        assert "nur mit Gremien derselben Körperschaft" in antwort.content.decode()
        assert not SessionMeeting.objects.filter(name="Gemeinsam über Grenzen").exists()

    def test_jahresplanung_gremien_gruppiert(self) -> None:
        welt = _welt()
        assert '<optgroup label="Gemeinde Musterdorf">' in welt.seite("/meetings/plan/")


# =============================================================================
# Nummernkreise je Körperschaft
# =============================================================================


class TestNummernkreise:
    def test_kreis_fuer_eine_koerperschaft_anlegen(self) -> None:
        welt = _welt()
        assert 'name="body"' in welt.seite("/settings/numbering/")
        antwort = welt.client.post(
            welt.url("/settings/numbering/save/"),
            {
                "action": "range",
                "name": "Vorlagen Musterdorf",
                "pattern": "MD/{jahr}/{lfd:3}",
                "reset": "yearly",
                "sub_pattern": "{parent}.{sub}",
                "is_active": "on",
                "body": str(welt.mitglied.pk),
            },
        )
        assert antwort.status_code == 302
        kreis = SessionNumberRange.objects.get(tenant=welt.tenant, name="Vorlagen Musterdorf")
        assert kreis.body == welt.mitglied
        vorlage = _vorlage(welt.rat_md, "Dorfplatz")
        from apps.session.services import numbering_service

        assert numbering_service.range_for(welt.tenant, "proposal", vorlage.body) == kreis

    def test_fremde_koerperschaft_abgewiesen(self) -> None:
        welt = _welt()
        fremd = body_service.default_body(SessionTenant.objects.create(name="Andere", slug="andere"))
        welt.client.post(
            welt.url("/settings/numbering/save/"),
            {
                "action": "range",
                "name": "Untergeschoben",
                "pattern": "X/{jahr}/{lfd:3}",
                "reset": "yearly",
                "body": str(fremd.pk),
            },
        )
        assert not SessionNumberRange.objects.filter(name="Untergeschoben").exists()


# =============================================================================
# Verwaltung der Körperschaften
# =============================================================================


class TestVerwaltung:
    def test_kachel_erst_ab_der_zweiten(self) -> None:
        welt = _welt()
        assert "settings/koerperschaften/" in welt.seite("/settings/")

    def test_kachel_bleibt_mit_deaktivierter_zweiter_zum_reaktivieren(self) -> None:
        welt = _welt(zweite=False)
        assert "settings/koerperschaften/" in welt.seite("/settings/")
        antwort = welt.client.post(
            welt.url(f"/settings/koerperschaften/{welt.mitglied.pk}/"),
            {"name": "Gemeinde Musterdorf", "short_name": "MD", "slug": "musterdorf", "is_active": "on"},
        )
        assert antwort.status_code == 302
        welt.mitglied.refresh_from_db()
        assert welt.mitglied.is_active

    def test_geleerte_kurzkennung_bleibt_beim_bearbeiten(self) -> None:
        # Filteradressen (?body=musterdorf) gelten weiter
        welt = _welt()
        antwort = welt.client.post(
            welt.url(f"/settings/koerperschaften/{welt.mitglied.pk}/"),
            {"name": "Gemeinde Musterdorf", "short_name": "MD", "slug": "", "is_active": "on"},
        )
        assert antwort.status_code == 302
        welt.mitglied.refresh_from_db()
        assert welt.mitglied.slug == "musterdorf"

    def test_anlegen_bearbeiten_standard_mit_protokoll(self) -> None:
        welt = _welt()
        liste = welt.seite("/settings/koerperschaften/")
        assert "Gemeinde Musterdorf" in liste and "Samtgemeinde Muster" in liste
        antwort = welt.client.post(
            welt.url("/settings/koerperschaften/neu/"),
            {
                "name": "Gemeinde Beispielhausen",
                "short_name": "BH",
                "body_type": "mitgliedsgemeinde",
                "parent": str(welt.samtgemeinde.pk),
                "is_active": "on",
            },
        )
        assert antwort.status_code == 302
        neu = SessionBody.objects.get(tenant=welt.tenant, name="Gemeinde Beispielhausen")
        assert (neu.slug, neu.parent, neu.is_default) == ("bh", welt.samtgemeinde, False)
        assert SessionAuditLog.objects.filter(tenant=welt.tenant, model_name="SessionBody", object_id=neu.pk).exists()

        bearbeitet = welt.client.post(
            welt.url(f"/settings/koerperschaften/{neu.pk}/"),
            {"name": "Gemeinde Beispielhausen", "short_name": "BHN", "slug": "bh", "is_active": "on"},
        )
        assert bearbeitet.status_code == 302
        neu.refresh_from_db()
        assert neu.short_name == "BHN"

        standard = welt.client.post(welt.url(f"/settings/koerperschaften/{neu.pk}/standard/"))
        assert standard.status_code == 302
        neu.refresh_from_db()
        welt.samtgemeinde.refresh_from_db()
        assert neu.is_default and not welt.samtgemeinde.is_default

    def test_formularfehler_statt_serverfehler(self) -> None:
        welt = _welt()
        doppelt = welt.client.post(
            welt.url("/settings/koerperschaften/neu/"), {"name": "Doppelt", "slug": "musterdorf", "is_active": "on"}
        )
        assert doppelt.status_code == 200
        assert "schon vergeben" in doppelt.content.decode()
        # Standardkörperschaft lässt sich nicht deaktivieren
        aus = welt.client.post(
            welt.url(f"/settings/koerperschaften/{welt.samtgemeinde.pk}/"),
            {"name": welt.samtgemeinde.name, "slug": welt.samtgemeinde.slug},
        )
        assert aus.status_code == 200
        assert "nicht deaktivieren" in aus.content.decode()
        # Kein Kreis über die übergeordnete Körperschaft
        kreis = welt.client.post(
            welt.url(f"/settings/koerperschaften/{welt.samtgemeinde.pk}/"),
            {
                "name": welt.samtgemeinde.name,
                "slug": welt.samtgemeinde.slug,
                "parent": str(welt.mitglied.pk),
                "is_active": "on",
            },
        )
        assert kreis.status_code == 200
        assert "über sich selbst" in kreis.content.decode()
        welt.samtgemeinde.refresh_from_db()
        assert welt.samtgemeinde.parent is None and welt.samtgemeinde.is_active
