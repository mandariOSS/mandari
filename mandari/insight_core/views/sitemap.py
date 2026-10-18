# SPDX-License-Identifier: AGPL-3.0-or-later
"""
robots.txt und Sitemaps des Bürgerportals (mandari Insight).

Aufteilung und Abfragen der Sitemaps: ``insight_core/services/sitemaps.py`` (Issue #914).
"""

import uuid
from datetime import UTC

from django.db.models import Q
from django.http import Http404, HttpResponse, HttpResponsePermanentRedirect
from django.urls import reverse

# GET und HEAD: Suchmaschinen und Prüfwerkzeuge fragen robots.txt und Sitemaps auch per HEAD an (Issue #914)
from django.views.decorators.http import require_safe

from .. import publication
from ..models import OParlBody
from ..services import sitemaps

# =============================================================================
# SEO: robots.txt und Sitemaps
# =============================================================================


def _site_url():
    from django.conf import settings

    return getattr(settings, "SITE_URL", "https://mandari.de")


@require_safe
def robots_txt(request):
    """robots.txt mit Verweis auf den Insight-Sitemap-Index.

    In der Produktions-Topologie beantwortet die Marketing-Site /robots.txt;
    dieser Endpunkt greift im Self-Hosting-Betrieb (Django ist einzige Site).
    """
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin/",
        "Disallow: /accounts/",
        "Disallow: /api/",
        "Disallow: /session/",
        "Disallow: /work/",
        "Disallow: /insight/merkliste/",
        "Disallow: /insight/gespeichert/",
        "",
        f"Sitemap: {_site_url()}/sitemap-insight-index.xml",
        "",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")


@require_safe
def indexnow_schluessel(request, name):
    """``/insight/<schlüssel>.txt``: Schlüsseldatei für IndexNow (``services/indexnow.py``), nur mit ``INDEXNOW_KEY``."""
    from ..services import indexnow

    aktuell = indexnow.schluessel()
    if not aktuell or name != aktuell:
        raise Http404
    response = HttpResponse(aktuell, content_type="text/plain; charset=utf-8")
    response["Cache-Control"] = "public, max-age=86400"
    return response


def _sitemap_key(body):
    """Kennung der Body-Sitemap: Slug, für Kommunen ohne Slug die ID."""
    return body.slug or str(body.id)


def _w3c(zeitpunkt):
    """Zeitpunkt im W3C-Format in UTC; Werte ohne Zeitzone stammen aus der Datenbank und sind schon UTC."""
    if zeitpunkt.tzinfo is None:
        zeitpunkt = zeitpunkt.replace(tzinfo=UTC)
    return zeitpunkt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _xml_antwort(xml_parts):
    response = HttpResponse("\n".join(xml_parts), content_type="application/xml; charset=utf-8")
    # Cache für 24 Stunden
    response["Cache-Control"] = "public, max-age=86400"
    return response


@require_safe
def sitemap_index(request):
    """
    Sitemap-Index: je gelistete Kommune die Grund-Sitemap und die nummerierten Dateien für Vorgänge und Sitzungen.

    Kennung ist der Slug, für Kommunen ohne Slug die ID. ``lastmod`` der Grund-Sitemap ist der letzte Abgleich,
    der nummerierten Dateien die jüngste glaubhafte Änderung ihrer Einträge (Issues #914, #939); ohne glaubhaften
    Zeitpunkt fehlt ``lastmod``.
    """
    site_url = _site_url()
    xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml_parts.append('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')

    def add_sitemap(pfad, lastmod):
        xml_parts.append("  <sitemap>")
        xml_parts.append(f"    <loc>{site_url}{pfad}</loc>")
        if lastmod:
            xml_parts.append(f"    <lastmod>{_w3c(lastmod)}</lastmod>")
        xml_parts.append("  </sitemap>")

    bodies = OParlBody.objects.listed().only("id", "slug", "last_sync").order_by("name")
    for body in bodies:
        kennung = _sitemap_key(body)
        add_sitemap(reverse("insight_core:body_sitemap", kwargs={"body_slug": kennung}), body.last_sync)
        for art in sitemaps.ARTEN.values():
            for datei in sitemaps.dateien(art, body.pk):
                pfad = reverse(
                    "insight_core:body_sitemap_seite",
                    kwargs={"body_slug": kennung, "art": art.kennung, "seite": datei.seite},
                )
                add_sitemap(pfad, datei.lastmod)
    xml_parts.append("</sitemapindex>")
    return _xml_antwort(xml_parts)


