# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS-Projektor für Session-Mandanten im Schattenbetrieb (Issue #536).

Geprüft über die echten Wege: Session meldet ihre Änderungen (``hub_events.track``), die Zustellung
(``deliver_batch``) ruft das Abonnement ``ris.session_projektor``, der Spiegel (``SessionMirror``) liest die
Session-Schnittstelle über HTTP wie im Betrieb. Der Vergleich (``hub.projections.ris_vergleich``) muss danach
für jeden Objekttyp 0 Abweichungen zeigen; die Schatten-Quelle enthält nichts Nichtöffentliches, meldet keine
Ereignisse und lässt den RIS-Bestand unberührt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from io import StringIO
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.db.models import Max
from django.test import Client
from django.utils import timezone

from apps.accounts.models import SecurityAuditLog
from apps.common.models import IdentifierBase
from apps.events import registry
from apps.events.dispatch import deliver_batch, ensure_subscription, rewind
from apps.events.models import Event, ParkedEvent
from apps.events.registry import Subscriber, get
from apps.session import hub_events, subscribers
from apps.session.models import (
    SessionAgendaItem,
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
from apps.session.services import insight_service
from hub.projections import ris_session, ris_vergleich
from hub.projections.models import RisSchatten
from insight_core.models import (
    OParlAgendaItem,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)
from insight_sync.session_mirror import SessionMirror

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
GEHEIM = "GEHEIMER-ORT"
GEHEIMER_BESCHLUSS = "GEHEIMER-BESCHLUSS"


@dataclass
class Welt:
    tenant: SessionTenant
    source: OParlSource
    rat: SessionOrganization
    sitzung: SessionMeeting
    termin: SessionMeeting
    geheim: SessionMeeting
    top: SessionAgendaItem
    top_geheim: SessionAgendaItem
    vorlage: SessionPaper
    entwurf: SessionPaper
    datei: SessionFile


@pytest.fixture
def leeres_register(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Subscriber]]:
    """Eigenes Register: Was ein Test registriert, bleibt nicht für andere stehen."""
    eintraege: dict[str, Subscriber] = {}
    monkeypatch.setattr(registry, "_REGISTRY", eintraege)
    yield eintraege


def _datei(name: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, f"%PDF-1.4 {name}".encode(), content_type="application/pdf")


def _mandant(slug: str, **werte: Any) -> SessionTenant:
    daten: dict[str, Any] = {"name": f"Bezirk {slug}", "slug": slug, "oparl_public_since": timezone.now()}
    daten.update(werte)
    return SessionTenant.objects.create(**daten)


@pytest.fixture
def welt(settings: Any, tmp_path: Any, leeres_register: dict[str, Subscriber]) -> Welt:
    cache.clear()
    settings.SITE_URL = SITE
    settings.MEDIA_ROOT = str(tmp_path)
    settings.OPARL_API_RATE_LIMIT = 0
    settings.SESSION_EVENTS = "aktiv"
    settings.RIS_SESSION_PROJECTOR = "schatten"
    settings.RIS_SESSION_PROJECTOR_TENANTS = []
    IdentifierBase.objects.update_or_create(pk=1, defaults={"url": SITE})
    tenant = _mandant("nord", insight_publish=True)
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", short_name="Rat", organization_type="council")
    bau = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss", organization_type="committee")
    person = SessionPerson.objects.create(tenant=tenant, given_name="Petra", family_name="Muster")
    SessionOrganizationMembership.objects.create(organization=rat, person=person, start_date=date(2025, 11, 1))
    SessionLegislativeTerm.objects.create(tenant=tenant, name="2025–2030", start_date=date(2025, 11, 1))
    beginn = timezone.now() + timedelta(days=7)
    sitzung = SessionMeeting.objects.create(
        tenant=tenant,
        name="Ratssitzung",
        organization=rat,
        start=beginn,
        is_public=True,
        location="Rathaus",
        room="Ratssaal",
        street_address="Markt 1",
        postal_code="12345",
        locality="Nordstadt",
    )
    # Nichtöffentliche Sitzung mit veröffentlichtem Termin (Issue #757): nur der Termin, nie der Ort
    termin = SessionMeeting.objects.create(
        tenant=tenant,
        name="Personalausschuss",
        organization=bau,
        start=beginn,
        is_public=False,
        date_public=True,
        location=GEHEIM,
    )
    geheim = SessionMeeting.objects.create(
        tenant=tenant, name="Vergabeausschuss", organization=bau, start=beginn, is_public=False, location=GEHEIM
    )
    vorlage = SessionPaper.objects.create(
        tenant=tenant, reference="V/2026/1", name="Radweg", is_public=True, status="approved", date=date(2026, 9, 1)
    )
    entwurf = SessionPaper.objects.create(tenant=tenant, reference="V/2026/2", name="Entwurf", is_public=True)
    top = SessionAgendaItem.objects.create(
        meeting=sitzung, number="1", order=1, name="Radweg", is_public=True, paper=vorlage
    )
    top_geheim = SessionAgendaItem.objects.create(
        meeting=sitzung, number="2", order=2, name="Grundstück", is_public=False, resolution_text=GEHEIMER_BESCHLUSS
    )
    SessionAgendaItem.objects.create(meeting=geheim, number="1", order=1, name="Vergabe", is_public=True)
    SessionConsultation.objects.create(
        paper=vorlage, organization=rat, meeting=sitzung, agenda_item=top, authoritative=True
    )
    datei = SessionFile.objects.create(tenant=tenant, name="Begründung", file=_datei("b.pdf"), paper=vorlage)
    SessionFile.objects.create(tenant=tenant, name="Lageplan", file=_datei("l.pdf"), meeting=sitzung)
    SessionFile.objects.create(tenant=tenant, name="Intern", file=_datei("i.pdf"), paper=vorlage, is_public=False)
    SessionFile.objects.create(tenant=tenant, name="Angebot", file=_datei("a.pdf"), meeting=geheim)
    source, _ = insight_service.register_source(tenant)
    return Welt(tenant, source, rat, sitzung, termin, geheim, top, top_geheim, vorlage, entwurf, datei)


