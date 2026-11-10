# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Offene Daten“: die OParl-Schnittstelle mit den Adressen jeder Kommune (Issue #1282).

Die Schnittstelle selbst antwortet nur mit JSON, und Suchmaschinen finden JSON kaum. Diese Seite nennt den Einstieg,
zeigt an einem Beispiel, wie man die Vorlagen einer Kommune abruft, und listet je Kommune die Adressen aus
``hub.api.verzeichnis``. Sie ist indexierbar und beschreibt das Angebot zusätzlich als ``DataCatalog`` (schema.org),
damit Suchmaschinen und Sprachmodelle die Adressen auch ohne die Tabelle finden.

Auf einem eigenen Portal-Host einer Kommune nennt sie nur diese Kommune.

Dazu je Kommune eine eigene Seite (``offene_daten_kommune``, Issue #1309): Eine Suche nach „OParl“ und dem Namen der
Kommune findet einen Anker auf der Sammelseite nicht, wohl aber eine Seite, die die Kommune im Titel trägt.
Sprachmodelle rufen meist nur Adressen ab, die als Suchtreffer erschienen sind.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_safe

from hub.api import verzeichnis
from hub.api.aggregator import DOCUMENTATION_URL
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
        "dataset": [_datensatz(eintrag) for eintrag in eintraege],
    }


def _datensatz(eintrag: verzeichnis.Eintrag) -> dict[str, Any]:
    """Eine Kommune als ``Dataset`` (schema.org): Sammelseite und Seite der Kommune beschreiben sie gleich."""
    return {
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


def seite_der_kommune(eintrag: verzeichnis.Eintrag) -> str:
    """Pfad der Seite „Offene Daten“ einer Kommune; ohne Slug die Zeile der Sammelseite."""
    if eintrag.slug:
        return reverse("insight_core:insight:portal_offene_daten", kwargs={"slug": eintrag.slug})
    return f"{reverse('insight_core:insight:offene_daten')}#kommune-{eintrag.body_id}"


@require_safe
def offene_daten(request: HttpRequest) -> HttpResponse:
    eintraege = verzeichnis.eintraege()
    portal = get_portal(request)
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
        "eintraege": [(eintrag, seite_der_kommune(eintrag)) for eintrag in eintraege],
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


def offene_daten_kommune(request: HttpRequest, body: Any) -> HttpResponse:
    """
    Die OParl-Adressen einer Kommune auf eigener Seite (``/insight/k/<slug>/offene-daten/``, Issue #1309). Die Kommune
    hat ``views.portal.portal_offene_daten`` schon geprüft; steht sie nicht im Verzeichnis (pausiert), gibt es die
    Seite nicht.
    """
    eintrag = next((e for e in verzeichnis.eintraege() if e.body_id == str(body.pk)), None)
    if eintrag is None:
        raise Http404("Keine offenen Daten für diese Kommune")
    uris = BestandUris(settings.OPARL_BASE_URL, settings.SITE_URL)
    seo = get_page_seo(
        request,
        title=f"OParl-Schnittstelle {eintrag.display_name}: offene Ratsdaten bei mandari",
        description=(
            f"Ratsinformationen von {eintrag.display_name} als offene Daten nach OParl 1.1 bei mandari: die Adressen "
            "für Vorlagen, Sitzungen, Gremien und Personen, mit Beispiel."
        ),
        keywords=[
            "OParl",
            eintrag.display_name,
            "Offene Daten",
            "Ratsinformationen",
            "Schnittstelle",
            "API",
            "mandari",
        ],
    )
    seo.json_ld = {
        "@context": "https://schema.org",
        **_datensatz(eintrag),
        "inLanguage": "de",
        "publisher": {"@type": "Organization", "name": "mandari", "url": "https://mandari.de"},
        "includedInDataCatalog": {"@type": "DataCatalog", "name": "mandari", "url": uris.system()},
    }
    context = {
        "eintrag": eintrag,
        "system_url": uris.system(),
        "sammelseite_url": reverse("insight_core:insight:offene_daten"),
        "doku_url": DOCUMENTATION_URL,
        "seitengroesse": page_size(),
        "anfragen_je_minute": rate_limit(),
        # Beispiel „seit“: Beginn des Vormonats, damit der Abruf etwas zurückgibt und die Seite sich selten ändert
        "beispiel_seit": (timezone.localdate().replace(day=1) - timedelta(days=1)).replace(day=1).isoformat()
        + "T00:00:00Z",
        "seo": seo.to_dict(),
    }
    return render(request, "pages/offene_daten_kommune.html", context)
