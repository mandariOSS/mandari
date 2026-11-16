# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Feeds und Kalenderdateien des Bürgerportals (Issue #1274): abonnieren ohne Konto.

=====================================  ===========================  ==========================================
Adresse                                Name                         Inhalt
=====================================  ===========================  ==========================================
``/insight/vorgaenge/feed.atom``        ``paper_list_feed``          Vorgangsliste mit ihren Filtern
``/insight/suche/feed.atom``            ``search_feed``              Vorgänge unter den Treffern einer Suche
``/insight/vorgaenge/<id>/feed.atom``   ``paper_feed``               „Neues zu diesem Vorgang“
``/insight/gremien/<id>/feed.atom``     ``organization_feed``        Sitzungen, Beschlüsse, Anträge (Fraktion)
``/insight/beschluesse/feed.atom``      ``decision_feed``            Beschlüsse der Kommune (``?gremium=``)
``/insight/termine/<id>/kalender.ics``  ``meeting_ics``              Kalenderdatei einer Sitzung
``/insight/abonnieren/?feed=…``         ``feed_hilfe``               Anleitung mit Feed-Adresse
=====================================  ===========================  ==========================================

Feed-Reader schicken kein Sitzungs-Cookie. Die Kommune steht deshalb in der Adresse (``?kommune=<uuid>``, wie beim
Kalender-Abo); ohne sie gilt der Portal-Host bzw. die Kommune der Sitzung im Browser. Die Feeds schreiben nichts in
die Sitzung. Eine abgeschaltete oder zurückgenommene Kommune sperrt die Middleware wie ihre Seiten
(``publication.BODY_PAGES``, ``QUERY_BODY_PAGES``, ``DETAIL_PAGES``).

