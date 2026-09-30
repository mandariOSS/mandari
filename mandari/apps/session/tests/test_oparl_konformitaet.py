# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OParl-1.1-Konformität der Session-Schnittstelle (``/session/<slug>/api/oparl/``).

Geprüft werden die Punkte, in denen die Ausgabe von der Spezifikation abwich: ``organizationType`` mit
den Werten der Spezifikation statt interner Schlüssel, ``File.date`` als Datum, ``license`` an System und
Body, der Sitzungsort als Location-Objekt (die bisherigen ``mandari:location*``-Felder bleiben),
``legislativeTerm`` als Pflichtfeld und bedingte Anfragen (ETag/304). Dazu läuft die gesamte Ausgabe
durch die Typprüfung in ``oparl_api/tests/konformitaet.py``.

Der Sitzungsort gehört zur Sitzung: Wird sie zurückgenommen, darf auch ein eigenes Location-Objekt im
RIS-Bestand nicht über den Aggregator abrufbar bleiben (Abschnitt „Rücknahme des Sitzungsortes“).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.utils import timezone

from apps.session.models import (
    SessionAgendaItem,
    SessionAuditLog,
    SessionConsultation,
    SessionFile,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionTenant,
)
from apps.session.services import insight_service, oparl_access
from apps.session.tests._niederschrift import client as angemeldet
from apps.session.tests._niederschrift import nutzer
from insight_core.models import OParlBody, OParlFile, OParlLocation, OParlMeeting, OParlOrganization
from insight_sync.session_mirror import SessionMirror
from oparl_api.tests.konformitaet import ORGANIZATION_TYPES, pruefe, pruefe_liste

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
PFAD = "/session/musterstadt/api/oparl/"
BASIS = f"{SITE}{PFAD}"
DL_ZERO = "https://www.govdata.de/dl-de/zero-2-0"

#: interner Schlüssel -> (organizationType, classification)
ARTEN = {
    "committee": ("Gremium", "Ausschuss"),
    "council": ("Gremium", "Rat"),
    "faction": ("Fraktion", "Fraktion"),
    "advisory": ("Gremium", "Beirat"),
    "commission": ("Gremium", "Kommission"),
    "department": ("Verwaltungsbereich", "Amt/Fachbereich"),
    "other": ("Sonstiges", "Sonstiges"),
}


@pytest.fixture(autouse=True)
def _einstellungen(tmp_path: Any) -> Any:
    cache.clear()
    with override_settings(SITE_URL=SITE, OPARL_API_RATE_LIMIT=0, MEDIA_ROOT=str(tmp_path)):
        yield
    cache.clear()


@pytest.fixture
def welt() -> dict[str, Any]:
    tenant = SessionTenant.objects.create(
        name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now()
    )
    gremien = {
        art: SessionOrganization.objects.create(tenant=tenant, name=f"Gremium {art}", organization_type=art)
        for art in ARTEN
    }
    person = SessionPerson.objects.create(tenant=tenant, given_name="Petra", family_name="Muster")
    SessionOrganizationMembership.objects.create(
        organization=gremien["council"], person=person, start_date=date(2025, 11, 1)
    )
    SessionLegislativeTerm.objects.create(tenant=tenant, name="2025–2030", start_date=date(2025, 11, 1))
    start = timezone.now()
    sitzung = SessionMeeting.objects.create(
        tenant=tenant,
        name="Ratssitzung",
        organization=gremien["council"],
        start=start,
        is_public=True,
        location="Rathaus",
        room="Ratssaal",
        street_address="Markt 1",
        postal_code="12345",
        locality="Musterstadt",
    )
    ohne_ort = SessionMeeting.objects.create(
        tenant=tenant, name="Bauausschuss", organization=gremien["committee"], start=start, is_public=True
    )
    geheim = SessionMeeting.objects.create(
        tenant=tenant,
        name="Personalausschuss",
        organization=gremien["committee"],
        start=start,
        is_public=False,
        location="GEHEIMER-ORT",
    )
    vorlage = SessionPaper.objects.create(
        tenant=tenant, reference="V/2026/1", name="Radweg", is_public=True, status="approved", date=date(2026, 9, 1)
    )
    top = SessionAgendaItem.objects.create(meeting=sitzung, number="1", name="Radweg", is_public=True, paper=vorlage)
    SessionConsultation.objects.create(
        paper=vorlage, organization=gremien["council"], meeting=sitzung, agenda_item=top, authoritative=True
    )
    datei = SessionFile.objects.create(
        tenant=tenant,
        name="Begründung",
        file=SimpleUploadedFile("begruendung.pdf", b"%PDF-1.4 Begruendung", content_type="application/pdf"),
        is_public=True,
        paper=vorlage,
    )
    return {
        "tenant": tenant,
        "gremien": gremien,
        "sitzung": sitzung,
        "ohne_ort": ohne_ort,
        "geheim": geheim,
        "vorlage": vorlage,
        "datei": datei,
    }


