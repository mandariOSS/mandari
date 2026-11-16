# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Offene Daten“: die OParl-Schnittstelle mit den Adressen jeder Kommune (Issue #1282).

Die Schnittstelle selbst antwortet nur mit JSON, und Suchmaschinen finden JSON kaum. Diese Seite nennt den Einstieg,
zeigt an einem Beispiel, wie man die Vorlagen einer Kommune abruft, und listet je Kommune die Adressen aus
``hub.api.verzeichnis``. Sie ist indexierbar und beschreibt das Angebot zusätzlich als ``DataCatalog`` (schema.org),
damit Suchmaschinen und Sprachmodelle die Adressen auch ohne die Tabelle finden.

Auf einem eigenen Portal-Host einer Kommune nennt sie nur diese Kommune.

Steht die Übersicht auf einer anderen Seite des Auftritts (``INSIGHT_OFFENE_DATEN_URL``, Issue #1309; auf mandari.de
die Website), leitet diese Adresse dauerhaft dorthin weiter.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponsePermanentRedirect
from django.shortcuts import render
from django.views.decorators.http import require_safe

from hub.api import verzeichnis
from hub.api.aggregator import DOCUMENTATION_URL, directory_url
from hub.api.http import rate_limit
from hub.api.serialization import page_size
from hub.ris.mapping.bestand import BestandUris

from ..portal import get_portal
from ..seo import get_page_seo

TITEL = "Offene Daten: OParl-Schnittstelle"
BESCHREIBUNG = (
    "Die Ratsinformationen aller Kommunen im Bürgerportal als offene Daten nach OParl 1.1: "
    "Einstieg, Beispiele und die Adressen je Kommune."
)
#: Beispiel auf der Seite, wenn es die Kommune gibt; sonst die erste der Liste
BEISPIEL_SLUG = "muenster"


def _katalog(eintraege: list[verzeichnis.Eintrag], system: str, seite: str) -> dict[str, Any]:
    """
    Das Angebot als ``DataCatalog`` mit einem ``Dataset`` je Kommune (schema.org, für Suchmaschinen). Die Lizenz
    steht je Datensatz, wie die Kommune sie angibt (``Eintrag.lizenz``); ohne Angabe entfällt sie.
    """
    return {
        "@context": "https://schema.org",
        "@type": "DataCatalog",
        "name": "mandari: Ratsinformationen als offene Daten (OParl 1.1)",
        "description": BESCHREIBUNG,
        "url": seite,
        "inLanguage": "de",
        "publisher": {"@type": "Organization", "name": "mandari", "url": "https://mandari.de"},
        "sameAs": system,
        "dataset": [
            {
                "@type": "Dataset",
                "name": f"Ratsinformationen {eintrag.display_name}",
                "description": (
                    f"Sitzungen, Vorlagen, Gremien und Personen aus dem Ratsinformationssystem von {eintrag.name}, "
                    "maschinenlesbar nach OParl 1.1."
                ),
                "url": eintrag.web,
                "isAccessibleForFree": True,
                **({"license": eintrag.lizenz} if eintrag.lizenz else {}),
                **({"identifier": eintrag.ags} if eintrag.ags else {}),
                "distribution": [
                    {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": eintrag.body},
                    {
                        "@type": "DataDownload",
                        "name": "Vorlagen",
                        "encodingFormat": "application/json",
                        "contentUrl": eintrag.listen["paper"],
                    },
                ],
            }
            for eintrag in eintraege
        ],
    }


@require_safe
def offene_daten(request: HttpRequest) -> HttpResponse:
    portal = get_portal(request)
    if settings.INSIGHT_OFFENE_DATEN_URL:
        # Die Übersicht steht auf einer anderen Seite des Auftritts (Issue #1309); das Portal einer Kommune unter
        # eigener Adresse führt gleich zu deren Seite
        slug = portal.body.slug if portal is not None and portal.via_host and portal.body.slug else ""
        return HttpResponsePermanentRedirect(directory_url(slug))
    eintraege = verzeichnis.eintraege()
    if portal is not None and portal.via_host:
        eintraege = [eintrag for eintrag in eintraege if eintrag.body_id == str(portal.body.pk)]
    uris = BestandUris(settings.OPARL_BASE_URL, settings.SITE_URL)
    beispiel = next((e for e in eintraege if e.slug == BEISPIEL_SLUG), eintraege[0] if eintraege else None)

    seo = get_page_seo(
        request,
        title=TITEL,
        description=BESCHREIBUNG,
        keywords=["OParl", "Offene Daten", "Ratsinformationen", "Schnittstelle", "API"],
    )
    seo.json_ld = _katalog(eintraege, uris.system(), seo.canonical_url)
    context = {
        "eintraege": eintraege,
        "beispiel": beispiel,
        "system_url": uris.system(),
        "bodies_url": uris.bodies(),
        "uebersicht_url": settings.OPARL_BASE_URL.rstrip("/") + "/",
        "doku_url": DOCUMENTATION_URL,
        "seitengroesse": page_size(),
        "anfragen_je_minute": rate_limit(),
        "seo": seo.to_dict(),
    }
    return render(request, "pages/offene_daten.html", context)
