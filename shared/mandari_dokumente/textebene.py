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

Betrachtet werden wie bei Tesseract höchstens ``max_pages`` Seiten; den Text liefert die Textebene für alle, höchstens
aber ``MAX_TEXT_PAGES`` Seiten mit zusammen ``MAX_TEXT_CHARS`` Zeichen (``TextLayer.truncated``).

**Speicher und Zeit** begrenzt ein eigener Prozess (``texterkennung.extract_text_layer_isolated``): pypdf hält die
entpackten Inhalte aller gelesenen Seiten, solange der Leser lebt. ``MemoryError`` geht deshalb an den Aufrufer, statt
als Teilergebnis zu enden.
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
#: Höchstens so viele Einträge prüft die Bildsuche je Seite; darüber gilt: im Zweifel ja
_MAX_IMAGE_CHECKS: Final = 5000
#: Text liefern höchstens so viele Seiten; die Seitenzahl bleibt die der Datei
MAX_TEXT_PAGES: Final = 3000
#: Höchstens so viele Zeichen liest die Textebene insgesamt (rund 1500 dicht beschriebene Seiten). Hält auch die
#: Ausgabe des eigenen Prozesses klein, die der Aufrufer in seinen Speicher liest.
MAX_TEXT_CHARS: Final = 5_000_000
#: Größte plausible Seitenzahl (wie die Grenze des Seitenbaums in pypdf); darüber gilt sie als unbekannt
MAX_PAGE_COUNT: Final = 100_000


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
    #: An der Grenze für Seiten oder Zeichen gekürzt: ``pages`` enthält nicht den ganzen Text der Datei
    truncated: bool = False

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


def read_text_layer(
    source: Path | bytes,
    max_pages: int,
    *,
    max_text_pages: int = MAX_TEXT_PAGES,
    max_chars: int = MAX_TEXT_CHARS,
) -> TextLayer:
    """
    Textebene einer Datei (Pfad oder Bytes). Ist sie nicht lesbar (defekte Datei, pypdf fehlt), ergibt das eine
    leere Textebene: Die Datei geht dann wie bisher an die Erkennung aus dem Bild.

    Text liefern höchstens ``max_text_pages`` Seiten mit zusammen höchstens ``max_chars`` Zeichen; danach endet das
    Lesen (``truncated``), ``page_count`` bleibt die Seitenzahl der Datei, sofern sie plausibel ist (``_seitenzahl``).
    ``MemoryError`` geht an den Aufrufer: An einer Speichergrenze gibt es kein Teilergebnis, das wie eine vollständige
    Textebene aussähe.
    """
    if PdfReader is None:
        return TextLayer()
    try:
        reader = PdfReader(BytesIO(source) if isinstance(source, bytes) else source)
        seiten: list[str] = []
        groessen: list[tuple[float, float] | None] = []
        scans: list[int] = []
        rest = max_chars
        gekuerzt = False
        for nummer, page in enumerate(reader.pages, start=1):
            if nummer > max_text_pages or rest <= 0:
                gekuerzt = True
                break
            text = _page_text(page)
            if len(text) > rest:
                text, gekuerzt = text[:rest], True
            rest -= len(text)
            seiten.append(text)
            if nummer <= max_pages:
                groessen.append(_page_size(page))
                if _zeichen(text) < MIN_PAGE_CHARS and _has_image(page):
                    scans.append(nummer)
        seitenzahl = _seitenzahl(reader, len(seiten)) if gekuerzt else len(seiten)
        if gekuerzt:
            logger.warning(
                "pypdf: Textebene gekürzt nach %d von %s Seiten (Grenzen %d Seiten, %d Zeichen)",
                len(seiten),
                seitenzahl if seitenzahl is not None else "?",
                max_text_pages,
                max_chars,
            )
        return TextLayer(tuple(seiten), seitenzahl, tuple(groessen), tuple(scans), gekuerzt)
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001 - kaputte Textebene: weiter mit der Erkennung aus dem Bild
        logger.warning("pypdf: Textebene nicht lesbar (%s)", type(exc).__name__)
        return TextLayer()


