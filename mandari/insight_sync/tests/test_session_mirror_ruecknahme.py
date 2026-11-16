# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Der Spiegel und die Rücknahme: Was zurückgenommen ist, veröffentlicht ein Lauf nicht wieder.

mandari Session nimmt ein Objekt im Bürgerportal sofort zurück (``retract_from_portal``). Ein Lauf des Spiegels ruft
seine Seiten aber ab, bevor er sie schreibt. Die Tests lassen die Rücknahme genau dazwischen geschehen: Die Seite ist
abgerufen und nennt das Objekt noch, dann stellt die Verwaltung es nichtöffentlich, dann schreibt der Lauf.

- Eine Zeile, die nach dem Abruf markiert wurde, behält ihre Markierung; unter ihr entsteht nichts.
- Eine Zeile, die es beim Abruf noch nicht gab, ist am Ende des Laufs markiert (Rücknahmen seit Beginn des Laufs).
- Eine dauerhafte Rücknahme („aus Datenschutzgründen entfernt“) übersteht jeden Lauf, wie beim Abgleich des Ingestors,
  auch an einer Zeile, die ein anderer Schreiber gerade erst angelegt hat.
- Die Rücknahmen liest ein Lauf nur nach, wenn er etwas neu angelegt oder wieder veröffentlicht hat.
- Der Normalfall bleibt: Veröffentlicht die Verwaltung wieder, hebt der nächste Lauf die Markierung auf.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.common.models import IdentifierBase
from apps.session.models import (
    SessionAgendaItem,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionTenant,
)
from apps.session.services import insight_service
from hub.ris import uebernahme
from insight_core.models import (
    REASON_NOT_PUBLIC,
    REASON_PRIVACY,
    OParlAgendaItem,
    OParlBody,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)
from insight_sync.session_mirror import BODY_LISTS, SessionMirror

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
BASIS = f"{SITE}/session/nord/api/oparl/"

Abruf = Callable[[str], dict[str, Any]]


@dataclass
class Welt:
    tenant: SessionTenant
    source: OParlSource
    sitzung: SessionMeeting
    top: SessionAgendaItem
    vorlage: SessionPaper


@pytest.fixture
def welt(settings: Any) -> Welt:
    cache.clear()
    settings.SITE_URL = SITE
    settings.OPARL_API_RATE_LIMIT = 0
    IdentifierBase.objects.update_or_create(pk=1, defaults={"url": SITE})
    tenant = SessionTenant.objects.create(
        name="Bezirk Nord", slug="nord", oparl_public_since=timezone.now(), insight_publish=True
    )
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", short_name="Rat", organization_type="council")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=rat, start=timezone.now() + timedelta(days=7), is_public=True
    )
    top = SessionAgendaItem.objects.create(meeting=sitzung, number="1", order=1, name="Radweg Hauptstraße")
    vorlage = SessionPaper.objects.create(
        tenant=tenant, reference="V/1", name="Radweg Hauptstraße", is_public=True, status="approved"
    )
    source, _ = insight_service.register_source(tenant)
    return Welt(tenant, source, sitzung, top, vorlage)


def _abruf(url: str) -> dict[str, Any]:
    """``fetch`` des Spiegels über die echte Schnittstelle (wie der Abgleich im Betrieb)."""
    ziel = urlsplit(url)
    antwort = Client().get(f"{ziel.path}?{ziel.query}" if ziel.query else ziel.path)
    assert antwort.status_code == 200, url
    return cast(dict[str, Any], antwort.json())


def _danach(liste: str, ereignis: Callable[[], None]) -> Abruf:
    """
    Wie ``_abruf``; sobald die erste Seite der Liste ``liste`` mit Einträgen abgerufen ist, geschieht ``ereignis``:
    Der Lauf hält dann einen Stand in der Hand, der älter ist als das Ereignis.
    """
    geschehen: list[str] = []

    def abruf(url: str) -> dict[str, Any]:
        seite = _abruf(url)
        if not geschehen and f"/{liste}/" in urlsplit(url).path and seite.get("data"):
            geschehen.append(url)
            ereignis()
        return seite

    return abruf