# -- Hilfen ------------------------------------------------------------------------------------------


def _abruf(url: str) -> dict[str, Any]:
    """``fetch`` des Spiegels über die echte Schnittstelle (wie der Abgleich im Betrieb)."""
    ziel = urlsplit(url)
    antwort = Client().get(f"{ziel.path}?{ziel.query}" if ziel.query else ziel.path)
    assert antwort.status_code == 200, url
    return cast(dict[str, Any], antwort.json())


def _spiegeln(welt: Welt, *, voll: bool = False) -> None:
    welt.source.refresh_from_db()
    cast(Any, SessionMirror)(welt.source, fetch=_abruf).sync(full=voll)


def _abonnement() -> Subscriber:
    assert subscribers.register()
    spec = get(ris_session.NAME)
    ensure_subscription(spec)
    return spec


def _nummerieren() -> None:
    """Folgenummern wie der Sequenzierer vergeben (in der Reihenfolge der Erfassung)."""
    hoechste = Event.objects.aggregate(hoechste=Max("seq"))["hoechste"] or 0
    for event in Event.objects.filter(seq__isnull=True).order_by("id"):
        hoechste += 1
        Event.objects.filter(pk=event.pk).update(seq=hoechste)


def _zustellen(spec: Subscriber) -> int:
    _nummerieren()
    zugestellt = 0
    while True:
        lauf = deliver_batch(spec)
        zugestellt += lauf.delivered
        if not lauf.more:
            return zugestellt


def _vergleich(welt: Welt) -> ris_vergleich.Vergleich:
    kommunen = ris_vergleich.kommunen_der_quellen([welt.source.pk])
    assert kommunen, "Der Spiegel hat die Kommune übernommen"
    return ris_vergleich.vergleichen(welt.tenant.pk, kommunen)


def _befund(vergleich: ris_vergleich.Vergleich) -> dict[str, Any]:
    """Nur die Typen mit Abweichungen (für lesbare Fehlermeldungen)."""
    return {
        typ: daten
        for typ, daten in vergleich.as_dict(beispiele=3)["typen"].items()
        if daten["fehlt"] or daten["ueberzaehlig"] or daten["abweichend"]
    }


def _start(welt: Welt) -> Subscriber:
    """Abonnement anlegen, Spiegel und Schatten vollständig bauen: beide gleich."""
    spec = _abonnement()
    _spiegeln(welt, voll=True)
    ergebnis = ris_session.aufbauen(cast(Any, _quelle(welt)))
    assert ergebnis.gesamt > 0
    vergleich = _vergleich(welt)
    assert vergleich.abweichungen == 0, _befund(vergleich)
    return spec


