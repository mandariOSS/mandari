# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gesamt-PDF der Sitzungsmappe (Issue #218).

Aufbau: Deckblatt, Inhaltsverzeichnis mit Sprungmarken, Einladung und Tagesordnung, je TOP mit
Unterlagen ein Trennblatt, die Vorlage und ihre Anlagen, am Ende weitere Unterlagen der Sitzung.
Die PDF-Lesezeichen bilden dieselbe Gliederung ab. Jede Seite außer dem Deckblatt trägt oben
rechts die fortlaufende Seitenzahl der Mappe; die Originalseitenzahlen der Anlagen bleiben
unberührt.

Werkzeuge: Deckblatt, Inhaltsverzeichnis, Trennblätter, Vorlagen und Verweisseiten rendert
``apps.common.pdf.html_to_pdf`` (xhtml2pdf); pypdf fügt zusammen und setzt Lesezeichen und
Sprungmarken. Die Einträge des Inhaltsverzeichnisses rendert xhtml2pdf als Link ``mappe:<n>``;
nach dem Zusammenfügen werden daraus Sprünge auf die Zielseite – so stammen die Klickflächen
aus dem Layout selbst.

Robustheit: Office-Dateien, verschlüsselte oder beschädigte PDFs und Anlagen über dem
Speicherbudget werden als Verweisseite aufgenommen – die Erzeugung scheitert nie an einer
einzelnen Anlage.

