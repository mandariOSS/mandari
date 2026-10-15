# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Personenfotos aus Ratsinformationssystemen lokal zwischenspeichern.

Warum lokal? Hotlinks auf das RIS brechen still (Systemwechsel, Bot-Schutz,
Referrer-Regeln) und erzeugen bei jedem Seitenaufruf 404-Requests im Browser.
Der Abruf läuft serverseitig mit unserer Kennung (``mandari-ingestor``, wie alle
automatischen Abrufe, siehe ``mandari_oparl.crawler``) und beachtet die robots.txt.
Braucht ein RIS einen anderen User-Agent, gilt der aus ``sync_config["download_headers"]``
der Quelle (nur nach Absprache mit dem Betreiber). Wie Dokument-Cache und Vorschau ruhen Fotoabrufe, wenn die
Quelle abgeschaltet ist (``is_active``), keine Dateien liefern soll (``sync_config["file_downloads"] = false``)
oder geschont wird (``file_cache.source_paused``, Issue #1027). Das Bild wird normalisiert
(max. 400 px, JPEG); „kein Foto vorhanden“ wird vermerkt, damit die
Avatar-Komponente sauber auf Initialen zurückfällt.
"""

import io
import logging
import time
from collections import Counter
from datetime import timedelta

import httpx
from django.core.files.base import ContentFile
from django.db.models import Q
from django.utils import timezone

from . import file_cache, host_pacing, redirects, robots

logger = logging.getLogger(__name__)

#: Dieselbe Kennung wie alle automatischen Abrufe (kein Browser-User-Agent)
USER_AGENT = robots.USER_AGENT
MAX_BYTES = 5 * 1024 * 1024
MAX_SIZE = (400, 400)

#: Bekannte RIS mit funktionierendem Foto-Muster (greift, wenn eine Kommune
#: noch keine Konfiguration hat). Der Schlüssel wird gegen Quelle/Body-ID
#: geprüft.
PHOTO_PRESETS = [
    {
        "match": "oparl.stadt-muenster.de",
        "template": "https://www.stadt-muenster.de/sessionnet/sessionnetbi/im/pe{id}.jpg",
        "pattern": r"/people/(\d+)$",
    },
]


def apply_photo_presets(body) -> bool:
    """Setzt für bekannte RIS die Foto-Konfiguration, falls sie fehlt."""
    if body.person_photo_url_template and body.person_photo_id_pattern:
        return False
    source_url = (getattr(body.source, "url", "") or "") if body.source_id else ""
    haystack = f"{source_url} {body.external_id or ''}"
    for preset in PHOTO_PRESETS:
        if preset["match"] in haystack:
            body.person_photo_url_template = preset["template"]
            body.person_photo_id_pattern = preset["pattern"]
            body.save(update_fields=["person_photo_url_template", "person_photo_id_pattern"])
            return True
    return False


def _normalize_image(data: bytes) -> bytes:
    """Bild auf max. 400 px verkleinern und als JPEG speichern (Pillow)."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail(MAX_SIZE)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85, optimize=True)
        return out.getvalue()


def _discard_photo(person) -> None:
    """Altes Foto entfernen; Dateisystemfehler (Lock, fehlende Datei) sind unkritisch."""
    if not person.photo:
        return
    try:
        person.photo.delete(save=False)
    except OSError as exc:
        logger.warning("Altes Foto von %s konnte nicht gelöscht werden: %s", person.id, exc)
    person.photo = None


def _mark(person, status: str, error: str = "") -> str:
    person.photo_status = status
    person.photo_error = error[:255]
    person.photo_fetched_at = timezone.now()
    person.save(update_fields=["photo_status", "photo_error", "photo_fetched_at"])
    return status


def _client() -> httpx.Client:
    """Client für Fotoabrufe: Weiterleitungen folgt ``fetch_person_photo`` selbst (``redirects``)."""
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15.0, follow_redirects=False)


def _source_blocked(body) -> str | None:
    """
    Darf die Quelle der Kommune gerade abgerufen werden? ``"disabled"``: Quelle abgeschaltet oder Dateiabruf aus
    (``sync_config["file_downloads"] = false``); ``"paused"``: Schonung nach Fehlversuchen; sonst ``None``.
    Dieselben Regeln wie für Dokument-Cache und Vorschau (``hub.ris.abruf.fetch_allowed``, ``file_cache``).
    """
    source = getattr(body, "source", None) if body is not None else None
    if source is not None and not getattr(source, "is_active", True):
        return "disabled"
    if file_cache.downloads_disabled(body):
        return "disabled"
    if file_cache.source_paused(body):
        return "paused"
    return None