def _spiegeln(welt: Welt, *, voll: bool = False, abruf: Abruf = _abruf) -> dict[str, int]:
    welt.source.refresh_from_db()
    # Der Spiegel ist nicht typisiert (wie in test_session_mirror_ort.py)
    return cast(dict[str, int], cast(Any, SessionMirror)(welt.source, fetch=abruf).sync(full=voll))


def _punkt(top: SessionAgendaItem) -> OParlAgendaItem:
    return OParlAgendaItem.objects.get(external_id=f"{BASIS}agendaitem/{top.pk}/")


def _sitzung(welt: Welt) -> OParlMeeting:
    return OParlMeeting.objects.get(external_id=f"{BASIS}meeting/{welt.sitzung.pk}/")


def _vorlagen(vorlage: SessionPaper) -> Any:
    return OParlPaper.objects.filter(external_id=f"{BASIS}paper/{vorlage.pk}/")


# =============================================================================
# Rücknahme während eines Laufs
# =============================================================================


def test_ruecknahme_nach_dem_abruf_bleibt_bestehen(welt: Welt, django_capture_on_commit_callbacks: Any) -> None:
    _spiegeln(welt, voll=True)
    assert _punkt(welt.top).deleted is False

    def nichtoeffentlich() -> None:
        with django_capture_on_commit_callbacks(execute=True):
            welt.top.is_public = False
            welt.top.save()

    statistik = _spiegeln(welt, voll=True, abruf=_danach("meetings", nichtoeffentlich))

    punkt = _punkt(welt.top)
    # Grund und Zeitpunkt sind die der Rücknahme aus Session: Die Markierung war nie aufgehoben
    assert (punkt.deleted, punkt.deletion_reason) == (True, REASON_NOT_PUBLIC)
    assert statistik["withheld"] == 1
    assert _sitzung(welt).deleted is False


def test_unter_einer_zurueckgenommenen_sitzung_entsteht_nichts(
    welt: Welt, django_capture_on_commit_callbacks: Any
) -> None:
    _spiegeln(welt, voll=True)
    neu = SessionAgendaItem.objects.create(meeting=welt.sitzung, number="2", order=2, name="Grundstück Nordufer")

    def nichtoeffentlich() -> None:
        with django_capture_on_commit_callbacks(execute=True):
            welt.sitzung.is_public = False
            welt.sitzung.save()

    _spiegeln(welt, voll=True, abruf=_danach("meetings", nichtoeffentlich))

    sitzung = _sitzung(welt)
    assert (sitzung.deleted, sitzung.deletion_reason) == (True, REASON_NOT_PUBLIC)
    assert _punkt(welt.top).deleted is True
    # Den neuen Punkt nannte die abgerufene Seite; eine Zeile bekommt er trotzdem nicht
    assert not OParlAgendaItem.objects.filter(external_id=f"{BASIS}agendaitem/{neu.pk}/").exists()
    assert "Grundstück Nordufer" not in str(sitzung.raw_json)


def test_neues_objekt_ist_am_ende_des_laufs_zurueckgenommen(
    welt: Welt, django_capture_on_commit_callbacks: Any
) -> None:
    """
    Für ein Objekt ohne Zeile findet die sofortige Rücknahme aus Session nichts vor. Der Lauf legt die Zeile aus dem
    abgerufenen Stand an und liest zum Abschluss die Rücknahmen seit seinem Beginn: Danach ist sie markiert.
    """
    _spiegeln(welt, voll=True)
    neu = SessionPaper.objects.create(
        tenant=welt.tenant, reference="V/2", name="Grundstück Nordufer", is_public=True, status="approved"
    )

    def zurueck_in_den_entwurf() -> None:
        with django_capture_on_commit_callbacks(execute=True):
            neu.status = "draft"
            neu.save()

    statistik = _spiegeln(welt, voll=True, abruf=_danach("papers", zurueck_in_den_entwurf))

    assert [zeile.deleted for zeile in _vorlagen(neu)] == [True]
    assert statistik["tombstones"] == 1
    # Die unveränderte Vorlage bleibt veröffentlicht
    assert [zeile.deleted for zeile in _vorlagen(welt.vorlage)] == [False]


# =============================================================================
# Dauerhafte Rücknahme
# =============================================================================


