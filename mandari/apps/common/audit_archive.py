# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Export und Archivpakete für Protokolle mit Hash-Kette (Issue #221).

- **Datensätze** (:func:`record_for`): je Eintrag genau die gehashten Felder in kanonischer Form,
  dazu ``entry_hash`` und optional Anzeigefelder unter ``anzeige`` (nicht gehasht). Damit lässt
  sich jeder Eintrag außerhalb von mandari nachrechnen (Verfahren in docs/PROTOKOLLIERUNG.md).
- **JSON** mit Umschlag: Metadaten, Einträge, Anzahl und Prüfsumme über das kanonische
  Eintrags-Array. **CSV** mit Semikolon, UTF-8 mit BOM und Schutz gegen Formel-Injektion.
- **ZIP**: JSON, CSV, ``kette.json`` (Kettenanker) und ``SHA256SUMS`` als Begleitdatei.
- **Archivpaket vor der Löschung** (:func:`archive_and_delete`): Die fristgerecht zu löschenden
  Einträge bilden immer ein Anfangsstück der Kette. Sie werden beim Schreiben des Pakets geprüft
  (eine manipulierte Kette wird nie gelöscht), das Paket landet im Archivspeicher, dann werden
  die Einträge gelöscht und der Kettenanker gesetzt – ab ihm beginnt künftig die Prüfung.

Alle Dateien entstehen in temporären Dateien (bis 8 MB im Speicher, darüber auf der Platte) und
werden stapelweise aus der Datenbank gelesen; große Protokolle belasten den Speicher nicht.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import IO, Any

from django.conf import settings
from django.core.files import File
from django.core.files.storage import FileSystemStorage, Storage, storages
from django.db import transaction
from django.db.models import Min
from django.utils import timezone

from apps.common import audit_chain, csv_safety

#: Formatkennung und -version im JSON-Umschlag
FORMAT_EXPORT = "mandari-protokoll-export"
FORMAT_ARCHIVE = "mandari-protokoll-archiv"
FORMAT_VERSION = 1
#: Ab dieser Größe schreiben die temporären Dateien auf die Platte
SPOOL_BYTES = 8 * 1024 * 1024

HASH_METHOD = (
    "SHA-256 über das kanonische JSON (sortierte Schlüssel, ohne Leerzeichen, UTF-8) aller Felder "
    "eines Eintrags ohne 'entry_hash' und 'anzeige'; 'prev_hash' ist der entry_hash des Vorgängers."
)

ExtrasFn = Callable[[list[Any]], dict[Any, dict[str, Any]]]


# =============================================================================
# Datensätze
# =============================================================================