def fetch_person_photo(person, client=None) -> str:
    """
    Foto einer Person abrufen und lokal speichern.

    Rückgabe: "ok", "missing", "error", "skipped" (keine URL/manuell), "deferred" (robots.txt nicht
    erreichbar), "disabled" (Quelle abgeschaltet oder Dateiabruf aus) oder "paused" (Quelle in Schonung). Bei
    den letzten drei wird nichts vermerkt; der nächste Lauf versucht es erneut. Einer Weiterleitung folgen wir nur,
    wenn die robots.txt ihres Ziels sie erlaubt (sonst wie bei einem gesperrten Foto), und nur im Takt je Host.
    """
    if person.photo_status == "manual":
        return "skipped"
    url = person.photo_url
    if not url:
        return "skipped"
    blocked = _source_blocked(getattr(person, "body", None))
    if blocked:
        return blocked
    # robots.txt gilt für jeden automatischen Abruf (RFC 9309), auch für Fotos – geprüft mit dem User-Agent,
    # mit dem das Foto geladen wird
    agent = robots.user_agent_for(person)
    sync_config = robots.sync_config_of(person)
    decision = robots.check(url, robots.KIND_FILES, sync_config=sync_config, agent=agent)
    if decision.unreachable:
        return "deferred"
    if not decision.allowed:
        return _mark(person, "error", decision.reason)
    host_pacing.wait(url, sync_config=sync_config)  # Drossel je Host über alle Prozesse

    own_client = client is None
    if own_client:
        client = _client()
    try:
        try:
            # Weiterleitungen nur nach der robots.txt ihres Ziels und im Takt (Issue #1027)
            request = client.build_request("GET", url, headers={"User-Agent": agent})
            response = redirects.send(client, request, before_redirect=redirects.guard(sync_config=sync_config))
        except redirects.RedirectBlockedError as exc:
            if exc.decision.unreachable:
                return "deferred"
            return _mark(person, "error", exc.decision.reason)
        except httpx.HTTPError as exc:
            return _mark(person, "error", f"{type(exc).__name__}: {exc}")

        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if response.status_code in (404, 410) or (
            response.status_code == 200 and not content_type.startswith("image/")
        ):
            _discard_photo(person)
            return _mark(person, "missing")
        if response.status_code != 200:
            return _mark(person, "error", f"HTTP {response.status_code}")
        if not response.content or len(response.content) > MAX_BYTES:
            return _mark(person, "error", "Leere oder zu große Datei")

        try:
            normalized = _normalize_image(response.content)
        except Exception as exc:  # Pillow kann exotische Formate ablehnen
            return _mark(person, "error", f"Bild nicht lesbar: {exc}")

        _discard_photo(person)
        person.photo.save(f"{person.id}.jpg", ContentFile(normalized), save=False)
        person.save(update_fields=["photo"])
        return _mark(person, "ok")
    finally:
        if own_client:
            client.close()


def fetch_photos_for_body(
    body, *, limit: int = 500, force: bool = False, max_age_days: int = 30, sleep: float = 0.1
) -> Counter:
    """
    Fotos aller Personen einer Kommune (mit Foto-Konfiguration) aktualisieren.
    Ohne ``force`` werden nur ungeprüfte oder ältere Einträge angefasst.
    """
    from ..models import OParlPerson

    apply_photo_presets(body)
    results: Counter = Counter()
    if not (body.person_photo_url_template and body.person_photo_id_pattern):
        results["no_config"] += 1
        return results
    blocked = _source_blocked(body)
    if blocked:
        # Quelle abgeschaltet, ohne Dateiabruf oder in Schonung: keine Anfrage, nichts vermerkt
        results[blocked] += 1
        return results

    persons = OParlPerson.objects.filter(body=body, deleted=False).exclude(photo_status="manual")
    if not force:
        cutoff = timezone.now() - timedelta(days=max_age_days)
        persons = persons.filter(Q(photo_fetched_at__isnull=True) | Q(photo_fetched_at__lt=cutoff))
    persons = persons.order_by("photo_fetched_at", "family_name")[:limit]

    with _client() as client:
        for person in persons:
            results[fetch_person_photo(person, client)] += 1
            if sleep:
                time.sleep(sleep)
    return results
