# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verzeichnis der Kommunen mit ihren Adressen in der OParl-Schnittstelle (Issue #1282).

Wer die Vorlagen einer Kommune abrufen will, braucht die Adresse ihres Body. Die Liste ``/oparl/v1/bodies`` nennt
sie, aber zwischen Wahlperioden und Erweiterungen, und nur mit Kennung statt Namen in der Adresse. Das Verzeichnis
nennt je gelistete Kommune Name, Kurzname, Schlüssel und die Adressen ihrer Listen, in einer Abfrage über wenige
Spalten. Es steht

- in der Übersicht ``/oparl/`` als ``bodyIndex`` (``hub.api.aggregator.root_view``), damit ein Abnehmer mit einem
  Abruf jede Kommune findet, auch ein Sprachmodell, das nur verlinkten Adressen folgen darf, und
- auf der Seite „Offene Daten“ im Bürgerportal (``insight_core.views.offene_daten``), die Suchmaschinen finden.

Es gelten dieselben Kommunen wie für die Stadtseiten (``insight_core.services.indexierung.stadtseiten``): gelistet,
nicht gelöscht, nicht vorübergehend abgeschaltet. Die Adressen kommen aus ``BestandUris`` und sind damit dieselben
wie im Body.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from hub.ris.mapping.bestand import BestandUris, body_page
from insight_core.models import OParlBody
from insight_core.publication import paused_body_ids

#: Listen je Kommune: Feld im Body (OParl 1.1) -> Segment der Adresse
LISTEN: dict[str, str] = {
    "paper": "papers",
    "meeting": "meetings",
    "organization": "organizations",
    "person": "people",
}


@dataclass(frozen=True)
class Eintrag:
    """Eine Kommune im Verzeichnis."""

    body_id: str
    name: str
    display_name: str
    slug: str
    ags: str
    body: str
    listen: dict[str, str]
    web: str

    def als_json(self) -> dict[str, Any]:
        """Eintrag in ``bodyIndex``: die Felder wie im Body, ``displayName`` und ``slug`` als Hilfe zum Finden."""
        data: dict[str, Any] = {
            "name": self.name,
            "displayName": self.display_name,
            "slug": self.slug,
            "ags": self.ags or None,
            "body": self.body,
            **self.listen,
            "web": self.web,
        }
        return {key: value for key, value in data.items() if value}


def _sortierschluessel(name: str) -> str:
    """Alphabetisch nach DIN 5007-1: Umlaute wie ihr Grundbuchstabe (Köln vor Krefeld)."""
    zerlegt = unicodedata.normalize("NFKD", name.casefold())
    return "".join(zeichen for zeichen in zerlegt if not unicodedata.combining(zeichen)).replace("ß", "ss")


def eintraege() -> list[Eintrag]:
    """Alle Kommunen des Verzeichnisses, alphabetisch nach Anzeigename."""
    uris = BestandUris(settings.OPARL_BASE_URL, settings.SITE_URL)
    pausiert = paused_body_ids()
    bodies = OParlBody.objects.listed().only(
        "id", "name", "short_name", "display_name", "slug", "ags", "is_listed", "deleted"
    )
    gefunden = [
        Eintrag(
            body_id=str(body.pk),
            name=body.name,
            display_name=body.get_display_name(),
            slug=body.slug or "",
            ags=(body.ags or "").strip(),
            body=uris.obj("body", body.pk),
            listen={feld: uris.list(body.pk, segment) for feld, segment in LISTEN.items()},
            web=uris.web(body_page(body)),
        )
        for body in bodies
        if str(body.pk) not in pausiert
    ]
    gefunden.sort(key=lambda eintrag: _sortierschluessel(eintrag.display_name))
    return gefunden
