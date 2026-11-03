"""
Textebene (pypdf) eines PDFs in einem eigenen Prozess.

Der Ingestor liest die Textebene nie im eigenen Prozess, der alle Abgleiche trägt: pypdf hält die entpackten Inhalte
aller gelesenen Seiten, solange der Leser lebt, und eine Datei von wenigen KiB kann so Hunderte MB belegen. Er startet
für jedes PDF dieses Skript (``python -I textebene.py <MB>``, ``extractor._read_text_layer_isolated``) mit einer
Zeitgrenze. Das Skript begrenzt zuerst seinen Adressraum (``RLIMIT_AS``, wo es das gibt, also nicht unter Windows) und
meldet sich dem Speicherwächter als Erster, liest das PDF von stdin und schreibt ``{"text": …, "page_count": …}`` als
JSON nach stdout. An der Speichergrenze endet es mit ``EXIT_MEMORY`` statt mit einem Teil des Textes.

Text liefern höchstens ``MAX_TEXT_PAGES`` Seiten mit zusammen ``MAX_TEXT_CHARS`` Zeichen. Das Skript braucht nur die
Standardbibliothek und pypdf, nicht das Paket ``src``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from io import BytesIO
from typing import Final

#: Text liefern höchstens so viele Seiten; die Seitenzahl bleibt die der Datei
MAX_TEXT_PAGES: Final = 3000
#: Höchstens so viele Zeichen Text je Datei (wie die Grenze beim Speichern, ``TEXT_CONTENT_MAX_CHARS``)
MAX_TEXT_CHARS: Final = 5_000_000
#: Rückgabewert an der Speichergrenze
EXIT_MEMORY: Final = 3


def read_text_layer(
    data: bytes, max_pages: int = MAX_TEXT_PAGES, max_chars: int = MAX_TEXT_CHARS
) -> tuple[str, int | None]:
    """
    Text der Textebene und Seitenzahl. Seiten ohne Text fallen weg, die übrigen trennt eine Leerzeile. Ist die Datei
    nicht lesbar, ergibt das keinen Text (und eine Seitenzahl nur, wenn sie schon bekannt war). ``MemoryError`` geht
    an den Aufrufer: An der Speichergrenze gibt es kein Teilergebnis, das wie eine vollständige Textebene aussähe.
    """
    page_count: int | None = None
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        page_count = len(reader.pages)
        fragments: list[str] = []
        total = 0
        for number, page in enumerate(reader.pages, start=1):
            if number > max_pages or total >= max_chars:
                break
            try:
                text = (page.extract_text() or "").strip()
            except MemoryError:
                raise
            except Exception:  # noqa: BLE001 - eine defekte Seite verhindert den Rest nicht
                text = ""
            if text:
                fragments.append(text)
                total += len(text) + 2
        return "\n\n".join(fragments)[:max_chars], page_count
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001 - kaputte Textebene: weiter mit der Erkennung aus dem Bild
        sys.stderr.write(f"pypdf: Textebene nicht lesbar ({type(exc).__name__})\n")
        return "", page_count


def _limit_memory(memory_limit_mb: int) -> None:
    """Grenze des Adressraums in MB (0 = ohne; unter Windows wirkungslos) und Vorrang beim Speicherwächter."""
    if sys.platform.startswith("linux"):
        with contextlib.suppress(OSError), open("/proc/self/oom_score_adj", "w") as datei:
            datei.write("1000")
    if memory_limit_mb <= 0 or sys.platform == "win32":  # Windows (Entwicklung): ohne Grenze
        return
    import resource

    grenze = memory_limit_mb * 1024 * 1024
    hart = resource.getrlimit(resource.RLIMIT_AS)[1]
    if hart != resource.RLIM_INFINITY:
        grenze = min(grenze, hart)
    resource.setrlimit(resource.RLIMIT_AS, (grenze, grenze))


def main(argv: list[str]) -> int:
    _limit_memory(int(argv[0]) if argv else 0)
    # Der Aufrufer liest stderr nur für seine Meldung: keine Warnung je unsauberem Objekt
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        text, page_count = read_text_layer(sys.stdin.buffer.read())
        ausgabe = json.dumps({"text": text, "page_count": page_count}, ensure_ascii=False)
        sys.stdout.buffer.write(ausgabe.encode("utf-8", "backslashreplace"))
    except MemoryError:
        sys.stderr.write("MemoryError\n")
        return EXIT_MEMORY
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