def _json(pfad: str, **kopf: str) -> dict[str, Any]:
    antwort = Client().get(pfad, headers=kopf)
    assert antwort.status_code == 200, (pfad, antwort.status_code)
    return cast(dict[str, Any], antwort.json())


# =============================================================================
# organizationType
# =============================================================================


def test_organization_type_mit_werten_der_spezifikation(welt: dict[str, Any]) -> None:
    for art, (erwartet, einordnung) in ARTEN.items():
        gremium = _json(f"{PFAD}organization/{welt['gremien'][art].id}/")
        assert gremium["organizationType"] == erwartet, art
        assert gremium["organizationType"] in ORGANIZATION_TYPES
        # Die feinere Art bleibt in classification erhalten
        assert gremium["classification"] == einordnung


# =============================================================================
# File.date
# =============================================================================


def test_file_date_ist_ein_datum_in_der_zeitzone_der_installation(welt: dict[str, Any]) -> None:
    # 23:30 UTC ist in Berlin bereits der Folgetag
    SessionFile.objects.filter(pk=welt["datei"].pk).update(
        created_at=datetime(2026, 9, 30, 23, 30, tzinfo=ZoneInfo("UTC"))
    )
    datei = _json(f"{PFAD}file/{welt['datei'].id}/")
    assert datei["date"] == "2026-10-01"
    eingebettet = _json(f"{PFAD}paper/{welt['vorlage'].id}/")["mainFile"]
    assert eingebettet["date"] == "2026-10-01"


# =============================================================================
# license
# =============================================================================


def test_ohne_festlegung_keine_lizenz(welt: dict[str, Any]) -> None:
    assert "license" not in _json(PFAD)
    body = _json(f"{PFAD}body/")
    assert "license" not in body and "licenseValidSince" not in body


def test_lizenz_an_system_und_body(welt: dict[str, Any]) -> None:
    assert oparl_access.set_license(welt["tenant"], DL_ZERO) is True

    assert _json(PFAD)["license"] == DL_ZERO
    body = _json(f"{PFAD}body/")
    assert body["license"] == DL_ZERO
    assert datetime.fromisoformat(body["licenseValidSince"]).tzinfo is not None
    assert pruefe(body, "Body") == []


def test_lizenz_festlegen_schreibt_audit_und_gueltigkeit(welt: dict[str, Any]) -> None:
    tenant = welt["tenant"]
    assert oparl_access.set_license(tenant, DL_ZERO) is True
    tenant.refresh_from_db()
    seit = tenant.oparl_license_valid_since
    assert tenant.oparl_license == DL_ZERO and seit is not None

    # Unverändert: kein zweiter Eintrag, Gültigkeit bleibt
    assert oparl_access.set_license(tenant, DL_ZERO) is False
    tenant.refresh_from_db()
    assert tenant.oparl_license_valid_since == seit
    eintraege = SessionAuditLog.objects.filter(tenant=tenant, object_repr="OParl-Schnittstelle", action="update")
    assert eintraege.count() == 1
    assert eintraege[0].changes == {
        "oparl_lizenz": {"alt": "keine Angabe", "neu": "Datenlizenz Deutschland – Zero – Version 2.0"}
    }

    # Zurück auf „keine Angabe“
    assert oparl_access.set_license(tenant, "") is True
    tenant.refresh_from_db()
    assert tenant.oparl_license == "" and tenant.oparl_license_valid_since is None


def test_lizenz_ausserhalb_der_liste_wird_abgelehnt(welt: dict[str, Any]) -> None:
    with pytest.raises(oparl_access.OParlAccessError):
        oparl_access.set_license(welt["tenant"], "https://example.org/eigene-lizenz")
    welt["tenant"].refresh_from_db()
    assert welt["tenant"].oparl_license == ""


