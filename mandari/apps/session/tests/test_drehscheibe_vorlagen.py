# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session meldet Vorlagen, Beratungsfolge und Anlagen an die Datendrehscheibe (Issue #534).

Geprüft an den Fachfunktionen (Ansichten und Dienste, wie die Oberfläche sie aufruft):

- Vorlage anlegen (``ris.paper.created``, nur nichtöffentlich, bei Anträgen mit der Einreichung), Freigabelauf
  (``ris.paper.released`` erst mit der Freigabe), ändern (öffentliche und interne Felder getrennt), zurück in
  den Entwurf bzw. nichtöffentlich (``ris.object.depublished`` mit Grund, samt Beratungen und Anlagen);
- Beratungsfolge: Station anlegen, ändern, verschieben, entfernen, terminieren – mit der Sichtbarkeit der
  Vorlage und ohne Gremium und Sitzung einer nichtöffentlichen Station im öffentlichen Ereignis;
- Anlagen: hochladen, umbenennen, ersetzen, nichtöffentlich stellen, löschen – öffentlich nur an
  veröffentlichten Objekten.

Jedes Ereignis prüft ``publish()`` gegen seinen Vertrag (``EVENTS_VALIDATE_CONTRACTS``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Max
from django.test import Client
from django.utils import timezone
from mandari_oparl.ids import canonical_id

from apps.common.models import IdentifierBase
from apps.common.tests.factories import UserFactory
from apps.events.models import Event
from apps.session.models import (
    SessionApplication,
    SessionConsultation,
    SessionFile,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import application_service

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
BASIS = f"{SITE}/session/nord/api/oparl/"
ALLE_RECHTE = {f.name for f in SessionRole._meta.get_fields() if f.name.startswith("can_")}


def kennung(kind: str, pk: Any) -> str:
    return str(canonical_id(f"{BASIS}{kind}/{pk}/"))


@dataclass
class Welt:
    tenant: SessionTenant
    rat: SessionOrganization
    bau: SessionOrganization
    user: SessionUser
    client: Client

    def url(self, pfad: str) -> str:
        return f"/session/{self.tenant.slug}{pfad}"

    def vorlage(self, **werte: Any) -> SessionPaper:
        daten: dict[str, Any] = {
            "tenant": self.tenant,
            "name": "Radweg Hauptstraße",
            "paper_type": "proposal",
            "is_public": True,
            "status": "approved",
            "has_financial_impact": False,
            "main_organization": self.rat,
        }
        daten.update(werte)
        return SessionPaper.objects.create(**daten)

    def sitzung(self, **werte: Any) -> SessionMeeting:
        daten: dict[str, Any] = {
            "tenant": self.tenant,
            "name": "Ratssitzung",
            "organization": self.rat,
            "start": (timezone.now() + timedelta(days=14)).replace(second=0, microsecond=0),
            "is_public": True,
        }
        daten.update(werte)
        return SessionMeeting.objects.create(**daten)

    def hochladen(self, ziel: Any, name: str = "anlage.txt", *, oeffentlich: bool = True) -> SessionFile:
        art = "paper" if isinstance(ziel, SessionPaper) else "meeting"
        daten: dict[str, Any] = {
            "target_type": art,
            "target_id": str(ziel.pk),
            "files": SimpleUploadedFile(name, f"Inhalt {name}".encode(), content_type="text/plain"),
        }
        if oeffentlich:
            daten["is_public"] = "on"
        antwort = self.client.post(self.url("/files/upload/"), daten)
        assert antwort.status_code == 302
        return SessionFile.objects.filter(tenant=self.tenant, name=name).latest("created_at")


@pytest.fixture
def welt(settings: Any, tmp_path: Path) -> Welt:
    cache.clear()
    settings.SITE_URL = SITE
    settings.SESSION_EVENTS = "aktiv"
    settings.MEDIA_ROOT = str(tmp_path)
    IdentifierBase.objects.update_or_create(pk=1, defaults={"url": SITE})
    tenant = SessionTenant.objects.create(name="Bezirk Nord", slug="nord", oparl_public_since=timezone.now())
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", short_name="Rat")
    bau = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss", short_name="Bau")
    rolle = SessionRole.objects.create(tenant=tenant, name="Sitzungsdienst", **dict.fromkeys(ALLE_RECHTE, True))
    user = SessionUser.objects.create(user=cast(Any, UserFactory)(), tenant=tenant)
    user.roles.add(rolle)
    client = Client()
    client.force_login(user.user)
    return Welt(tenant=tenant, rat=rat, bau=bau, user=user, client=client)


def _start() -> int:
    return int(Event.objects.aggregate(hoechste=Max("id"))["hoechste"] or 0)


def _neu(seit: int) -> list[Event]:
    return list(Event.objects.filter(id__gt=seit).order_by("id"))


def _kurz(events: list[Event]) -> list[tuple[str, str, str]]:
    return [(e.type, e.visibility, e.operation) for e in events]


def _bearbeiten(welt: Welt, vorlage: SessionPaper, **felder: Any) -> None:
    daten: dict[str, Any] = {
        "name": vorlage.name,
        "paper_type": vorlage.paper_type,
        "main_text": vorlage.main_text,
        "resolution_text": vorlage.resolution_text,
        "status": vorlage.status,
        "main_organization": str(vorlage.main_organization_id or ""),
        "date": vorlage.date.isoformat() if vorlage.date else "",
        "has_financial_impact": "True" if vorlage.has_financial_impact else "False",
    }
    if vorlage.is_public:
        daten["is_public"] = "on"
    for name, wert in felder.items():
        if wert is None:
            daten.pop(name, None)
        else:
            daten[name] = wert
    antwort = welt.client.post(welt.url(f"/papers/{vorlage.id}/edit/"), daten)
    assert antwort.status_code == 302, antwort.content.decode()[:3000]


# =============================================================================
# Vorlagen
# =============================================================================


class TestVorlagen:
    def test_anlegen_ist_nichtoeffentlich_bis_zur_freigabe(self, welt: Welt) -> None:
        seit = _start()
        antwort = welt.client.post(
            welt.url("/papers/create/"),
            {"name": "Neubau Kita", "paper_type": "proposal", "is_public": "on", "main_organization": str(welt.rat.pk)},
        )
        assert antwort.status_code == 302
        vorlage = SessionPaper.objects.get(tenant=welt.tenant, name="Neubau Kita")
        events = _neu(seit)
        assert _kurz(events) == [("ris.paper.created", "nichtoeffentlich", "upsert")]
        assert events[0].payload == {"paper": kennung("paper", vorlage.pk)}
        assert str(events[0].aggregate_id) == kennung("paper", vorlage.pk)

    def test_freigabelauf(self, welt: Welt) -> None:
        vorlage = welt.vorlage(status="draft")
        seit = _start()
        assert welt.client.post(welt.url(f"/papers/{vorlage.id}/workflow/submit/")).status_code == 302
        events = _neu(seit)
        assert _kurz(events) == [("ris.paper.changed", "nichtoeffentlich", "upsert")]
        assert events[0].payload["changed"] == ["status"]

        seit = _start()
        assert welt.client.post(welt.url(f"/papers/{vorlage.id}/workflow/approve/")).status_code == 302
        vorlage.refresh_from_db()
        assert vorlage.status == "approved"
        events = _neu(seit)
        assert _kurz(events) == [("ris.paper.released", "oeffentlich", "upsert")]
        assert events[0].payload == {"paper": kennung("paper", vorlage.pk)}

    def test_aendern_trennt_oeffentliche_und_interne_felder(self, welt: Welt) -> None:
        vorlage = welt.vorlage(date=date(2026, 9, 1))
        seit = _start()
        _bearbeiten(welt, vorlage, date="2026-09-15", main_organization=str(welt.bau.pk))
        events = _neu(seit)
        assert _kurz(events) == [("ris.paper.changed", "oeffentlich", "upsert")]
        assert events[0].payload["changed"] == ["date", "underDirectionOf"]

        entwurf = welt.vorlage(status="draft", name="Entwurf")
        seit = _start()
        _bearbeiten(welt, entwurf, main_text="Neuer Sachverhalt")
        events = _neu(seit)
        assert _kurz(events) == [("ris.paper.changed", "nichtoeffentlich", "upsert")]
        assert events[0].payload["changed"] == ["mainText"]

        vorlage.refresh_from_db()
        seit = _start()
        _bearbeiten(welt, vorlage)
        assert _neu(seit) == [], "Speichern ohne Änderung meldet nichts"

    def test_zurueckziehen_und_zurueck_in_den_entwurf(self, welt: Welt) -> None:
        vorlage = welt.vorlage(status="draft")
        anlage = welt.hochladen(vorlage)  # Anlagen kommen im Entwurf an die Vorlage
        SessionPaper.objects.filter(pk=vorlage.pk).update(status="approved", approved_at=timezone.now())
        vorlage.refresh_from_db()
        station = SessionConsultation.objects.create(paper=vorlage, organization=welt.rat, role="decision", order=1)

        # Zurückziehen: Die Vorlage bleibt veröffentlicht, der Bearbeitungsstand ist intern
        seit = _start()
        _bearbeiten(welt, vorlage, status="withdrawn")
        events = _neu(seit)
        assert _kurz(events) == [("ris.paper.changed", "nichtoeffentlich", "upsert")]
        assert events[0].payload["changed"] == ["status"]

        # Zurück in den Entwurf: Rücknahme der Vorlage samt Station und Anlage
        vorlage.refresh_from_db()
        seit = _start()
        _bearbeiten(welt, vorlage, status="draft")
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.object.depublished", "oeffentlich", "delete"),
            ("ris.paper.changed", "nichtoeffentlich", "upsert"),
            ("ris.object.depublished", "oeffentlich", "delete"),
            ("ris.object.depublished", "oeffentlich", "delete"),
        ]
        assert [e.payload["object"] for e in events if e.type == "ris.object.depublished"] == [
            kennung("paper", vorlage.pk),
            kennung("consultation", station.pk),
            kennung("file", anlage.pk),
        ]
        assert {e.payload["reason"] for e in events if e.type == "ris.object.depublished"} == {"zurueckgenommen"}
        assert events[1].payload["changed"] == ["status"]

    def test_nichtoeffentlich_stellen(self, welt: Welt) -> None:
        vorlage = welt.vorlage()
        seit = _start()
        _bearbeiten(welt, vorlage, is_public=None)
        events = _neu(seit)
        assert _kurz(events)[:2] == [
            ("ris.object.depublished", "oeffentlich", "delete"),
            ("ris.paper.changed", "nichtoeffentlich", "upsert"),
        ]
        assert events[0].payload["reason"] == "nichtoeffentlich"
        assert events[1].payload["changed"] == ["public"]

    def test_vorlage_aus_antrag_nennt_die_einreichung(self, welt: Welt) -> None:
        antrag = SessionApplication.objects.create(
            tenant=welt.tenant, title="Mehr Bäume", application_type="motion", status="accepted"
        )
        seit = _start()
        vorlage, angelegt = application_service.convert_to_paper(
            antrag, session_user=welt.user, main_organization_id=str(welt.rat.pk)
        )
        assert angelegt
        events = _neu(seit)
        assert _kurz(events) == [("ris.paper.created", "nichtoeffentlich", "upsert")]
        assert events[0].payload == {"paper": kennung("paper", vorlage.pk), "submission": str(antrag.pk)}


# =============================================================================
# Beratungsfolge
# =============================================================================


class TestBeratungsfolge:
    def test_station_anlegen_aendern_verschieben_entfernen(self, welt: Welt) -> None:
        vorlage = welt.vorlage()
        seit = _start()
        antwort = welt.client.post(
            welt.url(f"/papers/{vorlage.id}/consultations/add/"),
            {"organization": str(welt.bau.pk), "role": "preliminary"},
        )
        assert antwort.status_code == 302
        erste = SessionConsultation.objects.get(paper=vorlage)
        events = _neu(seit)
        assert _kurz(events) == [("ris.consultation.changed", "oeffentlich", "upsert")]
        assert events[0].payload == {
            "consultation": kennung("consultation", erste.pk),
            "paper": kennung("paper", vorlage.pk),
            "change": "added",
            "organization": kennung("organization", welt.bau.pk),
        }

        welt.client.post(
            welt.url(f"/papers/{vorlage.id}/consultations/add/"), {"organization": str(welt.rat.pk), "role": "decision"}
        )
        zweite = SessionConsultation.objects.get(paper=vorlage, organization=welt.rat)
        seit = _start()
        assert welt.client.post(welt.url(f"/consultations/{zweite.id}/move/"), {"direction": "up"}).status_code == 302
        events = _neu(seit)
        assert [(e.payload["consultation"], e.payload["changed"]) for e in events] == [
            (kennung("consultation", zweite.pk), ["order"]),
            (kennung("consultation", erste.pk), ["order"]),
        ]

        seit = _start()
        assert welt.client.post(welt.url(f"/consultations/{zweite.id}/delete/")).status_code == 302
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.consultation.changed", "oeffentlich", "upsert"),
            ("ris.object.depublished", "oeffentlich", "delete"),
        ]
        assert events[0].payload["changed"] == ["order"], "Die verbliebene Station rückt auf"
        assert events[1].payload["object"] == kennung("consultation", zweite.pk)

    def test_station_einer_nichtoeffentlichen_sitzung_nennt_weder_gremium_noch_sitzung(self, welt: Welt) -> None:
        vorlage = welt.vorlage()
        geheim = welt.sitzung(organization=welt.bau, is_public=False)
        seit = _start()
        welt.client.post(
            welt.url(f"/papers/{vorlage.id}/consultations/add/"),
            {"organization": str(welt.bau.pk), "role": "preliminary", "meeting": str(geheim.pk)},
        )
        events = _neu(seit)
        assert _kurz(events) == [("ris.consultation.changed", "oeffentlich", "upsert")]
        assert set(events[0].payload) == {"consultation", "paper", "change"}

    def test_station_einer_vorlage_im_entwurf_ist_nichtoeffentlich(self, welt: Welt) -> None:
        entwurf = welt.vorlage(status="draft")
        seit = _start()
        welt.client.post(
            welt.url(f"/papers/{entwurf.id}/consultations/add/"), {"organization": str(welt.bau.pk), "role": "decision"}
        )
        assert _kurz(_neu(seit)) == [("ris.consultation.changed", "nichtoeffentlich", "upsert")]

    def test_terminieren_meldet_vorlage_top_und_station(self, welt: Welt) -> None:
        vorlage = welt.vorlage()
        sitzung = welt.sitzung()
        station = SessionConsultation.objects.create(
            paper=vorlage, organization=welt.rat, role="decision", order=1, meeting=sitzung
        )
        seit = _start()
        assert welt.client.post(welt.url(f"/consultations/{station.id}/schedule/")).status_code == 302
        station.refresh_from_db()
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.paper.changed", "nichtoeffentlich", "upsert"),
            ("ris.agendaitem.changed", "oeffentlich", "upsert"),
            ("ris.consultation.changed", "oeffentlich", "upsert"),
        ]
        assert events[0].payload["changed"] == ["status"]
        assert events[1].payload["change"] == "added"
        assert events[1].payload["paper"] == kennung("paper", vorlage.pk)
        assert events[2].payload["change"] == "scheduled"
        assert events[2].payload["agenda_item"] == kennung("agendaitem", station.agenda_item_id)


