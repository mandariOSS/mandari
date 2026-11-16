# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Der Spiegel und die Rücknahme: Was zurückgenommen ist, veröffentlicht ein Lauf nicht wieder.

mandari Session nimmt ein Objekt im Bürgerportal sofort zurück (``retract_from_portal``). Ein Lauf des Spiegels ruft
seine Seiten aber ab, bevor er sie schreibt. Die Tests lassen die Rücknahme genau dazwischen geschehen: Die Seite ist
abgerufen und nennt das Objekt noch, dann stellt die Verwaltung es nichtöffentlich, dann schreibt der Lauf.

- Eine Zeile, die nach dem Abruf markiert wurde, behält ihre Markierung; unter ihr entsteht nichts.
- Eine Zeile, die es beim Abruf noch nicht gab, ist am Ende des Laufs markiert (Rücknahmen seit Beginn des Laufs).
- Eine dauerhafte Rücknahme („aus Datenschutzgründen entfernt“) übersteht jeden Lauf, wie beim Abgleich des Ingestors.
- Der Normalfall bleibt: Veröffentlicht die Verwaltung wieder, hebt der nächste Lauf die Markierung auf.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
from urllib.parse import urlsplit

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
from insight_core.models import (
    REASON_NOT_PUBLIC,
    REASON_PRIVACY,
    OParlAgendaItem,
    OParlBody,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)
from insight_sync.session_mirror import SessionMirror

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