def _quelle(welt: Welt) -> Any:
    from apps.session.ris_projektion import SessionQuelle

    return SessionQuelle(welt.tenant)


def _zeilen() -> list[Any]:
    return list(
        RisSchatten.objects.order_by("mandant", "bestand_id").values(
            "mandant", "typ", "bestand_id", "external_id", "spalten", "verweise", "deleted", "deletion_reason"
        )
    )


# -- Vollbau und Sichtbarkeit -----------------------------------------------------------------------------


def test_vollbau_gleicht_dem_spiegel_je_typ(welt: Welt) -> None:
    _start(welt)
    vergleich = _vergleich(welt)
    zahlen = {typ: daten["schatten"] for typ, daten in vergleich.as_dict()["typen"].items()}
    assert zahlen == {
        "body": 1,
        "legislativeterm": 1,
        "organization": 2,
        "person": 1,
        "membership": 1,
        # öffentliche Sitzung und der veröffentlichte Termin, nicht die nichtöffentliche Sitzung
        "meeting": 2,
        "agendaitem": 1,
        "paper": 1,
        "consultation": 1,
        "file": 2,
    }
    assert vergleich.typen["meeting"].gleich == 2


def test_nichts_nichtoeffentliches_in_der_schatten_quelle(welt: Welt) -> None:
    _start(welt)
    inhalt = json.dumps(_zeilen(), default=str)
    assert GEHEIM not in inhalt
    assert GEHEIMER_BESCHLUSS not in inhalt
    adressen = set(RisSchatten.objects.values_list("external_id", flat=True))
    quelle = _quelle(welt)
    for art, pk in [
        ("meeting", welt.geheim.pk),
        ("agendaitem", welt.top_geheim.pk),
        ("paper", welt.entwurf.pk),
    ]:
        assert quelle.adresse(art, pk) not in adressen, art
    for datei in SessionFile.objects.filter(name__in=["Intern", "Angebot"]):
        assert quelle.adresse("file", datei.pk) not in adressen, datei.name
    # Vom veröffentlichten Termin nur der Termin: ohne Ort
    termin = RisSchatten.objects.get(external_id=quelle.adresse("meeting", welt.termin.pk))
    assert termin.spalten["location_name"] is None and termin.spalten["location_address"] is None


def test_vor_der_freischaltung_entsteht_nichts(welt: Welt) -> None:
    welt.tenant.oparl_public_since = None
    welt.tenant.save()
    ergebnis = ris_session.aufbauen(_quelle(welt))
    assert ergebnis.gesamt == 0
    assert not RisSchatten.objects.exists()


# -- Ereignisse halten den Schatten gleich ----------------------------------------------------------------


def test_aenderungen_aus_session_halten_schatten_und_spiegel_gleich(
    welt: Welt, django_capture_on_commit_callbacks: Any
) -> None:
    spec = _start(welt)
    tenant = welt.tenant

    def schritt(aenderung: Callable[[Any], None]) -> None:
        # Rückrufe nach dem Commit wie im Betrieb (Rücknahme aus dem Bürgerportal, oparl_publication)
        with django_capture_on_commit_callbacks(execute=True), hub_events.track(tenant) as tracked:
            aenderung(tracked)

    def sitzung_verlegen(tracked: Any) -> None:
        tracked.meeting(welt.sitzung)
        welt.sitzung.name = "Ratssitzung (verlegt)"
        welt.sitzung.room = "Saal 2"
        welt.sitzung.save()

    def punkt_neu(tracked: Any) -> None:
        tracked.agenda(welt.sitzung)
        SessionAgendaItem.objects.create(meeting=welt.sitzung, number="3", order=3, name="Spielplatz", is_public=True)

    def punkt_nichtoeffentlich(tracked: Any) -> None:
        tracked.agenda(welt.sitzung)
        welt.top.is_public = False
        welt.top.save()

    def vorlage_freigeben(tracked: Any) -> None:
        neu = SessionPaper(tenant=tenant, reference="V/2026/3", name="Bäume", is_public=True, status="approved")
        tracked.paper(neu, created=True)
        neu.save()
        SessionFile.objects.create(tenant=tenant, name="Baumliste", file=_datei("baum.pdf"), paper=neu)

    def vorlage_zuruecknehmen(tracked: Any) -> None:
        tracked.paper(welt.vorlage)
        welt.vorlage.status = "draft"
        welt.vorlage.save()

    def anlage_loeschen(tracked: Any) -> None:
        anlage = SessionFile.objects.get(name="Lageplan")
        tracked.file(anlage)
        anlage.delete()

    seit = Event.objects.count()
    for aenderung in (
        sitzung_verlegen,
        punkt_neu,
        punkt_nichtoeffentlich,
        vorlage_freigeben,
        vorlage_zuruecknehmen,
        anlage_loeschen,
    ):
        schritt(aenderung)
    assert Event.objects.count() > seit, "Session meldet die Änderungen"

    _spiegeln(welt)
    vorher = Event.objects.count()
    assert _zustellen(spec) == vorher - seit
    assert Event.objects.count() == vorher, "Der Projektor meldet im Schatten nichts"
    assert not ParkedEvent.objects.exists()

    vergleich = _vergleich(welt)
    assert vergleich.abweichungen == 0, _befund(vergleich)
    # Die Rücknahmen stehen mit ihrem Grund in der Schatten-Quelle, ohne Inhalte
    quelle = _quelle(welt)
    zurueck = RisSchatten.objects.get(external_id=quelle.adresse("paper", welt.vorlage.pk))
    assert (zurueck.deleted, zurueck.deletion_reason, zurueck.spalten) == (True, "zurueckgenommen", {})
    punkt = RisSchatten.objects.get(external_id=quelle.adresse("agendaitem", welt.top.pk))
    assert (punkt.deleted, punkt.deletion_reason) == (True, "nichtoeffentlich")
    assert vergleich.typen["meeting"].gleich == 2 and vergleich.typen["agendaitem"].gleich == 1


