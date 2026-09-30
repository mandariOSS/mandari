# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abbildung der Session-Objekte auf das kanonische Modell (``hub.ris.mapping.session``).

Zwei Arten von Tests:

- **Ohne Session und ohne Datenbank:** Die Abbildung liest nur Attribute und die gereichten Funktionen
  (``SessionSource``). Einfache Attrappen genügen – das belegt zugleich, dass die Drehscheibe das
  Fachmodul nicht braucht, und hält die Form der Objekte fest.
- **Mit Session:** Die Session-OParl-Schnittstelle gibt genau diese Abbildung aus. Für jeden Objekttyp
  muss die Antwort der Schnittstelle dem Ergebnis der Abbildung gleichen; eine zweite Übersetzung im
  Fachmodul fiele hier auf.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.utils import timezone

from apps.session.api import oparl as schnittstelle
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
from hub.ris.canonical import ORGANIZATION_TYPES
from hub.ris.mapping import session as mapping
from hub.ris.mapping.session import SessionMapping, SessionSource, SessionUris
from oparl_api.tests.konformitaet import pruefe

SITE = "https://mandari.example"
BASIS = f"{SITE}/session/musterstadt/api/oparl/"
ZEIT = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
STEMPEL = {"created": "2026-09-01T08:00:00+00:00", "modified": "2026-09-01T08:00:00+00:00"}


# =============================================================================
# Ohne Session: Attrappen
# =============================================================================


def _alle(*eintraege: Any) -> SimpleNamespace:
    """Beziehung wie ein vorgeladener Manager: ``.all()`` liefert die Einträge."""
    return SimpleNamespace(all=lambda: list(eintraege))


def _objekt(**felder: Any) -> SimpleNamespace:
    return SimpleNamespace(created_at=ZEIT, updated_at=ZEIT, **felder)


def _abbildung(**quelle: Any) -> SessionMapping:
    source = SessionSource(
        is_published=quelle.get("is_published", lambda obj: True),
        download_name=quelle.get("download_name", lambda datei: "anlage.pdf"),
        mime_type=quelle.get("mime_type", lambda name: "application/pdf"),
        meeting_format=quelle.get("meeting_format", lambda sitzung: None),
        results_protocol=quelle.get("results_protocol", lambda sitzung: None),
    )
    return SessionMapping(_objekt(name="Stadt Musterstadt"), BASIS, source)


def test_uris_sind_die_adressen_der_schnittstelle() -> None:
    uris = SessionUris(BASIS)
    assert uris.system() == BASIS
    assert uris.body() == f"{BASIS}body/"
    assert uris.bodies() == f"{BASIS}bodies/"
    assert uris.list("meetings") == f"{BASIS}meetings/"
    assert uris.obj("meeting", 7) == f"{BASIS}meeting/7/"
    assert uris.file_download(7) == f"{BASIS}file/7/download/"
    # Eine Basis ohne abschließenden Schrägstrich ergibt dieselben URIs
    assert SessionUris(BASIS.rstrip("/")).obj("paper", 1) == f"{BASIS}paper/1/"


def test_gremium() -> None:
    gremium = _objekt(
        id=1,
        name="Bauausschuss",
        short_name="BA",
        organization_type="committee",
        get_organization_type_display=lambda: "Ausschuss",
        start_date=date(2025, 11, 1),
        end_date=None,
        parent_id=9,
        memberships=_alle(SimpleNamespace(id=5), SimpleNamespace(id=6)),
    )

    assert _abbildung().organization(gremium) == {
        "id": f"{BASIS}organization/1/",
        "type": "https://schema.oparl.org/1.1/Organization",
        "body": f"{BASIS}body/",
        "name": "Bauausschuss",
        "shortName": "BA",
        "organizationType": "Gremium",
        "classification": "Ausschuss",
        "startDate": "2025-11-01",
        "subOrganizationOf": f"{BASIS}organization/9/",
        "membership": [f"{BASIS}membership/5/", f"{BASIS}membership/6/"],
        **STEMPEL,
    }


def test_jede_art_des_gremiums_ergibt_einen_wert_der_spezifikation() -> None:
    assert set(mapping.ORGANIZATION_TYPES.values()) <= set(ORGANIZATION_TYPES)
    unbekannt = _objekt(
        id=1,
        name="Neu",
        short_name="",
        organization_type="arbeitskreis",
        get_organization_type_display=lambda: "Arbeitskreis",
        start_date=None,
        end_date=None,
        parent_id=None,
        memberships=_alle(),
    )
    assert _abbildung().organization(unbekannt)["organizationType"] == "Sonstiges"