# =============================================================================
# Anlagen
# =============================================================================


class TestAnlagen:
    def test_anlage_im_entwurf_wird_mit_der_freigabe_oeffentlich(self, welt: Welt) -> None:
        entwurf = welt.vorlage(status="draft", name="Entwurf")
        seit = _start()
        anlage = welt.hochladen(entwurf, "plan.txt")
        events = _neu(seit)
        assert _kurz(events) == [("ris.file.changed", "nichtoeffentlich", "upsert")]
        SessionPaper.objects.filter(pk=entwurf.pk).update(status="review")
        assert events[0].payload == {
            "file": kennung("file", anlage.pk),
            "change": "added",
            "paper": kennung("paper", entwurf.pk),
        }

        seit = _start()
        assert welt.client.post(welt.url(f"/papers/{entwurf.id}/workflow/approve/")).status_code == 302
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.paper.released", "oeffentlich", "upsert"),
            ("ris.file.changed", "oeffentlich", "upsert"),
        ]
        assert events[1].payload == {
            "file": kennung("file", anlage.pk),
            "change": "added",
            "paper": kennung("paper", entwurf.pk),
        }

    def test_umbenennen_ersetzen_zuruecknehmen_loeschen(self, welt: Welt) -> None:
        sitzung = welt.sitzung()
        anlage = welt.hochladen(sitzung, "einladung.txt")

        seit = _start()
        welt.client.post(welt.url(f"/files/{anlage.id}/update/"), {"name": "Einladung.txt", "is_public": "on"})
        welt.client.post(
            welt.url(f"/files/{anlage.id}/replace/"),
            {"file": SimpleUploadedFile("Einladung.txt", b"neuer Inhalt", content_type="text/plain")},
        )
        events = _neu(seit)
        assert [(e.visibility, e.payload["change"]) for e in events] == [
            ("oeffentlich", "renamed"),
            ("oeffentlich", "replaced"),
        ]
        assert events[0].payload["meeting"] == kennung("meeting", sitzung.pk)

        seit = _start()
        welt.client.post(welt.url(f"/files/{anlage.id}/update/"), {"name": "Einladung.txt"})
        events = _neu(seit)
        assert _kurz(events) == [("ris.object.depublished", "oeffentlich", "delete")]
        assert events[0].payload["reason"] == "nichtoeffentlich"

        seit = _start()
        welt.client.post(welt.url(f"/files/{anlage.id}/delete/"))
        events = _neu(seit)
        assert _kurz(events) == [("ris.file.changed", "nichtoeffentlich", "delete")]
        assert events[0].payload["change"] == "removed"

        oeffentlich = welt.hochladen(sitzung, "anlage2.txt")
        seit = _start()
        welt.client.post(welt.url(f"/files/{oeffentlich.id}/delete/"))
        events = _neu(seit)
        assert _kurz(events) == [("ris.object.depublished", "oeffentlich", "delete")]
        assert events[0].payload["reason"] == "quelle_geloescht"

    def test_ersetzen_mit_gleichem_inhalt_meldet_nichts(self, welt: Welt) -> None:
        sitzung = welt.sitzung()
        anlage = welt.hochladen(sitzung, "gleich.txt")
        seit = _start()
        welt.client.post(
            welt.url(f"/files/{anlage.id}/replace/"),
            {"file": SimpleUploadedFile("gleich.txt", b"Inhalt gleich.txt", content_type="text/plain")},
        )
        assert _neu(seit) == []