def record_for(spec: audit_chain.ChainSpec, entry: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Export-Datensatz eines Eintrags (gehashte Felder + entry_hash + Anzeige)."""
    data = audit_chain.payload(spec, entry)
    data["entry_hash"] = entry.entry_hash
    if extra:
        data["anzeige"] = extra
    return data


def iter_records(
    spec: audit_chain.ChainSpec, entries: Iterable[Any], extras: ExtrasFn | None = None, batch_size: int = 1000
) -> Iterator[dict[str, Any]]:
    """Datensätze stapelweise erzeugen; ``extras`` ergänzt Anzeigefelder je Stapel (eine Abfrage)."""
    batch: list[Any] = []
    for entry in entries:
        batch.append(entry)
        if len(batch) >= batch_size:
            yield from _records(spec, batch, extras)
            batch = []
    if batch:
        yield from _records(spec, batch, extras)


def _records(spec: audit_chain.ChainSpec, batch: list[Any], extras: ExtrasFn | None) -> Iterator[dict[str, Any]]:
    looked_up = extras(batch) if extras is not None else {}
    for entry in batch:
        yield record_for(spec, entry, looked_up.get(entry.pk))


def csv_columns(spec: audit_chain.ChainSpec, extra_columns: Iterable[str] = ()) -> list[str]:
    """Spaltenfolge der CSV: Nummer und Zeit vorn, Hashes hinten, Anzeigefelder am Ende."""
    middle = [name for name in spec.fields if name not in ("id", "created_at")]
    return ["seq", "created_at", *middle, "id", "prev_hash", "entry_hash", *(f"anzeige.{c}" for c in extra_columns)]


def _csv_value(value: Any) -> str:
    if isinstance(value, dict | list):
        return audit_chain.canonical_json(value)
    if isinstance(value, bool):
        return "ja" if value else "nein"
    return "" if value is None else str(value)


# =============================================================================
# Dateien schreiben (mit laufender Prüfsumme)
# =============================================================================


def _spooled() -> IO[bytes]:
    # Die Datei lebt im Rückgabewert weiter und wird vom Aufrufer bzw. der Antwort geschlossen
    return tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES, mode="w+b")  # noqa: SIM115


class _HashingWriter:
    """Schreibt Text als UTF-8 in eine temporäre Datei und rechnet SHA-256 und Größe mit."""

    def __init__(self) -> None:
        self.target = _spooled()
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, text: str) -> int:
        data = text.encode("utf-8")
        self.target.write(data)
        self.digest.update(data)
        self.size += len(data)
        return len(text)


@dataclass
class ExportFile:
    """Eine erzeugte Datei (temporär) mit Prüfsumme."""

    name: str
    content_type: str
    file: IO[bytes]
    sha256: str
    size: int
    count: int = 0
    entries_sha256: str = ""

    def read_all(self) -> bytes:
        self.file.seek(0)
        return self.file.read()


class JsonEnvelopeWriter:
    """JSON-Umschlag: Metadaten, Einträge (kanonisch), Anzahl und Prüfsumme der Einträge."""

    def __init__(self, meta: dict[str, Any], *, name: str) -> None:
        self.name = name
        self.out = _HashingWriter()
        self.entries_digest = hashlib.sha256(b"[")
        self.count = 0
        head = audit_chain.canonical_json(meta)
        self.out.write(head[:-1] + ("," if len(head) > 2 else "") + '"eintraege":[')

    def add(self, record: dict[str, Any]) -> None:
        chunk = ("," if self.count else "") + audit_chain.canonical_json(record)
        self.out.write(chunk)
        self.entries_digest.update(chunk.encode("utf-8"))
        self.count += 1

    def finish(self) -> ExportFile:
        self.entries_digest.update(b"]")
        checksum = {
            "algorithmus": "SHA-256",
            "umfang": "eintraege (kanonisches JSON)",
            "wert": self.entries_digest.hexdigest(),
        }
        self.out.write(f'],"anzahl":{self.count},"pruefsumme":{audit_chain.canonical_json(checksum)}}}')
        self.out.target.seek(0)
        return ExportFile(
            name=self.name,
            content_type="application/json",
            file=self.out.target,
            sha256=self.out.digest.hexdigest(),
            size=self.out.size,
            count=self.count,
            entries_sha256=self.entries_digest.hexdigest(),
        )


class CsvWriter:
    """CSV (Semikolon, CRLF, UTF-8 mit BOM) mit Schutz gegen Formel-Injektion in jeder Zelle."""

    def __init__(self, columns: list[str], *, name: str) -> None:
        self.name = name
        self.columns = columns
        self.out = _HashingWriter()
        self.out.write("﻿")
        self.rows = csv_safety.writer(self.out, delimiter=";", lineterminator="\r\n")
        self.rows.writerow(columns)
        self.count = 0

    def add(self, record: dict[str, Any]) -> None:
        extra = record.get("anzeige") or {}
        row = []
        for column in self.columns:
            value = extra.get(column[len("anzeige.") :]) if column.startswith("anzeige.") else record.get(column)
            row.append(_csv_value(value))
        self.rows.writerow(row)
        self.count += 1

    def finish(self) -> ExportFile:
        self.out.target.seek(0)
        return ExportFile(
            name=self.name,
            content_type="text/csv; charset=utf-8",
            file=self.out.target,
            sha256=self.out.digest.hexdigest(),
            size=self.out.size,
            count=self.count,
        )


def write_json(records: Iterable[dict[str, Any]], meta: dict[str, Any], *, name: str) -> ExportFile:
    writer = JsonEnvelopeWriter(meta, name=name)
    for record in records:
        writer.add(record)
    return writer.finish()


def write_csv(records: Iterable[dict[str, Any]], columns: list[str], *, name: str) -> ExportFile:
    writer = CsvWriter(columns, name=name)
    for record in records:
        writer.add(record)
    return writer.finish()


def write_json_and_csv(
    records: Iterable[dict[str, Any]], meta: dict[str, Any], columns: list[str], *, base_name: str
) -> tuple[ExportFile, ExportFile]:
    """JSON und CSV in einem Durchgang (die Einträge werden nur einmal gelesen)."""
    json_writer = JsonEnvelopeWriter(meta, name=f"{base_name}.json")
    csv_writer = CsvWriter(columns, name=f"{base_name}.csv")
    for record in records:
        json_writer.add(record)
        csv_writer.add(record)
    return json_writer.finish(), csv_writer.finish()


def write_zip(files: list[ExportFile], *, name: str, extra_files: dict[str, str] | None = None) -> ExportFile:
    """ZIP mit den Dateien, weiteren Textdateien und ``SHA256SUMS`` als Begleitdatei."""
    target = _spooled()
    sums: list[str] = []
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            item.file.seek(0)
            with archive.open(item.name, "w", force_zip64=True) as handle:
                while chunk := item.file.read(1024 * 1024):
                    handle.write(chunk)
            sums.append(f"{item.sha256}  {item.name}")
        for file_name, text in (extra_files or {}).items():
            data = text.encode("utf-8")
            archive.writestr(file_name, data)
            sums.append(f"{hashlib.sha256(data).hexdigest()}  {file_name}")
        archive.writestr("SHA256SUMS", "\n".join(sums) + "\n")
    digest = hashlib.sha256()
    size = 0
    target.seek(0)
    while chunk := target.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    target.seek(0)
    return ExportFile(
        name=name,
        content_type="application/zip",
        file=target,
        sha256=digest.hexdigest(),
        size=size,
        count=max((f.count for f in files), default=0),
        entries_sha256=next((f.entries_sha256 for f in files if f.entries_sha256), ""),
    )


def chain_info(spec: audit_chain.ChainSpec, scope_id: Any) -> dict[str, Any]:
    """Kettenkopf und Anker zum Zeitpunkt des Exports (zum Abgleich außerhalb aufbewahren)."""
    head = audit_chain.get_head(spec, scope_id)
    return {
        "bereich": spec.scope_key(scope_id),
        "verfahren": HASH_METHOD,
        "genesis": audit_chain.GENESIS_HASH,
        "kopf_nr": head.last_seq if head else 0,
        "kopf_hash": head.last_hash if head else "",
        "anker_nr": head.anchor_seq if head else 0,
        "anker_hash": head.anchor_hash if head else "",
        "verkettung_aktiv": bool(head and head.initialized),
    }


# =============================================================================
# Archivpaket vor der fristgerechten Löschung
# =============================================================================


def archive_storage() -> Storage:
    """Speicher für Archivpakete: STORAGES-Alias oder geschütztes Verzeichnis."""
    alias = getattr(settings, "AUDIT_ARCHIVE_STORAGE", "")
    if alias:
        return storages[alias]
    return FileSystemStorage(
        location=str(settings.AUDIT_ARCHIVE_ROOT), file_permissions_mode=0o600, directory_permissions_mode=0o700
    )


@dataclass
class ArchiveResult:
    """Ergebnis einer Archivierung mit Löschung."""

    deleted: int
    first_seq: int
    last_seq: int
    anchor_hash: str
    archive_name: str
    archive_sha256: str
    deferred: int = 0


def expired_range(
    spec: audit_chain.ChainSpec, scope_id: Any, cutoff: Any, *, limit: int | None = None
) -> tuple[int, int, int] | None:
    """Anfangsstück der Kette, dessen Einträge alle bis ``cutoff`` entstanden sind.

    Returns: (erste Nr., letzte Nr., zurückgestellte abgelaufene Einträge) oder ``None``.
    Zurückgestellt sind abgelaufene Einträge hinter einem jüngeren – sie folgen später.
    """
    head = audit_chain.get_head(spec, scope_id)
    if head is None or not head.initialized or head.last_seq <= head.anchor_seq:
        return None
    chained = spec.entries(scope_id).filter(seq__gt=head.anchor_seq)
    first_young = chained.filter(created_at__gt=cutoff).aggregate(first=Min("seq"))["first"]
    last = (first_young - 1) if first_young is not None else head.last_seq
    if limit is not None:
        last = min(last, head.anchor_seq + limit)
    if last <= head.anchor_seq:
        return None
    deferred = chained.filter(seq__gt=last, created_at__lte=cutoff).count()
    return head.anchor_seq + 1, last, deferred


def archive_and_delete(
    spec: audit_chain.ChainSpec,
    scope_id: Any,
    cutoff: Any,
    *,
    scope_label: str,
    meta: dict[str, Any] | None = None,
    extras: ExtrasFn | None = None,
    extra_columns: Iterable[str] = (),
    limit: int | None = None,
    storage: Storage | None = None,
) -> ArchiveResult | None:
    """Abgelaufene Einträge prüfen, als Archivpaket ablegen, dann löschen und den Anker setzen.

    Raises:
        audit_chain.ChainError: Die Kette ist im abgelaufenen Stück beschädigt – dann wird weder
            archiviert noch gelöscht, damit die Spur der Manipulation erhalten bleibt.
    """
    found = expired_range(spec, scope_id, cutoff, limit=limit)
    if found is None:
        return None
    first, last, deferred = found
    head = audit_chain.get_head(spec, scope_id)
    anchor_before = head.anchor_hash if head.anchor_seq else audit_chain.GENESIS_HASH
    state: dict[str, Any] = {"seq": head.anchor_seq, "prev": anchor_before}

    def checked_entries() -> Iterator[Any]:
        """Einträge des Archivstücks – mit Prüfung von Nummer, Verkettung und Hash."""
        for entry in audit_chain.iter_chain(spec, scope_id, after_seq=head.anchor_seq, upto_seq=last):
            if entry.seq != state["seq"] + 1 or entry.prev_hash != state["prev"]:
                raise audit_chain.ChainError(f"Kette bei Nr. {entry.seq} unterbrochen – keine Löschung.")
            if audit_chain.compute_hash(spec, entry) != entry.entry_hash:
                raise audit_chain.ChainError(f"Eintrag Nr. {entry.seq} wurde verändert – keine Löschung.")
            state["seq"] = entry.seq
            state["prev"] = entry.entry_hash
            yield entry

    now = timezone.now()
    base_name = f"protokoll-{scope_label}-nr{first:08d}-{last:08d}"
    envelope = {
        "format": FORMAT_ARCHIVE,
        "version": FORMAT_VERSION,
        "erstellt_am": now.isoformat(),
        "von_nr": first,
        "bis_nr": last,
        "anker_vorher": anchor_before,
        **(meta or {}),
    }
    json_file, csv_file = write_json_and_csv(
        iter_records(spec, checked_entries(), extras),
        envelope,
        csv_columns(spec, extra_columns),
        base_name=base_name,
    )
    if json_file.count != last - first + 1 or state["seq"] != last:
        raise audit_chain.ChainError("Nicht alle Einträge des Archivstücks sind vorhanden – keine Löschung.")
    anchor = {
        "bereich": spec.scope_key(scope_id),
        "verfahren": HASH_METHOD,
        "von_nr": first,
        "bis_nr": last,
        "anker_vorher": anchor_before,
        "anker_nachher": state["prev"],
        "kopf_nr": head.last_seq,
        "kopf_hash": head.last_hash,
        "pruefsumme_eintraege": json_file.entries_sha256,
    }
    package = write_zip(
        [json_file, csv_file],
        name=f"{base_name}.zip",
        extra_files={"kette.json": json.dumps(anchor, ensure_ascii=False, indent=2) + "\n"},
    )
    target = storage or archive_storage()
    folder = spec.scope_key(scope_id).replace(":", "/")
    stored_name = target.save(f"{folder}/{now:%Y%m%d-%H%M%S}-{package.name}", File(package.file))

    with transaction.atomic():
        locked = audit_chain.lock_head(spec, scope_id)
        if locked.anchor_seq != first - 1:
            raise audit_chain.ChainError("Der Kettenanker hat sich während der Archivierung verändert.")
        doomed = spec.entries(scope_id).filter(seq__gte=first, seq__lte=last)
        deleted = doomed._raw_delete(doomed.db)
        type(locked)._default_manager.filter(pk=locked.pk).update(
            anchor_seq=last,
            anchor_hash=state["prev"],
            anchor_archive=stored_name[:255],
            anchor_at=now,
            updated_at=now,
        )
    return ArchiveResult(
        deleted=deleted,
        first_seq=first,
        last_seq=last,
        anchor_hash=state["prev"],
        archive_name=stored_name,
        archive_sha256=package.sha256,
        deferred=deferred,
    )