def test_lizenz_in_den_einstellungen(welt: dict[str, Any]) -> None:
    tenant = welt["tenant"]
    admin = angemeldet(nutzer(tenant, "verwaltung", "manage_settings"))

    seite = admin.get(f"/session/{tenant.slug}/settings/")
    assert seite.status_code == 200
    assert "Lizenz der offenen Daten" in seite.content.decode()

    antwort = admin.post(f"/session/{tenant.slug}/settings/oparl-lizenz/", {"license": DL_ZERO})
    assert antwort.status_code == 302
    tenant.refresh_from_db()
    assert tenant.oparl_license == DL_ZERO

    abgelehnt = admin.post(f"/session/{tenant.slug}/settings/oparl-lizenz/", {"license": "https://example.org/x"})
    assert abgelehnt.status_code == 302
    tenant.refresh_from_db()
    assert tenant.oparl_license == DL_ZERO


def test_lizenz_nur_mit_einstellungsrecht(welt: dict[str, Any]) -> None:
    tenant = welt["tenant"]
    leser = angemeldet(nutzer(tenant, "leser", "view_meetings"))

    antwort = leser.post(f"/session/{tenant.slug}/settings/oparl-lizenz/", {"license": DL_ZERO})

    assert antwort.status_code in (302, 403)
    tenant.refresh_from_db()
    assert tenant.oparl_license == ""


# =============================================================================
# Meeting.location
# =============================================================================


def test_sitzungsort_als_location_objekt(welt: dict[str, Any]) -> None:
    sitzung = _json(f"{PFAD}meeting/{welt['sitzung'].id}/")
    ort = sitzung["location"]

    assert ort["id"] == f"{BASIS}location/{welt['sitzung'].id}/"
    assert ort["type"] == "https://schema.oparl.org/1.1/Location"
    assert ort["description"] == "Rathaus"
    assert ort["room"] == "Ratssaal"
    assert ort["streetAddress"] == "Markt 1"
    assert ort["postalCode"] == "12345"
    assert ort["locality"] == "Musterstadt"
    assert ort["bodies"] == [f"{BASIS}body/"]
    assert ort["meetings"] == [sitzung["id"]]
    assert pruefe(ort, "Location") == []
    # Die ID ist abrufbar und liefert dasselbe Objekt
    assert _json(f"{PFAD}location/{welt['sitzung'].id}/") == ort
    # … auch in der Liste der Sitzungen
    eintrag = next(m for m in _json(f"{PFAD}meetings/")["data"] if m["id"] == sitzung["id"])
    assert eintrag["location"] == ort


def test_bisherige_ortsfelder_bleiben_erhalten(welt: dict[str, Any]) -> None:
    sitzung = _json(f"{PFAD}meeting/{welt['sitzung'].id}/")
    assert sitzung["mandari:locationName"] == "Rathaus"
    assert sitzung["mandari:locationRoom"] == "Ratssaal"
    assert sitzung["mandari:locationAddress"] == "Markt 1, 12345 Musterstadt"


def test_sitzung_ohne_ort_hat_kein_location_objekt(welt: dict[str, Any]) -> None:
    assert "location" not in _json(f"{PFAD}meeting/{welt['ohne_ort'].id}/")
    gone = _json(f"{PFAD}location/{welt['ohne_ort'].id}/")
    assert gone["deleted"] is True
    assert pruefe(gone, "Location") == []
    # Felder und Reihenfolge wie bei jedem gekürzten Objekt der Schnittstelle
    assert list(gone) == ["id", "type", "created", "modified", "deleted"]


def test_ort_einer_nichtoeffentlichen_sitzung_existiert_nicht(welt: dict[str, Any]) -> None:
    antwort = Client().get(f"{PFAD}location/{welt['geheim'].id}/")
    assert antwort.status_code == 404
    assert b"GEHEIMER-ORT" not in antwort.content


def test_ort_nach_ruecknahme_der_sitzung_ohne_inhalt(welt: dict[str, Any]) -> None:
    sitzung = welt["sitzung"]
    sitzung.is_public = False
    sitzung.save()

    antwort = Client().get(f"{PFAD}location/{sitzung.id}/")

    assert antwort.status_code == 200
    gone = antwort.json()
    assert list(gone) == ["id", "type", "created", "modified", "deleted"]
    assert gone["deleted"] is True
    assert b"Rathaus" not in antwort.content and b"Markt" not in antwort.content


