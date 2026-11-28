# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Module der Installation und ihre Schalter (Issue #1594, ``docs/adr/20261128-installationsprofile.md``).

mandari hat drei Fachmodule: Session (Sitzungsdienst der Verwaltung), Work (Arbeitsbereich der Fraktionen) und das
Bürgerportal. Jedes lässt sich je Installation abschalten, mit ``MODUL_SESSION``, ``MODUL_WORK`` und
``MODUL_PORTAL`` (``an`` oder ``aus``, Standard ``an``). Ein abgeschaltetes Modul hat keine Adressen (HTTP und
WebSocket), keine Einträge in Menüs und in der Seitenleiste des Admins, keine Zeitpläne, keine Abonnements und keine
Bereiche und Befehle der Schnittstelle. Seine Apps bleiben installiert, Tabellen und Migrationen also auch:
Fremdschlüssel anderer Module zeigen noch auf sie (Stufe 1 der ADR).

Hier steht der Katalog, welcher Code und welche Adressen zu welchem Modul gehören. Er nennt die Module nur als
Zeichenketten und importiert keines (Plattform, ``docs/adr/20260929-schichtenmodell.md``). Geprüft wird er von der
Systemprüfung (``manage.py check``) und je Profil von ``manage.py installationsprofil_pruefen`` in der CI.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Final

from django.conf import settings
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils.module_loading import import_string

SESSION: Final = "session"
WORK: Final = "work"
PORTAL: Final = "portal"

#: Werte der Schalter
AN: Final = frozenset({"an", "true", "1", "yes", "ja"})
AUS: Final = frozenset({"aus", "false", "0", "no", "nein"})

#: WebSocket-Routen der Installation, wie ``ROOT_URLCONF`` über den Namen geladen (``mandari/routing.py``)
WEBSOCKET_ROUTEN: Final = "mandari.routing.websocket_urlpatterns"

#: Letzte Teile eines Modulnamens, die einen Einstieg in ein Modul bilden: Adressen, Ansichten, WebSocket,
#: Zeitpläne, Abonnements, Befehle (auch die der Befehlsschicht, #1588) und Schnittstellen. Ist ein Modul
#: abgeschaltet, lädt sie niemand.
EINSTIEGE: Final = frozenset(
    {"urls", "views", "routing", "consumers", "schedules", "subscribers", "commands", "befehle", "api", "public_api"}
)


@dataclass(frozen=True)
class Modul:
    """Ein Fachmodul mit seinem Schalter und dem Code, der es voraussetzt."""

    kennung: str
    titel: str
    #: Umgebungsvariable des Schalters
    schalter: str
    #: Einstieg nach der Anmeldung und für die installierte App (PWA)
    startseite: str
    #: Adresse für den Probeabruf der CI: antwortet ohne Daten und ohne Anmeldung, solange das Modul eingeschaltet
    #: ist (Seite, JSON oder Weiterleitung zur Anmeldung), sonst mit 404
    probe: str
    #: Code, der nur mit diesem Modul laufen darf: gepunktete Namen von Paketen, Modulen, Klassen oder Funktionen.
    #: Das Modul selbst und Stellen anderer Teile, die es voraussetzen.
    code: tuple[str, ...]
    #: Anfänge der Adressen des Moduls (ohne führenden Schrägstrich); sie verschwinden mit ihm
    pfade: tuple[str, ...] = ()
    #: Code innerhalb von ``code`` oder unter ``pfade``, der ohne das Modul bleibt, weil andere Teile ihn nutzen.
    #: Die Liste soll nur kürzer werden: Jeder Eintrag ist eine Stelle, die noch umziehen muss.
    gemeinsam: tuple[str, ...] = ()
    #: Apps, die ganz zum Modul gehören. Ihre Einstiege (``EINSTIEGE``) lädt ohne das Modul niemand; leer, solange
    #: das Modul keine eigene App hat (Bürgerportal, ``docs/adr/20260929-portal-modul.md``).
    pakete: tuple[str, ...] = ()