class TestFreischaltung:
    """Vor der Freischaltung der Schnittstelle (Issue #319): Vorlagen, Stationen und Anlagen nur nichtöffentlich."""

    @pytest.fixture(autouse=True)
    def gesperrt(self, welt: Welt) -> None:
        welt.tenant.oparl_public_since = None
        welt.tenant.save(update_fields=["oparl_public_since"])

    def test_freigabe_station_anlage_und_ruecknahme_ohne_oeffentliches_ereignis(self, welt: Welt) -> None:
        vorlage = welt.vorlage(status="draft")
        sitzung = welt.sitzung()
        seit = _start()
        anlage = welt.hochladen(vorlage, "plan.txt")
        antwort = welt.client.post(
            welt.url(f"/papers/{vorlage.id}/consultations/add/"),
            {"organization": str(welt.rat.pk), "role": "decision", "meeting": str(sitzung.pk)},
        )
        assert antwort.status_code == 302
        assert welt.client.post(welt.url(f"/papers/{vorlage.id}/workflow/submit/")).status_code == 302
        assert welt.client.post(welt.url(f"/papers/{vorlage.id}/workflow/approve/")).status_code == 302
        vorlage.refresh_from_db()
        assert vorlage.status == "approved"
        _bearbeiten(welt, vorlage, date="2026-09-15")
        vorlage.refresh_from_db()
        _bearbeiten(welt, vorlage, is_public=None)
        assert welt.client.post(welt.url(f"/files/{anlage.id}/delete/")).status_code == 302
        sitzungs_anlage = welt.hochladen(sitzung, "einladung.txt")
        welt.client.post(welt.url(f"/files/{sitzungs_anlage.id}/delete/"))

        events = _neu(seit)
        assert {e.type for e in events} >= {"ris.file.changed", "ris.consultation.changed", "ris.paper.changed"}
        assert {e.visibility for e in events} == {"nichtoeffentlich"}, "Nichts ist öffentlich vor der Freischaltung"
        assert not {"ris.paper.released", "ris.object.depublished"} & {e.type for e in events}


def test_sitzung_nichtoeffentlich_nimmt_ihre_anlagen_zurueck(welt: Welt) -> None:
    """Die Anlagen einer Sitzung folgen ihrer Öffentlichkeit (Beobachtung der Sitzung schließt sie ein)."""
    from apps.session import hub_events

    sitzung = welt.sitzung()
    anlage = welt.hochladen(sitzung, "bericht.txt")
    seit = _start()
    with hub_events.track(welt.tenant) as tracked:
        tracked.meeting(sitzung)
        sitzung.is_public = False
        sitzung.save()
    events = [e for e in _neu(seit) if e.aggregate_type == "File"]
    assert _kurz(events) == [("ris.object.depublished", "oeffentlich", "delete")]
    assert events[0].payload["object"] == kennung("file", anlage.pk)
