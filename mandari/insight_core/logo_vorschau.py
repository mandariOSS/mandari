# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorschaubilder der Kommunen-Logos in Anzeigegröße (WebP).

Insight zeigt das Logo einer Kommune mit 27 bis 52 CSS-Pixeln an (Auswahlliste, Seitenleiste,
Kommunenwahl, Startseite der Kommune). Hochgeladen sind die Logos oft mit 800 × 800 Pixeln als JPEG
(Münster 57 KB); jede Insight-Seite lud sie in voller Größe. Neben dem Original entstehen deshalb zwei
WebP-Fassungen für einfache und doppelte Pixeldichte (64 und 128 Pixel Kantenlänge, Seitenverhältnis
bleibt). Der Dateiname trägt den Inhalts-Hash des Originals: Ein neues Logo bekommt neue Namen, die
Fassungen dürfen deshalb ein Jahr im Browser bleiben (``mandari/media.py``).

- Erzeugt werden sie beim Speichern einer Kommune mit neuem oder geändertem Logo
  (:meth:`insight_core.models.OParlBody.save`) und für den Bestand mit
  ``python manage.py build_logo_thumbnails``.
- SVG bleibt SVG (skaliert verlustfrei); dann gibt es keine Fassungen.
- :func:`img_attrs` liefert ``src``/``srcset``/``width``/``height`` für das ``<img>``; ohne passende
  Fassung (noch nicht erzeugt, Bild nicht lesbar) zeigt es auf das Original.
"""

from __future__ import annotations

import hashlib
import logging
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models.fields.files import FieldFile
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe
from django.utils.text import slugify

logger = logging.getLogger(__name__)

#: Kantenlängen in Pixeln: 1x und 2x der größten Anzeige (52 CSS-Pixel auf der Startseite der Kommune)
GROESSEN = (64, 128)
#: Ablage unter ``MEDIA_ROOT``; liegt unter ``bodies/`` und ist damit öffentlich (``mandari/media.py``)
ORDNER = "bodies/logos/vorschau"
#: Erhöhen, wenn sich Größen oder Kodierung ändern: Der Befehl erzeugt dann alle Fassungen neu.
VERSION = 1
#: Größere Originale werden nicht verarbeitet (Schutz vor Speicherbedarf beim Dekodieren)
MAX_BYTES = 20 * 1024 * 1024
WEBP_QUALITAET = 90


def _ist_svg(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() == ".svg"


def _namen(daten: dict[str, Any] | None) -> list[str]:
    if not isinstance(daten, dict):
        return []
    return [str(eintrag["name"]) for eintrag in daten.get("sizes", []) if eintrag.get("name")]


def ist_aktuell(datei: FieldFile | None, daten: dict[str, Any] | None) -> bool:
    """Gehören die gespeicherten Fassungen zum aktuellen Logo (gleicher Dateiname, gleiche Version)?"""
    if not datei:
        return not daten
    return isinstance(daten, dict) and daten.get("source") == datei.name and daten.get("v") == VERSION


def _fassungen_erzeugen(datei: FieldFile) -> dict[str, Any]:
    """Liest das Original und legt die WebP-Fassungen an (vorhandene Dateien gleichen Namens bleiben)."""
    from PIL import Image, ImageOps

    name = datei.name or ""
    ergebnis: dict[str, Any] = {"source": name, "v": VERSION, "sizes": []}
    if _ist_svg(name):
        return ergebnis
    storage = datei.storage
    if storage.size(name) > MAX_BYTES:
        logger.warning("Logo %s ist zu groß für Vorschaubilder", name)
        return ergebnis
    with storage.open(name, "rb") as quelle:
        roh = quelle.read()
    inhalt = hashlib.sha256(roh).hexdigest()[:12]
    stamm = slugify(PurePosixPath(name).stem)[:40].strip("-") or "logo"
    ergebnis["hash"] = inhalt

    with Image.open(BytesIO(roh)) as geoeffnet:
        geoeffnet.seek(0)  # GIF: erstes Bild
        bild = ImageOps.exif_transpose(geoeffnet)
        transparent = bild.mode in ("RGBA", "LA", "PA") or (bild.mode == "P" and "transparency" in bild.info)
        bild = bild.convert("RGBA" if transparent else "RGB")
        bisher: tuple[int, int] | None = None
        for groesse in GROESSEN:
            kopie = bild.copy()
            kopie.thumbnail((groesse, groesse), Image.Resampling.LANCZOS)
            if kopie.size == bisher:
                break  # Original kleiner als die Fassung: keine größere Kopie desselben Bildes
            bisher = kopie.size
            ziel = f"{ORDNER}/{stamm}-{inhalt}-{groesse}.webp"
            if not storage.exists(ziel):
                puffer = BytesIO()
                kopie.save(puffer, "WEBP", quality=WEBP_QUALITAET, method=6)
                ziel = storage.save(ziel, ContentFile(puffer.getvalue()))
            ergebnis["sizes"].append({"name": ziel, "width": kopie.size[0], "height": kopie.size[1]})
    return ergebnis


def abgleichen(
    datei: FieldFile | None, bisher: dict[str, Any] | None, *, erzwingen: bool = False
) -> dict[str, Any] | None:
    """Neuer Wert für ``logo_thumbnails``: erzeugt fehlende Fassungen und räumt die des alten Logos ab.

    Idempotent: Passen die gespeicherten Fassungen zum Logo, bleibt alles, wie es ist. ``erzwingen``
    liest das Original erneut; Dateien mit gleichem Inhalts-Hash werden dabei nicht neu geschrieben.
    Ein nicht lesbares Bild wird protokolliert und ergibt keine Fassungen (Anzeige des Originals).
    """
    if not erzwingen and ist_aktuell(datei, bisher):
        return bisher
    neu: dict[str, Any] | None = None
    if datei:
        try:
            neu = _fassungen_erzeugen(datei)
        except Exception:  # ein defektes Logo darf das Speichern der Kommune nicht verhindern
            logger.warning("Vorschaubilder für Logo %s nicht erzeugt", datei.name, exc_info=True)
            neu = {"source": datei.name, "v": VERSION, "sizes": [], "error": True}
    behalten = set(_namen(neu))
    storage = datei.storage if datei is not None else default_storage
    for name in _namen(bisher):
        if name not in behalten and name.startswith(f"{ORDNER}/"):
            try:
                storage.delete(name)
            except OSError:
                logger.warning("Altes Vorschaubild %s nicht gelöscht", name, exc_info=True)
    return neu


def img_attrs(datei: FieldFile | None, daten: dict[str, Any] | None) -> SafeString:
    """``src``, ``srcset``, ``width`` und ``height`` für das ``<img>`` eines Logos (bereits maskiert).

    Mit Fassungen: ``src`` = 1x, ``srcset`` = 1x/2x, Maße der 1x-Fassung (Seitenverhältnis für das
    Layout, die CSS-Klassen bestimmen die Anzeigegröße). Sonst nur ``src`` auf das Original.
    """
    if not datei:
        return mark_safe("")
    groessen = daten.get("sizes", []) if isinstance(daten, dict) and ist_aktuell(datei, daten) else []
    if not groessen:
        return format_html('src="{}"', datei.url)
    storage = datei.storage
    erste = groessen[0]
    src = storage.url(erste["name"])
    srcset = ", ".join(f"{storage.url(g['name'])} {i}x" for i, g in enumerate(groessen[:2], start=1))
    return format_html('src="{}" srcset="{}" width="{}" height="{}"', src, srcset, erste["width"], erste["height"])