def _beratung(*, sitzung_oeffentlich: bool, top_oeffentlich: bool) -> SimpleNamespace:
    sitzung = SimpleNamespace(id=3, is_public=sitzung_oeffentlich)
    top = SimpleNamespace(id=4, is_public=top_oeffentlich, meeting=sitzung)
    return _objekt(
        id=2,
        paper_id=1,
        organization_id=8,
        meeting=sitzung,
        agenda_item=top,
        authoritative=True,
        get_role_display=lambda: "Entscheidung",
    )


def test_beratung_in_oeffentlicher_sitzung() -> None:
    assert _abbildung().consultation(_beratung(sitzung_oeffentlich=True, top_oeffentlich=True)) == {
        "id": f"{BASIS}consultation/2/",
        "type": "https://schema.oparl.org/1.1/Consultation",
        "paper": f"{BASIS}paper/1/",
        "organization": [f"{BASIS}organization/8/"],
        "meeting": f"{BASIS}meeting/3/",
        "agendaItem": f"{BASIS}agendaitem/4/",
        "authoritative": True,
        "role": "Entscheidung",
        **STEMPEL,
    }


@pytest.mark.parametrize(("sitzung", "top"), [(False, True), (True, False), (False, False)])
def test_beratung_im_nichtoeffentlichen_teil_nennt_nur_die_vorlage(sitzung: bool, top: bool) -> None:
    beratung = _abbildung().consultation(_beratung(sitzung_oeffentlich=sitzung, top_oeffentlich=top))

    erlaubt = {"id", "type", "paper", "created", "modified"} | ({"meeting"} if sitzung else set())
    assert set(beratung) == erlaubt
    assert "agendaItem" not in beratung and "organization" not in beratung and "role" not in beratung


def _datei(**felder: Any) -> SimpleNamespace:
    sitzung = SimpleNamespace(id=3, is_public=True)
    werte: dict[str, Any] = {
        "id": 7,
        "name": "Begründung",
        "file": "session/files/x.pdf",
        "size": 1234,
        "text_content": "Erkannter Text",
        "version": 2,
        "paper_id": 1,
        "paper": SimpleNamespace(id=1),
        "meeting_id": 3,
        "meeting": sitzung,
        "agenda_item_id": 4,
        "agenda_item": SimpleNamespace(id=4, is_public=True, meeting=sitzung),
    }
    werte.update(felder)
    return SimpleNamespace(created_at=datetime(2026, 9, 30, 23, 30, tzinfo=UTC), updated_at=ZEIT, **werte)


def test_datei() -> None:
    datei = _abbildung(download_name=lambda d: "Begründung.pdf").file(_datei())

    assert datei == {
        "id": f"{BASIS}file/7/",
        "type": "https://schema.oparl.org/1.1/File",
        "name": "Begründung",
        "fileName": "Begründung.pdf",
        "mimeType": "application/pdf",
        "size": 1234,
        # Datum in der Zeitzone der Installation: 23:30 UTC ist in Berlin der Folgetag
        "date": "2026-10-01",
        "accessUrl": f"{BASIS}file/7/download/",
        "downloadUrl": f"{BASIS}file/7/download/?download=1",
        "paper": [f"{BASIS}paper/1/"],
        "meeting": [f"{BASIS}meeting/3/"],
        "agendaItem": [f"{BASIS}agendaitem/4/"],
        "created": "2026-09-30T23:30:00+00:00",
        "modified": "2026-09-01T08:00:00+00:00",
        "mandari:version": 2,
    }
    assert pruefe(datei, "File") == []


def test_datei_text_nur_auf_wunsch() -> None:
    abbildung = _abbildung()
    assert "text" not in abbildung.file(_datei())
    assert abbildung.file_with_text(_datei())["text"] == "Erkannter Text"


def test_datei_verweist_nur_auf_oeffentliche_objekte() -> None:
    geheime_sitzung = SimpleNamespace(id=3, is_public=False)
    datei = _datei(meeting=geheime_sitzung, agenda_item=SimpleNamespace(id=4, is_public=True, meeting=geheime_sitzung))

    # Die Veröffentlichungsregel der Vorlage entscheidet das Fachmodul (gereichte Funktion)
    ergebnis = _abbildung(is_published=lambda obj: False).file(datei)

    assert "paper" not in ergebnis and "meeting" not in ergebnis and "agendaItem" not in ergebnis


