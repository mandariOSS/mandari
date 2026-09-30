# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Änderungsfeed je Kommune (Issue #562, ``docs/adr/20260929-aenderungsfeed-format.md``).

Die Tests erzeugen ihre Ereignisse selbst – geprüft gegen das Vertragsregister (``ereignisse.py``) – und
lesen den Feed über beide Ausgaben der Schnittstelle:

- Schalter: ausgeschaltet gibt es den Feed nicht, und nichts weist auf ihn hin
- Einträge ``{cursor, operation, type, id, modified, reason?}`` ohne Inhalte, für ``upsert``, ``delete``
  und ``redact``
- opaker Cursor über die Folgenummer, Blättern, ``ETag``/``304``
- nur Öffentliches: Nichtöffentliches ist weder als Eintrag noch an der Antwort erkennbar
- Aufbewahrung: Ein älterer Cursor ergibt ``410`` mit dem Verweis auf den Snapshot
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings

from apps.events import pruning
from apps.events.models import Event
from hub.api import aggregator, changes
from hub.api.http import BadRequestError
from hub.api.tests import ereignisse
from hub.api.tests.ereignisse import AGGREGATE, T0, TENANT, huelle, naechste_nummer, schreiben
from hub.contracts import EVENT, ContractViolationError, Envelope, get_registry
from insight_core import publication
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
API = f"{SITE}/oparl/v1"
RIS = "https://ris.example/oparl"
HEUTE = date(2026, 9, 30)
PAPER = "https://schema.oparl.org/1.1/Paper"
FELDER = {"cursor", "operation", "type", "id", "modified", "reason"}