def test_unbekannter_ort_ergibt_404(welt: dict[str, Any]) -> None:
    assert Client().get(f"{PFAD}location/00000000-0000-4000-8000-000000000000/").status_code == 404


# =============================================================================
# Body: Pflichtfeld legislativeTerm
# =============================================================================


def test_body_ohne_wahlperiode_traegt_leere_liste(welt: dict[str, Any]) -> None:
    SessionLegislativeTerm.objects.filter(tenant=welt["tenant"]).delete()
    body = _json(f"{PFAD}body/")
    assert body["legislativeTerm"] == []
    assert pruefe(body, "Body") == []


# =============================================================================
# Bedingte Anfragen (ETag / 304)
# =============================================================================


@pytest.mark.parametrize("ziel", ["", "body/", "bodies/", "meetings/", "meeting/{sitzung}/", "location/{sitzung}/"])
def test_etag_und_304(welt: dict[str, Any], ziel: str) -> None:
    url = PFAD + ziel.format(sitzung=welt["sitzung"].id)
    client = Client()
    erste = client.get(url)
    etag = erste["ETag"]

    assert erste.status_code == 200
    assert erste["Cache-Control"] == "no-cache"

    zweite = client.get(url, headers={"if-none-match": etag})
    assert zweite.status_code == 304
    assert zweite.content == b""
    assert zweite["ETag"] == etag
    assert zweite["Access-Control-Allow-Origin"] == "*"

    andere = client.get(url, headers={"if-none-match": '"veraltet"'})
    assert andere.status_code == 200
    assert andere.content == erste.content


def test_ruecknahme_wirkt_trotz_passendem_etag(welt: dict[str, Any]) -> None:
    """Eine nicht mehr öffentliche Sitzung darf nie mit 304 als „unverändert“ bestätigt werden."""
    sitzung = welt["sitzung"]
    url = f"{PFAD}meeting/{sitzung.id}/"
    client = Client()
    etag = client.get(url)["ETag"]

    sitzung.is_public = False
    sitzung.save()

    antwort = client.get(url, headers={"if-none-match": etag})
    assert antwort.status_code == 200
    assert antwort.json()["deleted"] is True
    assert b"Ratssitzung" not in antwort.content


def test_gesperrte_schnittstelle_antwortet_trotz_etag_mit_404(welt: dict[str, Any]) -> None:
    client = Client()
    etag = client.get(PFAD)["ETag"]

    SessionTenant.objects.filter(pk=welt["tenant"].pk).update(oparl_public_since=None)

    antwort = client.get(PFAD, headers={"if-none-match": etag})
    assert antwort.status_code == 404
    assert "ETag" not in antwort


# =============================================================================
# Gesamte Ausgabe gegen die Feldtypen der Spezifikation
# =============================================================================


def test_alle_antworten_entsprechen_den_feldtypen(welt: dict[str, Any]) -> None:
    oparl_access.set_license(welt["tenant"], DL_ZERO)
    probleme = pruefe(_json(PFAD), "System")
    probleme += pruefe_liste(_json(f"{PFAD}bodies/"), "Body")
    for segment, typ in (
        ("organizations", "Organization"),
        ("people", "Person"),
        ("memberships", "Membership"),
        ("meetings", "Meeting"),
        ("agendaitems", "AgendaItem"),
        ("papers", "Paper"),
        ("consultations", "Consultation"),
        ("files", "File"),
        ("legislativeterms", "LegislativeTerm"),
    ):
        probleme += pruefe_liste(_json(f"{PFAD}{segment}/"), typ)
    probleme += pruefe(_json(f"{PFAD}file/{welt['datei'].id}/"), "File")

    assert probleme == []


# =============================================================================
# Durchstich: Session-Schnittstelle -> Spiegel im RIS-Bestand -> Aggregator
# =============================================================================


def _abruf(url: str) -> dict[str, Any]:
    ziel = urlsplit(url)
    return _json(f"{ziel.path}?{ziel.query}" if ziel.query else ziel.path)


