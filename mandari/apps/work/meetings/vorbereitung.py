# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsvorbereitung im neuen Design (#856): Auswahl der Ansicht und feste Werte der Seite.

Die neue Seite (``work/meetings/vorbereitung/seite.html``) erscheint nur, wenn die Organisation das neue
Erscheinungsbild eingeschaltet hat (``apps.work.rahmen.neues_design``, #852). Mit ``?ansicht=bisher`` bleibt die
bisherige Seite erreichbar (dieselben Daten, kein Datenbankfeld). Positionen für Leiste, Blatt und Legende kommen
aus ``AgendaItemPosition.POSITION_CHOICES``. Der Reiter „Aufgaben“ bekommt Rechte, Zahl der sichtbaren Aufgaben
je TOP, die Liste der möglichen Zuständigen und den Endpunkt zum Abhaken (``aufgaben_config``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypedDict

from django.urls import reverse

from apps.common.formatting import member_name
from apps.work.rahmen import neues_design
from apps.work.tasks import selectors as task_selectors

from .models import AgendaItemPosition

if TYPE_CHECKING:
    from apps.tenants.models import Membership, Organization

#: Seite im neuen Design: Tagesordnung, Unterlagen groß, Arbeit der Fraktion, Leiste unten
NEUE_VORLAGE = "work/meetings/vorbereitung/seite.html"
#: Abfrageparameter für die bisherige Ansicht bei eingeschaltetem neuen Design (dieselben Daten)
BISHERIGE_ANSICHT = "bisher"
#: Vier gleichrangige Positionen in der Leiste (in dieser Reihenfolge); alle übrigen stehen unter „Andere …“
HAUPT_POSITIONEN = ("for", "against", "abstain", "open")
#: Farbklasse des Positionspunkts (static/css/work-vorbereitung.css), immer zusammen mit dem Text
POSITIONS_KLASSEN = {"for": "zustimmung", "against": "ablehnung", "abstain": "enthaltung", "open": "offen"}


class Positionen(TypedDict):
    """Positionen der neuen Vorbereitung als (Code, Beschriftung[, Farbklasse])."""

    positionen_haupt: list[tuple[str, str, str]]
    positionen_andere: list[tuple[str, str]]
    positionen_alle: list[tuple[str, str, str]]


def neue_ansicht(organization: Any, query: Mapping[str, Any]) -> bool:
    """Neue Seite nur mit Schalter der Organisation und ohne ausdrücklichen Wunsch nach der bisherigen Ansicht."""
    return neues_design(organization) and query.get("ansicht") != BISHERIGE_ANSICHT


def positionen_fuer_leiste() -> Positionen:
    """Positionen für Leiste, Blatt und Legende der neuen Vorbereitung (Werte aus ``POSITION_CHOICES``)."""
    labels = {code: str(label) for code, label in AgendaItemPosition.POSITION_CHOICES}
    andere = [code for code in labels if code not in HAUPT_POSITIONEN]
    return {
        "positionen_haupt": [(code, labels[code], POSITIONS_KLASSEN[code]) for code in HAUPT_POSITIONEN],
        "positionen_andere": [(code, labels[code]) for code in andere],
        "positionen_alle": [
            (code, labels[code], POSITIONS_KLASSEN.get(code, "andere")) for code in (*HAUPT_POSITIONEN, *andere)
        ],
    }


def aufgaben_config(
    organization: Organization,
    membership: Membership,
    agenda_item_ids: list[Any],
    *,
    darf_sehen: bool,
    darf_anlegen: bool,
) -> dict[str, Any]:
    """
    Reiter „Aufgaben“ der neuen Vorbereitung: Rechte, Zahl der sichtbaren Aufgaben je TOP, mögliche Zuständige.

    Gastzugänge sehen und erstellen keine Aufgaben (wie im Aufgaben-Modul). ``darf_sehen``/``darf_anlegen`` sind die
    Rechte „Aufgaben anzeigen“/„Aufgaben erstellen“ der Mitgliedschaft. ``abhaken`` ist der Endpunkt des
    Aufgabenboards (Aktion ``toggle_complete``, prüft selbst, ob das Mitglied die Aufgabe bearbeiten darf).
    """
    darf_sehen = darf_sehen and not membership.is_guest
    darf_anlegen = darf_anlegen and darf_sehen
    return {
        "darfSehen": darf_sehen,
        "darfAnlegen": darf_anlegen,
        "zahlen": task_selectors.task_counts_for_agenda_items(organization, membership, agenda_item_ids)
        if darf_sehen
        else {},
        "ich": str(membership.id),
        "abhaken": reverse("work:tasks_api", kwargs={"org_slug": organization.slug}) if darf_sehen else "",
        "mitglieder": sorted(
            ({"id": str(m.id), "name": member_name(m)} for m in task_selectors.assignable_members(organization)),
            key=lambda eintrag: eintrag["name"].casefold(),
        )
        if darf_anlegen
        else [],
    }
