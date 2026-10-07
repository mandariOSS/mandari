# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Was Suchmaschinen im Bürgerportal indexieren sollen (Issue #914).

Suchmaschinen haben keine Sitzung. Indexiert werden sollen die Detailseiten (Vorgänge, Sitzungen, Gremien,
Personen mit laufendem Mandat) und die Stadtseiten ``/insight/k/<slug>/``; alles, was nur eine andere Sicht
auf denselben Bestand ist, bekommt ``noindex``:

- **Suche** (``/insight/suche/``) immer: ``noindex, follow`` im Seitenkopf.
- **Listen** (Vorgänge, Sitzungen, Gremien, Personen, Beschlüsse …), sobald ein Filter-, Seiten- oder
  Sortierparameter gesetzt ist. Ohne Parameter bleibt die Liste, wie sie ist.
- **Teilansichten** (HTMX-Ausschnitte, JSON für Karte, Kalender und Kommunenwechsel) sowie **Dateien**
  (PDF-Vorschau, Download, Kalender-Abo, Kartenkacheln) über den Kopf ``X-Robots-Tag: noindex``.
- **Personen ohne laufende Mitgliedschaft**: ``noindex, follow`` und nicht in der Sitemap; die Seite bleibt
  erreichbar.

Die Dekoratoren stehen in ``insight_core/urls.py`` an den Adressen, für die sie gelten. Kanonische Adressen
setzt weiter ``insight_core/seo.py``.
"""

from __future__ import annotations

import functools
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db.models import Exists, OuterRef, Q, QuerySet
from django.http import HttpRequest, HttpResponseBase
from django.urls import reverse

#: robots-Wert im Seitenkopf: Seite nicht aufnehmen, ihren Links aber folgen
NOINDEX_FOLLOW = "noindex, follow"
#: Kopf ``X-Robots-Tag`` für Teilansichten und Dateien
X_ROBOTS_NOINDEX = "noindex"
#: Höchstens so viele Stadtseiten verlinkt die Kommunenauswahl (keine langen Listen)
STADTSEITEN_HOECHSTENS = 12

#: Parameter, die keine eigene Ansicht ergeben (Kampagnen- und Klick-Kennungen); alle übrigen filtern,
#: blättern oder sortieren
_NEUTRALE_PARAMETER = frozenset({"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid"})

View = Callable[..., HttpResponseBase]


# ---------------------------------------------------------------------------
# Anfragen
# ---------------------------------------------------------------------------


def hat_ansichtsparameter(request: HttpRequest) -> bool:
    """Filter-, Seiten- oder Sortierparameter gesetzt? Kampagnen-Kennungen (``utm_*`` u. a.) zählen nicht."""
    return any(name not in _NEUTRALE_PARAMETER and not name.startswith("utm_") for name in request.GET)


def ist_teilansicht(request: HttpRequest) -> bool:
    """Anfrage von htmx: Die Antwort ist ein Ausschnitt, keine ganze Seite."""
    return request.headers.get("HX-Request") == "true"


def ohne_index_kopf(response: HttpResponseBase) -> HttpResponseBase:
    """``X-Robots-Tag: noindex`` setzen; ein strengerer Wert der View bleibt stehen."""
    if "X-Robots-Tag" not in response:
        response["X-Robots-Tag"] = X_ROBOTS_NOINDEX
    return response


def _robots_im_kopf(response: HttpResponseBase, wert: str) -> bool:
    """robots-Wert der noch nicht gerenderten Seite (``TemplateResponse``) setzen; ``False``, wenn das nicht geht."""
    kontext = getattr(response, "context_data", None)
    if not isinstance(kontext, dict) or getattr(response, "is_rendered", True):
        return False
    seo = kontext.get("seo")
    # Eigene Kopie: Der SEO-Kontext darf nicht in andere Antworten durchschlagen
    kontext["seo"] = {**seo, "robots": wert} if isinstance(seo, dict) else {"robots": wert}
    return True


# ---------------------------------------------------------------------------
# Dekoratoren für die Adressen (insight_core/urls.py)
# ---------------------------------------------------------------------------


def ohne_index(view: View) -> View:
    """Teilansichten und Dateien: jede Antwort mit ``X-Robots-Tag: noindex``."""

    @functools.wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        return ohne_index_kopf(view(request, *args, **kwargs))

    return wrapper


def listenseite(view: View) -> View:
    """Liste: mit Filter-, Seiten- oder Sortierparameter ``noindex, follow``; htmx-Ausschnitte per Kopf."""

    @functools.wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        response = view(request, *args, **kwargs)
        if ist_teilansicht(request):
            return ohne_index_kopf(response)
        if hat_ansichtsparameter(request) and not _robots_im_kopf(response, NOINDEX_FOLLOW):
            # Schon fertige Antwort (z. B. Weiterleitung zur Auswahl der Kommune): dann über den Kopf
            ohne_index_kopf(response)
        return response

    return wrapper


def suchseite(view: View) -> View:
    """Suche: nie im Index, ihren Treffern dürfen Suchmaschinen folgen."""

    @functools.wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        response = view(request, *args, **kwargs)
        _robots_im_kopf(response, NOINDEX_FOLLOW)
        return ohne_index_kopf(response)

    return wrapper


# ---------------------------------------------------------------------------
# Personen
# ---------------------------------------------------------------------------


def laufend_q(stichtag: date, prefix: str = "") -> Q:
    """Mitgliedschaft läuft am Stichtag: ohne Ende oder mit Ende am bzw. nach dem Stichtag."""
    feld = f"{prefix}__" if prefix else ""
    return Q(**{f"{feld}end_date__isnull": True}) | Q(**{f"{feld}end_date__gte": stichtag})


def mit_laufender_mitgliedschaft(personen: QuerySet[Any], stichtag: date) -> QuerySet[Any]:
    """
    Nur Personen mit mindestens einer laufenden Mitgliedschaft – eine Unterabfrage (``EXISTS``), kein N+1.

    Gezählt werden dieselben Mitgliedschaften wie auf der Personenseite (``active_memberships``): nicht
    gelöscht, in einem nicht gelöschten Gremium, ohne Ende oder mit Ende ab dem Stichtag.
    """
    from ..models import OParlMembership

    laufende = OParlMembership.objects.filter(
        laufend_q(stichtag), person=OuterRef("pk"), deleted=False, organization__deleted=False
    )
    return personen.filter(Exists(laufende))


def robots_fuer_person(seo: dict[str, Any], laufende_mitgliedschaften: Any) -> dict[str, Any]:
    """Personenseite ohne laufende Mitgliedschaft: ``noindex, follow`` (die Seite bleibt erreichbar)."""
    if not laufende_mitgliedschaften:
        seo["robots"] = NOINDEX_FOLLOW
    return seo


# ---------------------------------------------------------------------------
# Stadtseiten
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stadtseite:
    name: str
    url: str


def stadtseite_url(body: Any) -> str:
    """Stadtseite ``/insight/k/<slug>/`` einer gelisteten Kommune mit Slug, sonst leer (dort gibt es keine)."""
    if body is None or not body.slug or not body.is_listed or body.deleted:
        return ""
    return reverse("insight_core:insight:portal_entry", kwargs={"slug": body.slug})


def _sortierschluessel(name: str) -> str:
    """Alphabetisch nach DIN 5007-1: Umlaute wie ihr Grundbuchstabe (Köln vor Krefeld, Münster vor Mülheim)."""
    zerlegt = unicodedata.normalize("NFKD", name.casefold())
    return "".join(zeichen for zeichen in zerlegt if not unicodedata.combining(zeichen)).replace("ß", "ss")


def stadtseiten(hoechstens: int = STADTSEITEN_HOECHSTENS) -> list[Stadtseite]:
    """
    Direkte Links auf die Stadtseiten ``/insight/k/<slug>/`` für die Kommunenauswahl (Issue #914).

    Gelistete Kommunen mit Slug, alphabetisch, höchstens ``hoechstens``; vorübergehend abgeschaltete Kommunen
    (Issue #618) fehlen. Eine Abfrage über wenige Spalten.
    """
    from ..models import OParlBody
    from ..publication import paused_body_ids

    pausiert = paused_body_ids()
    kommunen = [
        body
        for body in OParlBody.objects.listed()
        .exclude(slug__isnull=True)
        .exclude(slug="")
        .only("id", "slug", "name", "short_name", "display_name", "is_listed", "deleted")
        if str(body.pk) not in pausiert
    ]
    kommunen.sort(key=lambda body: _sortierschluessel(body.get_display_name()))
    return [Stadtseite(name=body.get_display_name(), url=stadtseite_url(body)) for body in kommunen[:hoechstens]]
