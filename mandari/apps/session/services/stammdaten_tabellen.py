# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tabellen aus Listen der Verwaltung lesen: CSV (Excel-Export, Semikolon oder Komma) und XLSX (Issue #762).

Für den Stammdaten-Import (:mod:`apps.session.services.stammdaten_import`). Liefert je Datei die Kopfzeile
und die Zeilen mit ihrer Zeilennummer in der Datei, damit Fehlermeldungen zeilengenau sind.

- CSV: UTF-8 (mit oder ohne BOM), sonst Windows-1252 (Excel unter Windows); Trennzeichen aus der Kopfzeile
  (Semikolon, Komma oder Tabulator).
- XLSX: erstes Tabellenblatt, ohne Zusatzbibliothek (ZIP + XML über ``defusedxml``). Zahlen in Datumsspalten
  sind Excel-Seriennummern; :func:`excel_date` rechnet sie um.
- Grenzen gegen versehentlich riesige oder präparierte Dateien: Größe, entpackte Größe, Zeilenzahl, Spalten
  (wie Excel höchstens XFD) und Zellen insgesamt.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from defusedxml import ElementTree

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_UNPACKED_BYTES = 50 * 1024 * 1024
MAX_ROWS = 20_000
# Excel: höchstens 16.384 Spalten (A bis XFD). Zellen insgesamt (Zeilen × belegte Breite) begrenzt, damit eine
# einzelne Zelle weit rechts je Zeile nicht Millionen leerer Zellen erzeugt
MAX_COLUMNS = 16_384
MAX_CELLS = 2_000_000
_OUTSIDE_SHEET = "Ein Zellbezug liegt außerhalb des Tabellenblatts (höchstens Spalte XFD)."

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
_CELL_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")


class TableError(Exception):
    """Datei nicht lesbar (Format, Größe); die Meldung ist für Sachbearbeitende formuliert."""


@dataclass
class Table:
    """Eine gelesene Datei: Kopfzeile und Zeilen (Zeilennummer in der Datei, Zellwerte als Text)."""

    path: Path
    header: list[str]
    rows: list[tuple[int, list[str]]] = field(default_factory=list)
    # XLSX mit Datumssystem 1904 (alte Mac-Dateien): Seriennummern zählen ab 1904
    date1904: bool = False
    spreadsheet: bool = False


