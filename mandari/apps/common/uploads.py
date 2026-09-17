# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame Prüfung hochgeladener Dateien.

BSI IT-Grundschutz verlangt als Basis-Anforderung, Upload-Funktionen nach
Dateigröße, Dateityp und Speicherort einzuschränken (CON.10.A5, APP.3.1.A4).
Bis Issue #260 stand diese Regel an drei Stellen unterschiedlich und an vier
Stellen gar nicht — jeder neue Upload-Pfad erbte die Lücke aufs Neue.

Deshalb steht sie jetzt hier einmal. Wer einen neuen Upload baut, ruft
``validate_upload`` auf und wählt ein Profil.

Zu den Profilen: ``DOCUMENTS`` ist der Alltag der Gremienarbeit (Vorlagen,
Anlagen, Tabellen). ``IMAGES`` gilt dort, wo eine Datei später eingebettet
angezeigt wird — Logos und Avatare. **SVG ist dort bewusst nicht erlaubt**:
Eine SVG-Datei darf Skript enthalten und wird vom Browser als aktives Dokument
behandelt, nicht als Bild.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile

#: Anlagen der Gremien- und Fraktionsarbeit.
DOCUMENTS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".odt",
        ".rtf",
        ".txt",
        ".md",
        ".xls",
        ".xlsx",
        ".ods",
        ".csv",
        ".ppt",
        ".pptx",
        ".odp",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".zip",
    }
)

#: Dateien, die eingebettet dargestellt werden. Ohne SVG (kann Skript tragen).
IMAGES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

#: Nur PDF — Briefköpfe, die unter den Text gelegt werden.
PDF = frozenset({".pdf"})

#: Vorlagen, aus denen Text übernommen wird (Antrags-Import).
IMPORTABLE_DOCUMENTS = frozenset({".pdf", ".docx"})

#: Strukturierte Daten für Import-Schnittstellen. Sie werden nur geparst (XML über
#: defusedxml), nie gespeichert oder ausgeliefert; deshalb darf XML hier stehen.
DATA = frozenset({".csv", ".json", ".xml"})

#: Endungen, die niemals angenommen werden — unabhängig vom Profil.
#: Aktive Inhalte, die ein Browser ausführen würde, und Serverskripte. XML fehlt
#: bewusst: Es ist Datenformat der Import-Schnittstellen, kein Dokument-Profil
#: nimmt es an, und ausgeliefert wird es nie.
NEVER = frozenset(
    {
        ".htm",
        ".html",
        ".xhtml",
        ".shtml",
        ".svg",
        ".svgz",
        ".xsl",
        ".xslt",
        ".js",
        ".mjs",
        ".jsx",
        ".php",
        ".phtml",
        ".py",
        ".rb",
        ".pl",
        ".sh",
        ".bash",
        ".exe",
        ".dll",
        ".bat",
        ".cmd",
        ".com",
        ".scr",
        ".msi",
        ".jar",
        ".hta",
        ".vbs",
        ".ps1",
    }
)

MB = 1024 * 1024


def _suffix(name: str) -> str:
    """Letzte Endung in Kleinbuchstaben; ohne Endung ein leerer Text."""
    return PurePosixPath((name or "").replace("\\", "/")).suffix.lower()


def validate_upload(
    uploaded_file: UploadedFile[Any] | None,
    *,
    allowed: frozenset[str],
    max_bytes: int,
    bezeichnung: str = "Datei",
) -> None:
    """Prüft eine hochgeladene Datei; wirft ``ValidationError`` mit klarer Meldung.

    ``allowed`` ist eines der Profile oben. ``NEVER`` sticht immer — auch wenn
    ein Profil eine dieser Endungen versehentlich enthält.
    """
    if uploaded_file is None:
        raise ValidationError(f"Es wurde keine {bezeichnung} ausgewählt.")

    groesse = getattr(uploaded_file, "size", 0) or 0
    if groesse <= 0:
        raise ValidationError(f"Die {bezeichnung} ist leer.")
    if groesse > max_bytes:
        raise ValidationError(f"Die {bezeichnung} darf höchstens {max_bytes // MB} MB groß sein.")

    endung = _suffix(getattr(uploaded_file, "name", "") or "")
    if not endung:
        raise ValidationError("Die Datei hat keine Endung. Bitte mit Dateiendung hochladen, etwa .pdf.")
    if endung in NEVER or endung not in allowed:
        erlaubt = ", ".join(sorted(e.lstrip(".") for e in allowed))
        raise ValidationError(f"Dateityp „{endung.lstrip('.')}“ ist nicht erlaubt. Möglich sind: {erlaubt}.")


def is_embeddable(path: str) -> bool:
    """True, wenn die Datei gefahrlos eingebettet ausgeliefert werden darf.

    Alles andere geht als Download hinaus (``Content-Disposition: attachment``),
    damit eine Datei nicht im Ursprung der Anwendung zur Anzeige kommt.
    """
    return _suffix(path) in IMAGES