def test_vergleich_zeigt_eine_aenderung_ohne_ereignis(welt: Welt) -> None:
    """Der Vergleich ist nicht blind: Ändert sich der Bestand ohne Ereignis, weicht der Schatten ab."""
    _start(welt)
    SessionMeeting.objects.filter(pk=welt.sitzung.pk).update(name="Umbenannt ohne Meldung", updated_at=timezone.now())
    SessionPaper.objects.filter(pk=welt.entwurf.pk).update(status="approved", updated_at=timezone.now())
    _spiegeln(welt)

    vergleich = _vergleich(welt)
    assert vergleich.typen["meeting"].felder == {"name": 1}
    assert vergleich.typen["meeting"].abweichend == [str(OParlMeeting.objects.get(name="Umbenannt ohne Meldung").pk)]
    assert len(vergleich.typen["paper"].fehlt) == 1
    assert vergleich.abweichungen == 2


def test_doppelte_zustellung_aendert_die_schatten_quelle_nicht(welt: Welt) -> None:
    """Zustellung mindestens einmal: Nachspielen schreibt denselben Stand (DOPPELZUSTELLUNG)."""
    spec = _start(welt)
    with hub_events.track(welt.tenant) as tracked:
        tracked.meeting(welt.sitzung)
        tracked.agenda(welt.sitzung)
        welt.sitzung.name = "Sondersitzung"
        welt.sitzung.save()
        SessionAgendaItem.objects.create(meeting=welt.sitzung, number="4", order=4, name="Neu", is_public=True)
    with hub_events.track(welt.tenant) as tracked:
        tracked.file(welt.datei)
        welt.datei.delete()
    anzahl = _zustellen(spec)
    assert anzahl >= 3
    vorher = _zeilen()

    erstes = Event.objects.order_by("seq").first()
    assert erstes is not None and erstes.seq is not None
    rewind(ris_session.NAME, erstes.seq)
    assert _zustellen(spec) == anzahl

    assert _zeilen() == vorher
    assert not ParkedEvent.objects.exists()


# -- Unsichtbar nach außen, Schalter und Auswahl --------------------------------------------------------


def test_schatten_beruehrt_den_bestand_nicht_und_meldet_nichts(welt: Welt) -> None:
    spec = _abonnement()
    _spiegeln(welt, voll=True)
    modelle = (OParlMeeting, OParlAgendaItem, OParlPaper, OParlConsultation, OParlFile)
    bestand = {modell.__name__: list(modell.objects.order_by("pk").values()) for modell in modelle}
    with hub_events.track(welt.tenant) as tracked:
        tracked.meeting(welt.sitzung)
        welt.sitzung.name = "Nur im Schatten"
        welt.sitzung.save()
    ereignisse = Event.objects.count()

    assert _zustellen(spec) == ereignisse
    assert RisSchatten.objects.filter(spalten__name="Nur im Schatten").count() == 1
    assert Event.objects.count() == ereignisse
    assert not Event.objects.filter(tenant_ref__startswith="source:").exists()
    assert {modell.__name__: list(modell.objects.order_by("pk").values()) for modell in modelle} == bestand
    assert not OParlMeeting.objects.filter(name="Nur im Schatten").exists()


