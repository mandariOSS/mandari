# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Änderungsprotokoll ohne Inhalte.

Das Protokoll hält fest, wer wann was geändert hat – bei Inhaltsfeldern nur die Tatsache der Änderung: Texte von
Vorlagen, Tagesordnungspunkten, Niederschriften und Anlagen stehen nie im Diff, ebenso Kontaktangaben und Vermerke,
die der Löschlauf später entfernt. Status, Ergebnis und Stimmenzahlen bleiben lesbar.
"""

from __future__ import annotations

import json
from datetime import date, time
from typing import Any

import pytest
from django.utils import timezone

from apps.session import audit
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionAttendanceDisruption,
    SessionAuditLog,
    SessionFile,
    SessionPaper,
    SessionPerson,
    SessionProtocol,
)
from apps.session.services import privacy_service
from apps.session.tests._niederschrift import Welt, base, client, nutzer, welt

pytestmark = pytest.mark.django_db

MARKE = "INHALTSMARKE-7Q"
GEAENDERT = {"alt": audit.CHANGED, "neu": audit.CHANGED}


def _protokollwerte(w: Welt) -> str:
    """Alle gespeicherten Einträge des Mandanten als Text (für Negativprüfungen)."""
    eintraege = SessionAuditLog.objects.filter(tenant=w.tenant).values_list("object_repr", "changes")
    return json.dumps([list(eintrag) for eintrag in eintraege], ensure_ascii=False)


def _letzter(w: Welt, model_name: str) -> SessionAuditLog:
    return SessionAuditLog.objects.filter(tenant=w.tenant, model_name=model_name, action="update").latest("seq")


def test_texte_von_top_und_niederschrift_stehen_nicht_im_diff() -> None:
    w = welt(status="review")
    top = SessionAgendaItem.objects.get(pk=w.top_noe.pk)
    top.resolution_text = f"{MARKE} Beschluss"
    top.protocol_note = f"{MARKE} Aussprache"
    top.vote_result = "rejected"
    top.votes_no = 3
    top.save()
    protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
    protokoll.content = f"{MARKE} allgemeiner Teil"
    protokoll.chair_name = "Viktor Vorsitz"
    protokoll.save()

    eintrag = _letzter(w, "SessionAgendaItem")
    assert eintrag.changes["resolution_text"] == GEAENDERT
    assert eintrag.changes["protocol_note"] == GEAENDERT
    # Ergebnis und Stimmenzahlen bleiben lesbar
    assert eintrag.changes["vote_result"] == {"alt": "approved", "neu": "rejected"}
    assert eintrag.changes["votes_no"] == {"alt": 0, "neu": 3}
    eintrag = _letzter(w, "SessionProtocol")
    assert eintrag.changes["content"] == GEAENDERT
    assert eintrag.changes["chair_name"] == {"alt": "Vera Vorsitz", "neu": "Viktor Vorsitz"}
    werte = _protokollwerte(w)
    assert MARKE not in werte and "GEHEIMBESCHLUSS" not in werte and "GEHEIMNOTIZ" not in werte


def test_texte_von_vorlage_und_anlage_stehen_nicht_im_diff() -> None:
    w = welt(status="review")
    vorlage = SessionPaper.objects.create(
        tenant=w.tenant, name="Grundstück", is_public=False, main_text="ALTTEXT-9Z Sachverhalt"
    )
    vorlage.main_text = f"{MARKE} Sachverhalt"
    vorlage.resolution_text = f"{MARKE} Beschlussvorschlag"
    vorlage.financial_impact_note = f"{MARKE} Kosten"
    vorlage.status = "review"
    vorlage.save()
    anlage = SessionFile.objects.create(
        tenant=w.tenant, paper=vorlage, name="gutachten.pdf", text_content="ALTTEXT-9Z", is_public=False
    )
    anlage.text_content = f"{MARKE} Volltext"
    anlage.is_public = True
    anlage.save()

    eintrag = _letzter(w, "SessionPaper")
    for feld in ("main_text", "resolution_text", "financial_impact_note"):
        assert eintrag.changes[feld] == GEAENDERT
    assert eintrag.changes["status"] == {"alt": "draft", "neu": "review"}
    eintrag = _letzter(w, "SessionFile")
    assert eintrag.changes["text_content"] == GEAENDERT
    assert eintrag.changes["is_public"] == {"alt": False, "neu": True}
    werte = _protokollwerte(w)
    assert MARKE not in werte and "ALTTEXT-9Z" not in werte


def test_kontaktangaben_ueberstehen_den_loeschlauf_nicht_im_diff() -> None:
    w = welt(status="review")
    person = SessionPerson.objects.create(
        tenant=w.tenant,
        given_name="P",
        family_name="Ausgeschieden",
        email="alt-adresse@example.org",
        is_active=False,
        end_date=date(2020, 1, 1),
    )
    # Vor dem Löschlauf geändert: Diese Einträge erreicht der Lauf nicht mehr
    person.email = "neue-adresse@example.org"
    person.contact_consent_evidence = "Schriftliche Erklärung vom 01.05.2019"
    person.family_name = "Ausgeschieden-Neu"
    person.save()
    zeile = SessionAttendance.objects.create(meeting=w.sitzung, person=person, status="present")
    stoerung = SessionAttendanceDisruption.objects.create(
        attendance=zeile, started_at=time(18, 40), note="erster Vermerk"
    )
    stoerung.note = "PERSOENLICHER-VERMERK"
    stoerung.save()
    w.tenant.settings = {"privacy": {"persons_years": 2}}
    w.tenant.save()

    assert privacy_service.run_privacy_purge(w.tenant, now=timezone.now())["persons_anonymized"] == 1

    eintrag = SessionAuditLog.objects.filter(
        tenant=w.tenant, model_name="SessionPerson", action="update", changes__has_key="email"
    ).latest("seq")
    assert eintrag.changes["email"] == GEAENDERT
    assert eintrag.changes["contact_consent_evidence"] == GEAENDERT
    assert eintrag.changes["family_name"] == {"alt": "Ausgeschieden", "neu": "Ausgeschieden-Neu"}
    assert _letzter(w, "SessionAttendanceDisruption").changes["note"] == GEAENDERT
    # Über alle Einträge des Mandanten, nicht nur die seit dem Lauf
    werte = _protokollwerte(w)
    for wert in ("alt-adresse@example.org", "neue-adresse@example.org", "Schriftliche Erklärung", "PERSOENLICHER"):
        assert wert not in werte


def test_kontrollrolle_liest_keine_texte_in_ansicht_und_export() -> None:
    w = welt(status="review")
    top = SessionAgendaItem.objects.get(pk=w.top_noe.pk)
    top.resolution_text = f"{MARKE} Beschluss"
    top.save()
    revision = nutzer(w.tenant, "revision", "view_audit_log", "export_audit_log")

    seite = client(revision).get(f"{base(w)}/audit/")
    export: Any = client(revision).post(f"{base(w)}/audit/export/", {"format": "json"})

    assert seite.status_code == 200 and export.status_code == 200
    inhalt = b"".join(export.streaming_content) if export.streaming else bytes(export.content)
    assert MARKE not in seite.content.decode()
    assert MARKE not in inhalt.decode("utf-8")
    assert "resolution_text" in inhalt.decode("utf-8")