#: Alle Fachmodule. Neue Stellen, die ein Modul voraussetzen, gehören in sein ``code``.
MODULE: Final[Mapping[str, Modul]] = {
    SESSION: Modul(
        kennung=SESSION,
        titel="Session",
        schalter="MODUL_SESSION",
        # Session hat keine Seite ohne Mandanten; die Anmeldung leitet in den Mandanten des Kontos weiter
        startseite="/accounts/login/",
        probe="/api/v1/session/openapi.json",
        code=("apps.session",),
        pfade=("session/", "ladung/", "api/v1/session/", ".well-known/oauth-protected-resource/session/"),
        pakete=("apps.session",),
    ),
    WORK: Modul(
        kennung=WORK,
        titel="Work",
        schalter="MODUL_WORK",
        startseite="/work/",
        probe="/work/",
        code=(
            "apps.work",
            # Organisationen unter eigener Subdomain leiten nach /work/<organisation>/ weiter
            "apps.tenants.middleware.SubdomainRedirectMiddleware",
            # Öffentliche Protokolle von Fraktionssitzungen im Bürgerportal lesen Work
            "insight_core.views.protocols",
        ),
        pfade=("work/", "nachweis/", "kalender/feed/", "api/public/v1/"),
        pakete=("apps.work",),
    ),
    PORTAL: Modul(
        kennung=PORTAL,
        titel="Bürgerportal",
        schalter="MODUL_PORTAL",
        startseite="/insight/",
        probe="/insight/",
        code=(
            "insight_core.views",
            "insight_core.portal.PortalHostMiddleware",
            "insight_core.publication.PublicationStateMiddleware",
            # MCP-Server des Bürgerportals: Seine Werkzeuge verlinken dessen Seiten
            "insight_ai.mcp",
            # Meldet geänderte Seiten des Bürgerportals an Suchmaschinen
            "insight_core.services.indexnow",
            "insight_core.schedules.sitemap_aufteilen",
            "insight_core.schedules.rueckmeldungen_aufraeumen",
            "insight_core.schedules.kommunenverzeichnis_abgleichen",
        ),
        pfade=("insight/", "public/", "sitemap-insight-"),
        gemeinsam=(
            # Dokumentvorschau und Kartenkacheln zeigt auch die Recherche in Work; sie gehören zur Drehscheibe
            "insight_core.views.files.file_proxy",
            "insight_core.views.maps.tile_proxy",
            # robots.txt gilt für die ganze Installation; die Sitemap nennt sie nur mit Bürgerportal
            "insight_core.views.sitemap.robots_txt",
            # Bewertungen der KI-Zusammenfassungen für das ERP (Schnittstelle, unter /api/)
            "insight_core.views.ki_bewertung_api",
        ),
    ),
}


def aus_umgebung(umgebung: Mapping[str, str]) -> frozenset[str]:
    """Eingeschaltete Module nach den Schaltern in ``umgebung`` (für ``settings.MANDARI_MODULE``)."""
    aktiv: set[str] = set()
    for modul in MODULE.values():
        wert = (umgebung.get(modul.schalter) or "an").strip().lower()
        if wert in AN:
            aktiv.add(modul.kennung)
        elif wert not in AUS:
            raise ImproperlyConfigured(f"{modul.schalter} muss an oder aus sein, nicht {wert!r}.")
    return frozenset(aktiv)


def aktive() -> frozenset[str]:
    """Kennungen der eingeschalteten Module (``settings.MANDARI_MODULE``, ohne Angabe alle)."""
    return frozenset(getattr(settings, "MANDARI_MODULE", MODULE))


def aktiv(kennung: str) -> bool:
    """Ist das Modul ``kennung`` eingeschaltet?"""
    if kennung not in MODULE:
        raise ImproperlyConfigured(f"Unbekanntes Modul {kennung!r}.")
    return kennung in aktive()


def abgeschaltete() -> list[Modul]:
    """Die abgeschalteten Module."""
    eingeschaltet = aktive()
    return [modul for modul in MODULE.values() if modul.kennung not in eingeschaltet]


def startseite(kennungen: Collection[str] | None = None) -> str:
    """Einstieg der Installation nach der Anmeldung: Work, sonst Session, sonst das Bürgerportal, sonst das Konto."""
    eingeschaltet = aktive() if kennungen is None else kennungen
    for kennung in (WORK, SESSION, PORTAL):
        if kennung in eingeschaltet:
            return MODULE[kennung].startseite
    return "/accounts/login/"


def _trifft(name: str, praefixe: Iterable[str]) -> bool:
    return any(name == praefix or name.startswith(f"{praefix}.") for praefix in praefixe)


def module_fuer(code: str, pfad: str = "") -> frozenset[str]:
    """
    Module, die ``code`` (gepunkteter Name) oder die Adresse ``pfad`` (ohne führenden Schrägstrich) voraussetzen.

    Was in ``gemeinsam`` steht, setzt das Modul nicht voraus, auch wenn es in dessen Code oder Adressen liegt.
    """
    return frozenset(
        modul.kennung
        for modul in MODULE.values()
        if (_trifft(code, modul.code) or (pfad and pfad.startswith(modul.pfade))) and not _trifft(code, modul.gemeinsam)
    )


def abgeschaltet_fuer(code: str, pfad: str = "") -> str | None:
    """Erstes abgeschaltete Modul, das ``code`` oder ``pfad`` voraussetzt; ``None``, wenn beides laufen darf."""
    fehlend = module_fuer(code, pfad) - aktive()
    return min(fehlend) if fehlend else None


