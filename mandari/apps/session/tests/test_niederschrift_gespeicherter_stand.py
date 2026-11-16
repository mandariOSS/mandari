# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Niederschrift: Bearbeiten, Workflow-Schritte und öffentliche Fassung gelten für den gespeicherten Stand.

- Ein Workflow-Schritt prüft Status und Vier-Augen-Prinzip an der frisch gelesenen Zeile, nicht an dem Stand, den
  die Anfrage vorher geladen hat, und schreibt nur seine eigenen Felder.
- Das Bearbeiten prüft den Status unter derselben Sperre und speichert nur Inhaltsfelder.
- Die öffentliche Fassung entsteht aus dem Stand unter der Sperre; es bleibt genau eine Datei.
- Als inhaltliche Bearbeitung zählen auch Absetzung, Abstimmungsart und Betreff eines TOP.
- Der Verlauf der Berichtigungen folgt der heutigen Sichtbarkeit des TOP.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest
from django.contrib.messages import get_messages

from apps.session import audit
from apps.session.models import (
    SessionAgendaItem,
    SessionFile,
    SessionMeeting,
    SessionProtocol,
    SessionProtocolCorrection,
    SessionUser,
)
from apps.session.services import (
    four_eyes_service,
    protocol_correction_service,
    protocol_publication,
    protocol_service,
)
from apps.session.services.four_eyes_service import PROCESS_PROTOCOL, ApprovalError
from apps.session.tests._niederschrift import Welt, base, client, welt

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media(settings: Any, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


def _lesestand(w: Welt) -> SessionProtocol:
    """Die Niederschrift, wie eine Anfrage sie zu Beginn lädt."""
    return SessionProtocol.objects.select_related("meeting__tenant", "meeting__organization").get(pk=w.protokoll.pk)


def _gespeichert(w: Welt) -> SessionProtocol:
    return SessionProtocol.objects.get(pk=w.protokoll.pk)


def _oeffentliche_dateien(w: Welt) -> list[SessionFile]:
    return list(SessionFile.objects.filter(meeting=w.sitzung, name=protocol_publication.PUBLIC_FILE_NAME))


class _Anfrage:
    """Laufender Request für die Signal-Hooks (wie ihn die Middleware setzt)."""

    def __init__(self, session_user: SessionUser) -> None:
        self.request = SimpleNamespace(session_user=session_user, META={})

    def __enter__(self) -> None:
        cast(Any, audit).set_current_request(self.request)

    def __exit__(self, *args: object) -> None:
        cast(Any, audit).clear_current_request()


# --- Workflow-Schritte -----------------------------------------------------------------------------------------


def test_genehmigung_sieht_die_bearbeitung_nach_dem_laden() -> None:
    w = welt(status="review", four_eyes=True)
    protokoll = _lesestand(w)
    # Nach dem Laden bearbeitet dieselbe Person einen TOP: Der Vermerk steht nur in der Datenbank
    SessionProtocol.objects.filter(pk=protokoll.pk).update(content_edited_by=w.zweite)

    with pytest.raises(protocol_service.WorkflowError):
        protocol_service.perform_action(protokoll, "approve", user=w.zweite, data={})

    gespeichert = _gespeichert(w)
    assert gespeichert.status == "review"
    assert gespeichert.content_edited_by_id == w.zweite.pk


def test_workflow_schritt_schreibt_nur_seine_felder() -> None:
    w = welt(status="review", four_eyes=True)
    protokoll = _lesestand(w)
    SessionProtocol.objects.filter(pk=protokoll.pk).update(content_edited_by=w.zweite, content="Neuer Stand")

    protocol_service.perform_action(protokoll, "approve", user=w.genehmiger, data={})

    gespeichert = _gespeichert(w)
    assert gespeichert.status == "approved"
    assert gespeichert.approved_by_id == w.genehmiger.pk
    # Inhalt und Bearbeitungsvermerk bleiben, wie sie gespeichert waren
    assert gespeichert.content == "Neuer Stand"
    assert gespeichert.content_edited_by_id == w.zweite.pk


def test_zurueckweisen_nach_zwischenzeitlicher_genehmigung_wird_abgewiesen() -> None:
    w = welt(status="review")
    protokoll = _lesestand(w)
    protocol_service.perform_action(_lesestand(w), "approve", user=w.genehmiger, data={})

    with pytest.raises(protocol_service.WorkflowError):
        protocol_service.perform_action(protokoll, "reject", user=w.zweite, data={"comment": "bitte ändern"})

    gespeichert = _gespeichert(w)
    assert gespeichert.status == "approved"
    assert gespeichert.approved_by_id == w.genehmiger.pk


def test_zweites_veroeffentlichen_hinterlaesst_genau_eine_datei() -> None:
    w = welt()
    protokoll = _lesestand(w)
    protocol_service.perform_action(_lesestand(w), "publish", user=w.genehmiger, data={})
    erste = _gespeichert(w).public_file_id
    assert erste is not None

    # Dieselbe Aktion aus dem Stand vor der ersten Veröffentlichung
    with pytest.raises(protocol_service.WorkflowError):
        protocol_service.perform_action(protokoll, "publish", user=w.zweite, data={})

    assert _gespeichert(w).public_file_id == erste
    assert [datei.pk for datei in _oeffentliche_dateien(w)] == [erste]


# --- Bearbeiten ------------------------------------------------------------------------------------------------


def test_bearbeiten_nach_zwischenzeitlicher_veroeffentlichung_aendert_nichts() -> None:
    from apps.session.views import protocols as protocol_views

    w = welt(status="review")
    echt: Any = protocol_views._get_meeting

    def laden_dann_veroeffentlichen(view: Any, meeting_id: Any) -> SessionMeeting:
        # Die Anfrage lädt Sitzung und Niederschrift; erst danach wird veröffentlicht
        meeting = cast(SessionMeeting, echt(view, meeting_id))
        assert cast(Any, meeting).protocol.status == "review"
        SessionProtocol.objects.filter(pk=w.protokoll.pk).update(status="published", approved_by=w.genehmiger)
        return meeting

    with mock.patch.object(protocol_views, "_get_meeting", laden_dann_veroeffentlichen):
        antwort = client(w.protokollant).post(
            f"{base(w)}/meetings/{w.sitzung.pk}/protocol/edit/",
            {"content": "Überschrieben", f"protocol_note_{w.top.pk}": "Überschrieben"},
        )

    assert antwort.status_code == 302
    meldungen = " ".join(str(m) for m in get_messages(cast(Any, antwort).wsgi_request))
    assert "nicht mehr bearbeitbar" in meldungen
    gespeichert = _gespeichert(w)
    assert gespeichert.status == "published"
    assert gespeichert.approved_by_id == w.genehmiger.pk
    assert gespeichert.content == "Allgemeiner Teil der Sitzung."
    assert SessionAgendaItem.objects.get(pk=w.top.pk).protocol_note == "Aussprache zum Radweg."


def test_bearbeiten_speichert_inhalt_und_vermerk() -> None:
    w = welt(status="review", four_eyes=True)

    antwort = client(w.protokollant).post(
        f"{base(w)}/meetings/{w.sitzung.pk}/protocol/edit/",
        {"content": "Neuer allgemeiner Teil", "chair_name": "Vera Vorsitz", f"protocol_note_{w.top.pk}": "Neu"},
    )

    assert antwort.status_code == 302
    gespeichert = _gespeichert(w)
    assert gespeichert.status == "review"
    assert gespeichert.content == "Neuer allgemeiner Teil"
    assert gespeichert.content_edited_by_id == w.protokollant.pk
    assert SessionAgendaItem.objects.get(pk=w.top.pk).protocol_note == "Neu"


# --- Öffentliche Fassung ---------------------------------------------------------------------------------------


def test_erneuerung_nimmt_den_stand_unter_der_sperre() -> None:
    w = welt()
    protocol_service.perform_action(_lesestand(w), "publish", user=w.genehmiger, data={})
    assert "Radweg Hauptstraße" in _oeffentliche_dateien(w)[0].text_content
    # Eine Änderung am allgemeinen Teil stößt die Erneuerung an …
    SessionProtocol.objects.filter(pk=w.protokoll.pk).update(chair_name="Viktor Vorsitz")
    echt = protocol_service.build_protocol_pdf
    aufrufe: list[int] = []

    def pdf_und_top_wird_nichtoeffentlich(protocol: SessionProtocol, **kwargs: Any) -> bytes:
        # … und während das PDF entsteht, wird der TOP nichtöffentlich
        aufrufe.append(1)
        SessionAgendaItem.objects.filter(pk=w.top.pk).update(is_public=False)
        return echt(protocol, **kwargs)

    with mock.patch.object(protocol_service, "build_protocol_pdf", pdf_und_top_wird_nichtoeffentlich):
        protocol_publication.refresh_meeting(w.sitzung.pk)

    dateien = _oeffentliche_dateien(w)
    assert len(dateien) == 1
    assert dateien[0].pk == _gespeichert(w).public_file_id
    assert "Viktor Vorsitz" in dateien[0].text_content
    assert "Radweg Hauptstraße" not in dateien[0].text_content
    assert len(aufrufe) == 2, "Text und PDF entstehen nach der Änderung neu"


def test_erneuerung_ohne_aenderung_laesst_die_datei_stehen() -> None:
    w = welt()
    protocol_service.perform_action(_lesestand(w), "publish", user=w.genehmiger, data={})
    vorher = _gespeichert(w).public_file_id

    protocol_publication.refresh_meeting(w.sitzung.pk)

    assert _gespeichert(w).public_file_id == vorher
    assert len(_oeffentliche_dateien(w)) == 1


def test_erneuerung_nach_ruecknahme_legt_keine_datei_an() -> None:
    w = welt()
    protokoll = _lesestand(w)
    protocol_service.perform_action(protokoll, "publish", user=w.genehmiger, data={})
    veraltet = _lesestand(w)
    protocol_service.perform_action(_lesestand(w), "unpublish", user=w.genehmiger, data={})
    assert _oeffentliche_dateien(w) == []

    # Eine Erneuerung, die noch den veröffentlichten Stand gelesen hat
    protocol_publication._refresh(veraltet)

    assert _oeffentliche_dateien(w) == []
    assert _gespeichert(w).public_file_id is None


# --- Vier-Augen-Prinzip: was als Bearbeitung zählt ----------------------------------------------------------------


@pytest.mark.parametrize(
    "aenderung",
    [
        {"voting_method": "roll_call"},
        {"is_election": True},
        {"name": "Radweg Nebenstraße"},
        {"is_supplementary": True},
    ],
)
def test_aenderung_am_top_zaehlt_als_bearbeitung(aenderung: dict[str, Any]) -> None:
    w = welt(status="review", four_eyes=True)
    assert four_eyes_service.authorize(PROCESS_PROTOCOL, _gespeichert(w), w.zweite) is None

    item = SessionAgendaItem.objects.get(pk=w.top.pk)
    for name, value in aenderung.items():
        setattr(item, name, value)
    with _Anfrage(w.zweite):
        item.save()

    protokoll = _gespeichert(w)
    assert protokoll.content_edited_by_id == w.zweite.pk
    with pytest.raises(ApprovalError):
        four_eyes_service.authorize(PROCESS_PROTOCOL, protokoll, w.zweite)
    assert four_eyes_service.authorize(PROCESS_PROTOCOL, protokoll, w.genehmiger) is None


def test_absetzen_zaehlt_als_bearbeitung() -> None:
    w = welt(status="review", four_eyes=True)

    antwort = client(w.genehmiger).post(f"{base(w)}/agenda/{w.top.pk}/withdraw/", {"reason": "vertagt"})

    assert antwort.status_code == 302
    assert SessionAgendaItem.objects.get(pk=w.top.pk).is_withdrawn
    protokoll = _gespeichert(w)
    assert protokoll.content_edited_by_id == w.genehmiger.pk
    with pytest.raises(protocol_service.WorkflowError):
        protocol_service.perform_action(_lesestand(w), "approve", user=w.genehmiger, data={})


def test_speichern_ohne_aenderung_zaehlt_nicht_als_bearbeitung() -> None:
    w = welt(status="review", four_eyes=True)

    with _Anfrage(w.zweite):
        SessionAgendaItem.objects.get(pk=w.top.pk).save()

    assert _gespeichert(w).content_edited_by_id is None


# --- Verlauf der Berichtigungen --------------------------------------------------------------------------------


def _berichtigung(w: Welt) -> SessionProtocolCorrection:
    protocol_correction_service.propose(
        _lesestand(w),
        target=SessionProtocolCorrection.TARGET_ITEM,
        item=SessionAgendaItem.objects.select_related("meeting__tenant").get(pk=w.top.pk),
        data={"resolution_text": "Der Ausschuss beschließt den Radweg mit Beleuchtung."},
        reason="Schreibfehler im Beschlusstext",
        user=w.genehmiger,
        can_view_np=True,
    )
    return SessionProtocolCorrection.objects.get(protocol=w.protokoll)


def test_berichtigung_eines_nachtraeglich_nichtoeffentlichen_top_nur_mit_noe_recht() -> None:
    w = welt()
    berichtigung = _berichtigung(w)
    assert berichtigung.is_public
    zeilen = protocol_correction_service.internal_rows(_lesestand(w), can_view_np=False)
    assert [zeile["correction"].pk for zeile in zeilen] == [berichtigung.pk]

    # Rücknahme aus der Veröffentlichung: Der TOP wird nichtöffentlich, das Kennzeichen der Berichtigung bleibt
    item = SessionAgendaItem.objects.get(pk=w.top.pk)
    item.is_public = False
    item.save()

    assert protocol_correction_service.internal_rows(_lesestand(w), can_view_np=False) == []
    mit_recht = protocol_correction_service.internal_rows(_lesestand(w), can_view_np=True)
    assert [zeile["correction"].pk for zeile in mit_recht] == [berichtigung.pk]

    seite = client(w.leser).get(f"{base(w)}/meetings/{w.sitzung.pk}/protocol/")
    assert seite.status_code == 200
    assert "Schreibfehler im Beschlusstext" not in seite.content.decode()
    assert "mit Beleuchtung" not in seite.content.decode()


def test_berichtigung_eines_oeffentlichen_top_bleibt_sichtbar() -> None:
    w = welt()
    _berichtigung(w)

    seite = client(w.leser).get(f"{base(w)}/meetings/{w.sitzung.pk}/protocol/")

    assert seite.status_code == 200
    assert "Schreibfehler im Beschlusstext" in seite.content.decode()