def plausible_page_count(wert: object) -> int | None:
    """Seitenzahl, wenn sie eine ganze Zahl von 0 bis ``MAX_PAGE_COUNT`` ist; sonst ``None`` (unbekannt)."""
    if isinstance(wert, bool) or not isinstance(wert, int):
        return None
    return wert if 0 <= wert <= MAX_PAGE_COUNT else None


def _seitenzahl(reader: Any, gelesen: int) -> int | None:
    """
    Seitenzahl einer gekürzten Textebene. pypdf übernimmt sie teils ungeprüft aus ``/Count`` (verschlüsselte Dateien);
    weniger als die gelesenen Seiten oder mehr als ``MAX_PAGE_COUNT`` gelten deshalb als unbekannt.
    """
    try:
        zahl = len(reader.pages)
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - ohne Seitenzahl erkennt Tesseract bis zur ersten nicht renderbaren Seite
        return None
    gesamt = plausible_page_count(zahl)
    return gesamt if gesamt is not None and gesamt >= gelesen else None


def _page_text(page: Any) -> str:
    """Text einer Seite ohne Leerraum am Rand; leer, wenn die Seite defekt ist (``MemoryError`` geht weiter)."""
    try:
        return (page.extract_text() or "").strip()
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - eine defekte Seite verhindert den Rest nicht
        return ""


def _zeichen(text: str) -> int:
    return sum(1 for zeichen in text if not zeichen.isspace())


def _page_size(page: Any) -> tuple[float, float] | None:
    """Breite und Höhe in PDF-Punkten (MediaBox wie pdftoppm, mit UserUnit); ``None`` bei Fehlern."""
    try:
        box = page.mediabox
        unit = float(getattr(page, "user_unit", 1) or 1)
        return abs(float(box.width)) * unit, abs(float(box.height)) * unit
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - ohne Größe skaliert die Texterkennung über die lange Seite
        return None


def _resolve(objekt: Any) -> Any:
    return objekt.get_object() if hasattr(objekt, "get_object") else objekt


def _has_image(page: Any) -> bool:
    """Trägt die Seite ein Bild (auch in einem Formular)? Im Zweifel ja: Dann liest die Erkennung die Seite."""
    try:
        return _contains_image(page.get("/Resources"), 0, set(), [_MAX_IMAGE_CHECKS])
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - nicht lesbare Ressourcen: lieber einmal zu viel erkennen
        return True


def _contains_image(resources: Any, depth: int, geprueft: set[tuple[int, int]], budget: list[int]) -> bool:
    """
    Bildsuche in den Ressourcen einer Seite und ihrer Formulare.

    ``geprueft`` merkt sich je Seite, welches Verzeichnis in welcher Tiefe schon ohne Fund durchsucht ist:
    Verweisen viele Einträge auf dasselbe Formular, wüchse die Zahl der Besuche sonst mit jeder Ebene um diesen
    Faktor. ``budget`` begrenzt die geprüften Einträge je Seite; ist es aufgebraucht, gilt wie bei nicht lesbaren
    Ressourcen: im Zweifel ja.
    """
    resources = _resolve(resources)
    if not resources or depth > _MAX_FORM_DEPTH:
        return False
    schluessel = (id(resources), depth)
    if schluessel in geprueft:
        return False
    geprueft.add(schluessel)
    xobjects = _resolve(resources.get("/XObject"))
    if not xobjects:
        return False
    for name in xobjects:
        budget[0] -= 1
        if budget[0] < 0:
            return True
        objekt = _resolve(xobjects[name])
        art = objekt.get("/Subtype")
        if art == "/Image":
            return True
        if art == "/Form" and _contains_image(objekt.get("/Resources"), depth + 1, geprueft, budget):
            return True
    return False
