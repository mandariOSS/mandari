"""
Textebene eines PDFs: die erste, leichte Stufe der Texterkennung (Issue #1107).

pypdf liest den Text jeder Seite und die Seitengrößen. Daraus folgt, ob die Textebene reicht oder die Datei die
Erkennung aus dem Bild braucht (Mistral, Tesseract). ``texterkennung.extract_text`` trifft die Entscheidung über
dieselbe Methode ``TextLayer.sufficient``; die Anwendung führt die Stufe als eigenen Auftrag aus
(``file.read_text_layer``, ``texterkennung.extract_text_layer_isolated``).

**Wann reicht die Textebene?**

- Ohne jeden Text in der Textebene nie: Die Datei braucht die Erkennung (wie bisher).
- Mit ``mixed_pages`` (Anwendung, ``TEXT_EXTRACTION_OCR_MIXED_PAGES``) auch dann nicht, wenn einzelne Seiten ein Bild
  tragen, aber kaum Text (weniger als ``MIN_PAGE_CHARS`` Zeichen ohne Leerraum): eingescannte Anlagen hinter einem
  getippten Anschreiben, oft mit einem aufgestempelten Kopf des RIS. Die Erkennung liest dann nur diese Seiten
  (``scan_pages``), der Text der übrigen kommt aus der Textebene. Ohne ``mixed_pages`` (Ingestor) reicht wie bisher
  jeder Text.
- Seiten ohne Bild und ohne Text (Leerseiten, reine Zeichnungen) brauchen keine Erkennung.

Betrachtet werden wie bei Tesseract höchstens ``max_pages`` Seiten; den Text liefert die Textebene für alle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Final

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - pypdf ist Abhängigkeit beider Verbraucher
    PdfReader = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

#: Eine Seite mit Bild und weniger Zeichen (ohne Leerraum) in der Textebene gilt als Scan
MIN_PAGE_CHARS: Final = 20
#: So tief sucht die Bildprüfung in verschachtelten Formularen (Form-XObjects)
_MAX_FORM_DEPTH: Final = 3


@dataclass(frozen=True)
class TextLayer:
    """Textebene eines PDFs: Text je Seite, Seitenzahl und -größen, Seiten, die wie ein Scan aussehen."""

    #: Text je Seite (ohne Leerraum am Rand), in der Reihenfolge der Seiten
    pages: tuple[str, ...] = ()
    page_count: int | None = None
    #: Breite und Höhe in PDF-Punkten je Seite (höchstens ``max_pages``) für die Auflösung der Erkennung
    page_sizes: tuple[tuple[float, float] | None, ...] = ()
    #: Seiten (ab 1, höchstens ``max_pages``) mit Bild und kaum Text
    scan_pages: tuple[int, ...] = ()

    @property
    def text(self) -> str:
        return "\n\n".join(seite for seite in self.pages if seite)

    @property
    def has_text(self) -> bool:
        return any(self.pages)

    def sufficient(self, mixed_pages: bool) -> bool:
        """Reicht die Textebene, oder braucht die Datei die Erkennung aus dem Bild? (siehe Modulbeschreibung)"""
        if not self.has_text:
            return False
        return not (mixed_pages and self.scan_pages)


def read_text_layer(source: Path | bytes, max_pages: int) -> TextLayer:
    """
    Textebene einer Datei (Pfad oder Bytes). Ist sie nicht lesbar (defekte Datei, pypdf fehlt), ergibt das eine
    leere Textebene: Die Datei geht dann wie bisher an die Erkennung aus dem Bild.
    """
    if PdfReader is None:
        return TextLayer()
    try:
        reader = PdfReader(BytesIO(source) if isinstance(source, bytes) else source)
        seiten: list[str] = []
        groessen: list[tuple[float, float] | None] = []
        scans: list[int] = []
        for nummer, page in enumerate(reader.pages, start=1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception:  # noqa: BLE001 - eine defekte Seite verhindert den Rest nicht
                text = ""
            seiten.append(text)
            if nummer <= max_pages:
                groessen.append(_page_size(page))
                if _zeichen(text) < MIN_PAGE_CHARS and _has_image(page):
                    scans.append(nummer)
        return TextLayer(tuple(seiten), len(seiten), tuple(groessen), tuple(scans))
    except Exception as exc:  # noqa: BLE001 - kaputte Textebene: weiter mit der Erkennung aus dem Bild
        logger.warning("pypdf: Textebene nicht lesbar (%s)", type(exc).__name__)
        return TextLayer()


def _zeichen(text: str) -> int:
    return sum(1 for zeichen in text if not zeichen.isspace())


def _page_size(page: Any) -> tuple[float, float] | None:
    """Breite und Höhe in PDF-Punkten (MediaBox wie pdftoppm, mit UserUnit); ``None`` bei Fehlern."""
    try:
        box = page.mediabox
        unit = float(getattr(page, "user_unit", 1) or 1)
        return abs(float(box.width)) * unit, abs(float(box.height)) * unit
    except Exception:  # noqa: BLE001 - ohne Größe skaliert die Texterkennung über die lange Seite
        return None


def _resolve(objekt: Any) -> Any:
    return objekt.get_object() if hasattr(objekt, "get_object") else objekt


def _has_image(page: Any) -> bool:
    """Trägt die Seite ein Bild (auch in einem Formular)? Im Zweifel ja: Dann liest die Erkennung die Seite."""
    try:
        return _contains_image(page.get("/Resources"), 0)
    except Exception:  # noqa: BLE001 - nicht lesbare Ressourcen: lieber einmal zu viel erkennen
        return True


def _contains_image(resources: Any, depth: int) -> bool:
    resources = _resolve(resources)
    if not resources or depth > _MAX_FORM_DEPTH:
        return False
    xobjects = _resolve(resources.get("/XObject"))
    if not xobjects:
        return False
    for name in xobjects:
        objekt = _resolve(xobjects[name])
        art = objekt.get("/Subtype")
        if art == "/Image":
            return True
        if art == "/Form" and _contains_image(objekt.get("/Resources"), depth + 1):
            return True
    return False
