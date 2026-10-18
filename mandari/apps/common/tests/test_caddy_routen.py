# SPDX-License-Identifier: AGPL-3.0-or-later
"""Jeder öffentliche Pfad der obersten URL-Ebene muss im mitgelieferten Caddyfile an Django gehen (Issue #634).

Der ``Caddyfile`` leitet nur ausdrücklich genannte Pfade (Matcher ``@mandari``) an die Anwendung weiter, alles
andere geht an die Website. Fehlt dort ein Pfad aus ``mandari/urls.py``, landet etwa der Rückmeldelink aus der
Ladungsmail (``/ladung/<token>/``) bei der Website statt bei mandari.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import Path

from django.conf import settings
from django.urls import URLPattern, URLResolver, get_resolver
from django.urls.resolvers import RoutePattern

# Pfade, die bewusst nicht über den Matcher @mandari laufen
NICHT_UEBER_CADDY = {
    # Caddy beantwortet /health und /health/ selbst; /health/live/, /health/ready/ und /health/worker/ fragen
    # Kubernetes und Statusseite direkt am Container ab.
    "/health/",
    # Prometheus-Metriken nur intern (METRICS_ALLOWED_NETWORKS / METRICS_TOKEN)
    "/metrics/",
    # robots.txt liefert in der Installation mit Website die Website aus (insight_core/urls.py)
    "/robots.txt",
    # Komponentenvorschau nur in der Entwicklung
    "/dev/",
}


def _caddy_pfade() -> list[str]:
    """Die Pfadmuster des Matchers ``@mandari`` im mitgelieferten Caddyfile."""
    caddyfile = (Path(settings.BASE_DIR).parent / "Caddyfile").read_text(encoding="utf-8")
    block = re.search(r"@mandari \{\n(.*?)\n\s*\}", caddyfile, re.S)
    assert block, "Matcher @mandari nicht im Caddyfile gefunden"
    muster: list[str] = []
    for zeile in block.group(1).splitlines():
        teile = zeile.split("#", 1)[0].split()
        if teile and teile[0] == "path":
            muster.extend(teile[1:])
    return muster


def _beispielpfad(route: str, ist_include: bool) -> str:
    """Ein konkreter Pfad, der zu ``route`` passt (Platzhalter und Regex-Gruppen durch ``x`` ersetzt)."""
    if route.startswith("^"):
        # re_path: nur der feste Anfang bis zur ersten Gruppe zählt
        route = re.split(r"[(\[.*+?$\\]", route[1:], maxsplit=1)[0] + "x"
    else:
        route = re.sub(r"<[^>]+>", "x", route)
    return "/" + route + ("x" if ist_include else "")


def _oberste_ebene(muster: Sequence[URLPattern | URLResolver], praefix: str = "") -> list[str]:
    """Beispielpfade aller Routen der obersten Ebene; ``include`` ohne eigenen Präfix wird aufgelöst."""
    pfade: list[str] = []
    for eintrag in muster:
        route = praefix + str(eintrag.pattern)
        if isinstance(eintrag, URLResolver):
            if route == "":
                pfade.extend(_oberste_ebene(eintrag.url_patterns))
            else:
                pfade.append(_beispielpfad(route, ist_include=True))
        elif isinstance(eintrag, URLPattern):
            assert isinstance(eintrag.pattern, RoutePattern) or route.startswith("^"), route
            pfade.append(_beispielpfad(route, ist_include=False))
    return pfade


def _geht_an_django(pfad: str, muster: list[str]) -> bool:
    return any(fnmatchcase(pfad, m) for m in muster)


def test_alle_pfade_der_obersten_ebene_gehen_an_django() -> None:
    muster = _caddy_pfade()
    fehlend = sorted(
        pfad
        for pfad in set(_oberste_ebene(get_resolver().url_patterns))
        if not any(pfad.startswith(a) for a in NICHT_UEBER_CADDY) and not _geht_an_django(pfad, muster)
    )
    assert not fehlend, f"Im Caddyfile (Matcher @mandari) fehlen: {fehlend}"


def test_rueckmeldelink_zur_ladung_geht_an_django() -> None:
    """Der Link aus der Ladungsmail (Issue #225) erreicht die Anwendung, nicht die Website."""
    assert _geht_an_django("/ladung/abc123/", _caddy_pfade())


def test_pruefung_erkennt_fehlende_pfade() -> None:
    """Gegenprobe: Ein Pfad außerhalb der Matcher gilt als fehlend."""
    assert not _geht_an_django("/gibt-es-nicht/x/", _caddy_pfade())