def test_spiegel_und_aggregator_bleiben_konform(welt: dict[str, Any]) -> None:
    source, _ = insight_service.register_source(welt["tenant"])
    cast(Any, SessionMirror)(source, fetch=_abruf).sync(full=True)

    # Bürgerportal: Ort und Anschrift der Sitzung wie bisher (aus den bisherigen Feldern)
    sitzung = OParlMeeting.objects.get(external_id=f"{BASIS}meeting/{welt['sitzung'].id}/")
    assert sitzung.location_name == "Rathaus"
    assert sitzung.location_address == "Markt 1, 12345 Musterstadt"
    # File.date als reines Datum wird zu Mitternacht UTC (wie im Ingestor), nie zu einem Wert ohne Zeitzone
    datei = OParlFile.objects.get(external_id=f"{BASIS}file/{welt['datei'].id}/")
    assert datei.file_date is not None and datei.file_date.tzinfo is not None
    assert datei.file_date.astimezone(ZoneInfo("UTC")).time().isoformat() == "00:00:00"
    # Der Bestand übernimmt die Werte der Spezifikation
    rat = OParlOrganization.objects.get(external_id=f"{BASIS}organization/{welt['gremien']['council'].id}/")
    assert (rat.organization_type, rat.classification) == ("Gremium", "Rat")

    body = OParlBody.objects.get(source=source)
    probleme: list[str] = []
    for segment, typ in (
        ("organizations", "Organization"),
        ("people", "Person"),
        ("meetings", "Meeting"),
        ("papers", "Paper"),
    ):
        probleme += pruefe_liste(_json(f"/oparl/v1/body/{body.id}/{segment}"), typ)
    assert probleme == []
    gespiegelt = _json(f"/oparl/v1/meeting/{sitzung.id}")
    assert gespiegelt["location"]["description"] == "Rathaus, Markt 1, 12345 Musterstadt"
    assert _json(f"/oparl/v1/file/{datei.id}")["date"] == _json(f"{PFAD}file/{welt['datei'].id}/")["date"]


def test_spiegel_liest_den_ort_auch_ohne_die_abgekuendigten_textfelder(welt: dict[str, Any]) -> None:
    """Entfallen ``mandari:location*``, ergibt das Location-Objekt denselben Text – und kein eigenes Objekt."""

    def ohne_textfelder(url: str) -> dict[str, Any]:
        antwort = _abruf(url)
        for eintrag in antwort.get("data", [antwort]):
            for feld in [feld for feld in eintrag if feld.startswith("mandari:location")]:
                del eintrag[feld]
        return antwort

    source, _ = insight_service.register_source(welt["tenant"])
    cast(Any, SessionMirror)(source, fetch=ohne_textfelder).sync(full=True)

    sitzung = OParlMeeting.objects.get(external_id=f"{BASIS}meeting/{welt['sitzung'].id}/")
    assert "mandari:locationName" not in sitzung.raw_json
    assert sitzung.location_name == "Rathaus"
    assert sitzung.location_address == "Markt 1, 12345 Musterstadt"
    assert not OParlLocation.objects.exists()
    ohne_ort = OParlMeeting.objects.get(external_id=f"{BASIS}meeting/{welt['ohne_ort'].id}/")
    assert (ohne_ort.location_name, ohne_ort.location_address) == (None, None)


# =============================================================================
# Rücknahme des Sitzungsortes im RIS-Bestand
# =============================================================================


def _bestand_mit_ortsobjekt(welt: dict[str, Any]) -> tuple[OParlBody, OParlMeeting, OParlLocation]:
    """
    Gespiegelter Bestand, in dem der Sitzungsort zusätzlich als eigenes Location-Objekt steht – so hat
    ihn ein Ingestor angelegt, der eingebettete Orte jeder Quelle übernimmt.
    """
    source, _ = insight_service.register_source(welt["tenant"])
    cast(Any, SessionMirror)(source, fetch=_abruf).sync(full=True)
    body = OParlBody.objects.get(source=source)
    sitzung = OParlMeeting.objects.get(external_id=f"{BASIS}meeting/{welt['sitzung'].id}/")
    ort = OParlLocation.objects.create(
        external_id=f"{BASIS}location/{welt['sitzung'].id}/",
        body=body,
        description="Rathaus",
        room="Ratssaal",
        street_address="Markt 1",
        postal_code="12345",
        locality="Musterstadt",
    )
    return body, sitzung, ort