def code_von(ziel: Any) -> str:
    """Gepunkteter Name einer Ansicht, eines Consumers, Auftrags oder Handlers."""
    for attribut in ("view_class", "consumer_class", "cls"):
        klasse = getattr(ziel, attribut, None)
        if isinstance(klasse, type):
            return f"{klasse.__module__}.{klasse.__qualname__}"
    modul = getattr(ziel, "__module__", None) or type(ziel).__module__
    name = getattr(ziel, "__qualname__", None) or type(ziel).__qualname__
    return f"{modul}.{name}"


def einstieg_abgeschaltet(app_name: str) -> bool:
    """Gehört die App ganz zu einem abgeschalteten Modul? Dann lädt niemand ihre Einstiege."""
    return any(_trifft(app_name, modul.pakete) for modul in abgeschaltete())


def entdecken(modulname: str) -> None:
    """
    Lädt ``<app>.<modulname>`` aller installierten Apps wie ``autodiscover_modules``, aber nicht für Apps
    abgeschalteter Module. So registrieren sich deren Zeitpläne (``schedules``) und Abonnements (``subscribers``)
    gar nicht erst.
    """
    from django.apps import apps
    from django.utils.module_loading import module_has_submodule

    for app in apps.get_app_configs():
        if einstieg_abgeschaltet(app.name) or not module_has_submodule(app.module, modulname):
            continue
        import_module(f"{app.name}.{modulname}")


# =============================================================================
# Prüfungen: Adressen, WebSocket, Einstiege
# =============================================================================


@dataclass(frozen=True)
class Befund:
    """Eine Stelle, an der ein Profil ein abgeschaltetes Modul erreicht."""

    modul: str
    art: str
    stelle: str

    def __str__(self) -> str:
        return f"{self.art} {self.stelle} setzt das abgeschaltete Modul {MODULE[self.modul].titel} voraus"


def adressen(
    eintraege: Iterable[URLPattern | URLResolver] | None = None, praefix: str = ""
) -> Iterator[tuple[str, str]]:
    """Alle Adressen als ``(Pfad, Code der Ansicht)``; der Django-Admin bleibt außen vor (Stufe 1)."""
    for eintrag in get_resolver().url_patterns if eintraege is None else eintraege:
        if isinstance(eintrag, URLResolver):
            if eintrag.namespace == "admin":
                continue
            yield from adressen(eintrag.url_patterns, praefix + str(eintrag.pattern))
        else:
            yield praefix + str(eintrag.pattern), code_von(eintrag.callback)


def adressen_pruefen(
    eintraege: Iterable[URLPattern | URLResolver] | None = None, *, art: str = "Adresse"
) -> list[Befund]:
    """Adressen, deren Ansicht oder Pfad ein abgeschaltetes Modul voraussetzt."""
    befunde: list[Befund] = []
    for pfad, code in adressen(eintraege):
        modul = abgeschaltet_fuer(code, pfad.lstrip("^"))
        if modul is not None:
            befunde.append(Befund(modul, art, f"/{pfad.lstrip('^')} ({code})"))
    return befunde


def einstiege_geladen(geladen: Iterable[str]) -> list[Befund]:
    """Geladene Einstiege (``EINSTIEGE``) aus Apps abgeschalteter Module, etwa aus ``sys.modules``."""
    befunde: list[Befund] = []
    for name in sorted(geladen):
        if not EINSTIEGE & set(name.split(".")[1:]):
            continue
        for modul in abgeschaltete():
            if _trifft(name, modul.pakete):
                befunde.append(Befund(modul.kennung, "Geladener Einstieg", name))
    return befunde


def registrierte_pruefen(art: str, eintraege: Iterable[tuple[str, str]]) -> list[Befund]:
    """Zeitpläne oder Abonnements ``(Name, Code)``, die ein abgeschaltetes Modul voraussetzen."""
    befunde: list[Befund] = []
    for name, code in eintraege:
        modul = abgeschaltet_fuer(code)
        if modul is not None:
            befunde.append(Befund(modul, art, f"{name} ({code})"))
    return befunde


@checks.register("module")
def module_pruefen(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    """Systemprüfung: Keine Adresse (HTTP oder WebSocket) setzt ein abgeschaltetes Modul voraus."""
    unbekannt = sorted(set(aktive()) - set(MODULE))
    if unbekannt:
        return [checks.Error(f"MANDARI_MODULE nennt unbekannte Module: {unbekannt}", id="module.E001")]
    befunde = adressen_pruefen() + adressen_pruefen(import_string(WEBSOCKET_ROUTEN)(), art="WebSocket")
    return [checks.Error(str(befund), id="module.E002") for befund in befunde]


def nur_mit(kennung: str, muster: Callable[[], list[URLPattern | URLResolver]]) -> list[URLPattern | URLResolver]:
    """Adressen eines Moduls, nur wenn es eingeschaltet ist; ``muster`` importiert dessen Ansichten erst dann."""
    return muster() if aktiv(kennung) else []
