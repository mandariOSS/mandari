# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kartenanalyse über den lokalen Dateicache einer Kommune (Issue #599).

Liest ausschließlich Dateien, die schon im Dateicache liegen (``OParlFile.local_path``), und nie etwas
aus dem Netz – also keinen einzigen Abruf beim Ratsinformationssystem. Nur öffentliche Dateien:
gelöschte Dateien, Dateien gelöschter Vorgänge und Sitzungen sowie von mandari Session
zurückgenommene Dateien bleiben außen vor. Eine Datei, deren Auswertung scheitert, wird gezählt und
übersprungen; der Lauf geht weiter.

Je Seite misst ``apps.common.documents.page_analysis`` die Merkmale und ordnet sie als Karte ein;
Straßennamen zählt das Straßenverzeichnis der Kommune. ``MapSurvey`` sammelt daraus die Kennzahlen
(Kartenseiten, Maßstab im PDF, Koordinatenbeschriftung, Straßennamen, Scans ohne Text) und zieht eine
geschichtete Stichprobe für die Prüfung von Hand. ``evaluate_labels`` rechnet aus der von Hand
ausgefüllten Stichprobe Präzision und Trefferquote – roh und nach Schichten gewichtet.
"""

from __future__ import annotations

import csv
import logging
import random
import statistics
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apps.common import csv_safety
from apps.common.documents.page_analysis import (
    DocumentAnalysis,
    DocumentError,
    PageAnalysis,
    analyze_pdf,
    classify_page,
    format_scale,
)

if TYPE_CHECKING:
    from insight_core.models import OParlBody, OParlFile

logger = logging.getLogger(__name__)

MANY_STREETS = 4
SAMPLE_FIELDS = [
    "file_id",
    "page",
    "reference",
    "file_name",
    "predicted",
    "score",
    "reasons",
    "format",
    "segments",
    "density",
    "image_coverage",
    "max_dpi",
    "text_chars",
    "keywords",
    "coordinates",
    "scales",
    "streets",
    "scan",
    "weight",
    "label",
]


@dataclass
class PageRecord:
    """Eine ausgewertete Seite (für Stichprobe und Kennzahlen)."""

    file_id: str
    reference: str
    file_name: str
    page: PageAnalysis
    is_map: bool
    score: int
    reasons: tuple[str, ...]
    streets: int | None

    def row(self) -> dict[str, Any]:
        page = self.page
        return {
            "file_id": self.file_id,
            "page": page.number,
            "reference": self.reference,
            "file_name": self.file_name,
            "predicted": "ja" if self.is_map else "nein",
            "score": self.score,
            "reasons": "; ".join(self.reasons),
            "format": page.paper_format,
            "segments": page.path_segments,
            "density": round(page.vector_density, 1),
            "image_coverage": round(page.image_coverage, 2),
            "max_dpi": page.max_image_dpi or "",
            "text_chars": page.text_chars,
            "keywords": page.keyword_count,
            "coordinates": page.coordinate_labels,
            "scales": " ".join(format_scale(value) for value in page.scale_denominators),
            "streets": "" if self.streets is None else self.streets,
            "scan": "ja" if page.is_scan else "nein",
        }


@dataclass
class _Reservoir:
    """Zufallsstichprobe fester Größe aus einem Strom (Algorithmus R)."""

    size: int
    rng: random.Random
    seen: int = 0
    items: list[PageRecord] = field(default_factory=list)

    def offer(self, record: PageRecord) -> None:
        self.seen += 1
        if len(self.items) < self.size:
            self.items.append(record)
            return
        index = self.rng.randrange(self.seen)
        if index < self.size:
            self.items[index] = record


@dataclass
class MapSurvey:
    """Kennzahlen einer Auswertung; ``add`` nimmt Dokument für Dokument auf."""

    sample_size: int = 0
    seed: int = 599
    files: int = 0
    files_with_maps: int = 0
    pages: int = 0
    map_pages: int = 0
    map_pages_with_scale: int = 0
    map_pages_geopdf: int = 0
    map_pages_with_coordinates: int = 0
    map_pages_many_streets: int = 0
    map_pages_scan: int = 0
    map_pages_bitonal: int = 0
    scan_pages: int = 0
    unreadable_pages: int = 0
    truncated_files: int = 0
    street_directory: bool = False
    skipped: Counter[str] = field(default_factory=Counter)
    formats: Counter[str] = field(default_factory=Counter)
    producers: Counter[str] = field(default_factory=Counter)
    map_dpis: list[float] = field(default_factory=list)
    seconds: float = 0.0
    max_file_seconds: float = 0.0
    _maps: _Reservoir = field(init=False)
    _others: _Reservoir = field(init=False)

    def __post_init__(self) -> None:
        rng = random.Random(self.seed)
        half = self.sample_size // 2
        self._maps = _Reservoir(half, rng)
        self._others = _Reservoir(self.sample_size - half, rng)

    def add(self, document: DocumentAnalysis, records: list[PageRecord], seconds: float) -> None:
        self.files += 1
        self.seconds += seconds
        self.max_file_seconds = max(self.max_file_seconds, seconds)
        if document.truncated:
            self.truncated_files += 1
        self.unreadable_pages += document.unreadable_pages
        has_map = False
        for record in records:
            page = record.page
            self.pages += 1
            if page.is_scan:
                self.scan_pages += 1
            if record.is_map:
                has_map = True
                self.map_pages += 1
                self.formats[page.paper_format if page.paper_format.startswith("A") else "Sonderformat"] += 1
                self.map_pages_with_scale += page.has_scale
                self.map_pages_geopdf += page.geo_pdf
                self.map_pages_with_coordinates += page.coordinate_labels > 0
                self.map_pages_many_streets += (record.streets or 0) >= MANY_STREETS
                self.map_pages_scan += page.is_scan
                self.map_pages_bitonal += page.bitonal_basemap
                if page.max_image_dpi:
                    self.map_dpis.append(page.max_image_dpi)
                self._maps.offer(record)
            else:
                self._others.offer(record)
        if has_map:
            self.files_with_maps += 1
            self.producers[(document.creator or document.producer or "unbekannt")[:60]] += 1

    def sample_rows(self) -> list[dict[str, Any]]:
        """Stichprobe: gleich viele vorhergesagte Karten und Nicht-Karten, mit Gewicht je Schicht."""
        rows: list[dict[str, Any]] = []
        for reservoir in (self._maps, self._others):
            if not reservoir.items:
                continue
            weight = round(reservoir.seen / len(reservoir.items), 4)
            for record in reservoir.items:
                rows.append({**record.row(), "weight": weight, "label": ""})
        return rows

    def summary(self) -> dict[str, Any]:
        def share(part: int, whole: int) -> float:
            return round(part / whole, 4) if whole else 0.0

        return {
            "dateien": self.files,
            "dateien_mit_karte": self.files_with_maps,
            "seiten": self.pages,
            "kartenseiten": self.map_pages,
            "anteil_kartenseiten": share(self.map_pages, self.pages),
            "kartenseiten_mit_massstab": self.map_pages_with_scale,
            "kartenseiten_geopdf": self.map_pages_geopdf,
            "kartenseiten_mit_koordinaten": self.map_pages_with_coordinates,
            "kartenseiten_mit_4_strassen": self.map_pages_many_streets if self.street_directory else None,
            "kartenseiten_scan_ohne_text": self.map_pages_scan,
            "kartenseiten_mit_1bit_grundkarte": self.map_pages_bitonal,
            "scanseiten_ohne_text": self.scan_pages,
            "median_dpi_kartenraster": statistics.median(self.map_dpis) if self.map_dpis else None,
            "formate": dict(self.formats.most_common()),
            "erzeuger": dict(self.producers.most_common(10)),
            "uebersprungen": dict(self.skipped),
            "seiten_nicht_lesbar": self.unreadable_pages,
            "abgeschnitten": self.truncated_files,
            "sekunden": round(self.seconds, 1),
            "max_sekunden_je_datei": round(self.max_file_seconds, 2),
        }


def cached_files(body: OParlBody, *, max_bytes: int) -> Iterator[tuple[OParlFile, Path | None, str]]:
    """Öffentliche Dateien der Kommune mit lokaler Kopie: (Datei, Pfad oder None, Grund fürs Überspringen).

    Außen vor bleiben gelöschte Dateien und Dateien gelöschter Vorgänge bzw. Sitzungen. Das schließt
    alles ein, was mandari Session zurückgenommen hat (``withdrawn_q`` setzt ``deleted`` voraus).
    """
    from insight_core.models import OParlFile
    from insight_core.services.file_cache import local_file

    files = (
        OParlFile.objects.filter(body=body, deleted=False)
        .exclude(paper__deleted=True)
        .exclude(meeting__deleted=True)
        .exclude(local_path__isnull=True)
        .exclude(local_path="")
        .select_related("paper")
        .only("id", "name", "file_name", "mime_type", "local_path", "paper__reference")
        .order_by("-file_date", "-created_at")
    )
    for file_obj in files.iterator(chunk_size=500):
        path = local_file(file_obj)
        if path is None:
            yield file_obj, None, "nicht im Cache"
            continue
        is_pdf = path.suffix.lower() == ".pdf" or "pdf" in (file_obj.mime_type or "").lower()
        if not is_pdf:
            yield file_obj, None, "kein PDF"
            continue
        if path.stat().st_size > max_bytes:
            yield file_obj, None, "zu groß"
            continue
        yield file_obj, path, ""


def survey_body(
    body: OParlBody,
    *,
    limit: int | None = None,
    max_bytes: int = 80 * 1024 * 1024,
    max_pages: int = 300,
    sample_size: int = 0,
    progress: Any = None,
) -> MapSurvey:
    """Dateicache einer Kommune auswerten (ohne Netz, ohne Speichern)."""
    from insight_core.services.gazetteer import StreetGazetteer

    gazetteer = StreetGazetteer(body)
    survey = MapSurvey(sample_size=sample_size, street_directory=bool(gazetteer))
    analyzed = 0
    for file_obj, path, reason in cached_files(body, max_bytes=max_bytes):
        if limit is not None and analyzed >= limit:
            break
        if path is None:
            survey.skipped[reason] += 1
            continue
        started = time.perf_counter()
        try:
            document = analyze_pdf(path, max_pages=max_pages)
            records = list(_records(file_obj, document, gazetteer))
        except (DocumentError, OSError):
            survey.skipped["nicht lesbar"] += 1
            continue
        except Exception as exc:  # noqa: BLE001 – letzter Schutz: eine Datei darf den stundenlangen Lauf nicht beenden
            # Nur Datei und Fehlerart, kein Ausnahmetext (kann Inhalte aus dem PDF enthalten)
            logger.warning("Kartenanalyse: Datei %s übersprungen (%s)", file_obj.id, type(exc).__name__)
            survey.skipped["Analysefehler"] += 1
            continue
        analyzed += 1
        survey.add(document, records, time.perf_counter() - started)
        if progress is not None and analyzed % 500 == 0:
            progress(analyzed, survey)
    return survey


def _records(file_obj: OParlFile, document: DocumentAnalysis, gazetteer: Any) -> Iterable[PageRecord]:
    reference = (file_obj.paper.reference or "") if file_obj.paper_id and file_obj.paper else ""
    for page in document.pages:
        verdict = classify_page(page)
        streets: int | None = None
        # Straßennamen nur, wo sie das Ergebnis ändern können oder für die Kennzahl zählen
        if gazetteer and page.text_chars and (verdict.is_map or verdict.score == 2):
            streets = len(gazetteer.find_in_text(page.text))
            verdict = classify_page(page, street_count=streets)
        yield PageRecord(
            file_id=str(file_obj.id),
            reference=reference,
            file_name=(file_obj.file_name or file_obj.name or "")[:120],
            page=page,
            is_map=verdict.is_map,
            score=verdict.score,
            reasons=verdict.reasons,
            streets=streets,
        )


def write_sample(rows: list[dict[str, Any]], path: Path) -> None:
    """Stichprobe als CSV (Semikolon); Zellen gegen Formel-Injektion geschützt (Dateinamen aus dem RIS)."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv_safety.writer(handle, delimiter=";")
        writer.writerow(SAMPLE_FIELDS)
        writer.writerows([row.get(name, "") for name in SAMPLE_FIELDS] for row in rows)