Der Feed der Beschlüsse ist der Anschluss für die Liste der Entscheidungen (Issue #1270): Deren Filter gehören in
``beschluss_punkte`` (``feed_eintraege``) bzw. ``_beschluss_auswahl``, damit Liste und Feed dieselbe Auswahl zeigen.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any, Final
from urllib.parse import parse_qsl, urlsplit

from django.db.models import Count, OuterRef, QuerySet, Subquery
from django.db.models.functions import Coalesce
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_safe

from hub.ris import selectors as ris_selectors
from hub.ris.original import seite_im_ris

from ..models import (
    OParlAgendaItem,
    OParlBody,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    VorgangUrheber,
    withdrawn_q,
)
from ..portal import get_portal
from ..services import feed_eintraege as ereignisse
from ..services import feeds
from ..services.feeds import Feed
from ._withdrawn import withdrawn_response

#: Höchstens so viele Vorgänge je Feed einer Liste oder Suche (jeweils neueste und kürzlich beratene)
VORGAENGE_HOECHSTENS: Final = 50
#: Beratungen in diesem Zeitraum bringen auch ältere Vorgänge einer Auswahl in ihren Feed
BERATEN_SEIT: Final = timedelta(days=90)
#: Beschlüsse so weit zurück (Gremium, Beschlüsse der Kommune)
BESCHLUESSE_SEIT: Final = timedelta(days=365)
#: Kommende Sitzungen so weit voraus (Gremium)
SITZUNGEN_BIS: Final = timedelta(days=400)
#: Längster Wert eines Filters in Schlüssel und Adresse eines Feeds
_FILTER_MAX: Final = 200
#: Filter der Vorgangsliste, die auch für ihren Feed gelten (``papers.vorgaenge_der_auswahl``)
VORGANGSFILTER: Final = ("type", "fraktion", "q")
#: Adressen, die die Anleitung „Abonnieren“ als Feed annimmt
FEED_NAMEN: Final = frozenset({"paper_list_feed", "search_feed", "paper_feed", "organization_feed", "decision_feed"})
_NAMESPACE: Final = "insight_core:insight"


def _basis() -> str:
    from ..seo import get_site_url

    return str(get_site_url()).rstrip("/")


def _rechte(body: OParlBody | None) -> str:
    quelle = f"dem Ratsinformationssystem von {body.get_display_name()}" if body else "den Ratsinformationssystemen"
    return f"Öffentliche Angaben aus {quelle}, aufbereitet von mandari. Maßgeblich sind die Dokumente im Original."


def _kommune_des_feeds(request: HttpRequest) -> OParlBody | None:
    """
    Kommune eines Feeds: Portal (Host bzw. Einstieg im Browser), sonst ``?kommune=<uuid>``, sonst die gewählte Kommune
    der Sitzung, in dieser Reihenfolge wie die Middleware (``publication._active_body_id``). Schreibt nichts.
    """
    portal = get_portal(request)
    if portal is not None:
        return portal.body
    roh = request.GET.get("kommune", "")
    if roh == "alle":
        return None
    if not roh:
        session = getattr(request, "session", None)
        roh = str(session.get("active_body_id") or "") if session is not None else ""
        if roh == "all":
            return None
    try:
        kennung = uuid.UUID(str(roh))
    except ValueError:
        return None
    return OParlBody.objects.filter(pk=kennung, deleted=False).first()


def _liste_der_kommune(body: OParlBody, abfrage: str) -> str:
    """Vorgangsliste der Kommune mit der Auswahl: unter ihrer eigenen Adresse, sonst im gemeinsamen Portal."""
    from ..services.indexierung import liste_der_kommune

    pfad = liste_der_kommune(body, "paper_list") or reverse(f"{_NAMESPACE}:paper_list")
    return _basis() + pfad + (f"?{abfrage}" if abfrage else "")


def _abfrage(werte: Mapping[str, str]) -> str:
    """Abfrage ohne leere Werte, sortiert (gleiche Auswahl, gleiche Adresse und gleicher Schlüssel)."""
    return urlencode(sorted((name, wert) for name, wert in werte.items() if wert))


def vorgangsfilter(get: Mapping[str, Any]) -> dict[str, str]:
    """Filter der Vorgangsliste aus einer Anfrage, bereinigt und begrenzt."""
    return {name: " ".join(str(get.get(name, "") or "").split())[:_FILTER_MAX] for name in VORGANGSFILTER}


# ---------------------------------------------------------------------------
# Verweise auf Feeds (Seitenkopf, Knöpfe)
# ---------------------------------------------------------------------------


def abo(name: str, titel: str, *, kwargs: Mapping[str, Any] | None = None, abfrage: str = "") -> dict[str, str]:
    """
    Verweis auf einen Feed für eine Seite (Kontext ``abo_feed``): absolute Adresse für ``<link rel="alternate">``,
    Titel und die Anleitung „Abonnieren“ mit dieser Adresse.
    """
    pfad = reverse(f"{_NAMESPACE}:{name}", kwargs=dict(kwargs) if kwargs else None)
    if abfrage:
        pfad = f"{pfad}?{abfrage}"
    return {
        "titel": titel,
        "url": _basis() + pfad,
        "hilfe": reverse(f"{_NAMESPACE}:feed_hilfe") + "?" + urlencode({"feed": pfad}),
    }


def abo_vorgangsliste(body: OParlBody, get: Mapping[str, Any]) -> dict[str, str]:
    """Feed der Vorgangsliste mit ihren Filtern (für ``PaperListView``)."""
    filter_ = vorgangsfilter(get)
    return abo(
        "paper_list_feed",
        f"Vorgänge in {body.get_display_name()}" + (" (Auswahl)" if any(filter_.values()) else ""),
        abfrage=_abfrage({"kommune": str(body.pk), **filter_}),
    )


def abo_suche(params: Any, body: OParlBody | None) -> dict[str, str]:
    """Feed einer Suche (für ``SearchView``): Suchbegriff, Zeitraum und Art wie auf der Seite."""
    abfrage = params.url(sort="relevance").lstrip("?")
    kommune = str(body.pk) if body is not None else "alle"
    return abo("search_feed", f"Suche „{params.query}“", abfrage=f"{abfrage}&{urlencode({'kommune': kommune})}")


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------


def _adresse(name: str, abfrage: str = "", **kwargs: Any) -> str:
    """Absolute Adresse eines Feeds mit bereinigter Abfrage (``rel="self"``): unbekannte Parameter fallen weg."""
    pfad = reverse(f"{_NAMESPACE}:{name}", kwargs=kwargs or None)
    return _basis() + pfad + (f"?{abfrage}" if abfrage else "")


def _feed(
    selbst: str,
    kennung: str,
    titel: str,
    untertitel: str,
    seite: str,
    eintraege: list[feeds.Eintrag],
    body: OParlBody | None,
) -> Feed:
    return Feed(
        kennung=kennung,
        titel=titel,
        untertitel=untertitel,
        selbst=selbst,
        seite=seite,
        eintraege=eintraege,
        basis=_basis(),
        rechte=_rechte(body),
    )


def _kuerzlich_beratene(body_ids: list[Any], jetzt: Any) -> QuerySet[Any]:
    """Kennungen der Vorgänge mit einer Beratung in einer Sitzung der letzten ``BERATEN_SEIT``."""
    from ..models import OParlConsultation

    sitzungen = OParlMeeting.objects.filter(
        body_id__in=body_ids, deleted=False, start__gte=jetzt - BERATEN_SEIT, start__lte=jetzt
    ).values("external_id")
    return OParlConsultation.objects.filter(meeting_external_id__in=sitzungen, paper__isnull=False).values("paper_id")


_VORGANG_FELDER: Final = ("id", "body_id", "name", "reference", "paper_type", "date", "oparl_created", "created_at")


def _neueste_und_beratene(auswahl: QuerySet[OParlPaper], body_ids: list[Any]) -> list[OParlPaper]:
    """Neueste Vorgänge der Auswahl und die kürzlich beratenen, je höchstens ``VORGAENGE_HOECHSTENS``."""
    auswahl = auswahl.only(*_VORGANG_FELDER)
    neu = list(auswahl.order_by("-date", "-oparl_created")[:VORGAENGE_HOECHSTENS])
    beraten = list(
        auswahl.filter(pk__in=_kuerzlich_beratene(body_ids, timezone.now()))
        .exclude(pk__in=[paper.pk for paper in neu])
        .order_by("-date", "-oparl_created")[:VORGAENGE_HOECHSTENS]
    )
    return neu + beraten


@require_safe
def paper_list_feed(request: HttpRequest) -> HttpResponse:
    """Feed der Vorgangsliste: neue Vorgänge der Auswahl und Ergebnisse ihrer Beratungen."""
    from .papers import vorgaenge_der_auswahl

    body = _kommune_des_feeds(request)
    if body is None:
        raise Http404("Keine Kommune gewählt")
    filter_ = vorgangsfilter(request.GET)
    abfrage = _abfrage(filter_)

    def bauen() -> Feed:
        papers = _neueste_und_beratene(vorgaenge_der_auswahl(body, filter_), [body.pk])
        name = body.get_display_name()
        return _feed(
            _adresse("paper_list_feed", _abfrage({"kommune": str(body.pk), **filter_})),
            feeds.kennung("feed", "vorgaenge", body.pk) + (f"?{abfrage}" if abfrage else ""),
            f"Vorgänge in {name}" + (" (Auswahl)" if abfrage else ""),
            f"Neue Vorgänge und Ergebnisse ihrer Beratungen in {name}"
            + (f", Auswahl: {', '.join(v for v in filter_.values() if v)}" if abfrage else "")
            + ".",
            _liste_der_kommune(body, abfrage),
            ereignisse.vorgaenge_eintraege(papers, ereignisse.Adressen(_basis()), body_slug=body.slug or ""),
            body,
        )

    return feeds.antwort(request, f"vorgaenge|{body.pk}|{abfrage}", bauen, body_id=body.pk)


def _suchtreffer(params: Any, body: OParlBody | None, body_ids: list[Any]) -> list[OParlPaper]:
    """Vorgänge unter den Treffern, neueste zuerst; ohne Suchdienst die Suche in Titel und Nummer."""
    from ..services.search_presentation import normalize_paper_type

    kennungen: list[str] | None
    try:
        from ..services.search_service import get_search_service

        von, bis = params.date_range(timezone.localdate())
        gruppen = get_search_service().search_grouped(
            params.query,
            body_id=str(body.pk) if body is not None else None,
            body_ids=None if body is not None else body_ids,
            page=1,
            page_size=VORGAENGE_HOECHSTENS,
            date_from=von,
            date_to=bis,
            sort="newest",
            kinds={"paper"},
        )
        kennungen = [str(g["key"]) for g in gruppen["groups"] if g.get("kind") == "paper"]
    except Exception:  # Suchdienst nicht erreichbar: Datenbanksuche wie auf der Suchseite
        kennungen = None
    bereich = OParlPaper.objects.filter(deleted=False).only(*_VORGANG_FELDER)
    bereich = bereich.filter(body=body) if body is not None else bereich.filter(body_id__in=body_ids)
    if kennungen is None:
        papers = list(ris_selectors.search_papers_in(bereich, params.query)[:VORGAENGE_HOECHSTENS])
    else:
        papers = list(bereich.filter(pk__in=kennungen))
    if params.paper_types:
        slug = (body.slug or "") if body is not None else ""
        papers = [p for p in papers if normalize_paper_type(p.paper_type, slug) in params.paper_types]
    return papers


@require_safe
def search_feed(request: HttpRequest) -> HttpResponse:
    """Feed einer Suche: Vorgänge unter den Treffern (neueste) und Ergebnisse ihrer Beratungen."""
    from ..services.search_filters import SearchQuery
    from .search import _searchable_body_ids

    params = SearchQuery.from_get(request.GET, portal=True)
    if len(params.query) < 2:
        raise Http404("Kein Suchbegriff")
    body = _kommune_des_feeds(request)
    abfrage = params.url(sort="relevance").lstrip("?")

    def bauen() -> Feed:
        papers = _suchtreffer(params, body, _searchable_body_ids() if body is None else [])
        wo = f" in {body.get_display_name()}" if body is not None else ""
        adressen = ereignisse.Adressen(_basis())
        kommune = urlencode({"kommune": str(body.pk) if body is not None else "alle"})
        return _feed(
            _adresse("search_feed", f"{abfrage}&{kommune}"),
            feeds.kennung("feed", "suche", body.pk if body is not None else "alle") + f"?{abfrage}",
            f"Suche „{params.query}“{wo}",
            f"Neue Vorgänge zur Suche „{params.query}“{wo} und Ergebnisse ihrer Beratungen.",
            adressen.seite("search", abfrage),
            ereignisse.vorgaenge_eintraege(papers, adressen, body_slug=(body.slug or "") if body else ""),
            body,
        )

    return feeds.antwort(
        request,
        f"suche|{body.pk if body is not None else 'alle'}|{abfrage}",
        bauen,
        body_id=body.pk if body is not None else None,
    )


@require_safe
def paper_feed(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    """„Neues zu diesem Vorgang“: Vorlage, Dokumente, angesetzte Beratungen und Ergebnisse."""
    from ..services import file_reconcile
    from ..services.vorgang_urheber import urheber_je_vorgang

    paper = get_object_or_404(OParlPaper.objects.select_related("body"), pk=pk)
    if paper.withdrawn_by_publisher:
        return withdrawn_response(request, paper)

    def bauen() -> Feed:
        adressen = ereignisse.Adressen(_basis())
        slug = paper.body.slug or ""
        vorlage = ereignisse.vorlage_eintrag(
            paper, adressen, body_slug=slug, urheber=urheber_je_vorgang([paper.pk]).get(paper.pk, [])
        )
        original = seite_im_ris(paper)
        if original:
            vorlage = replace(vorlage, original=original)
        dateien = [
            datei
            for datei in paper.files.defer("text_content", "raw_json")
            if not datei.withdrawn_by_publisher and not file_reconcile.is_blocked(datei)
        ]
        beratungen = ereignisse.beratungen_aus_verlauf(ris_selectors.consultation_history(paper))
        zeichen = ereignisse.kennzeichen(paper, slug)
        return _feed(
            _adresse("paper_feed", pk=paper.pk),
            feeds.kennung("feed", "vorgang", paper.pk),
            f"{zeichen}: {ereignisse.betreff(paper)}" if ereignisse.betreff(paper) else zeichen,
            f"Neues zu diesem Vorgang in {paper.body.get_display_name()}: Dokumente, angesetzte Beratungen und "
            "Ergebnisse.",
            adressen.vorgang(paper.pk),
            [
                vorlage,
                *ereignisse.beratung_eintraege(
                    paper, beratungen, adressen, timezone.now(), body_slug=slug, mit_terminen=True
                ),
                *ereignisse.dokument_eintraege(paper, dateien, adressen),
            ],
            paper.body,
        )

    return feeds.antwort(request, f"vorgang|{paper.pk}", bauen, body_id=paper.body_id)


def kommende_sitzungen(gremium: OParlOrganization, jetzt: Any) -> QuerySet[OParlMeeting]:
    """Kommende Sitzungen eines Gremiums mit Zahl und erstem Bekanntwerden der öffentlichen Punkte."""
    punkte = OParlAgendaItem.objects.filter(meeting=OuterRef("pk"), deleted=False, public=True)
    return (
        OParlMeeting.objects.filter(
            organizations=gremium, deleted=False, start__gte=jetzt, start__lte=jetzt + SITZUNGEN_BIS
        )
        .annotate(
            anzahl_tops=Coalesce(
                Subquery(punkte.order_by().values("meeting").annotate(n=Count("pk")).values("n")[:1]), 0
            ),
            tops_seit=Subquery(punkte.order_by("created_at").values("created_at")[:1]),
        )
        .prefetch_related("organizations")
        .order_by("start")
    )


@require_safe
def organization_feed(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    """Feed eines Gremiums: kommende Sitzungen, Beschlüsse und – bei Fraktionen – ihre Anträge."""
    gremium = get_object_or_404(OParlOrganization.objects.select_related("body"), pk=pk)
    if gremium.withdrawn_by_publisher:
        return withdrawn_response(request, gremium)

    def bauen() -> Feed:
        jetzt = timezone.now()
        adressen = ereignisse.Adressen(_basis())
        body = gremium.body
        sitzungen = list(kommende_sitzungen(gremium, jetzt)[:30])
        punkte = ereignisse.beschluss_punkte(body, jetzt, seit=BESCHLUESSE_SEIT, gremium=gremium)
        antraege = list(
            OParlPaper.objects.filter(
                body=body,
                deleted=False,
                pk__in=VorgangUrheber.objects.filter(body_id=body.pk, organisation_id=gremium.pk).values("paper_id"),
            )
            .only(*_VORGANG_FELDER)
            .order_by("-date", "-oparl_created")[:VORGAENGE_HOECHSTENS]
        )
        return _feed(
            _adresse("organization_feed", pk=gremium.pk),
            feeds.kennung("feed", "gremium", gremium.pk),
            f"{gremium.name or 'Gremium'} – {body.get_display_name()}",
            "Kommende Sitzungen, veröffentlichte Tagesordnungen und Beschlüsse"
            + (" sowie Vorgänge der Fraktion." if antraege else "."),
            adressen.gremium(gremium.pk),
            [
                *ereignisse.sitzung_eintraege(sitzungen, adressen, jetzt),
                *ereignisse.beschluss_eintraege(punkte[: feeds.EINTRAEGE_HOECHSTENS], adressen),
                *ereignisse.vorgaenge_eintraege(antraege, adressen, body_slug=body.slug or ""),
            ],
            body,
        )

    return feeds.antwort(request, f"gremium|{gremium.pk}", bauen, body_id=gremium.body_id)


def _beschluss_auswahl(request: HttpRequest, body: OParlBody) -> tuple[OParlOrganization | None, str]:
    """Filter des Beschluss-Feeds: Gremium der Kommune (``?gremium=<uuid>``) und die bereinigte Abfrage."""
    roh = request.GET.get("gremium", "")
    if not roh:
        return None, ""
    try:
        kennung = uuid.UUID(roh)
    except ValueError:
        raise Http404("Gremium nicht gefunden") from None
    gremium = OParlOrganization.objects.filter(pk=kennung, body=body, deleted=False).first()
    if gremium is None:
        raise Http404("Gremium nicht gefunden")
    return gremium, urlencode({"gremium": str(gremium.pk)})


@require_safe
def decision_feed(request: HttpRequest) -> HttpResponse:
    """Beschlüsse der Kommune (Tagesordnungspunkte mit Ergebnis), auf Wunsch eines Gremiums."""
    body = _kommune_des_feeds(request)
    if body is None:
        raise Http404("Keine Kommune gewählt")
    gremium, abfrage = _beschluss_auswahl(request, body)

    def bauen() -> Feed:
        adressen = ereignisse.Adressen(_basis())
        punkte = ereignisse.beschluss_punkte(body, timezone.now(), seit=BESCHLUESSE_SEIT, gremium=gremium)
        wer = gremium.name if gremium is not None and gremium.name else body.get_display_name()
        return _feed(
            _adresse(
                "decision_feed", _abfrage({"kommune": str(body.pk), "gremium": str(gremium.pk) if gremium else ""})
            ),
            feeds.kennung("feed", "beschluesse", body.pk) + (f"?{abfrage}" if abfrage else ""),
            f"Beschlüsse – {wer}",
            f"Ergebnisse der Beratungen in {wer}, neueste zuerst.",
            adressen.gremium(gremium.pk) if gremium is not None else adressen.seite("decision_list"),
            ereignisse.beschluss_eintraege(punkte[: feeds.EINTRAEGE_HOECHSTENS], adressen),
            body,
        )

    return feeds.antwort(request, f"beschluesse|{body.pk}|{abfrage}", bauen, body_id=body.pk)


# ---------------------------------------------------------------------------
# Kalenderdatei einer Sitzung
# ---------------------------------------------------------------------------


@require_safe
def meeting_ics(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    """
    Kalenderdatei einer Sitzung („Termin speichern“). Dieselbe UID wie im Kalender-Abo (``calendar_feed``): Wer die
    Datei nach einer Änderung erneut öffnet, aktualisiert den Termin, statt einen zweiten anzulegen.
    """
    from apps.common.ical import build_ics_event

    from ..services.sitzung_kalender import hat_tagesordnung, ics_ereignis

    meeting = get_object_or_404(OParlMeeting.objects.prefetch_related("organizations"), pk=pk)
    if meeting.withdrawn_by_publisher:
        return withdrawn_response(request, meeting)
    if meeting.start is None:
        raise Http404("Die Sitzung hat keinen Termin")
    ereignis = ics_ereignis(meeting, _basis(), angekuendigt=not hat_tagesordnung(meeting))
    inhalt = build_ics_event(**ereignis)
    response = HttpResponse(inhalt, content_type="text/calendar; charset=utf-8")
    tag = timezone.localtime(meeting.start).strftime("%Y-%m-%d")
    response["Content-Disposition"] = f'attachment; filename="sitzung-{tag}.ics"'
    response["Cache-Control"] = "public, max-age=900"
    return response


# ---------------------------------------------------------------------------
# Anleitung „Abonnieren“
# ---------------------------------------------------------------------------


def _feed_der_anfrage(roh: str) -> dict[str, Any] | None:
    """
    Feed aus ``?feed=``: nur ein relativer Pfad zu einem Feed des Bürgerportals, sonst ``None``. Titel und Seite
    kommen aus dem Bestand, nie aus der Anfrage.
    """
    teile = urlsplit(roh)
    if teile.scheme or teile.netloc or not teile.path.startswith("/insight/"):
        return None
    try:
        treffer = resolve(teile.path)
    except Resolver404:
        return None
    if treffer.namespace != _NAMESPACE or treffer.url_name not in FEED_NAMEN:
        return None
    werte = dict(parse_qsl(teile.query)[:20])
    abfrage = urlencode(werte)
    pfad = teile.path + (f"?{abfrage}" if abfrage else "")
    titel, seite = _titel_und_seite(str(treffer.url_name), treffer.kwargs, werte)
    if not titel:
        return None
    return {"url": _basis() + pfad, "pfad": pfad, "titel": titel, "seite": seite}


def _veroeffentlicht(body_id: Any) -> bool:
    """Die Kommune zeigt ihre Seiten (nicht abgeschaltet, nicht zurückgenommen; ein Archiv bleibt lesbar)."""
    from .. import publication

    stand = publication.body_state(body_id)
    return stand is None or not (stand.paused or stand.withdrawn)


def _titel_und_seite(name: str, kwargs: Mapping[str, Any], werte: Mapping[str, str]) -> tuple[str, str]:
    """
    Was abonniert wird (Satz für die Anleitung) und die Seite dazu; leer, wenn es das Ziel nicht gibt oder seine
    Kommune es nicht zeigt. Zurückgenommene Einträge (Session, Ö→NÖ) und Einträge abgeschalteter bzw. zurückgenommener
    Kommunen nennt die Anleitung nicht: Ihre Kennung steht in ``?feed=``, die Middleware sieht sie nicht
    (``publication.DETAIL_PAGES``). Titel stammen aus dem Bestand oder sind feste Sätze, nie Text der Anfrage.
    """
    if name == "paper_feed":
        paper = (
            OParlPaper.objects.exclude(withdrawn_q())
            .filter(pk=kwargs["pk"])
            .only("id", "body_id", "name", "reference", "paper_type")
            .first()
        )
        if paper is None or not _veroeffentlicht(paper.body_id):
            return "", ""
        ueber = ereignisse.betreff(paper) or paper.reference or "diesem Vorgang"
        return f"Neues zu „{ueber}“", reverse(f"{_NAMESPACE}:paper_detail", kwargs={"pk": paper.pk})
    if name == "organization_feed":
        gremium = (
            OParlOrganization.objects.exclude(withdrawn_q())
            .filter(pk=kwargs["pk"])
            .only("id", "body_id", "name")
            .first()
        )
        if gremium is None or not _veroeffentlicht(gremium.body_id):
            return "", ""
        return f"Neues aus „{gremium.name or 'Gremium'}“", reverse(
            f"{_NAMESPACE}:organization_detail", kwargs={"pk": gremium.pk}
        )
    if not _veroeffentlicht(werte.get("kommune")):
        return "", ""
    filter_ = {k: v for k, v in werte.items() if k != "kommune"}
    if name == "search_feed":
        return "Neue Treffer dieser Suche", reverse(f"{_NAMESPACE}:search") + "?" + urlencode(filter_)
    if name == "decision_feed":
        return "Neue Beschlüsse", reverse(f"{_NAMESPACE}:decision_list")
    abfrage = _abfrage(vorgangsfilter(filter_))
    return "Neue Vorgänge dieser Auswahl", reverse(f"{_NAMESPACE}:paper_list") + (f"?{abfrage}" if abfrage else "")


@require_safe
def feed_hilfe(request: HttpRequest) -> HttpResponse:
    """Anleitung „Abonnieren“: Feed-Adresse zum Kopieren, drei Schritte, Weg zurück."""
    from ..seo import get_page_seo
    from ..services.indexierung import NOINDEX_FOLLOW

    feed = _feed_der_anfrage(request.GET.get("feed", ""))
    seo = get_page_seo(
        request,
        title="Abonnieren",
        description="Neues aus dem Ratsinformationssystem im Feed-Reader erhalten, ohne Konto und ohne E-Mail-Adresse.",
        robots=NOINDEX_FOLLOW,
    ).to_dict()
    response = render(request, "pages/feed_hilfe.html", {"feed": feed, "seo": seo})
    response["X-Robots-Tag"] = "noindex"
    return response
