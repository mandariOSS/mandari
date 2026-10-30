# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Was Suchmaschinen im Bürgerportal indexieren sollen (Issue #914).

Suchmaschinen haben keine Sitzung. Indexiert werden sollen die Detailseiten (Vorgänge, Sitzungen, Gremien,
Personen mit laufendem Mandat), die Stadtseiten ``/insight/k/<slug>/`` und deren Listen
``/insight/k/<slug>/vorgaenge/`` usw. (Issue #939, ``liste_der_kommune``): Über deren Seitenlinks erreichen
Suchmaschinen die Detailseiten. Alles, was nur eine andere Sicht auf denselben Bestand ist, bekommt ``noindex``:

- **Suche** (``/insight/suche/``) immer: ``noindex, follow`` im Seitenkopf.
- **Listen** (Vorgänge, Sitzungen, Gremien, Personen, Beschlüsse …), sobald ein Filter-, Seiten- oder
  Sortierparameter gesetzt ist. Ohne Parameter bleibt die Liste, wie sie ist. Ausnahme sind die Blätteransichten der
  Listen je Kommune (``blaetteransicht``): Sie tragen ihre eigene kanonische Adresse und bleiben im Index, sonst
  folgen Suchmaschinen ihren Links auf Dauer nicht mehr.
- **Teilansichten** (HTMX-Ausschnitte, JSON für Karte, Kalender und Kommunenwechsel) sowie **Dateien**
  (PDF-Vorschau, Download, Kalender-Abo, Kartenkacheln) über den Kopf ``X-Robots-Tag: noindex``.
- **Personen ohne laufende Mitgliedschaft**: ``noindex, follow`` und nicht in der Sitemap; die Seite bleibt
  erreichbar.
- **Sitzungen ohne Inhalt** (``sitzung_mit_inhalt``): ``noindex, follow`` und nicht in der Sitemap, bis eine
  Tagesordnung oder Niederschrift vorliegt.

Die Dekoratoren stehen in ``insight_core/urls.py`` an den Adressen, für die sie gelten. Kanonische Adressen
setzt weiter ``insight_core/seo.py``.
"""

from __future__ import annotations

import functools
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from django.db.models import Exists, OuterRef, Q, QuerySet
from django.http import HttpRequest, HttpResponseBase
from django.urls import reverse

#: robots-Wert im Seitenkopf: Seite nicht aufnehmen, ihren Links aber folgen
NOINDEX_FOLLOW = "noindex, follow"
#: Kopf ``X-Robots-Tag`` für Teilansichten und Dateien
X_ROBOTS_NOINDEX = "noindex"
#: Höchstens so viele Stadtseiten verlinkt die Kommunenauswahl. Die Grenze muss über der Zahl der gelisteten Kommunen
#: liegen: Die Liste ist alphabetisch, und was herausfällt, hat keinen starken internen Link mehr (Issue #1145)
STADTSEITEN_HOECHSTENS = 100
#: Eine Sitzung ohne Tagesordnung bleibt so lange vor ihrem Termin im Index: Gesucht wird dann der Termin selbst
SITZUNG_OHNE_INHALT_VORLAUF = timedelta(days=60)
#: Vorgänge ohne ausgelesenen Text stehen nur so lange in der Sitemap (Issue #1145)
VORGANG_OHNE_TEXT_SITEMAP = timedelta(days=2 * 365)
#: Nur diese Parameter ergeben in einer Liste je Kommune eine eigene Seite (``blaetteransicht``)
_BLAETTER_PARAMETER = frozenset({"page", "period"})

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


def blaetteransicht(request: HttpRequest, liste: str | None) -> dict[str, str] | None:
    """
    Parameter einer Blätteransicht der Liste je Kommune für ihre kanonische Adresse, sonst ``None`` (Issue #1145).

    Das sind weitere Seiten (``?page=2``) und die vergangenen Sitzungen (``?period=past``), auch zusammen. Eine leere
    Antwort ist die Liste selbst (``?page=1``). Jeder weitere Parameter filtert oder sortiert, das bleibt ``noindex``.
    """
    namen = {name for name in request.GET if name not in _NEUTRALE_PARAMETER and not name.startswith("utm_")}
    seite = request.GET.get("page", "1")
    if not namen <= _BLAETTER_PARAMETER or not seite.isdecimal() or int(seite) < 1:
        return None
    ansicht: dict[str, str] = {}
    if "period" in namen:
        if liste != "meeting_list" or request.GET["period"] != "past":
            return None
        ansicht["period"] = "past"
    if int(seite) > 1:
        ansicht["page"] = str(int(seite))
    return ansicht


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
        # Nur die Adressen je Kommune haben ein ``slug``; im gemeinsamen Portal bleibt jeder Parameter ``noindex``
        if "slug" in kwargs and blaetteransicht(request, kwargs.get("liste")) is not None:
            return response
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
# Sitzungen und Vorgänge
# ---------------------------------------------------------------------------


def sitzung_mit_inhalt(start: datetime | None, tagesordnungspunkte: int, niederschrift: Any, jetzt: datetime) -> bool:
    """
    Hat die Sitzungsseite etwas zu bieten? Tagesordnung, Niederschrift oder ein Termin in den nächsten Wochen.

    Dieselbe Regel gilt für die Sitemap (``sitzungen_mit_inhalt``). Ohne das steht auf der Seite nur Gremium und Datum.
    """
    if tagesordnungspunkte or niederschrift:
        return True
    return start is not None and jetzt <= start <= jetzt + SITZUNG_OHNE_INHALT_VORLAUF


def sitzungen_mit_inhalt(sitzungen: QuerySet[Any], jetzt: datetime) -> QuerySet[Any]:
    """``sitzung_mit_inhalt`` als Abfrage für die Sitemap: Unterabfrage statt Join, die Zeilen bleiben einzeln."""
    from ..models import OParlAgendaItem

    return sitzungen.filter(
        Exists(OParlAgendaItem.objects.filter(meeting=OuterRef("pk"), deleted=False))
        | Q(raw_json__has_key="resultsProtocol")
        | Q(raw_json__has_key="verbatimProtocol")
        | Q(start__gte=jetzt, start__lte=jetzt + SITZUNG_OHNE_INHALT_VORLAUF)
    )


def vorgaenge_mit_inhalt(vorgaenge: QuerySet[Any], jetzt: datetime) -> QuerySet[Any]:
    """
    Vorgänge für die Sitemap (Issue #1145): mit ausgelesenem Dokumententext oder aus den letzten zwei Jahren.

    Alte Vorgänge ohne Text zeigen nur Titel und Dateinamen. Sie bleiben im Index erlaubt und über die Listen
    erreichbar, sollen aber nicht um das knappe Crawl-Budget konkurrieren. Sobald ihr Text vorliegt, kommen sie dazu.
    """
    from ..models import OParlFile

    return vorgaenge.filter(
        Exists(OParlFile.objects.filter(paper=OuterRef("pk"), deleted=False, text_extraction_status="completed"))
        | Q(date__gte=jetzt - VORGANG_OHNE_TEXT_SITEMAP)
    )


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


def liste_der_kommune(body: Any, list_name: str) -> str:
    """
    Eigene Adresse einer Liste je Kommune (Issue #939): ``/insight/k/<slug>/vorgaenge/`` für ``paper_list``.

    Wie die Stadtseite nur für gelistete Kommunen mit Slug; leer für Listen ohne eigene Adresse
    (``navigation.KOMMUNEN_LISTEN``). Die Adresse ist die kanonische der Liste, auch für die Liste im gemeinsamen
    Portal (``/insight/vorgaenge/`` mit gewählter Kommune).
    """
    from ..navigation import KOMMUNEN_LISTEN

    name = KOMMUNEN_LISTEN.get(list_name)
    if name is None or not stadtseite_url(body):
        return ""
    return reverse(f"insight_core:insight:{name}", kwargs={"slug": body.slug})


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