@dataclass(frozen=True)
class Evaluation:
    labeled: int
    precision: float | None
    recall: float | None
    weighted_precision: float | None
    weighted_recall: float | None
    false_positives: list[str]
    false_negatives: list[str]


def _truthy(value: str) -> bool | None:
    text = (value or "").strip().lower()
    if text in ("ja", "j", "1", "x", "karte", "true"):
        return True
    if text in ("nein", "n", "0", "false", "keine"):
        return False
    return None


def evaluate_labels(path: Path) -> Evaluation:
    """Präzision und Trefferquote aus der von Hand ausgefüllten Stichprobe (Spalte ``label``: ja/nein)."""
    counts = {"tp": 0.0, "fp": 0.0, "fn": 0.0}
    weighted = {"tp": 0.0, "fp": 0.0, "fn": 0.0}
    labeled = 0
    false_positives: list[str] = []
    false_negatives: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            truth = _truthy(row.get("label", ""))
            if truth is None:
                continue
            labeled += 1
            predicted = _truthy(row.get("predicted", "")) is True
            try:
                weight = float(row.get("weight") or 1)
            except ValueError:
                weight = 1.0
            key = f"{row.get('file_id', '')} S. {row.get('page', '')}"
            outcome = "tp" if predicted and truth else "fp" if predicted else "fn" if truth else ""
            if outcome:
                counts[outcome] += 1
                weighted[outcome] += weight
            if outcome == "fp":
                false_positives.append(key)
            elif outcome == "fn":
                false_negatives.append(key)

    def ratio(part: float, rest: float) -> float | None:
        return round(part / (part + rest), 3) if part + rest else None

    return Evaluation(
        labeled=labeled,
        precision=ratio(counts["tp"], counts["fp"]),
        recall=ratio(counts["tp"], counts["fn"]),
        weighted_precision=ratio(weighted["tp"], weighted["fp"]),
        weighted_recall=ratio(weighted["tp"], weighted["fn"]),
        false_positives=false_positives,
        false_negatives=false_negatives,
    )
