# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auslieferung von Anlagen aus Ratsinformationssystemen in der Dateivorschau.

Die Dateien und ihre Angaben (MIME-Typ, Dateiname) stammen aus Quellen, die wir nicht
kontrollieren. Sie laufen aber im selben Ursprung wie Work, Session und der Admin. Deshalb zeigt
der Browser nur passive Formate an: PDF, Rasterbilder und reinen Text. Alles andere wird
heruntergeladen und kommt als ``application/octet-stream`` an – HTML, SVG, XML oder Skripte
werden nie mit ihrem eigenen Typ ausgeliefert. Dazu kommen ``nosniff`` und, außer bei PDF, eine
Sandbox per CSP. PDF bleibt ohne Sandbox, weil die PDF-Betrachter der Browser in einer Sandbox
nicht starten; die iframe-Vorschau braucht sie.
"""

from __future__ import annotations

import re
import unicodedata

from django.http import HttpResponseBase
from django.utils.http import content_disposition_header

#: Formate, die die Vorschau im Browser anzeigt; alles andere wird heruntergeladen
INLINE_CONTENT_TYPES = frozenset(
    {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "text/plain"}
)
#: Formate ohne Sandbox (PDF-Betrachter laufen darin nicht)
UNSANDBOXED_CONTENT_TYPES = frozenset({"application/pdf"})
DOWNLOAD_CONTENT_TYPE = "application/octet-stream"
DEFAULT_FILENAME = "dokument.pdf"
MAX_FILENAME_LENGTH = 150

# Zeichen, die in Dateinamen stören (Windows-Sperrzeichen, Parameter-Trenner im Header)
_STOERZEICHEN = re.compile(r'[<>:"|?*;]')
_LEERRAUM = re.compile(r"\s+")


def delivery_type(content_type: str | None) -> tuple[str, bool]:
    """(Content-Type, im Browser anzeigen?) für eine Datei aus einer fremden Quelle."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in INLINE_CONTENT_TYPES:
        return ctype, True
    return DOWNLOAD_CONTENT_TYPE, False


def safe_filename(name: str | None, fallback: str = DEFAULT_FILENAME) -> str:
    """
    Dateinamen aus der Quelle für ``Content-Disposition`` bereinigen.

    Ohne Pfadanteile, Steuer- und Formatzeichen (Zeilenumbrüche, Richtungswechsel) und
    Sperrzeichen; höchstens ``MAX_FILENAME_LENGTH`` Zeichen, die Endung bleibt erhalten.
    Umlaute bleiben – der Header kodiert sie nach RFC 5987.
    """
    text = unicodedata.normalize("NFC", str(name or ""))
    text = re.split(r"[\\/]", text)[-1]
    text = "".join(" " if unicodedata.category(zeichen).startswith("C") else zeichen for zeichen in text)
    text = _LEERRAUM.sub(" ", _STOERZEICHEN.sub("", text)).strip(" .")
    if len(text) > MAX_FILENAME_LENGTH:
        stamm, punkt, endung = text.rpartition(".")
        if punkt and 0 < len(endung) <= 10:
            text = f"{stamm[: MAX_FILENAME_LENGTH - len(endung) - 1].rstrip(' .')}.{endung}"
        else:
            text = text[:MAX_FILENAME_LENGTH].rstrip(" .")
    return text or fallback


def apply(response: HttpResponseBase, content_type: str | None, filename: str | None, *, download: bool) -> None:
    """Typ, Anzeigeart, Dateinamen und Schutz-Header einer Dateiantwort setzen."""
    ctype, inline = delivery_type(content_type)
    inline = inline and not download
    response["Content-Type"] = ctype
    disposition = content_disposition_header(as_attachment=not inline, filename=safe_filename(filename))
    response["Content-Disposition"] = disposition or ("inline" if inline else "attachment")
    response["X-Content-Type-Options"] = "nosniff"
    if ctype not in UNSANDBOXED_CONTENT_TYPES:
        response["Content-Security-Policy"] = "sandbox"