def _kommune_der_sitemap(body_slug, umleitung):
    """
    Kommune einer Sitemap – oder die Antwort, die an ihre Stelle tritt.

    Kommunen mit Slug haben nur die Adresse mit Slug, die ID leitet dauerhaft um (``umleitung`` baut das Ziel
    aus dem Slug). Dauerhaft zurückgenommen: 410, vorübergehend abgeschaltet: 503 mit Retry-After (Issue #618).
    """
    body = OParlBody.objects.listed().filter(slug=body_slug).first()
    body_id = None
    if body is None:
        # Kommunen ohne Slug stehen mit ihrer ID im Index; mit Slug gilt nur dessen Adresse
        try:
            body_id = uuid.UUID(body_slug)
        except ValueError:
            body_id = None
        body = OParlBody.objects.listed().filter(pk=body_id).first() if body_id else None
        if body is not None and body.slug:
            return None, HttpResponsePermanentRedirect(umleitung(body.slug))
    if body is None:
        # Dauerhaft zurückgenommen (Issue #618): „nicht mehr verfügbar“ statt „gibt es nicht“
        kennung = (Q(slug=body_slug) | Q(pk=body_id)) if body_id else Q(slug=body_slug)
        gone = OParlBody.objects.filter(kennung).values_list("id", flat=True).first()
        state = publication.body_state(gone)
        if state is not None and state.withdrawn:
            return None, HttpResponse(
                "Sitemap nicht mehr verfügbar", status=410, content_type="text/plain; charset=utf-8"
            )
        raise Http404("Kommune nicht gefunden")

    # Vorübergehend abgeschaltet (Issue #618): wie die Seiten 503 mit Retry-After, Suchmaschinen
    # behalten die Adressen. Archiv: unverändert, der Bestand bleibt lesbar.
    state = publication.body_state(body.pk)
    if state is not None and state.paused:
        response = HttpResponse(
            "Sitemap vorübergehend nicht verfügbar", status=503, content_type="text/plain; charset=utf-8"
        )
        response["Retry-After"] = str(publication.RETRY_AFTER_SECONDS)
        response["Cache-Control"] = "no-store"
        return None, response
    return body, None


class _Urlset:
    """``<urlset>`` einer Sitemap, Eintrag für Eintrag."""

    def __init__(self):
        self.site_url = _site_url()
        self.xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>']
        self.xml_parts.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')

    def add(self, loc, lastmod=None, changefreq="monthly", priority="0.5"):
        # Ein Teil je Eintrag statt je Zeile: Bei 10.000 Einträgen hält die Liste ein Sechstel der Objekte
        zeitpunkt = f"\n    <lastmod>{_w3c(lastmod)}</lastmod>" if lastmod else ""
        self.xml_parts.append(
            f"  <url>\n    <loc>{self.site_url}{loc}</loc>{zeitpunkt}\n"
            f"    <changefreq>{changefreq}</changefreq>\n    <priority>{priority}</priority>\n  </url>"
        )

    def antwort(self):
        self.xml_parts.append("</urlset>")
        return _xml_antwort(self.xml_parts)


@require_safe
def body_sitemap(request, body_slug):
    """
    Grund-Sitemap einer Kommune: Stadtseite, Gremien, Personen mit laufender Mitgliedschaft und Ratsfragen.

    Vorgänge und Sitzungen stehen in eigenen, nummerierten Dateien (``body_sitemap_seite``, Issue #914).
    """
    body, antwort = _kommune_der_sitemap(
        body_slug, lambda slug: reverse("insight_core:body_sitemap", kwargs={"body_slug": slug})
    )
    if antwort is not None:
        return antwort

    urlset = _Urlset()
    if body.slug:
        # Stadtseite der Kommune (/insight/k/<slug>/): Einstieg für „Ratsinformationen <Kommune>“
        stadtseite = reverse("insight_core:insight:portal_entry", kwargs={"slug": body.slug})
        urlset.add(stadtseite, body.last_sync, "daily", "0.9")
    for org_id, lastmod in sitemaps.gremien(body.pk):
        urlset.add(f"/insight/gremien/{org_id}/", lastmod, "monthly", "0.5")
    # Nur Personen mit laufender Mitgliedschaft; die übrigen Personenseiten tragen noindex
    for person_id, lastmod in sitemaps.personen(body.pk):
        urlset.add(f"/insight/personen/{person_id}/", lastmod, "monthly", "0.4")
    # Ratsfragen (öffentlich, mit eigener URL)
    for question_id, lastmod in sitemaps.ratsfragen(body.pk):
        urlset.add(f"/insight/fragen/{question_id}/", lastmod, "weekly", "0.5")
    return urlset.antwort()


@require_safe
def body_sitemap_seite(request, body_slug, art, seite):
    """Nummerierte Sitemap mit den Vorgängen bzw. Sitzungen einer Kommune, je Datei höchstens 10.000 (Issue #914)."""
    sitemap_art = sitemaps.ARTEN.get(art)
    if sitemap_art is None:
        raise Http404("Keine Sitemap unter dieser Adresse")
    nummer = int(seite)
    body, antwort = _kommune_der_sitemap(
        body_slug,
        lambda slug: reverse(
            "insight_core:body_sitemap_seite", kwargs={"body_slug": slug, "art": art, "seite": nummer}
        ),
    )
    if antwort is not None:
        return antwort

    urlset = _Urlset()
    leer = True
    for eintrag_id, lastmod in sitemaps.eintraege(sitemap_art, body.pk, nummer):
        leer = False
        urlset.add(f"{sitemap_art.pfad}{eintrag_id}/", lastmod, sitemap_art.changefreq, sitemap_art.priority)
    if leer:
        # Hinter der letzten Datei gibt es keine weitere; der Index nennt nur Dateien mit Einträgen
        raise Http404("Keine Sitemap unter dieser Adresse")
    return urlset.antwort()
