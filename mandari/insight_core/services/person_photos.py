# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Personenfotos aus Ratsinformationssystemen lokal zwischenspeichern.

Warum lokal? Hotlinks auf das RIS brechen still (Systemwechsel, Bot-Schutz,
Referrer-Regeln) und erzeugen bei jedem Seitenaufruf 404-Requests im Browser.
Der Abruf läuft serverseitig mit Browser-User-Agent, normalisiert das Bild
(max. 400 px, JPEG) und merkt sich „kein Foto vorhanden“, damit die
Avatar-Komponente sauber auf Initialen zurückfällt.
"""

import io
import logging
import time
from collections import Counter
from datetime import timedelta

from django.core.files.base import ContentFile
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Safari/537.36 mandari-photo-cache/1.0"
)
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


def fetch_person_photo(person, client=None) -> str:
    """
    Foto einer Person abrufen und lokal speichern.

    Rückgabe: "ok", "missing", "error", "skipped" (keine URL/manuell).
    """
    import httpx

    if person.photo_status == "manual":
        return "skipped"
    url = person.photo_url
    if not url:
        return "skipped"

    own_client = client is None
    if own_client:
        client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15.0, follow_redirects=True)
    try:
        try:
            response = client.get(url)
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
    import httpx

    from ..models import OParlPerson

    apply_photo_presets(body)
    results: Counter = Counter()
    if not (body.person_photo_url_template and body.person_photo_id_pattern):
        results["no_config"] += 1
        return results

    persons = OParlPerson.objects.filter(body=body, deleted=False).exclude(photo_status="manual")
    if not force:
        cutoff = timezone.now() - timedelta(days=max_age_days)
        persons = persons.filter(Q(photo_fetched_at__isnull=True) | Q(photo_fetched_at__lt=cutoff))
    persons = persons.order_by("photo_fetched_at", "family_name")[:limit]

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15.0, follow_redirects=True) as client:
        for person in persons:
            results[fetch_person_photo(person, client)] += 1
            if sleep:
                time.sleep(sleep)
    return results
