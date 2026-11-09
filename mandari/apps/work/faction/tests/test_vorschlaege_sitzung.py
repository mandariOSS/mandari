# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Offene TOP-Vorschläge in der laufenden Sitzung und auf der Protokollseite (Issue #1287): anzeigen, aufnehmen und
ablehnen wie vor der Sitzung, nur mit dem Recht zum Genehmigen, nichtöffentliche nur für Vereidigte.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.utils import timezone

from apps.work.faction.models import FactionAgendaItem, FactionMeeting

from . import test_sitzung_vorbereiten as basis
from .test_sitzung_vorbereiten import _aktion, _html, _seite

pytestmark = pytest.mark.django_db

welt = basis.welt
VORSCHLAG = "Radweg am Musterweg"


def _status(welt: dict[str, Any], **felder: Any) -> None:
    FactionMeeting.objects.filter(pk=welt["meeting"].pk).update(**felder)


def test_laufende_sitzung_zeigt_offene_vorschlaege(welt: dict[str, Any], client_for: Any) -> None:
    _status(welt, status="ongoing")
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert 'data-testid="fs-vorschlaege"' in html and VORSCHLAG in html
    assert 'value="accept_proposal"' in html and 'value="reject_proposal"' in html
    # Vor dem Senden werden die Notizen gesichert, die Antwort lädt die Seite am aktuellen TOP neu
    assert "data-sichern-vorher" in html.split('data-testid="fs-vorschlaege"', 1)[1]
    # Ohne Recht zum Genehmigen kein Abschnitt
    assert 'data-testid="fs-vorschlaege"' not in _html(_seite(welt, client_for, "rat"))


def test_aufnehmen_und_ablehnen_in_der_laufenden_sitzung(welt: dict[str, Any], client_for: Any) -> None:
    _status(welt, status="ongoing")
    meeting = welt["meeting"]
    vorschlag = meeting.agenda_items.get(title=VORSCHLAG)
    antwort = client_for(welt["vorsitz"].user).post(
        _aktion(welt), {"action": "accept_proposal", "item_id": str(vorschlag.pk)}, HTTP_HX_REQUEST="true"
    )
    assert antwort.status_code == 200 and antwort["HX-Refresh"] == "true"
    vorschlag.refresh_from_db()
    assert vorschlag.proposal_status == "active" and vorschlag.number == "3"

    zweiter = FactionAgendaItem.objects.create(
        meeting=meeting, title="Parkplätze", visibility="public", proposal_status="proposed", proposed_at=timezone.now()
    )
    client_for(welt["vorsitz"].user).post(
        _aktion(welt),
        {"action": "reject_proposal", "item_id": str(zweiter.pk), "reason": "Nächste Sitzung"},
        HTTP_HX_REQUEST="true",
    )
    zweiter.refresh_from_db()
    assert zweiter.proposal_status == "rejected"
    assert 'data-testid="fs-vorschlaege"' not in _html(_seite(welt, client_for, "vorsitz"))


def test_nichtoeffentlicher_vorschlag_nur_fuer_vereidigte(
    welt: dict[str, Any], client_for: Any, make_member: Any
) -> None:
    _status(welt, status="ongoing")
    FactionAgendaItem.objects.create(
        meeting=welt["meeting"],
        title="Grundstück Flurstück 7",
        visibility="internal",
        proposal_status="proposed",
        proposed_at=timezone.now(),
    )
    welt["genehmigend"] = make_member(welt["org"], ["faction.view_public", "agenda.approve"], email="gina@example.org")
    html = _html(_seite(welt, client_for, "genehmigend"))
    assert VORSCHLAG in html and "Grundstück Flurstück 7" not in html
    assert "Grundstück Flurstück 7" in _html(_seite(welt, client_for, "vorsitz"))


def test_protokollseite_zeigt_vorschlaege_bis_zur_genehmigung(welt: dict[str, Any], client_for: Any) -> None:
    _status(welt, status="completed")
    html = _html(_seite(welt, client_for, "vorsitz"))
    assert 'data-testid="fs-vorschlaege"' in html and VORSCHLAG in html

    _status(welt, protocol_approved=True)
    assert 'data-testid="fs-vorschlaege"' not in _html(_seite(welt, client_for, "vorsitz"))