def _top(**felder: Any) -> SimpleNamespace:
    werte: dict[str, Any] = {
        "id": 4,
        "meeting_id": 3,
        "number": "1",
        "order": 1,
        "name": "Radweg",
        "consultation": None,
        "vote_result": "approved",
        "get_vote_result_display": lambda: "Angenommen",
        "voting_method": "roll_call",
        "get_voting_method_display": lambda: "Namentlich",
        "votes_yes": 2,
        "votes_no": 1,
        "votes_abstain": 0,
        "resolution_text": "Der Radweg wird gebaut.",
        "resolution_number": "B/2026/1",
        "files": _alle(),
        "votes": _alle(
            *(
                SimpleNamespace(person=SimpleNamespace(display_name=name), vote=stimme, get_vote_display=lambda: "x")
                for name, stimme in (
                    ("Zeh", "no"),
                    ("Beh", "yes"),
                    ("Ah", "yes"),
                    ("Deh", "excluded"),
                    ("Eh", "absent"),
                )
            )
        ),
    }
    werte.update(felder)
    return _objekt(**werte)


def test_tagesordnungspunkt_mit_namentlicher_abstimmung() -> None:
    top = _abbildung().agenda_item(_top())

    assert top["result"] == "Angenommen"
    assert top["resolutionText"] == "Der Radweg wird gebaut."
    assert top["mandari:resolutionNumber"] == "B/2026/1"
    assert top["mandari:vote"] == {
        "method": "roll_call",
        "methodLabel": "Namentlich",
        "result": "approved",
        "resultLabel": "Angenommen",
        "yes": 2,
        "no": 1,
        "abstain": 0,
    }
    # Ja vor Nein vor Enthaltung vor Befangenheit, je Gruppe nach Namen; Abwesende fehlen
    assert [(e["name"], e["vote"]) for e in top["mandari:rollCall"]] == [
        ("Ah", "yes"),
        ("Beh", "yes"),
        ("Zeh", "no"),
        ("Deh", "excluded"),
    ]
    assert pruefe(top, "AgendaItem") == []


@pytest.mark.parametrize("verfahren", ["open", "secret", "show_of_hands"])
def test_einzelstimmen_nur_bei_namentlicher_abstimmung(verfahren: str) -> None:
    top = _abbildung().agenda_item(_top(voting_method=verfahren))
    assert "mandari:rollCall" not in top
    assert top["mandari:vote"]["method"] == verfahren


def test_offener_tagesordnungspunkt_ohne_ergebnis() -> None:
    top = _abbildung().agenda_item(_top(vote_result="pending", resolution_text="", resolution_number=""))
    assert not {"result", "resolutionText", "mandari:vote", "mandari:rollCall", "mandari:resolutionNumber"} & set(top)


def test_beratung_am_top_nur_bei_veroeffentlichter_vorlage() -> None:
    beratung = SimpleNamespace(id=2, paper=SimpleNamespace(id=1))
    top = _top(consultation=beratung)

    assert _abbildung().agenda_item(top)["consultation"] == f"{BASIS}consultation/2/"
    assert "consultation" not in _abbildung(is_published=lambda obj: False).agenda_item(top)


def test_geloeschtes_objekt_traegt_nur_kennung_und_zeiten() -> None:
    geloescht = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
    assert _abbildung().tombstone("paper", 1, ZEIT, geloescht) == {
        "id": f"{BASIS}paper/1/",
        "type": "https://schema.oparl.org/1.1/Paper",
        "created": "2026-09-01T08:00:00+00:00",
        "modified": "2026-09-02T09:00:00+00:00",
        "deleted": True,
    }


# =============================================================================
# Mit Session: Die Schnittstelle gibt genau diese Abbildung aus
# =============================================================================