def read_table(path: Path) -> Table:
    """CSV oder XLSX nach Dateiendung lesen."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise TableError(f"Datei größer als {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _read_csv(path)
    if suffix == ".xlsx":
        return _read_xlsx(path)
    raise TableError("Nur CSV- und XLSX-Dateien werden gelesen.")


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _delimiter(first_line: str) -> str:
    counts = {d: first_line.count(d) for d in (";", ",", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ";"


def _read_csv(path: Path) -> Table:
    text = _decode(path.read_bytes())
    first_line = text.split("\n", 1)[0]
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=_delimiter(first_line))
    try:
        header = next(reader)
    except StopIteration:
        raise TableError("Die Datei ist leer.") from None
    except csv.Error:
        raise TableError("Die Kopfzeile ist kein gültiges CSV.") from None
    table = Table(path=path, header=[cell.strip() for cell in header])
    try:
        for values in reader:
            if not any(cell.strip() for cell in values):
                continue
            table.rows.append((reader.line_num, [cell.strip() for cell in values]))
            if len(table.rows) > MAX_ROWS:
                raise TableError(f"Mehr als {MAX_ROWS} Zeilen.")
    except csv.Error:
        raise TableError(f"Zeile {reader.line_num} ist kein gültiges CSV (Anführungszeichen prüfen).") from None
    return table


# ---------------------------------------------------------------------------
# XLSX (Office Open XML, erstes Tabellenblatt)
# ---------------------------------------------------------------------------


def _column_index(letters: str) -> int:
    """Spaltenbuchstaben -> Index ab 0; außerhalb von A bis XFD ein :class:`TableError`."""
    index = 0
    for char in letters[:4]:  # mehr als drei Buchstaben liegen ohnehin jenseits von XFD
        index = index * 26 + (ord(char) - ord("A") + 1)
    if len(letters) > 3 or index > MAX_COLUMNS:
        raise TableError(_OUTSIDE_SHEET)
    return index - 1


def _xml(archive: zipfile.ZipFile, name: str) -> Any:
    with archive.open(name) as handle:
        return ElementTree.fromstring(handle.read())


def _first_sheet_path(archive: zipfile.ZipFile) -> tuple[str, bool]:
    workbook = _xml(archive, "xl/workbook.xml")
    props = workbook.find("m:workbookPr", _NS)
    date1904 = props is not None and props.get("date1904") in ("1", "true")
    sheet = workbook.find("m:sheets/m:sheet", _NS)
    if sheet is None:
        raise TableError("Die Arbeitsmappe enthält kein Tabellenblatt.")
    rel_id = sheet.get(f"{{{_REL_NS}}}id")
    rels = _xml(archive, "xl/_rels/workbook.xml.rels")
    for rel in rels.findall("r:Relationship", _PKG_REL_NS):
        if rel.get("Id") == rel_id:
            target = str(rel.get("Target") or "").lstrip("/")
            return (target if target.startswith("xl/") else f"xl/{target}"), date1904
    raise TableError("Das erste Tabellenblatt fehlt in der Arbeitsmappe.")


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _xml(archive, "xl/sharedStrings.xml")
    return ["".join(t.text or "" for t in item.iter(f"{{{_NS['m']}}}t")) for item in root.findall("m:si", _NS)]


def _cell_text(cell: Any, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{{{_NS['m']}}}t"))
    value = cell.find("m:v", _NS)
    raw = (value.text or "") if value is not None else ""
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "ja" if raw == "1" else "nein"
    if kind in (None, "n") and raw:
        # Ganze Zahlen ohne ".0" (Excel speichert alle Zahlen als Gleitkomma)
        try:
            number = float(raw)
        except ValueError:
            return raw
        return str(int(number)) if number.is_integer() else raw
    return raw


def _read_xlsx(path: Path) -> Table:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise TableError("Die Datei ist keine gültige XLSX-Datei.") from None
    with archive:
        if sum(info.file_size for info in archive.infolist()) > MAX_UNPACKED_BYTES:
            raise TableError("Die XLSX-Datei ist entpackt zu groß.")
        try:
            sheet_path, date1904 = _first_sheet_path(archive)
            shared = _shared_strings(archive)
            sheet = _xml(archive, sheet_path)
        except KeyError:
            raise TableError("Die XLSX-Datei ist unvollständig.") from None
        except ElementTree.ParseError:
            raise TableError("Die XLSX-Datei enthält ungültiges XML.") from None

    rows: list[tuple[int, list[str]]] = []
    total_cells = 0
    for row in sheet.iterfind("m:sheetData/m:row", _NS):
        cells: dict[int, str] = {}
        for position, cell in enumerate(row.findall("m:c", _NS)):
            match = _CELL_REF_RE.match(cell.get("r") or "")
            index = _column_index(match.group(1)) if match else position
            if index >= MAX_COLUMNS:
                raise TableError(_OUTSIDE_SHEET)
            text = _cell_text(cell, shared).strip()
            if text:  # leere (z. B. nur formatierte) Zellen verlängern die Zeile nicht
                cells[index] = text
        try:
            number = int(row.get("r") or 0) or len(rows) + 1
        except ValueError:
            number = len(rows) + 1
        total_cells += max(cells) + 1 if cells else 0
        if total_cells > MAX_CELLS:
            raise TableError("Das Tabellenblatt hat zu viele Zellen (Spalten weit rechts belegt?).")
        values = [cells.get(i, "") for i in range(max(cells) + 1)] if cells else []
        if any(values):
            rows.append((number, values))
        if len(rows) > MAX_ROWS + 1:
            raise TableError(f"Mehr als {MAX_ROWS} Zeilen.")
    if not rows:
        raise TableError("Das erste Tabellenblatt ist leer.")
    (_, header), body = rows[0], rows[1:]
    return Table(path=path, header=header, rows=body, date1904=date1904, spreadsheet=True)


def excel_date(serial: float, *, date1904: bool = False) -> date:
    """Excel-Seriennummer in ein Datum umrechnen (1900er System mit Excels 29.02.1900, sonst 1904er)."""
    base = date(1904, 1, 1) if date1904 else date(1899, 12, 30)
    return base + timedelta(days=int(serial))