def test_dauerhafte_ruecknahme_uebersteht_jeden_lauf(welt: Welt) -> None:
    _spiegeln(welt, voll=True)
    damals = timezone.now() - timedelta(days=3)
    _vorlagen(welt.vorlage).update(
        deleted=True, deleted_at=damals, deletion_reason=REASON_PRIVACY, oparl_modified=damals
    )
    _sitzung(welt).mark_deleted(damals, reason=REASON_PRIVACY)
    # Die Quelle liefert beides weiter, die Vorlage sogar mit neuerem Stand
    welt.vorlage.name = "Radweg Hauptstraße (2. Fassung)"
    welt.vorlage.save()
    neu = SessionAgendaItem.objects.create(meeting=welt.sitzung, number="2", order=2, name="Grundstück Nordufer")

    for voll in (False, True):
        _spiegeln(welt, voll=voll)

    vorlage = _vorlagen(welt.vorlage).get()
    assert (vorlage.deleted, vorlage.deletion_reason, vorlage.deleted_at) == (True, REASON_PRIVACY, damals)
    # Der Zeitpunkt der Rücknahme bleibt in ``modified_since``-Abfragen der eigenen Schnittstelle an seiner Stelle
    assert vorlage.oparl_modified == damals
    assert vorlage.name == "Radweg Hauptstraße"
    sitzung = _sitzung(welt)
    assert (sitzung.deleted, sitzung.deletion_reason) == (True, REASON_PRIVACY)
    assert not OParlAgendaItem.objects.filter(external_id=f"{BASIS}agendaitem/{neu.pk}/").exists()


def test_ohne_abruf_gilt_nur_die_dauerhafte_ruecknahme(db: None) -> None:
    """Direkt aufgerufen kennt der Spiegel das Alter eines Eintrags nicht: Nur die dauerhafte Rücknahme hält."""
    source = OParlSource.objects.create(name="Nord (Session)", url=BASIS)
    body = OParlBody.objects.create(external_id=f"{BASIS}body/", source=source, name="Nord", slug="nord")
    mirror = cast(Any, SessionMirror)(source, fetch=lambda url: {})
    daten = {"id": f"{BASIS}meeting/1/", "name": "Rat"}
    mirror._upsert_meeting(body, daten)
    sitzung = OParlMeeting.objects.get(external_id=daten["id"])

    sitzung.mark_deleted(reason=REASON_NOT_PUBLIC)
    mirror._upsert_meeting(body, daten)
    sitzung.refresh_from_db()
    assert sitzung.deleted is False

    sitzung.mark_deleted(reason=REASON_PRIVACY)
    mirror._upsert_agenda_item(sitzung, {"id": f"{BASIS}agendaitem/1/", "name": "Radweg"})
    assert mirror._upsert_meeting(body, daten) is None
    sitzung.refresh_from_db()
    assert (sitzung.deleted, sitzung.deletion_reason) == (True, REASON_PRIVACY)
    assert not OParlAgendaItem.objects.filter(external_id=f"{BASIS}agendaitem/1/").exists()
    assert mirror.stats["withheld"] == 2