@pytest.fixture
def welt(db: None, tmp_path: Path) -> Any:
    cache.clear()
    with override_settings(SITE_URL=SITE, OPARL_API_RATE_LIMIT=0, MEDIA_ROOT=str(tmp_path)):
        tenant = SessionTenant.objects.create(
            name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now()
        )
        rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
        person = SessionPerson.objects.create(tenant=tenant, given_name="Petra", family_name="Muster")
        mitgliedschaft = SessionOrganizationMembership.objects.create(organization=rat, person=person)
        periode = SessionLegislativeTerm.objects.create(tenant=tenant, name="2025–2030", start_date=date(2025, 11, 1))
        sitzung = SessionMeeting.objects.create(
            tenant=tenant,
            name="Ratssitzung",
            organization=rat,
            start=timezone.now(),
            is_public=True,
            location="Rathaus",
            room="Ratssaal",
        )
        vorlage = SessionPaper.objects.create(
            tenant=tenant, reference="V/2026/1", name="Radweg", is_public=True, status="approved"
        )
        top = SessionAgendaItem.objects.create(
            meeting=sitzung, number="1", name="Radweg", is_public=True, paper=vorlage
        )
        beratung = SessionConsultation.objects.create(
            paper=vorlage, organization=rat, meeting=sitzung, agenda_item=top, authoritative=True
        )
        datei = SessionFile.objects.create(
            tenant=tenant,
            name="Begründung",
            file=SimpleUploadedFile("begruendung.pdf", b"%PDF-1.4 Begruendung", content_type="application/pdf"),
            is_public=True,
            paper=vorlage,
        )
        yield {
            "tenant": tenant,
            "organization": rat,
            "person": person,
            "membership": mitgliedschaft,
            "legislativeterm": periode,
            "meeting": sitzung,
            "agendaitem": top,
            "paper": vorlage,
            "consultation": beratung,
            "file": datei,
        }
    cache.clear()


def _json(pfad: str) -> dict[str, Any]:
    antwort = Client().get(pfad)
    assert antwort.status_code == 200, (pfad, antwort.status_code)
    return cast(dict[str, Any], antwort.json())


ARTEN = {
    "organization": ("organizations", SessionMapping.organization),
    "person": ("people", SessionMapping.person),
    "membership": ("memberships", SessionMapping.membership),
    "legislativeterm": ("legislativeterms", SessionMapping.legislative_term),
    "meeting": ("meetings", SessionMapping.meeting),
    "agendaitem": ("agendaitems", SessionMapping.agenda_item),
    "paper": ("papers", SessionMapping.paper),
    "consultation": ("consultations", SessionMapping.consultation),
    "file": ("files", SessionMapping.file),
}


@pytest.mark.parametrize("art", sorted(ARTEN))
def test_schnittstelle_gibt_die_abbildung_aus(welt: dict[str, Any], art: str) -> None:
    segment, abbilden = ARTEN[art]
    abbildung = SessionMapping(welt["tenant"], BASIS, schnittstelle.SOURCE)
    objekt = type(welt[art]).objects.get(pk=welt[art].pk)
    erwartet = abbilden(abbildung, objekt)

    assert erwartet["id"] == f"{BASIS}{art}/{objekt.pk}/"
    # Liste: genau die Abbildung (Dateien dort ohne erkannten Text)
    assert _json(f"/session/musterstadt/api/oparl/{segment}/")["data"] == [erwartet]
    # Objekt-Endpunkt: dieselbe Abbildung, bei Dateien mit erkanntem Text
    am_endpunkt = abbildung.file_with_text(objekt) if art == "file" else erwartet
    assert _json(f"/session/musterstadt/api/oparl/{art}/{objekt.pk}/") == am_endpunkt


def test_system_und_body_aus_der_abbildung(welt: dict[str, Any]) -> None:
    abbildung = SessionMapping(welt["tenant"], BASIS, schnittstelle.SOURCE)

    assert _json("/session/musterstadt/api/oparl/") == abbildung.system()
    body = abbildung.body()
    assert _json("/session/musterstadt/api/oparl/body/") == body
    assert _json("/session/musterstadt/api/oparl/bodies/")["data"] == [body]
    assert body["legislativeTerm"] == [abbildung.legislative_term(welt["legislativeterm"])]


def test_sitzungsort_aus_der_abbildung(welt: dict[str, Any]) -> None:
    abbildung = SessionMapping(welt["tenant"], BASIS, schnittstelle.SOURCE)
    ort = abbildung.location(welt["meeting"])

    assert ort is not None and ort["id"] == f"{BASIS}location/{welt['meeting'].pk}/"
    assert _json(f"/session/musterstadt/api/oparl/location/{welt['meeting'].pk}/") == ort
    assert _json(f"/session/musterstadt/api/oparl/meeting/{welt['meeting'].pk}/")["location"] == ort


def test_session_schnittstelle_uebersetzt_nicht_selbst() -> None:
    """Typ-URLs und Feldnamen des Modells stehen nur in der Drehscheibe, nicht in der Schnittstelle."""
    quelltext = Path(schnittstelle.__file__).read_text(encoding="utf-8")
    assert "schema_type" not in quelltext
    assert "schema.oparl.org" not in quelltext
    assert '"type":' not in quelltext