Speicher: Anlagen werden einzeln geöffnet, übernommen werden nur ihre Seitenobjekte (pypdf teilt
die Bytes der Datenströme zwischen Quelle und Ziel, Inhalte werden nicht dekodiert). Obergrenzen
(``SESSION_PACKAGE_MAX_EMBED_MB``, ``SESSION_PACKAGE_MAX_PAGES``) halten den Bedarf im Rahmen
des Containers.
"""

from __future__ import annotations

import io
import logging
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

from django.conf import settings
from django.template.loader import render_to_string
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NumberObject,
    PdfObject,
)
from reportlab.pdfbase.pdfmetrics import stringWidth

from apps.common.pdf import html_to_pdf
from apps.session.models import SessionFile
from apps.session.services import invitation_service
from apps.session.services.meeting_package_plan import (
    AGENDA_ZIP_PATH,
    MeetingPackagePlan,
    PlannedFile,
    PlannedPaper,
    PlannedTop,
)

logger = logging.getLogger(__name__)

#: Linkschema der Inhaltsverzeichnis-Einträge vor dem Umschreiben auf Sprungziele
LINK_PREFIX = "mappe:"
STAMP_FONT = "/MandariMappe"
STAMP_FONT_SIZE = 7
STAMP_MARGIN = 14  # pt vom oberen und rechten Rand
MAX_TOC_LEVEL = 4

#: Hinweistexte der Verweisseiten je Grund
REASONS = {
    "format": "Diese Anlage liegt im Format {ext} vor und wird nicht in die Gesamt-PDF umgewandelt.",
    "encrypted": "Diese PDF-Datei ist geschützt (verschlüsselt) und kann nicht eingebunden werden.",
    "damaged": "Diese PDF-Datei ist beschädigt oder konnte nicht gelesen werden.",
    "size": "Diese Anlage ist zu umfangreich, um sie in die Gesamt-PDF aufzunehmen.",
    "missing": "Die Datei dieser Anlage ist derzeit nicht verfügbar.",
}


def max_embed_bytes() -> int:
    """Obergrenze der eingebundenen PDF-Anlagen je Mappe (Bytes)."""
    return int(getattr(settings, "SESSION_PACKAGE_MAX_EMBED_MB", 200)) * 1024 * 1024


def max_pages() -> int:
    """Obergrenze der Seiten aus eingebundenen PDF-Anlagen je Mappe."""
    return int(getattr(settings, "SESSION_PACKAGE_MAX_PAGES", 3000))


@dataclass
class TocEntry:
    """Eintrag im Inhaltsverzeichnis und zugleich PDF-Lesezeichen."""

    title: str
    level: int
    parent: TocEntry | None = None
    note: str = ""
    #: erste Seite im Hauptteil (0-basiert, ohne Deckblatt und Inhaltsverzeichnis)
    page: int | None = None
    #: Sprungziel im Hauptteil: eigene Seite oder erste Seite eines Untereintrags
    target: int = 0
    children: list[TocEntry] = field(default_factory=list)


@dataclass
class PdfResult:
    """Ergebnis der PDF-Erzeugung; ``generated`` enthält die erzeugten Dokumente fürs ZIP-Paket."""

    page_count: int
    embedded: int
    referenced: int
    entries: list[TocEntry]
    front_pages: int
    generated: dict[str, bytes]


class PackagePdfBuilder:
    """Setzt das Gesamt-PDF einer Fassung zusammen (einmal verwenden)."""

    def __init__(self, plan: MeetingPackagePlan, *, version: int, as_of: datetime) -> None:
        self.plan = plan
        self.version = version
        self.as_of = as_of
        self.writer = PdfWriter()
        self.entries: list[TocEntry] = []
        self.generated: dict[str, bytes] = {}
        self.embedded = 0
        self.referenced = 0
        self._embedded_bytes = 0
        self._embedded_pages = 0
        self._paper_pdfs: dict[Any, bytes] = {}
        self._orphans = False
        self._stack = ExitStack()
        meeting = plan.meeting
        tenant = meeting.tenant
        self._context: dict[str, Any] = {
            "tenant": tenant,
            "meeting": meeting,
            "plan": plan,
            "variant_label": plan.variant_label,
            "is_internal": plan.is_internal,
            "version": version,
            "as_of": as_of,
            "generated_at": as_of,
            "address_lines": [line for line in (tenant.address or "").splitlines() if line.strip()],
            "reference_label": tenant.reference_label,
            "site_url": str(getattr(settings, "SITE_URL", "") or "").rstrip("/"),
        }

    # ------------------------------------------------------------------ Ablauf

    def build(self, target: Path) -> PdfResult:
        """Mappe erzeugen und nach ``target`` schreiben."""
        with self._stack:
            agenda_start = self._add_agenda()
            for section in self.plan.sections:
                if not section.tops:
                    continue
                section_entry = self._entry(section.title, 0)
                for top in section.tops:
                    self._add_top(top, section_entry, 1)
            if self.plan.meeting_files:
                files_entry = self._entry("Weitere Unterlagen zur Sitzung", 0)
                for planned in self.plan.meeting_files:
                    self._add_attachment(planned, files_entry, 1)
            self._resolve_targets(agenda_start)

            cover_pages, toc_pages = self._add_front_matter()
            front = cover_pages + toc_pages
            self._add_outline(cover_pages, front)
            self._link_toc(cover_pages, toc_pages, front)
            self._stamp_pages(first=cover_pages)
            self._finish_document()
            with open(target, "wb") as handle:
                self.writer.write(handle)
            page_count = len(self.writer.pages)
        return PdfResult(
            page_count=page_count,
            embedded=self.embedded,
            referenced=self.referenced,
            entries=self.entries,
            front_pages=front,
            generated=self.generated,
        )

    # ------------------------------------------------------------------ Bausteine

    def _entry(self, title: str, level: int, parent: TocEntry | None = None, note: str = "") -> TocEntry:
        entry = TocEntry(title=title, level=level, parent=parent, note=note)
        self.entries.append(entry)
        if parent is not None:
            parent.children.append(entry)
        return entry

    def _render(self, template: str, extra: dict[str, Any]) -> bytes:
        return html_to_pdf(render_to_string(template, {**self._context, **extra}))

    def _add_generated(self, pdf_bytes: bytes) -> int:
        start = len(self.writer.pages)
        self.writer.append(PdfReader(io.BytesIO(pdf_bytes)), import_outline=False)
        return start

    def _add_agenda(self) -> int:
        """Einladung und Tagesordnung – dasselbe Dokument wie der Einzel-Download."""
        pdf = invitation_service.build_agenda_pdf(self.plan.meeting, include_non_public=self.plan.is_internal)
        self.generated[AGENDA_ZIP_PATH] = pdf
        entry = self._entry("Einladung und Tagesordnung", 0)
        entry.page = self._add_generated(pdf)
        return entry.page

    def _add_top(self, top: PlannedTop, parent: TocEntry, level: int) -> None:
        item = top.item
        notes = []
        if item.is_supplementary:
            notes.append("Nachtrag")
        if item.is_withdrawn:
            notes.append("abgesetzt")
        elif not top.has_documents:
            notes.append("ohne Unterlagen")
        entry = self._entry(f"TOP {item.number} {item.name}", level, parent, note=", ".join(notes))
        if top.has_documents:
            entry.page = self._add_generated(self._render("session/pdf/package_top.html", {"top": top}))
            if top.paper is not None:
                self._add_paper(top.paper, entry, level + 1)
            for planned in top.files:
                self._add_attachment(planned, entry, level + 1)
        for child in top.children:
            self._add_top(child, entry, level + 1)

    def _add_paper(self, planned: PlannedPaper, parent: TocEntry, level: int) -> None:
        paper = planned.paper
        entry = self._entry(f"Vorlage {paper.display_reference}: {paper.name}", level, parent)
        pdf = self._paper_pdfs.get(paper.pk)
        if pdf is None:
            pdf = self._render("session/pdf/package_paper.html", {"paper": paper})
            self._paper_pdfs[paper.pk] = pdf
        self.generated[planned.document_zip_path] = pdf
        entry.page = self._add_generated(pdf)
        for attachment in planned.files:
            self._add_attachment(attachment, entry, level + 1)

    def _add_attachment(self, planned: PlannedFile, parent: TocEntry, level: int) -> None:
        session_file = planned.file
        entry = self._entry(f"Anlage: {session_file.name}", level, parent)
        start = len(self.writer.pages)
        reason = self._embed_pdf(session_file)
        if reason is None:
            entry.page = start
            self.embedded += 1
            return
        extension = PurePosixPath(session_file.name).suffix.lstrip(".").upper() or "unbekannt"
        entry.note = "Verweisseite"
        entry.page = self._add_generated(
            self._render(
                "session/pdf/package_reference.html",
                {
                    "file": session_file,
                    "zip_path": planned.zip_path,
                    "in_zip": reason != "missing",
                    "reason": REASONS[reason].format(ext=extension),
                    "context_title": parent.title,
                },
            )
        )
        self.referenced += 1

    def _embed_pdf(self, session_file: SessionFile) -> str | None:
        """PDF-Anlage anhängen; bei Problemen den Grund für die Verweisseite liefern."""
        name = session_file.name.lower()
        if not (name.endswith(".pdf") or session_file.mime_type == "application/pdf"):
            return "format"
        try:
            handle = self._stack.enter_context(session_file.file.storage.open(str(session_file.file.name), "rb"))
            size = int(handle.size or 0)
        except (OSError, ValueError):
            return "missing"
        if self._embedded_bytes + size > max_embed_bytes():
            return "size"
        try:
            if b"%PDF" not in handle.read(1024):
                return "damaged"
            handle.seek(0)
            reader = PdfReader(handle, strict=False)
            if reader.is_encrypted:
                return "encrypted"
            page_count = len(reader.pages)
        except Exception as exc:  # noqa: BLE001 — jede Lesestörung führt zur Verweisseite
            logger.warning("Sitzungsmappe: Anlage %s nicht lesbar (%s).", session_file.pk, type(exc).__name__)
            return "damaged"
        if page_count == 0:
            return "damaged"
        if self._embedded_pages + page_count > max_pages():
            return "size"
        start = len(self.writer.pages)
        try:
            # /AA (Seitenaktionen, z. B. Skripte beim Öffnen) nicht übernehmen
            self.writer.append(reader, import_outline=False, excluded_fields=["/AA"])
        except Exception:  # noqa: BLE001 — halb übernommene Anlage zurückrollen
            logger.warning("Sitzungsmappe: Anlage %s nicht übernehmbar.", session_file.pk, exc_info=True)
            pages: Any = self.writer.pages
            del pages[start:]
            self._orphans = True
            return "damaged"
        self._embedded_bytes += size
        self._embedded_pages += page_count
        return None

    def _resolve_targets(self, fallback: int) -> None:
        """Sprungziel je Eintrag: eigene Seite, sonst erste Seite darunter, sonst die Tagesordnung."""

        def resolve(entry: TocEntry) -> int | None:
            first = entry.page
            for child in entry.children:
                child_first = resolve(child)
                if first is None and child_first is not None:
                    first = child_first
            entry.target = first if first is not None else fallback
            return first

        for entry in self.entries:
            if entry.parent is None:
                resolve(entry)

    # ------------------------------------------------------------------ Deckblatt und Inhaltsverzeichnis

    def _add_front_matter(self) -> tuple[int, int]:
        """Deckblatt und Inhaltsverzeichnis erzeugen und vor den Hauptteil setzen."""
        counts = {
            "tops": sum(1 for _ in self.plan.all_tops()),
            "papers": sum(1 for _ in self.plan.all_papers()),
            "files": self.embedded + self.referenced,
            "referenced": self.referenced,
        }
        cover = self._render("session/pdf/package_cover.html", {"counts": counts})
        cover_pages = len(PdfReader(io.BytesIO(cover)).pages)

        # Die Seitenzahlen hängen von der Länge des Verzeichnisses ab: rendern, bis sie stabil ist
        toc_pages = 1
        toc = self._render_toc(offset=cover_pages + toc_pages)
        for _ in range(5):
            rendered_pages = len(PdfReader(io.BytesIO(toc)).pages)
            if rendered_pages == toc_pages:
                break
            toc_pages = rendered_pages
            toc = self._render_toc(offset=cover_pages + toc_pages)

        self.writer.merge(0, PdfReader(io.BytesIO(cover)), import_outline=False)
        self.writer.merge(cover_pages, PdfReader(io.BytesIO(toc)), import_outline=False)
        return cover_pages, toc_pages

    def _render_toc(self, offset: int) -> bytes:
        rows = [
            {
                "title": entry.title,
                "level": min(entry.level, MAX_TOC_LEVEL),
                "note": entry.note,
                "page": offset + entry.page + 1 if entry.page is not None else None,
                "href": f"{LINK_PREFIX}{index}",
            }
            for index, entry in enumerate(self.entries)
        ]
        return self._render("session/pdf/package_toc.html", {"rows": rows})

    def _add_outline(self, cover_pages: int, front: int) -> None:
        self.writer.add_outline_item("Deckblatt", 0)
        self.writer.add_outline_item("Inhaltsverzeichnis", cover_pages)
        outline: dict[int, Any] = {}
        for entry in self.entries:
            parent = outline.get(id(entry.parent)) if entry.parent is not None else None
            title = f"{entry.title} ({entry.note})" if entry.note else entry.title
            outline[id(entry)] = self.writer.add_outline_item(title, front + entry.target, parent=parent)

    def _link_toc(self, first: int, count: int, front: int) -> None:
        """Links ``mappe:<n>`` des Inhaltsverzeichnisses in Sprünge auf die Zielseite umschreiben."""
        for page_index in range(first, first + count):
            page = self.writer.pages[page_index]
            annots = page.get("/Annots")
            if annots is None:
                continue
            for ref in list(annots.get_object()):
                annot = ref.get_object()
                action = annot.get("/A")
                uri = action.get_object().get("/URI") if action is not None else None
                if not isinstance(uri, str) or not uri.startswith(LINK_PREFIX):
                    continue
                del annot[NameObject("/A")]
                try:
                    target = front + self.entries[int(uri[len(LINK_PREFIX) :])].target
                except (ValueError, IndexError):
                    continue
                annot[NameObject("/Dest")] = ArrayObject(
                    [self.writer.pages[target].indirect_reference, NameObject("/Fit")]
                )
                annot[NameObject("/Border")] = ArrayObject([NumberObject(0), NumberObject(0), NumberObject(0)])

    # ------------------------------------------------------------------ Paginierung und Abschluss

    def _stamp_pages(self, first: int) -> None:
        """Fortlaufende Seitenzahl der Mappe oben rechts auf jede Seite ab ``first``."""
        total = len(self.writer.pages)
        label = "Sitzungsmappe (nichtöffentlich)" if self.plan.is_internal else "Sitzungsmappe"
        # Eigene Inhaltsströme statt merge_page: merge_page dekodiert den Seiteninhalt und
        # schriebe ihn unkomprimiert zurück – eine Planzeichnung würde dabei um ein Vielfaches größer.
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
        font_ref = self.writer._add_object(font)
        push = self.writer._add_object(_stream(b"q\n"))
        pop = self.writer._add_object(_stream(b"\nQ\n"))
        for index in range(first, total):
            try:
                self._stamp_page(index, f"{label} – Seite {index + 1} von {total}", font_ref, push, pop)
            except Exception:  # noqa: BLE001 — eine Seite ohne Zählung ist besser als keine Mappe
                logger.warning("Sitzungsmappe: Seite %s nicht paginiert.", index + 1, exc_info=True)

    def _stamp_page(
        self, index: int, text: str, font_ref: IndirectObject, push: IndirectObject, pop: IndirectObject
    ) -> None:
        page = self.writer.pages[index]
        box = page.cropbox
        x0, y0 = float(box.left), float(box.bottom)
        width, height = float(box.right) - x0, float(box.top) - y0
        rotation = int(page.rotation or 0) % 360
        # Transformation von der sichtbaren (gedrehten) Seite in den Nutzerraum der Seite
        if rotation == 90:
            visible_w, visible_h, matrix = height, width, (0, 1, -1, 0, x0 + width, y0)
        elif rotation == 180:
            visible_w, visible_h, matrix = width, height, (-1, 0, 0, -1, x0 + width, y0 + height)
        elif rotation == 270:
            visible_w, visible_h, matrix = height, width, (0, -1, 1, 0, x0, y0 + height)
        else:
            visible_w, visible_h, matrix = width, height, (1, 0, 0, 1, x0, y0)
        text_x = visible_w - STAMP_MARGIN - stringWidth(text, "Helvetica", STAMP_FONT_SIZE)
        text_y = visible_h - STAMP_MARGIN
        encoded = text.encode("cp1252", "replace").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        content = (
            b"q %g %g %g %g %.2f %.2f cm BT %s %d Tf 0.35 g %.2f %.2f Td ("
            % (*matrix, STAMP_FONT.encode("ascii"), STAMP_FONT_SIZE, text_x, text_y)
            + encoded
            + b") Tj ET Q\n"
        )
        stamp_ref = self.writer._add_object(_stream(content))

        resources = page.get("/Resources")
        if resources is None:
            resources = DictionaryObject()
            page[NameObject("/Resources")] = resources
        resources_dict = cast(DictionaryObject, resources.get_object())
        fonts = resources_dict.get("/Font")
        if fonts is None:
            fonts = DictionaryObject()
            resources_dict[NameObject("/Font")] = fonts
        cast(DictionaryObject, fonts.get_object())[NameObject(STAMP_FONT)] = font_ref

        contents: list[PdfObject] = [push]
        existing = page.get("/Contents")
        if existing is not None:
            resolved = existing.get_object()
            if isinstance(resolved, ArrayObject):
                contents.extend(resolved)
            else:
                contents.append(existing)
        contents.extend([pop, stamp_ref])
        page[NameObject("/Contents")] = ArrayObject(contents)

    def _finish_document(self) -> None:
        meeting = self.plan.meeting
        self.writer.add_metadata(
            {
                "/Title": f"Sitzungsmappe {meeting.name} ({self.plan.variant_label}, Fassung {self.version})",
                "/Author": meeting.tenant.name,
                "/Subject": f"{meeting.organization.name}, Sitzung am {meeting.start:%d.%m.%Y}",
                "/Creator": "mandari",
            }
        )
        self.writer.page_mode = "/UseOutlines"
        if self._orphans:
            # Reste zurückgerollter Anlagen nicht mitschreiben
            self.writer.compress_identical_objects(remove_identicals=False, remove_orphans=True)


def _stream(data: bytes) -> DecodedStreamObject:
    stream = DecodedStreamObject()
    stream.set_data(data)
    return stream


def build_pdf(plan: MeetingPackagePlan, *, version: int, as_of: datetime, target: Path) -> PdfResult:
    """Gesamt-PDF einer Fassung erzeugen (siehe Modul-Docstring)."""
    return PackagePdfBuilder(plan, version=version, as_of=as_of).build(target)