@pytest.fixture(autouse=True)
def _heute(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feste Uhr: Cursor tragen ihren Ausgabetag."""
    monkeypatch.setattr(changes, "today", lambda: HEUTE)


@pytest.fixture
def kommune() -> Any:
    cache.clear()
    with override_settings(
        SITE_URL=SITE,
        OPARL_BASE_URL=f"{SITE}/oparl",
        OPARL_API_RATE_LIMIT=0,
        OPARL_API_CACHE_SECONDS=0,
        OPARL_CHANGES_ENABLED=True,
    ):
        source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
        yield OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Musterstadt")
    cache.clear()


def _pfad(body: OParlBody) -> str:
    return f"/oparl/v1/body/{body.pk}/changes"


def _feed(body: OParlBody, **parameter: str) -> dict[str, Any]:
    antwort = Client().get(_pfad(body), parameter)
    assert antwort.status_code == 200, (antwort.status_code, antwort.content)
    return cast(dict[str, Any], antwort.json())


def _stand(body: OParlBody, token: str) -> changes.Cursor:
    return changes.decode_cursor(body.pk, token)


def _im_bestand(kommune: OParlBody, art: str, kennung: uuid.UUID) -> None:
    """Objekt im RIS-Bestand anlegen: Der Aggregator nennt nur Adressen, unter denen er etwas ausliefert."""
    modell = aggregator.OBJECT_TYPES[art].model
    if art == "body" or modell._default_manager.filter(pk=kennung).exists():
        return
    adresse = f"{RIS}/{art}/{kennung}"
    if art == "agendaitem":
        sitzung = OParlMeeting.objects.create(external_id=f"{adresse}/sitzung", body=kommune)
        OParlAgendaItem.objects.create(id=kennung, external_id=adresse, meeting=sitzung)
    elif art == "membership":
        person = OParlPerson.objects.create(external_id=f"{adresse}/person", body=kommune)
        gremium = OParlOrganization.objects.create(external_id=f"{adresse}/gremium", body=kommune)
        OParlMembership.objects.create(id=kennung, external_id=adresse, person=person, organization=gremium)
    else:
        modell._default_manager.create(id=kennung, external_id=adresse, body=kommune)


def ereignis(typ: str, body: uuid.UUID | None, objekt: uuid.UUID | None = None, **angaben: Any) -> Event:
    """
    Ereignis im Journal und – wie beim Ingestor in derselben Transaktion – sein Objekt im Bestand der
    Kommune. ``bestand=False``: nur das Ereignis (das Objekt fehlt im Bestand).
    """
    bestand = angaben.pop("bestand", True)
    angelegt = ereignisse.ereignis(typ, body, objekt, **angaben)
    kommune = OParlBody.objects.filter(pk=body).first() if body else None
    gegenstand = changes._subject(angelegt)
    if bestand and kommune is not None and gegenstand is not None:
        _im_bestand(kommune, *gegenstand)
    return angelegt


def ruecknahme(body: uuid.UUID, objekttyp: str, objekt: uuid.UUID, grund: str, **angaben: Any) -> Event:
    """``ris.object.depublished`` (``ereignisse.ruecknahme``); das Objekt steht im Bestand."""
    angaben.setdefault("operation", "redact" if grund == "datenschutz" else "delete")
    nutzlast = {"object_type": objekttyp, "object": str(objekt), "reason": grund}
    return ereignis("ris.object.depublished", body, objekt, nutzlast=nutzlast, **angaben)


# =============================================================================
# Schalter
# =============================================================================


def test_ausgeschaltet_gibt_es_den_feed_nicht() -> None:
    """Standard: aus. Die Adresse verhält sich wie vor dem Feed, und der Body weist nicht auf ihn hin."""
    cache.clear()
    with override_settings(OPARL_API_RATE_LIMIT=0, OPARL_API_CACHE_SECONDS=0):
        source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
        body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Musterstadt")
        ereignis("ris.paper.changed", body.pk)

        antwort = Client().get(_pfad(body))
        unbekannt = Client().get(f"/oparl/v1/body/{body.pk}/quatsch")

        assert not changes.enabled()
        assert antwort.status_code == 404
        assert antwort.json()["error"] == unbekannt.json()["error"].replace("quatsch", "changes")
        assert "mandari:changes" not in Client().get(f"/oparl/v1/body/{body.pk}").json()
        assert "changes" not in Client().get("/oparl/v1/bodies").content.decode()


def test_eingeschaltet_nennt_der_body_die_adresse_des_feeds(kommune: OParlBody) -> None:
    body = Client().get(f"/oparl/v1/body/{kommune.pk}").json()

    assert body["mandari:changes"] == f"{API}/body/{kommune.pk}/changes"
    assert Client().get("/oparl/v1/bodies").json()["data"][0]["mandari:changes"] == body["mandari:changes"]
    leer = _feed(kommune)
    assert leer["data"] == []
    assert leer["links"] == {
        "self": body["mandari:changes"],
        "next": f"{body['mandari:changes']}?after={leer['cursor']}",
        "snapshot": f"{API}/body/{kommune.pk}/snapshot",
    }


def test_unbekannte_kommune_und_abgeschaltete_veroeffentlichung(kommune: OParlBody) -> None:
    assert Client().get(f"/oparl/v1/body/{uuid.uuid4()}/changes").status_code == 404
    publication.set_source_state(kommune.source, publication.PAUSED)

    pausiert = Client().get(_pfad(kommune))

    assert pausiert.status_code == 503 and pausiert["Retry-After"]


# =============================================================================
# Einträge
# =============================================================================


def test_eintraege_nennen_operation_typ_adresse_und_zeit(kommune: OParlBody) -> None:
    vorlage, sitzung, datei, person = (uuid.uuid4() for _ in range(4))
    ereignis("ris.paper.changed", kommune.pk, vorlage, zeit=T0)
    ereignis("ris.meeting.scheduled", kommune.pk, sitzung, zeit=T0 + timedelta(hours=1))
    ruecknahme(kommune.pk, "File", datei, "nichtoeffentlich", zeit=T0 + timedelta(hours=2))
    ruecknahme(kommune.pk, "Person", person, "datenschutz", zeit=T0 + timedelta(hours=3))

    eintraege = _feed(kommune)["data"]

    assert [{name: wert for name, wert in eintrag.items() if name != "cursor"} for eintrag in eintraege] == [
        {"operation": "upsert", "type": PAPER, "id": f"{API}/paper/{vorlage}", "modified": "2026-09-01T08:00:00+00:00"},
        {
            "operation": "upsert",
            "type": "https://schema.oparl.org/1.1/Meeting",
            "id": f"{API}/meeting/{sitzung}",
            "modified": "2026-09-01T09:00:00+00:00",
        },
        {
            "operation": "delete",
            "type": "https://schema.oparl.org/1.1/File",
            "id": f"{API}/file/{datei}",
            "modified": "2026-09-01T10:00:00+00:00",
            "reason": "nichtoeffentlich",
        },
        {
            "operation": "redact",
            "type": "https://schema.oparl.org/1.1/Person",
            "id": f"{API}/person/{person}",
            "modified": "2026-09-01T11:00:00+00:00",
            "reason": "datenschutz",
        },
    ]
    assert [list(eintrag)[:5] for eintrag in eintraege] == [["cursor", "operation", "type", "id", "modified"]] * 4


def test_eintraege_enthalten_keine_inhalte(kommune: OParlBody) -> None:
    vorlage = OParlPaper.objects.create(external_id=f"{RIS}/paper/1", body=kommune, name="Geheimnisvoller Betreff")
    ereignis("ris.paper.changed", kommune.pk, vorlage.pk, nutzlast={"paper": str(vorlage.pk), "changed": ["paperType"]})

    antwort = Client().get(_pfad(kommune))
    eintrag = antwort.json()["data"][0]

    assert set(eintrag) <= FELDER
    text = antwort.content.decode()
    assert "Geheimnisvoll" not in text and "paperType" not in text and "changed" not in text
    # Die Inhalte holt der Abnehmer über die Adresse des Objekts
    assert Client().get(eintrag["id"].removeprefix(SITE)).json()["name"] == "Geheimnisvoller Betreff"


@pytest.mark.parametrize("grund", sorted(changes.DELETE_REASONS))
def test_ruecknahme_ist_delete_mit_grund(kommune: OParlBody, grund: str) -> None:
    ruecknahme(kommune.pk, "Paper", uuid.uuid4(), grund)

    (eintrag,) = _feed(kommune)["data"]

    assert (eintrag["operation"], eintrag["reason"]) == ("delete", grund)


def test_ruecknahme_folgt_dem_vertrag_auch_bei_abweichender_operation_der_huelle(kommune: OParlBody) -> None:
    """``ris.object.depublished`` ist nie ``upsert``; der Grund ``datenschutz`` ist immer ``redact``."""
    ruecknahme(kommune.pk, "Paper", uuid.uuid4(), "zurueckgenommen", operation="upsert")
    ruecknahme(kommune.pk, "Paper", uuid.uuid4(), "datenschutz", operation="delete")
    # Entfernen ohne Grund: ein anderes Ereignis mit der Operation delete
    ereignis("ris.file.changed", kommune.pk, operation="delete")

    eintraege = _feed(kommune)["data"]

    # Ein delete nennt immer seinen Grund (ADR): ohne Rücknahme hat die Quelle das Objekt entfernt
    assert [(e["operation"], e["reason"]) for e in eintraege] == [
        ("delete", "zurueckgenommen"),
        ("redact", "datenschutz"),
        ("delete", changes.DEFAULT_DELETE_REASON),
    ]
    assert changes.DEFAULT_DELETE_REASON == "quelle_geloescht"


def test_jeder_oeffentliche_ereignistyp_des_registers_ist_eingeordnet(kommune: OParlBody) -> None:
    """Gegen das Vertragsregister: Jedes ``ris.*``-Ereignis, das öffentlich sein darf, ergibt einen Eintrag."""
    oeffentlich = {
        vertrag.name
        for vertrag in get_registry().contracts(EVENT)
        if vertrag.name.startswith(changes.TYPE_PREFIX) and "oeffentlich" in vertrag.visibility
    }
    assert oeffentlich == set(AGGREGATE), "neuer Ereignistyp: in AGGREGATE einordnen"

    erwartet = []
    for typ in sorted(oeffentlich):
        for beispiel in get_registry().latest(typ).examples:
            # Ein Ereignis zum Body nennt die Kommune selbst
            objekt = kommune.pk if AGGREGATE[typ][0] == "Body" else None
            angelegt = ereignis(typ, kommune.pk, objekt, nutzlast=beispiel, operation=_operation(typ, beispiel))
            if angelegt.aggregate_type in changes.KINDS:
                art = changes.KINDS[angelegt.aggregate_type]
                erwartet.append(
                    (f"https://schema.oparl.org/1.1/{angelegt.aggregate_type}", f"{API}/{art}/{angelegt.aggregate_id}")
                )
            else:
                # Ohne eigene Adresse: Die Abstimmung erscheint an ihrem Tagesordnungspunkt
                assert angelegt.aggregate_type == "Voting"
                erwartet.append(
                    ("https://schema.oparl.org/1.1/AgendaItem", f"{API}/agendaitem/{beispiel['agenda_item']}")
                )

    eintraege = _feed(kommune, limit="1000")["data"]

    assert [(e["type"], e["id"]) for e in eintraege] == erwartet
    assert len(eintraege) == Event.objects.count()


def test_abstimmung_erscheint_als_aenderung_ihres_tagesordnungspunkts(kommune: OParlBody) -> None:
    top = uuid.uuid4()
    # Auch eine entfernte Abstimmung ändert den Tagesordnungspunkt nur – er selbst bleibt
    ereignis(
        "ris.voting.recorded",
        kommune.pk,
        nutzlast={"voting": str(uuid.uuid4()), "agenda_item": str(top)},
        operation="redact",
    )
    # Die Rücknahme einer Abstimmung nennt keinen Tagesordnungspunkt: kein Eintrag, der Stand bleibt stehen
    ruecknahme(kommune.pk, "Voting", uuid.uuid4(), "zurueckgenommen")

    seite = _feed(kommune)

    assert [(e["operation"], e["type"], e["id"]) for e in seite["data"]] == [
        ("upsert", "https://schema.oparl.org/1.1/AgendaItem", f"{API}/agendaitem/{top}")
    ]
    assert seite["cursor"] == seite["data"][0]["cursor"]


def _operation(typ: str, nutzlast: dict[str, Any]) -> str:
    if typ != "ris.object.depublished":
        return "upsert"
    return "redact" if nutzlast["reason"] == "datenschutz" else "delete"


def test_ereignisse_der_tests_halten_den_vertrag_ein(kommune: OParlBody) -> None:
    """Die Testhilfe schreibt nichts in das Journal, was der Vertrag des Typs nicht erlaubt."""
    with pytest.raises(ContractViolationError):
        ereignis("ris.paper.released", kommune.pk, sichtbarkeit="intern")
    with pytest.raises(ContractViolationError):
        ereignis("ris.paper.changed", kommune.pk, nutzlast={"paper": str(uuid.uuid4()), "name": "Inhalt"})
    assert not Event.objects.exists()


# =============================================================================
# Cursor und Blättern
# =============================================================================


def test_seiten_in_aufsteigender_reihenfolge_bis_zur_leeren_seite(kommune: OParlBody) -> None:
    kennungen = [ereignis("ris.paper.changed", kommune.pk).aggregate_id for _ in range(5)]

    gesehen: list[str] = []
    seite = _feed(kommune, limit="2")
    seiten = 1
    while seite["data"]:
        gesehen += [eintrag["id"] for eintrag in seite["data"]]
        assert seite["cursor"] == seite["data"][-1]["cursor"]
        assert seite["links"]["next"] == f"{API}/body/{kommune.pk}/changes?after={seite['cursor']}&limit=2"
        seite = cast(dict[str, Any], Client().get(seite["links"]["next"].removeprefix(SITE)).json())
        seiten += 1

    assert gesehen == [f"{API}/paper/{kennung}" for kennung in kennungen]
    assert seiten == 4  # 2 + 2 + 1 Einträge, dann die leere Seite
    # Leere Seite: Der Abnehmer ist aktuell; sein Stand bleibt die letzte gesehene Folgenummer
    assert _stand(kommune, seite["cursor"]).seq == naechste_nummer() - 1


def test_neue_aenderung_erscheint_nach_dem_letzten_stand(kommune: OParlBody) -> None:
    ereignis("ris.paper.changed", kommune.pk)
    stand = _feed(kommune)["cursor"]
    assert _feed(kommune, after=stand)["data"] == []

    neu = ereignis("ris.paper.changed", kommune.pk)

    (eintrag,) = _feed(kommune, after=stand)["data"]
    assert eintrag["id"] == f"{API}/paper/{neu.aggregate_id}"
    # Jeder Eintrag trägt den Cursor, von dem aus es nach ihm weitergeht
    assert _feed(kommune, after=eintrag["cursor"])["data"] == []


def test_cursor_ist_opak(kommune: OParlBody) -> None:
    for nummer in (987654321, 987654322):
        angelegt = ereignis("ris.paper.changed", kommune.pk, nummeriert=False)
        Event.objects.filter(pk=angelegt.pk).update(seq=nummer)

    antwort = Client().get(_pfad(kommune), {"after": changes.encode_cursor(kommune.pk, 987654320, HEUTE)})
    tokens = [eintrag["cursor"] for eintrag in antwort.json()["data"]]

    assert len(tokens) == 2 and all(re.fullmatch(r"[A-Za-z0-9_-]{38}", token) for token in tokens)
    # Keine Folgenummer in der Antwort, auch nicht im Cursor selbst
    assert "98765432" not in antwort.content.decode()
    for token in tokens:
        roh = base64.urlsafe_b64decode(token + "==")
        assert (987654321).to_bytes(8, "big")[4:] not in roh and (987654322).to_bytes(8, "big")[4:] not in roh
    # Aufeinanderfolgende Nummern ergeben keine ähnlichen Cursor
    gleich = sum(a == b for a, b in zip(tokens[0], tokens[1], strict=True))
    assert gleich < 12
    # Nur die Installation liest ihn wieder
    assert [_stand(kommune, token).seq for token in tokens] == [987654321, 987654322]
    assert _stand(kommune, tokens[0]).day == HEUTE


def test_cursor_gilt_nur_fuer_seine_kommune_und_seine_installation(kommune: OParlBody) -> None:
    token = changes.encode_cursor(kommune.pk, 7, HEUTE)

    assert changes.decode_cursor(kommune.pk, token) == changes.Cursor(seq=7, day=HEUTE)
    assert token == changes.encode_cursor(kommune.pk, 7, HEUTE), "gleicher Stand, gleicher Cursor"
    assert token != changes.encode_cursor(uuid.uuid4(), 7, HEUTE)
    with pytest.raises(changes.CursorExpiredError):
        changes.decode_cursor(uuid.uuid4(), token)
    with override_settings(SECRET_KEY="ein-anderer-schluessel-" + "x" * 40), pytest.raises(changes.CursorExpiredError):
        changes.decode_cursor(kommune.pk, token)
    # Nach einem Schlüsselwechsel gelten Cursor des alten Schlüssels weiter, solange er als Rückfall hinterlegt ist
    from django.conf import settings

    with override_settings(SECRET_KEY="neuer-schluessel-" + "y" * 40, SECRET_KEY_FALLBACKS=[settings.SECRET_KEY]):
        assert changes.decode_cursor(kommune.pk, token).seq == 7


@pytest.mark.parametrize("token", ["abc", "x" * 37, "x" * 39, "ä" * 38, "=" * 38, "a b" + "c" * 35])
def test_was_kein_cursor_ist_wird_abgelehnt(kommune: OParlBody, token: str) -> None:
    with pytest.raises(BadRequestError):
        changes.decode_cursor(kommune.pk, token)

    antwort = Client().get(_pfad(kommune), {"after": token})

    assert antwort.status_code == 400
    assert "after" in antwort.json()["error"]


def test_veraenderter_cursor_gilt_als_abgelaufen(kommune: OParlBody) -> None:
    token = changes.encode_cursor(kommune.pk, 7, HEUTE)
    gefaelscht = ("A" if token[0] != "A" else "B") + token[1:]

    antwort = Client().get(_pfad(kommune), {"after": gefaelscht})

    assert antwort.status_code == 410


@pytest.mark.parametrize(("limit", "status"), [("0", 400), ("-3", 400), ("viele", 400), ("1", 200), ("5000", 200)])
def test_limit(kommune: OParlBody, limit: str, status: int) -> None:
    for _ in range(3):
        ereignis("ris.paper.changed", kommune.pk)

    antwort = Client().get(_pfad(kommune), {"limit": limit})

    assert antwort.status_code == status
    if limit == "1":
        assert len(antwort.json()["data"]) == 1
    if limit == "5000":
        # Obergrenze statt Fehler; die Links nennen die geltende Seitengröße
        assert antwort.json()["links"]["self"].endswith(f"?limit={changes.MAX_LIMIT}")


def test_unveraenderte_seite_hat_denselben_etag(kommune: OParlBody) -> None:
    ereignis("ris.paper.changed", kommune.pk)
    erste = Client().get(_pfad(kommune))
    stand = erste.json()["cursor"]

    for adresse, parameter in ((_pfad(kommune), {}), (_pfad(kommune), {"after": stand})):
        a, b = Client().get(adresse, parameter), Client().get(adresse, parameter)
        assert a.content == b.content and a["ETag"] == b["ETag"]
        assert a["Cache-Control"] == "no-cache" and a["Access-Control-Allow-Origin"] == "*"
        unveraendert = Client().get(adresse, parameter, headers={"If-None-Match": a["ETag"]})
        assert unveraendert.status_code == 304 and unveraendert.content == b""

    ereignis("ris.paper.changed", kommune.pk)
    nachher = Client().get(_pfad(kommune), {"after": stand}, headers={"If-None-Match": erste["ETag"]})
    assert nachher.status_code == 200 and len(nachher.json()["data"]) == 1


def test_feed_ist_rein_lesend_und_begrenzt_wie_die_uebrige_schnittstelle(
    kommune: OParlBody, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert Client().post(_pfad(kommune)).status_code == 405
    assert Client().options(_pfad(kommune)).status_code == 204
    assert Client().get(f"{_pfad(kommune)}/").status_code == 301

    monkeypatch.setattr("hub.api.http.time.time", lambda: 1_790_000_010.0)
    with override_settings(OPARL_API_RATE_LIMIT=1):
        cache.clear()
        assert Client().get(_pfad(kommune)).status_code == 200
        gedrosselt = Client().get(_pfad(kommune))

    assert gedrosselt.status_code == 429
    # Das Zählfenster ist die laufende Minute
    assert gedrosselt["Retry-After"] == str(60 - 1_790_000_010 % 60)


# =============================================================================
# Nur Öffentliches
# =============================================================================


def _verborgenes(kommune: OParlBody) -> None:
    """Alles, was im öffentlichen Feed der Kommune nichts zu suchen hat."""
    ereignis("ris.paper.changed", kommune.pk, sichtbarkeit="nichtoeffentlich")
    ereignis("ris.meeting.changed", kommune.pk, sichtbarkeit="nichtoeffentlich")
    schreiben(
        Envelope(
            type="ris.file.text_extracted",
            version=1,
            aggregate_type="File",
            aggregate_id=uuid.uuid4(),
            tenant_ref=TENANT,
            visibility="intern",
            body_id=kommune.pk,
            payload={"file": str(uuid.uuid4()), "method": "pypdf"},
        )
    )
    # andere Kommune, ohne Kommune, noch ohne Folgenummer
    ereignis("ris.paper.changed", uuid.uuid4())
    ereignis("ris.paper.changed", None)
    ereignis("ris.paper.changed", kommune.pk, nummeriert=False)
    # kein Ereignis des RIS-Modells
    Event.objects.create(
        type="work.task.assigned",
        version=1,
        aggregate_type="Paper",
        aggregate_id=uuid.uuid4(),
        tenant_ref="org:3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d",
        body_id=kommune.pk,
        visibility="oeffentlich",
        occurred_at=T0,
        correlation_id=uuid.uuid4(),
        payload={},
        seq=naechste_nummer(),
    )


def test_nichtoeffentliches_erscheint_nie(kommune: OParlBody) -> None:
    sichtbar = ereignis("ris.paper.changed", kommune.pk)
    _verborgenes(kommune)

    eintraege = _feed(kommune)["data"]

    assert [eintrag["id"] for eintrag in eintraege] == [f"{API}/paper/{sichtbar.aggregate_id}"]


def test_nichtoeffentliches_ist_auch_an_der_antwort_nicht_erkennbar(kommune: OParlBody) -> None:
    """Weder Cursor noch ``ETag`` ändern sich, wenn Nichtöffentliches geschieht – auch nicht der Zeitpunkt."""
    ereignis("ris.paper.changed", kommune.pk)
    stand = _feed(kommune)["cursor"]
    vorher = [Client().get(_pfad(kommune)), Client().get(_pfad(kommune), {"after": stand})]

    _verborgenes(kommune)

    nachher = [Client().get(_pfad(kommune)), Client().get(_pfad(kommune), {"after": stand})]
    for a, b in zip(vorher, nachher, strict=True):
        assert b.content == a.content
        assert b["ETag"] == a["ETag"]
    assert nachher[1].json()["data"] == [] and nachher[1].json()["cursor"] == stand
    # … während Öffentliches sofort erscheint
    ereignis("ris.paper.changed", kommune.pk)
    assert Client().get(_pfad(kommune), {"after": stand}).content != nachher[1].content


def test_wechsel_auf_nichtoeffentlich_erscheint_als_delete(kommune: OParlBody) -> None:
    vorlage = uuid.uuid4()
    ereignis("ris.paper.released", kommune.pk, vorlage)
    ruecknahme(kommune.pk, "Paper", vorlage, "nichtoeffentlich", zeit=T0 + timedelta(days=1))
    # Was danach mit der nichtöffentlichen Vorlage geschieht, bleibt verborgen
    ereignis("ris.paper.changed", kommune.pk, vorlage, sichtbarkeit="nichtoeffentlich", zeit=T0 + timedelta(days=2))

    eintraege = _feed(kommune)["data"]

    assert [(e["operation"], e.get("reason"), e["modified"]) for e in eintraege] == [
        ("upsert", None, "2026-09-01T08:00:00+00:00"),
        ("delete", "nichtoeffentlich", "2026-09-02T08:00:00+00:00"),
    ]


# =============================================================================
# Ereignisse ohne Adresse
# =============================================================================


def test_objekt_ohne_eintrag_im_bestand_bekommt_keinen_eintrag(kommune: OParlBody) -> None:
    """
    Wie die Session-Schnittstelle nennt der Aggregator nur Adressen, unter denen er etwas ausliefert. Ein
    Ereignis zu einem Objekt, das der Bestand nicht kennt (fälschlich als öffentlich gemeldet oder noch
    nicht übernommen), ergibt keinen Eintrag, und die Antwort bleibt dieselbe.
    """
    ereignis("ris.paper.changed", kommune.pk)
    stand = _feed(kommune)["cursor"]
    vorher = Client().get(_pfad(kommune), {"after": stand})

    fehlt = ereignis("ris.paper.changed", kommune.pk, bestand=False)
    ruecknahme(kommune.pk, "File", uuid.uuid4(), "zurueckgenommen", bestand=False)
    abstimmung = {"voting": str(uuid.uuid4()), "agenda_item": str(uuid.uuid4())}
    ereignis("ris.voting.recorded", kommune.pk, nutzlast=abstimmung, bestand=False)

    nachher = Client().get(_pfad(kommune), {"after": stand})
    assert nachher.content == vorher.content and nachher["ETag"] == vorher["ETag"]
    assert str(fehlt.aggregate_id) not in nachher.content.decode()
    # Übernimmt der Bestand das Objekt, erscheint der Eintrag: Der Cursor stand vor dem Ereignis
    _im_bestand(kommune, "paper", fehlt.aggregate_id)
    assert [e["id"] for e in _feed(kommune, after=stand)["data"]] == [f"{API}/paper/{fehlt.aggregate_id}"]


def test_geloeschtes_und_der_ort_einer_sitzung_haben_eine_adresse(kommune: OParlBody) -> None:
    """Gelöschtes bleibt als gekürztes Objekt abrufbar; ein Ort ohne eigenes Objekt trägt die Kennung der Sitzung."""
    vorlage = OParlPaper.objects.create(external_id=f"{RIS}/paper/weg", body=kommune, deleted=True)
    sitzung = OParlMeeting.objects.create(external_id=f"{RIS}/meeting/1", body=kommune)
    ruecknahme(kommune.pk, "Paper", vorlage.pk, "quelle_geloescht", bestand=False)
    ruecknahme(kommune.pk, "Location", sitzung.pk, "zurueckgenommen", bestand=False)

    eintraege = _feed(kommune)["data"]

    assert [(e["type"].rsplit("/", 1)[1], e["id"]) for e in eintraege] == [
        ("Paper", f"{API}/paper/{vorlage.pk}"),
        ("Location", f"{API}/location/{sitzung.pk}"),
    ]
    assert Client().get(eintraege[0]["id"].removeprefix(SITE)).json()["deleted"] is True


def _block(kommune: OParlBody, anzahl: int) -> None:
    """
    ``anzahl`` öffentliche Ereignisse ohne Adresse: Rücknahmen von Abstimmungen, die keinen
    Tagesordnungspunkt nennen – etwa, wenn eine ganze Kommune zurückgenommen wird.
    """
    muster = huelle(
        "ris.object.depublished",
        kommune.pk,
        nutzlast={"object_type": "Voting", "object": str(uuid.uuid4()), "reason": "zurueckgenommen"},
        operation="delete",
    )
    get_registry().validate_event(muster)
    erste = naechste_nummer()
    zeilen = []
    for nummer in range(anzahl):
        kennung = uuid.uuid4()
        zeilen.append(
            Event(
                type=muster.type,
                version=muster.version,
                aggregate_type=muster.aggregate_type,
                aggregate_id=kennung,
                tenant_ref=muster.tenant_ref,
                body_id=kommune.pk,
                visibility=muster.visibility,
                operation=muster.operation,
                occurred_at=T0,
                correlation_id=uuid.uuid4(),
                payload={**muster.payload, "object": str(kennung)},
                seq=erste + nummer,
            )
        )
    Event.objects.bulk_create(zeilen, batch_size=500)


@pytest.mark.parametrize("limit", [None, "1", "1000"])
def test_feed_bleibt_hinter_einem_block_ereignisse_ohne_adresse_nicht_stehen(
    kommune: OParlBody, limit: str | None
) -> None:
    """
    Mehr Ereignisse ohne Adresse, als eine Anfrage liest: Der Cursor rückt über den gelesenen Abschnitt
    vor. Sonst bekäme ein Abnehmer nie wieder eine Änderung und hielte sich für aktuell.
    """
    parameter = {"limit": limit} if limit else {}
    budget = max(int(limit or changes.DEFAULT_LIMIT) * changes._MAX_EXAMINED_FACTOR, changes._MIN_EXAMINED)
    ereignis("ris.paper.changed", kommune.pk)
    stand = _feed(kommune, **parameter)["cursor"]
    _block(kommune, budget + 1)
    danach = ereignis("ris.paper.changed", kommune.pk)

    leer = _feed(kommune, after=stand, **parameter)
    seite = _feed(kommune, after=leer["cursor"], **parameter)

    assert leer["data"] == [] and leer["cursor"] != stand
    assert _stand(kommune, leer["cursor"]).seq == _stand(kommune, stand).seq + budget
    assert [eintrag["id"] for eintrag in seite["data"]] == [f"{API}/paper/{danach.aggregate_id}"]
    assert _feed(kommune, after=seite["cursor"], **parameter)["data"] == []


def test_kleinerer_block_ohne_adresse_aendert_die_antwort_nicht(kommune: OParlBody) -> None:
    """Unterhalb des Lesebudgets bleibt der Stand stehen: Die Antwort verrät übersprungene Ereignisse nicht."""
    ereignis("ris.paper.changed", kommune.pk)
    stand = _feed(kommune)["cursor"]
    vorher = Client().get(_pfad(kommune), {"after": stand})

    _block(kommune, changes._MIN_EXAMINED - 1)

    nachher = Client().get(_pfad(kommune), {"after": stand})
    assert nachher.content == vorher.content and nachher["ETag"] == vorher["ETag"]
    danach = ereignis("ris.paper.changed", kommune.pk)
    assert [e["id"] for e in _feed(kommune, after=stand)["data"]] == [f"{API}/paper/{danach.aggregate_id}"]


# =============================================================================
# Aufbewahrung
# =============================================================================


def _abgelaufen(antwort: Any, kommune: OParlBody) -> None:
    assert antwort.status_code == 410
    assert antwort["Content-Type"] == "application/problem+json; charset=utf-8"
    assert antwort["Cache-Control"] == "no-store" and "ETag" not in antwort
    assert antwort["Access-Control-Allow-Origin"] == "*"
    problem = json.loads(antwort.content)
    assert problem == {
        "type": "https://docs.mandari.de/api/probleme/cursor-abgelaufen",
        "title": "Nicht mehr verfügbar",
        "status": 410,
        "detail": problem["detail"],
        "instance": _pfad(kommune),
        "snapshot": f"{API}/body/{kommune.pk}/snapshot",
    }
    assert "Snapshot" in problem["detail"]


def test_cursor_aelter_als_die_aufbewahrung_ergibt_410_mit_verweis_auf_den_snapshot(kommune: OParlBody) -> None:
    ereignis("ris.paper.changed", kommune.pk)
    tage = changes.retention_days()
    gerade_noch = changes.encode_cursor(kommune.pk, 0, HEUTE - timedelta(days=tage))
    zu_alt = changes.encode_cursor(kommune.pk, 0, HEUTE - timedelta(days=tage + 1))

    assert len(_feed(kommune, after=gerade_noch)["data"]) == 1
    _abgelaufen(Client().get(_pfad(kommune), {"after": zu_alt}), kommune)


def test_aufbewahrung_betraegt_mindestens_30_tage() -> None:
    assert changes.retention_days() >= changes.MIN_RETENTION_DAYS == 30
    with override_settings(OPARL_CHANGES_RETENTION_DAYS=30):
        assert changes.retention_days() == 30
    with override_settings(OPARL_CHANGES_RETENTION_DAYS=29), pytest.raises(ImproperlyConfigured):
        changes.retention_days()


def test_jede_antwort_gibt_einen_frischen_cursor_aus(kommune: OParlBody) -> None:
    """Wer regelmäßig fragt, bleibt gültig – auch wenn sich lange nichts ändert."""
    ereignis("ris.paper.changed", kommune.pk)
    alt = changes.encode_cursor(kommune.pk, 1, HEUTE - timedelta(days=60))

    seite = _feed(kommune, after=alt)

    assert seite["data"] == []
    frisch = _stand(kommune, seite["cursor"])
    assert (frisch.seq, frisch.day) == (1, HEUTE)
    assert seite["cursor"] != alt


def _um(tag: date, stunde: int = 0) -> datetime:
    return datetime.combine(tag, time(stunde), UTC)


def test_ohne_cursor_von_vorn_nur_solange_der_anfang_des_journals_da_ist(kommune: OParlBody) -> None:
    erste = ereignis("ris.paper.changed", kommune.pk)
    ereignis("ris.paper.changed", kommune.pk)
    assert len(_feed(kommune)["data"]) == 2

    # Das Journal wurde aufgeräumt: Der Anfang fehlt, neue Abnehmer steigen über den Snapshot ein
    Event.objects.filter(pk=erste.pk).delete()
    pruning.record(erste.seq or 0, _um(HEUTE - timedelta(days=100)))

    _abgelaufen(Client().get(_pfad(kommune)), kommune)


def test_luecke_am_anfang_des_journals_ist_kein_aufraeumen(kommune: OParlBody) -> None:
    """
    Der Sequenzierer darf Nummern verwerfen (``nextval()`` ist nicht transaktional). Beginnt das Journal
    deshalb nicht bei 1, fehlt trotzdem nichts: kein ``410`` – weder ohne Cursor noch für einen Cursor,
    der vor dem ersten Ereignis ausgegeben wurde.
    """
    vor_dem_ersten = _feed(kommune)["cursor"]
    gestern = changes.encode_cursor(kommune.pk, 0, HEUTE - timedelta(days=1))
    angelegt = ereignis("ris.paper.changed", kommune.pk, nummeriert=False)
    # Zwei Nummern hat ein zurückgerollter Lauf verbraucht
    Event.objects.filter(pk=angelegt.pk).update(seq=3, recorded_at=_um(HEUTE, 9))
    erwartet = [f"{API}/paper/{angelegt.aggregate_id}"]

    for parameter in ({}, {"after": vor_dem_ersten}, {"after": gestern}):
        assert [eintrag["id"] for eintrag in _feed(kommune, **parameter)["data"]] == erwartet


def test_aufgeraeumtes_journal_laesst_gueltige_cursor_gelten(kommune: OParlBody) -> None:
    """Räumt das Journal länger Zurückliegendes auf, verpasst ein Abnehmer mit gültigem Cursor nichts."""
    alt = [ereignis("ris.paper.changed", uuid.uuid4()) for _ in range(3)]
    neu = ereignis("ris.paper.changed", kommune.pk)
    Event.objects.filter(pk__in=[e.pk for e in alt]).delete()
    # Gelöscht ist, was vor 45 Tagen erfasst wurde
    pruning.record(alt[-1].seq or 0, _um(HEUTE - timedelta(days=45)))
    gestern = changes.encode_cursor(kommune.pk, 1, HEUTE - timedelta(days=1))

    (eintrag,) = _feed(kommune, after=gestern)["data"]

    assert eintrag["id"] == f"{API}/paper/{neu.aggregate_id}"
    # Ein Abnehmer, der nach dem Aufräumen über den Snapshot einsteigt, liest weiter
    snapshot = changes.encode_cursor(kommune.pk, changes.head(kommune.pk), HEUTE)
    assert _feed(kommune, after=snapshot)["data"] == []


def test_journal_das_kuerzer_aufbewahrt_als_zugesagt_ergibt_410(kommune: OParlBody) -> None:
    """Sicherheitsnetz: Wurden Zeilen gelöscht, die nach der Ausgabe des Cursors entstanden sein können, gilt er nicht."""
    alt = [ereignis("ris.paper.changed", uuid.uuid4()) for _ in range(3)]
    ereignis("ris.paper.changed", kommune.pk)
    Event.objects.filter(pk__in=[e.pk for e in alt]).delete()
    # Gelöscht ist auch, was gestern früh erfasst wurde
    pruning.record(alt[-1].seq or 0, _um(HEUTE - timedelta(days=1), 6))

    for tag in (HEUTE - timedelta(days=1), HEUTE - timedelta(days=2)):
        _abgelaufen(Client().get(_pfad(kommune), {"after": changes.encode_cursor(kommune.pk, 1, tag)}), kommune)
    # Nach dem Aufräumen ausgegeben: Es fehlt nichts
    assert len(_feed(kommune, after=changes.encode_cursor(kommune.pk, 1, HEUTE))["data"]) == 1
    # Schließt der Cursor an die gelöschten Zeilen an, fehlt nichts
    anschluss = changes.encode_cursor(kommune.pk, alt[-1].seq or 0, HEUTE - timedelta(days=2))
    assert len(_feed(kommune, after=anschluss)["data"]) == 1


def test_aufraeumen_wird_ausdruecklich_festgehalten(kommune: OParlBody) -> None:
    assert pruning.horizon() is None
    pruning.record(7, _um(HEUTE - timedelta(days=100)))
    pruning.record(12, _um(HEUTE - timedelta(days=95)))

    assert pruning.horizon() == pruning.Horizon(through_seq=12, recorded_before=_um(HEUTE - timedelta(days=95)))
    with pytest.raises(ValueError):
        pruning.record(0, _um(HEUTE))
    with pytest.raises(ValueError):
        pruning.record(3, datetime(2026, 9, 1))


def test_stand_der_kommune_ist_ihr_neuestes_oeffentliches_ereignis(kommune: OParlBody) -> None:
    assert changes.head(kommune.pk) == 0
    letztes = ereignis("ris.paper.changed", kommune.pk)
    _verborgenes(kommune)

    assert changes.head(kommune.pk) == letztes.seq