def _ohne_inhalt(pfad: str, *verboten: str) -> None:
    """Die Adresse liefert nur noch das gekürzte Objekt – ohne Ort, Anschrift oder Kennung der Sitzung."""
    antwort = Client().get(pfad)
    assert antwort.status_code == 200, (pfad, antwort.status_code)
    assert set(antwort.json()) == {"id", "type", "created", "modified", "deleted"}
    assert antwort.json()["deleted"] is True
    inhalt = antwort.content.decode()
    assert not [wort for wort in ("Rathaus", "Ratssaal", "Markt", "12345", *verboten) if wort in inhalt]


@pytest.mark.parametrize("weg", ["nichtoeffentlich", "geloescht"])
def test_ruecknahme_der_sitzung_nimmt_den_ort_im_bestand_mit(
    welt: dict[str, Any], django_capture_on_commit_callbacks: Any, weg: str
) -> None:
    body, gespiegelt, ort = _bestand_mit_ortsobjekt(welt)
    sitzung = welt["sitzung"]
    kennung = str(sitzung.id)
    assert _json(f"/oparl/v1/location/{ort.id}")["mandari:originalId"] == f"{BASIS}location/{kennung}/"

    with django_capture_on_commit_callbacks(execute=True):
        if weg == "geloescht":
            sitzung.delete()
        else:
            sitzung.is_public = False
            sitzung.save()

    ort.refresh_from_db()
    assert ort.deleted is True
    _ohne_inhalt(f"/oparl/v1/location/{ort.id}", kennung)
    # … auch unter der Adresse, die der Aggregator aus dem Text an der Sitzung bildet
    _ohne_inhalt(f"/oparl/v1/location/{gespiegelt.id}", kennung)
    assert _json(f"/oparl/v1/body/{body.id}/locations")["data"] == []
    # Inkrementelle Abnehmer erfahren die Rücknahme als gekürztes Objekt
    seit = _json(f"/oparl/v1/body/{body.id}/locations?modified_since=2000-01-01T00:00:00%2B00:00")["data"]
    assert [(eintrag["deleted"], set(eintrag)) for eintrag in seit] == [
        (True, {"id", "type", "created", "modified", "deleted"})
    ]


def test_geleerte_ortsangabe_nimmt_den_ort_im_bestand_zurueck(
    welt: dict[str, Any], django_capture_on_commit_callbacks: Any
) -> None:
    body, _, ort = _bestand_mit_ortsobjekt(welt)
    sitzung = welt["sitzung"]

    with django_capture_on_commit_callbacks(execute=True):
        sitzung.location = sitzung.room = sitzung.street_address = sitzung.postal_code = sitzung.locality = ""
        sitzung.save()

    ort.refresh_from_db()
    assert ort.deleted is True
    _ohne_inhalt(f"/oparl/v1/location/{ort.id}", str(sitzung.id))
    assert _json(f"/oparl/v1/body/{body.id}/locations")["data"] == []
    # Die Sitzung selbst bleibt öffentlich
    assert _json(f"{PFAD}meeting/{sitzung.id}/")["name"] == "Ratssitzung"


def test_geaenderte_ortsangabe_ist_keine_ruecknahme(
    welt: dict[str, Any], django_capture_on_commit_callbacks: Any
) -> None:
    _, _, ort = _bestand_mit_ortsobjekt(welt)
    sitzung = welt["sitzung"]

    with django_capture_on_commit_callbacks(execute=True):
        sitzung.room = "Kleiner Saal"
        sitzung.save()

    ort.refresh_from_db()
    assert ort.deleted is False


def test_ruecknahme_einer_sitzung_laesst_orte_anderer_sitzungen_stehen(
    welt: dict[str, Any], django_capture_on_commit_callbacks: Any
) -> None:
    body, _, ort = _bestand_mit_ortsobjekt(welt)
    anderer = OParlLocation.objects.create(
        external_id=f"{BASIS}location/{welt['ohne_ort'].id}/", body=body, description="Technisches Rathaus"
    )

    with django_capture_on_commit_callbacks(execute=True):
        welt["sitzung"].is_public = False
        welt["sitzung"].save()

    ort.refresh_from_db()
    anderer.refresh_from_db()
    assert (ort.deleted, anderer.deleted) == (True, False)