def test_schalter_aus_registriert_kein_abonnement(welt: Welt, settings: Any) -> None:
    settings.RIS_SESSION_PROJECTOR = "aus"
    assert subscribers.register() is False
    assert ris_session.NAME not in {spec.name for spec in registry.registered()}


def test_nur_gewaehlte_und_veroeffentlichte_mandanten(welt: Welt, settings: Any) -> None:
    spec = _abonnement()
    anderer = _mandant("sued", insight_publish=True)
    still = _mandant("west")  # Schnittstelle frei, aber nicht im Bürgerportal veröffentlicht
    for tenant in (anderer, still):
        rat = SessionOrganization.objects.create(tenant=tenant, name="Rat")
        with hub_events.track(tenant) as tracked:
            neu = SessionMeeting.objects.create(
                tenant=tenant, name="Sitzung", organization=rat, start=timezone.now(), is_public=True
            )
            tracked.meeting(neu, created=True)
    settings.RIS_SESSION_PROJECTOR_TENANTS = [str(welt.tenant.pk)]
    _zustellen(spec)
    assert not RisSchatten.objects.exists()

    settings.RIS_SESSION_PROJECTOR_TENANTS = []
    erstes = Event.objects.order_by("seq").first()
    assert erstes is not None and erstes.seq is not None
    rewind(ris_session.NAME, erstes.seq)
    _zustellen(spec)
    assert set(RisSchatten.objects.values_list("mandant", flat=True)) == {anderer.pk}


# -- Befehl --------------------------------------------------------------------------------------------


def _befehl(*argumente: str) -> str:
    ausgabe = StringIO()
    call_command("ris_projektor_schatten", *argumente, stdout=ausgabe)
    return ausgabe.getvalue()


def test_befehl_aufbauen_vergleichen_loeschen(welt: Welt, settings: Any) -> None:
    _abonnement()
    _spiegeln(welt, voll=True)
    with pytest.raises(CommandError, match="--alle"):
        _befehl("aufbauen")
    assert "Trockenlauf" in _befehl("aufbauen", "--mandant", str(welt.tenant.pk), "--trocken")
    assert not RisSchatten.objects.exists()
    _befehl("aufbauen", "--mandant", str(welt.tenant.pk))

    daten = json.loads(_befehl("vergleichen", "--json"))
    assert daten["abweichungen"] == 0
    [mandant] = daten["mandanten"]
    assert mandant["mandant"] == str(welt.tenant.pk)
    assert mandant["typen"]["meeting"] == {
        "bestand": 2,
        "schatten": 2,
        "gleich": 2,
        "fehlt": 0,
        "ueberzaehlig": 0,
        "abweichend": 0,
        "felder": {},
        "beispiele": {"fehlt": [], "ueberzaehlig": [], "abweichend": []},
    }
    assert "Abonnement ris.session_projektor: schatten" in _befehl("status")

    with pytest.raises(CommandError, match="schreibt noch"):
        _befehl("loeschen", "--ja")
    settings.RIS_SESSION_PROJECTOR = "aus"
    with pytest.raises(CommandError, match="--ja"):
        _befehl("loeschen")
    assert "Abonnement ris.session_projektor: entfernt" in _befehl("loeschen", "--ja", "--abonnement")
    assert not RisSchatten.objects.exists()
    protokoll = SecurityAuditLog.objects.get(event="betrieb")
    assert protokoll.details["aktion"] == "ris_projektor_schatten_loeschen"


def test_befehl_vergleichen_streng_bei_abweichung(welt: Welt) -> None:
    _start(welt)
    SessionMeeting.objects.filter(pk=welt.sitzung.pk).update(name="Ohne Meldung", updated_at=timezone.now())
    _spiegeln(welt)
    with pytest.raises(SystemExit) as ende:
        _befehl("vergleichen", "--mandant", str(welt.tenant.pk), "--streng")
    assert ende.value.code == 1
    ausgabe = _befehl("vergleichen", "--mandant", str(welt.tenant.pk))
    assert "Felder: name 1" in ausgabe
    assert "Ohne Meldung" not in ausgabe, "nur Kennungen und Feldnamen, nie Inhalte"