class _SpaeteZeile:
    """Manager, dessen erste Suche die Zeile noch nicht findet: Ein anderer Schreiber legt sie gerade an."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.suchen = 0

    def select_for_update(self, **optionen: Any) -> Any:
        self.suchen += 1
        return self.manager.none() if self.suchen == 1 else self.manager.select_for_update(**optionen)

    def update_or_create(self, **felder: Any) -> Any:
        return self.manager.update_or_create(**felder)


def test_gleichzeitig_angelegte_zeile_behaelt_die_dauerhafte_ruecknahme(db: None) -> None:
    """Zwischen Suche und Anlegen entstanden und dauerhaft zurückgenommen: Der Lauf überschreibt die Zeile nicht."""
    source = OParlSource.objects.create(name="Nord (Session)", url=BASIS)
    body = OParlBody.objects.create(external_id=f"{BASIS}body/", source=source, name="Nord", slug="nord")
    mirror = cast(Any, SessionMirror)(source, fetch=lambda url: {})
    daten = {"id": f"{BASIS}meeting/1/", "name": "Rat"}
    mirror._upsert_meeting(body, daten)
    sitzung = OParlMeeting.objects.get(external_id=daten["id"])
    sitzung.mark_deleted(reason=REASON_PRIVACY)
    werte = {"body": body, **uebernahme.meeting(daten), **mirror._base_defaults(daten)}

    zeile, uebernommen = mirror._uebernehmen(_SpaeteZeile(OParlMeeting.objects), daten, werte)

    assert (zeile.pk, uebernommen) == (sitzung.pk, False)
    sitzung.refresh_from_db()
    assert (sitzung.deleted, sitzung.deletion_reason) == (True, REASON_PRIVACY)
    assert mirror.stats["withheld"] == 1
    # Normalfall: Ohne Markierung übernimmt derselbe Weg die inzwischen angelegte Zeile
    OParlMeeting.objects.filter(pk=sitzung.pk).update(deleted=False, deleted_at=None, deletion_reason=None)
    _zeile, uebernommen = mirror._uebernehmen(_SpaeteZeile(OParlMeeting.objects), {**daten, "name": "Stadtrat"}, werte)
    assert uebernommen is True


# =============================================================================
# Abschluss eines Laufs: Rücknahmen nur nachlesen, wenn der Lauf etwas angelegt oder wieder veröffentlicht hat
# =============================================================================


def _gezaehlt(aufrufe: list[str]) -> Abruf:
    def abruf(url: str) -> dict[str, Any]:
        aufrufe.append(url)
        return _abruf(url)

    return abruf


def test_lauf_ohne_neues_liest_keine_ruecknahmen_nach(welt: Welt) -> None:
    _spiegeln(welt, voll=True)

    aufrufe: list[str] = []
    _spiegeln(welt, abruf=_gezaehlt(aufrufe))

    # System, Liste der Körperschaften und je Liste ein Abruf: so viele wie vor dem Abschlussschritt
    assert len(aufrufe) == 2 + len(BODY_LISTS)


def test_lauf_mit_neuem_objekt_liest_die_ruecknahmen_mit_ueberlappung_nach(welt: Welt) -> None:
    _spiegeln(welt, voll=True)
    SessionPaper.objects.create(
        tenant=welt.tenant, reference="V/2", name="Grundstück Nordufer", is_public=True, status="approved"
    )
    beginn = timezone.now()

    aufrufe: list[str] = []
    _spiegeln(welt, abruf=_gezaehlt(aufrufe))

    assert len(aufrufe) == 2 + 2 * len(BODY_LISTS)
    for url in aufrufe[-len(BODY_LISTS) :]:
        seit = datetime.fromisoformat(parse_qs(urlsplit(url).query)["modified_since"][0])
        assert seit <= beginn - timedelta(minutes=4), url


# =============================================================================
# Normalfall: Wiederveröffentlichung aus Session
# =============================================================================


def test_wiederveroeffentlichter_punkt_kommt_mit_dem_naechsten_lauf_zurueck(
    welt: Welt, django_capture_on_commit_callbacks: Any
) -> None:
    _spiegeln(welt, voll=True)
    with django_capture_on_commit_callbacks(execute=True):
        welt.top.is_public = False
        welt.top.save()
    assert _punkt(welt.top).deleted is True
    # Ein Lauf dazwischen ändert daran nichts: Die Schnittstelle nennt den Punkt als zurückgenommen
    _spiegeln(welt)
    assert _punkt(welt.top).deleted is True

    with django_capture_on_commit_callbacks(execute=True):
        welt.top.is_public = True
        welt.top.save()
    statistik = _spiegeln(welt)

    punkt = _punkt(welt.top)
    assert (punkt.deleted, punkt.deleted_at, punkt.deletion_reason) == (False, None, None)
    assert punkt.name == "Radweg Hauptstraße"
    assert statistik["withheld"] == 0


def test_wiederveroeffentlichte_sitzung_bringt_ihre_punkte_zurueck(
    welt: Welt, django_capture_on_commit_callbacks: Any
) -> None:
    _spiegeln(welt, voll=True)
    with django_capture_on_commit_callbacks(execute=True):
        welt.sitzung.is_public = False
        welt.sitzung.save()
    assert (_sitzung(welt).deleted, _punkt(welt.top).deleted) == (True, True)

    with django_capture_on_commit_callbacks(execute=True):
        welt.sitzung.is_public = True
        welt.sitzung.save()
    statistik = _spiegeln(welt)

    assert (_sitzung(welt).deleted, _punkt(welt.top).deleted) == (False, False)
    assert _punkt(welt.top).deletion_reason is None
    assert statistik["withheld"] == 0
