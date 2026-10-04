# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auslieferung hochgeladener Dateien unter ``/media/``.

In Produktion reicht Caddy ``/media/*`` an Django weiter. Zwei Wege führen zur Datei:

- :class:`PublicMediaMiddleware` liefert öffentliche Dateien (Logos, Personenfotos) aus, bevor
  Sitzung und Anmeldung ins Spiel kommen. Eine Seite wie „Frage stellen“ lädt Dutzende Fotos
  zugleich; liefe jedes durch die Sitzungs-Middleware, schriebe es wegen
  ``SESSION_SAVE_EVERY_REQUEST`` die Sitzung in die Datenbank (bei Angemeldeten lüde es zusätzlich
  das Konto) und bräuchte dafür eine Verbindung aus dem Pool. Über ``max_size`` plus
  ``max_waiting`` hinaus scheiterte das sofort, Django meldete es als „session was deleted“ (400,
  Issue #667). Der kurze Weg braucht keine Datenbank und setzt kein Cookie; die Antwort ist für
  alle gleich und öffentlich cachebar.
- :func:`serve_media` (URL ``media``) bedient alles Übrige mit Anmeldeprüfung.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.http import Http404, HttpRequest, HttpResponseBase
from django.urls import Resolver404, resolve
from django.views.static import serve as static_serve

from apps.common.maintenance import wartungsmodus_aktiv
from apps.common.uploads import is_embeddable
from apps.work.files import PROTECTED_PREFIXES as WORK_PROTECTED_PREFIXES

#: Medien, die ohne Anmeldung ausgeliefert werden (Logos, Hero-Bilder, Demo).
PUBLIC_MEDIA_PREFIXES = (
    "bodies/",
    "persons/photos/",
    "organizations/logos/",
    "parties/logos/",
    "session/tenants/logos/",
    "avatars/",
    "demo/",
)

#: Öffentliche Medien mit Inhalts-Hash im Namen: ein Jahr cachebar (Logo-Vorschauen, insight_core/logo_vorschau.py)
IMMUTABLE_MEDIA_RE = re.compile(r"^bodies/logos/vorschau/[^/]+-[0-9a-f]{12}-\d+\.webp$")

#: Medien, die NIE direkt ausgeliefert werden – nur über zugriffsgeprüfte
#: Download-Views (Session-Anlagen, Dokument-Anhänge im Work-Portal).
PROTECTED_MEDIA_PREFIXES = (
    "session/files/",
    "motions/documents/",
    # Archivpakete des Protokolls (Issue #221): nie über eine URL
    "audit_archive/",
    # Dokument-Cache der OParl-Dateien (Standardablage ohne OPARL_FILES_ROOT): nur über den
    # Datei-Proxy, der auch zurückgezogene Dokumente berücksichtigt
    "oparl_files/",
    # Anhänge von Aufgaben, Fraktionssitzungen, Vorbereitung, Support, Briefköpfe und
    # Datenexporte (apps/work/files.py)
    *WORK_PROTECTED_PREFIXES,
)

#: Einstellungen mit eigenen Ablagen, die nie über ``/media/`` hinausgehen. Zeigen sie in ein
#: Verzeichnis unter ``MEDIA_ROOT``, gilt dessen Präfix zusätzlich als geschützt.
_PROTECTED_ROOT_SETTINGS = ("OPARL_FILES_ROOT", "AUDIT_ARCHIVE_ROOT")

#: URL-Präfix der Medien (wie das Muster ``media`` in ``mandari/urls.py``)
MEDIA_URL_PREFIX = "/media/"


def _protected_prefixes() -> tuple[str, ...]:
    """``PROTECTED_MEDIA_PREFIXES`` plus die eingestellten Ablagen, sofern sie unter ``MEDIA_ROOT`` liegen."""
    root = Path(settings.MEDIA_ROOT).resolve()
    extra: list[str] = []
    for name in _PROTECTED_ROOT_SETTINGS:
        value = getattr(settings, name, None)
        if not value:
            continue
        try:
            relative = Path(value).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        if relative != ".":
            extra.append(f"{relative.lower()}/")
    return PROTECTED_MEDIA_PREFIXES + tuple(extra)


def _media_path(path: str) -> str | None:
    """Relativer Medienpfad in eindeutiger Schreibweise – oder ``None``.

    Die Zugriffsregeln unten gelten für genau die Datei, die ausgeliefert würde.
    Darum wird jeder Pfad abgelehnt, den das Dateisystem anders auflösen könnte
    als er geschrieben steht: Punkt-Segmente (``.``/``..``), leere Segmente,
    Backslashes, Steuerzeichen und weitere Prozent-Kodierungen (Django hat den
    Pfad bereits einmal dekodiert). Zusätzlich muss der aufgelöste Pfad unter
    ``MEDIA_ROOT`` liegen und derselbe sein (keine Symlinks hinaus, keine andere
    Groß-/Kleinschreibung auf Dateisystemen, die sie ignorieren).
    """
    from urllib.parse import unquote

    from django.core.exceptions import SuspiciousFileOperation
    from django.utils._os import safe_join

    if not path or "\\" in path or any(ord(zeichen) < 32 for zeichen in path) or unquote(path) != path:
        return None
    if any(segment in ("", ".", "..") for segment in path.split("/")):
        return None
    root = Path(settings.MEDIA_ROOT).resolve()
    try:
        aufgeloest = Path(safe_join(root, path)).resolve()
        relativ = aufgeloest.relative_to(root).as_posix()
    except (SuspiciousFileOperation, ValueError, OSError):
        return None
    if aufgeloest.exists() and relativ != path:
        return None
    return path


def _is_protected(path: str) -> bool:
    return path.lower().startswith(_protected_prefixes())


def _is_public(path: str) -> bool:
    return path.startswith(PUBLIC_MEDIA_PREFIXES)


def _cache_control(path: str) -> str:
    """Vorschaubilder mit Inhalts-Hash im Namen ändern sich nie (ein neues Logo bekommt einen neuen Namen)."""
    if IMMUTABLE_MEDIA_RE.match(path):
        return "public, max-age=31536000, immutable"
    return "public, max-age=3600" if _is_public(path) else "private, no-store"


def _deliver(request: HttpRequest, path: str) -> HttpResponseBase:
    """Die Datei unter dem geprüften Pfad; ``Http404``, wenn es sie nicht gibt."""
    response = static_serve(request, path, document_root=str(settings.MEDIA_ROOT))
    response["Cache-Control"] = _cache_control(path)
    # Zweite Verteidigungslinie zur Upload-Pruefung (Issue #260): Nur Bildformate
    # werden eingebettet ausgeliefert. Alles andere geht als Download hinaus, damit
    # eine Datei nicht im Ursprung der Anwendung zur Anzeige und Ausfuehrung kommt.
    if not is_embeddable(path):
        dateiname = PurePosixPath(path).name.replace('"', "")
        response["Content-Disposition"] = f'attachment; filename="{dateiname}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


def serve_media(request: HttpRequest, path: str) -> HttpResponseBase:
    """Serve uploaded media files (logos, uploads) via Django.

    In Produktion proxied Caddy /media/* an Django. Der frühere
    ``static()``-Helper ist bei DEBUG=False ein No-Op und lieferte
    dort für alle Uploads 404. ``django.views.static.serve`` kümmert
    sich um Last-Modified/304; wir ergänzen einen moderaten Cache-Header.

    Sicherheit (drei Stufen, jeweils auf dem normalisierten Pfad aus
    ``_media_path``):
    - PROTECTED_MEDIA_PREFIXES (und die eingestellten Ablagen unter
      MEDIA_ROOT) werden hier NIE ausgeliefert – sie können nichtöffentlich
      sein und sind nur über die zugriffsgeprüften Download-Views erreichbar
      (Session-Anlagen, Dokument-Anhänge, Datenexporte).
    - PUBLIC_MEDIA_PREFIXES (Logos, Hero-Bilder) sind ohne Anmeldung
      abrufbar. Vorhandene Dateien liefert meist schon
      :class:`PublicMediaMiddleware` aus; hier landen sie nur, wenn sie
      fehlen oder der Wartungsmodus aktiv ist.
    - Alle übrigen Uploads (z. B. Anhänge von Aufgaben, Fraktionssitzungen,
      Support) erfordern mindestens eine Anmeldung; sie sind nicht mehr
      per bloßer URL-Kenntnis für Dritte abrufbar.
    """
    normalized = _media_path(path)
    if normalized is None:
        raise Http404("Datei nicht gefunden.")
    if _is_protected(normalized):
        raise Http404("Diese Datei wird nur über die geschützte Download-View ausgeliefert.")
    if not _is_public(normalized) and not request.user.is_authenticated:
        raise Http404("Datei nicht gefunden.")
    return _deliver(request, normalized)


def public_media_response(request: HttpRequest) -> HttpResponseBase | None:
    """Antwort für eine vorhandene öffentliche Datei – oder ``None`` für den regulären Weg.

    Den regulären Weg (URL-Auflösung, Sitzung, Anmeldung, :func:`serve_media`) nimmt alles, was
    nicht eindeutig eine vorhandene öffentliche Datei ist: andere Methoden als GET/HEAD, unzulässige
    Hosts, verdächtige Pfade, geschützte und anmeldepflichtige Präfixe, fehlende Dateien (404-Seite)
    und der aktive Wartungsmodus (503, Mitarbeitende sehen die Datei).
    """
    if request.method not in ("GET", "HEAD") or not request.path_info.startswith(MEDIA_URL_PREFIX):
        return None
    try:
        request.get_host()
    except DisallowedHost:
        return None
    path = _media_path(request.path_info.removeprefix(MEDIA_URL_PREFIX))
    if path is None or _is_protected(path) or not _is_public(path):
        return None
    if wartungsmodus_aktiv():
        return None
    try:
        match = resolve(request.path_info)
    except Resolver404:
        return None
    if match.url_name != "media":
        return None
    try:
        response = _deliver(request, path)
    except Http404:
        return None
    # Für die Metriken (apps/common/metrics.py): gezählt unter „media“ wie auf dem regulären Weg
    request.resolver_match = match
    return response


class PublicMediaMiddleware:
    """Öffentliche Medien vor Sitzung und Anmeldung ausliefern (Issue #667).

    Steht in ``MIDDLEWARE`` direkt nach WhiteNoise und vor ``SessionMiddleware``: Sicherheits- und
    CSP-Header, Request-ID und Metriken gelten weiter, Sitzung, Anmeldung und Datenbank bleiben
    außen vor.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = public_media_response(request)
        if response is not None:
            return response
        return self.get_response(request)
