# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsmappe als Gesamtdokument (Issue #218).

Entlang der Akzeptanzkriterien:
- 20 TOPs mit 60 Anlagen: Das Gesamt-PDF entsteht, Inhaltsverzeichnis-Links und Lesezeichen
  springen auf die richtige Seite, jede Seite trägt die fortlaufende Seitenzahl.
- Die öffentliche Fassung enthält keinen nichtöffentlichen Inhalt – weder im PDF-Text noch in
  Lesezeichen, ZIP-Namen oder ZIP-Inhalten (analog zur Sicherheitsmatrix); die NÖ-Fassung gibt
  es nur für Berechtigte.
- Ein Nachtrag erzeugt eine neue Fassung mit Stand; die ältere bleibt abrufbar, wird aber
  gesperrt, sobald ein enthaltener Teil gelöscht oder nichtöffentlich wird.
- Das ZIP-Paket trägt Umlaute und Sonderzeichen korrekt (UTF-8-Flag), Pfade sind bereinigt.
- Die Erzeugung läuft im Hintergrund: Anfordern und Statusabfrage erzeugen nichts, das
  Management-Command erzeugt; das Status-Fragment fragt während der Erstellung neu ab.
Dazu Verweisseiten (Office, verschlüsselt, beschädigt), Download-Protokoll und Tenant-Isolation.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from pypdf import PdfReader, PdfWriter
from pypdf.generic import Destination
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionAuditLog,
    SessionFile,
    SessionMeeting,
    SessionMeetingPackage,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import meeting_package_service as service
from apps.session.services.meeting_package_pdf import build_pdf
from apps.session.services.meeting_package_plan import (
    INTERNAL,
    PUBLIC,
    build_plan,
    safe_component,
    variants_for,
)

pytestmark = pytest.mark.django_db

Package = SessionMeetingPackage
SECRET = "GEHEIM"
ALL_PERMS = {f.name[4:] for f in SessionRole._meta.get_fields() if f.name.startswith("can_")}


# =============================================================================
# Hilfsfunktionen
# =============================================================================


def _pdf(label: str, pages: int = 1, *, rotate: int = 0, quer: bool = False) -> bytes:
    """Kleines PDF, dessen Seiten „<label> Seite n“ tragen."""
    buffer = io.BytesIO()
    doc = canvas.Canvas(buffer, pagesize=landscape(A4) if quer else A4)
    for number in range(1, pages + 1):
        doc.drawString(72, 400, f"{label} Seite {number}")
        doc.showPage()
    doc.save()
    data = buffer.getvalue()
    if not rotate:
        return data
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(data)).pages:
        page.rotate(rotate)
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _encrypted_pdf(label: str) -> bytes:
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_pdf(label))))
    writer.encrypt("kennwort")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _attach(tenant: SessionTenant, name: str, data: bytes, *, public: bool = True, **parent: Any) -> SessionFile:
    mime = "application/pdf" if name.lower().endswith(".pdf") else "application/octet-stream"
    return SessionFile.objects.create(
        tenant=tenant,
        name=name,
        file=SimpleUploadedFile(name, data),
        mime_type=mime,
        size=len(data),
        is_public=public,
        **parent,
    )


def _build(meeting: SessionMeeting, variant: str) -> Package:
    package, _ = service.request_package(meeting, variant, None)
    service.process_requested()
    package.refresh_from_db()
    assert package.status == Package.STATUS_READY, package.error
    return package


def _pdf_of(package: Package) -> PdfReader:
    return PdfReader(io.BytesIO(Path(package.pdf_file.path).read_bytes()))


def _texts(reader: PdfReader) -> list[str]:
    return [page.extract_text() or "" for page in reader.pages]


def _outline(reader: PdfReader) -> list[tuple[str, int]]:
    """Lesezeichen flach: (Titel, Seitenindex)."""
    result: list[tuple[str, int]] = []

    def walk(items: list[Any]) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item)
            else:
                destination = cast(Destination, item)
                result.append((str(destination.title), cast(int, reader.get_destination_page_number(destination))))

    walk(cast(list[Any], reader.outline))
    return result


def _zip_of(package: Package) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(Path(package.zip_file.path).read_bytes()))


def _client(tenant: SessionTenant, name: str, perms: set[str], *, admin: bool = False) -> Client:
    flags = {f"can_{perm}": perm in perms for perm in ALL_PERMS}
    role = SessionRole.objects.create(tenant=tenant, name=f"rolle-{name}", is_admin=admin, **flags)
    user = cast(User, UserFactory(email=f"{name}@example.org"))  # type: ignore[no-untyped-call]
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    client = Client()
    client.force_login(user)
    return client


def _download(client: Client, url: str) -> tuple[int, bytes]:
    response = cast(Any, client.get(url))
    try:
        body = b"".join(cast(Iterator[bytes], response.streaming_content)) if response.streaming else response.content
    finally:
        response.close()
    return response.status_code, body


# =============================================================================
# Testwelt: Sitzung mit öffentlichem und nichtöffentlichem Teil
# =============================================================================


@dataclass
class Welt:
    tenant: SessionTenant
    org: SessionOrganization
    meeting: SessionMeeting
    paper: SessionPaper
    top1: SessionAgendaItem
    plan_pdf: SessionFile

    @property
    def base(self) -> str:
        return f"/session/{self.tenant.slug}/meetings/{self.meeting.pk}/mappe/"


@pytest.fixture
def media(settings: Any, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


@pytest.fixture
def welt(media: Path) -> Welt:
    tenant = SessionTenant.objects.create(
        name="Stadt Musterstadt", slug="musterstadt", address="Rathausplatz 1\n12345 Musterstadt"
    )
    org = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss", short_name="HA")
    meeting = SessionMeeting.objects.create(
        tenant=tenant,
        name="3. Sitzung des Hauptausschusses",
        organization=org,
        start=timezone.now() + timedelta(days=7),
        location="Rathaus",
        room="Ratssaal",
        is_public=True,
    )
    paper = SessionPaper.objects.create(
        tenant=tenant,
        reference="V/2026/0001",
        name="Radweg Hauptstraße",
        main_text="Sachverhalt zum Radweg",
        resolution_text="Der Ausschuss beschließt den Radweg.",
        is_public=True,
        status="approved",
    )
    paper_np = SessionPaper.objects.create(
        tenant=tenant, reference="V/2026/0002", name="GEHEIM-VORLAGE Grundstück", main_text="GEHEIM-SACHVERHALT"
    )
    paper_np.is_public = False
    paper_np.save()
    top1 = SessionAgendaItem.objects.create(
        meeting=meeting, number="1", name="Radweg Hauptstraße", order=1, paper=paper
    )
    SessionAgendaItem.objects.create(meeting=meeting, number="2", name="Mitteilungen", order=2)
    top_np = SessionAgendaItem.objects.create(
        meeting=meeting, number="N1", name="GEHEIM-TOP Grundstück", order=3, is_public=False, paper=paper_np
    )
    plan_pdf = _attach(tenant, "Plan.pdf", _pdf("PLAN", 2), paper=paper)
    _attach(tenant, "GEHEIM-anlage.pdf", _pdf("GEHEIM-ANLAGE"), public=False, paper=paper)
    _attach(tenant, "Stellungnahme der Ämter.docx", b"PK\x03\x04 office", agenda_item=top1)
    _attach(tenant, "GEHEIM-vorlagenanlage.pdf", _pdf("GEHEIM-VORLAGENANLAGE"), paper=paper_np)
    _attach(tenant, "GEHEIM-topanlage.pdf", _pdf("GEHEIM-TOPANLAGE"), agenda_item=top_np)
    _attach(tenant, "Teilnehmerliste.pdf", _pdf("TEILNEHMERLISTE"), meeting=meeting)
    _attach(tenant, "GEHEIM-sitzungsnotiz.pdf", _pdf("GEHEIM-NOTIZ"), public=False, meeting=meeting)
    return Welt(tenant=tenant, org=org, meeting=meeting, paper=paper, top1=top1, plan_pdf=plan_pdf)


# =============================================================================
# Ö/NÖ-Sichtbarkeit
# =============================================================================


class TestSichtbarkeit:
    def test_oeffentliche_fassung_ohne_noe_inhalte(self, welt: Welt) -> None:
        package = _build(welt.meeting, PUBLIC)
        reader = _pdf_of(package)
        text = "\n".join(_texts(reader))
        assert SECRET not in text, "Öffentliche Mappe enthält NÖ-Text"
        assert all(SECRET not in title for title, _ in _outline(reader)), "NÖ-Lesezeichen in öffentlicher Mappe"
        for marker in ("Radweg Hauptstraße", "PLAN Seite 2", "TEILNEHMERLISTE", "Stellungnahme der Ämter.docx"):
            assert marker in text, f"öffentlicher Inhalt fehlt: {marker}"

        archive = _zip_of(package)
        assert all(SECRET not in name for name in archive.namelist()), archive.namelist()
        for name in archive.namelist():
            data = archive.read(name)
            if name.endswith(".pdf"):
                inhalt = "\n".join(_texts(PdfReader(io.BytesIO(data))))
                assert SECRET not in inhalt, f"NÖ-Inhalt in {name}"
        assert package.contents["meeting_public"] is True

    def test_noe_fassung_enthaelt_alles(self, welt: Welt) -> None:
        package = _build(welt.meeting, INTERNAL)
        text = "\n".join(_texts(_pdf_of(package)))
        for marker in (
            "GEHEIM-TOP",
            "GEHEIM-SACHVERHALT",
            "GEHEIM-ANLAGE",
            "GEHEIM-VORLAGENANLAGE",
            "GEHEIM-TOPANLAGE",
            "GEHEIM-NOTIZ",
            "nichtöffentlich",
        ):
            assert marker in text, f"NÖ-Inhalt fehlt: {marker}"
        names = _zip_of(package).namelist()
        assert "N1 GEHEIM-TOP Grundstück/V-2026-0002/GEHEIM-vorlagenanlage.pdf" in names

    def test_plan_nutzt_die_anlagenregel(self, welt: Welt) -> None:
        plan = build_plan(welt.meeting, PUBLIC)
        names = {planned.file.name for planned in plan.all_files()}
        assert names == {"Plan.pdf", "Stellungnahme der Ämter.docx", "Teilnehmerliste.pdf"}
        assert [section.title for section in plan.sections] == ["Öffentlicher Teil"]
        intern = build_plan(welt.meeting, INTERNAL)
        assert len({planned.file.name for planned in intern.all_files()}) == 7

    def test_noe_sitzung_hat_keine_oeffentliche_fassung(self, welt: Welt) -> None:
        welt.meeting.is_public = False
        welt.meeting.save()
        assert variants_for({"view_meetings", "view_papers"}, welt.meeting) == []
        alle = {"view_meetings", "view_papers", "view_non_public_meetings", "view_non_public_papers"}
        assert variants_for(alle, welt.meeting) == [INTERNAL]
        # Ohne NÖ-Recht an Vorlagen gibt es keine NÖ-Fassung
        assert variants_for(alle - {"view_non_public_papers"}, welt.meeting) == []


# =============================================================================
# Akzeptanz: 20 TOPs, 60 Anlagen
# =============================================================================


@pytest.fixture
def grosse_sitzung(media: Path) -> SessionMeeting:
    tenant = SessionTenant.objects.create(name="Stadt Großstadt", slug="grossstadt")
    org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    meeting = SessionMeeting.objects.create(
        tenant=tenant, name="12. Sitzung des Rates", organization=org, start=timezone.now(), is_public=True
    )
    for n in range(1, 21):
        paper = SessionPaper.objects.create(
            tenant=tenant,
            reference=f"V/2026/{n:04d}",
            name=f"Vorlage Nummer {n}",
            main_text=f"Sachverhalt der Vorlage {n}",
            is_public=True,
            status="approved",
        )
        SessionAgendaItem.objects.create(
            meeting=meeting, number=str(n), name=f"Beratungspunkt {n}", order=n, paper=paper
        )
        for k in range(1, 4):
            label = f"ANLAGE-{n:02d}-{k}"
            data = _pdf(label, pages=1 + (n + k) % 2, rotate=90 if k == 3 and n % 5 == 0 else 0, quer=k == 2)
            _attach(tenant, f"anlage-{n:02d}-{k}.pdf", data, paper=paper)
    return meeting


def _erwartet(title: str) -> str:
    """Regulärer Ausdruck für den Text, der auf der Zielseite eines Eintrags stehen muss."""
    title = title.split(" (")[0]
    if match := re.fullmatch(r"Anlage: anlage-(\d\d-\d)\.pdf", title):
        return rf"ANLAGE-{match.group(1)} Seite 1\b"
    if match := re.fullmatch(r"Vorlage V/2026/\d{4}: Vorlage Nummer (\d+)", title):
        return rf"Sachverhalt der Vorlage {match.group(1)}\b"
    if match := re.fullmatch(r"TOP (\d+) Beratungspunkt \d+", title):
        return rf"TOP {match.group(1)}\b"
    return {
        "Öffentlicher Teil": r"TOP 1\b",
        "Einladung und Tagesordnung": "Einladung",
        "Deckblatt": "Sitzungsmappe",
        "Inhaltsverzeichnis": "Inhaltsverzeichnis",
    }[title]


class TestGrosseMappe:
    def test_20_tops_60_anlagen_inhaltsverzeichnis_und_lesezeichen(self, grosse_sitzung: SessionMeeting) -> None:
        package = _build(grosse_sitzung, PUBLIC)
        assert package.embedded_count == 60
        assert package.referenced_count == 0
        reader = _pdf_of(package)
        texts = _texts(reader)
        total = len(reader.pages)
        assert package.page_count == total

        # Fortlaufende Seitenzählung auf jeder Seite außer dem Deckblatt, Originalseiten bleiben
        for index in range(1, total):
            assert f"Seite {index + 1} von {total}" in texts[index], f"Seite {index + 1} ohne Mappenzählung"
        assert "ANLAGE-07-2 Seite 2" in "\n".join(texts)

        # Lesezeichen: TOPs, Vorlagen und alle 60 Anlagen springen auf die richtige Seite
        outline = _outline(reader)
        anlagen = [(t, p) for t, p in outline if t.startswith("Anlage: ")]
        assert len(anlagen) == 60
        assert len([t for t, _ in outline if t.startswith("TOP ")]) == 20
        assert len([t for t, _ in outline if t.startswith("Vorlage ")]) == 20
        for title, page in outline:
            assert re.search(_erwartet(title), texts[page]), f"Lesezeichen „{title}“ zeigt auf Seite {page + 1}"

        # Inhaltsverzeichnis: jede Zeile verlinkt; Titel-Links führen zum Inhalt, Seitenzahl-Links zur Zahl
        bookmarks = dict(outline)
        toc_pages = range(bookmarks["Inhaltsverzeichnis"], bookmarks["Einladung und Tagesordnung"])
        assert len(toc_pages) >= 2, "Inhaltsverzeichnis mit über 100 Einträgen passt nicht auf eine Seite"
        geprueft = 0
        for index in toc_pages:
            toc_page = reader.pages[index]
            fragments: list[tuple[str, float, float]] = []

            def visit(text: str, cm: Any, tm: Any, font: Any, size: Any, sammlung: list[Any] = fragments) -> None:
                if text.strip():
                    sammlung.append((text.strip(), tm[4] * cm[0] + cm[4], tm[5] * cm[3] + cm[5]))

            toc_page.extract_text(visitor_text=visit)
            for ref in cast(list[Any], toc_page.get("/Annots", [])):
                annot = ref.get_object()
                assert "/A" not in annot, "Link ohne Sprungziel übrig"
                x0, y0, _, y1 = (float(v) for v in annot["/Rect"])
                target = cast(int, reader.get_page_number(annot["/Dest"][0].get_object()))
                label = next(text for text, x, y in fragments if y0 <= y <= y1 and abs(x - x0) < 2)
                if label.isdigit():
                    assert int(label) == target + 1, f"Seitenzahl {label} verlinkt Seite {target + 1}"
                else:
                    assert re.search(_erwartet(label), texts[target]), f"„{label}“ verlinkt Seite {target + 1}"
                geprueft += 1
        # je Eintrag ein Titel-Link, je Eintrag mit Seite ein Zahlen-Link
        assert geprueft >= 2 * (20 + 20 + 60)

    def test_pdf_waechst_nicht_durch_paginierung(self, grosse_sitzung: SessionMeeting) -> None:
        """Die Zählung hängt eigene Inhaltsströme an, statt Seiteninhalte zu dekodieren."""
        package = _build(grosse_sitzung, PUBLIC)
        anlagen = sum(f.size for f in SessionFile.objects.filter(tenant=grosse_sitzung.tenant))
        assert package.pdf_size < anlagen * 3 + 600_000


# =============================================================================
# Verweisseiten
# =============================================================================


class TestVerweisseiten:
    def test_office_verschluesselt_beschaedigt_werden_verweisseite(self, welt: Welt) -> None:
        _attach(welt.tenant, "geschuetzt.pdf", _encrypted_pdf("SCHUTZ"), paper=welt.paper)
        _attach(welt.tenant, "kaputt.pdf", b"%PDF-1.7\nkein echtes pdf", paper=welt.paper)
        _attach(welt.tenant, "abgeschnitten.pdf", _pdf("ABGESCHNITTEN")[:300], paper=welt.paper)
        package = _build(welt.meeting, PUBLIC)
        # Office + drei defekte/geschützte PDFs; Plan.pdf und Teilnehmerliste eingebunden
        assert package.referenced_count == 4
        assert package.embedded_count == 2
        texts = _texts(_pdf_of(package))
        verweise = [t for t in texts if "Verweisseite" in t.split("\n")[0] or "VERWEISSEITE" in t]
        assert len(verweise) == 4
        joined = "\n".join(verweise)
        assert "Format DOCX" in joined
        assert "geschützt (verschlüsselt)" in joined
        assert "beschädigt" in joined
        assert "SCHUTZ Seite" not in "\n".join(texts)
        # Im ZIP-Paket liegen alle im Original
        names = _zip_of(package).namelist()
        assert "1 Radweg Hauptstraße/V-2026-0001/geschuetzt.pdf" in names
        assert "1 Radweg Hauptstraße/Stellungnahme der Ämter.docx" in names

    def test_speicherbudget_fuehrt_zur_verweisseite(self, welt: Welt, settings: Any) -> None:
        settings.SESSION_PACKAGE_MAX_EMBED_MB = 0
        package = _build(welt.meeting, PUBLIC)
        assert package.embedded_count == 0
        assert "zu umfangreich" in "\n".join(_texts(_pdf_of(package)))


# =============================================================================
# Fassungen
# =============================================================================


class TestFassungen:
    def test_gleicher_inhalt_gleiche_fassung(self, welt: Welt) -> None:
        erste = _build(welt.meeting, PUBLIC)
        package, created = service.request_package(welt.meeting, PUBLIC, None)
        assert (package.pk, created) == (erste.pk, False)

    def test_nachtrag_erzeugt_neue_fassung_mit_stand(self, welt: Welt) -> None:
        erste = _build(welt.meeting, PUBLIC)
        SessionAgendaItem.objects.create(
            meeting=welt.meeting, number="3", name="Dringlichkeitsantrag Spielplatz", order=4, is_supplementary=True
        )
        zweite, created = service.request_package(welt.meeting, PUBLIC, None)
        assert created and zweite.version == 2
        zweite = _build(welt.meeting, PUBLIC)
        assert zweite.version == 2
        assert zweite.content_as_of is not None and erste.content_as_of is not None
        assert zweite.content_as_of >= erste.content_as_of
        text = "\n".join(_texts(_pdf_of(zweite)))
        assert "Fassung 2" in text and "Dringlichkeitsantrag Spielplatz" in text and "Nachtrag" in text
        # Die ältere Fassung bleibt abrufbar
        erste.refresh_from_db()
        assert erste.status == Package.STATUS_READY
        assert service.blocked_packages([erste, zweite]) == set()

    def test_fehlgeschlagene_fassung_wird_wiederverwendet(self, welt: Welt) -> None:
        package, _ = service.request_package(welt.meeting, PUBLIC, None)
        Package.objects.filter(pk=package.pk).update(status=Package.STATUS_FAILED, error="x")
        again, created = service.request_package(welt.meeting, PUBLIC, None)
        assert created and again.pk == package.pk and again.version == 1
        assert again.status == Package.STATUS_REQUESTED and again.error == ""

    def test_aeltere_fassung_gesperrt_wenn_anlage_nichtoeffentlich_wird(self, welt: Welt) -> None:
        public = _build(welt.meeting, PUBLIC)
        internal = _build(welt.meeting, INTERNAL)
        welt.plan_pdf.is_public = False
        welt.plan_pdf.save()
        public.refresh_from_db()
        internal.refresh_from_db()
        assert service.blocked_packages([public, internal]) == {public.pk}

    def test_aeltere_fassung_gesperrt_wenn_anlage_geloescht_wird(self, welt: Welt) -> None:
        internal = _build(welt.meeting, INTERNAL)
        welt.plan_pdf.delete()
        internal.refresh_from_db()
        assert service.blocked_packages([internal]) == {internal.pk}

    def test_fassung_gesperrt_wenn_top_nichtoeffentlich_wird(self, welt: Welt) -> None:
        public = _build(welt.meeting, PUBLIC)
        SessionAgendaItem.objects.filter(pk=welt.top1.pk).update(is_public=False)
        public.refresh_from_db()
        assert service.blocked_packages([public]) == {public.pk}

    def test_dateien_verschwinden_mit_der_fassung(self, welt: Welt, django_capture_on_commit_callbacks: Any) -> None:
        package = _build(welt.meeting, PUBLIC)
        pfade = [Path(package.pdf_file.path), Path(package.zip_file.path)]
        assert all(p.exists() for p in pfade)
        with django_capture_on_commit_callbacks(execute=True):
            welt.meeting.delete()
        assert not any(p.exists() for p in pfade)


# =============================================================================
# ZIP-Paket
# =============================================================================


class TestZip:
    def test_umlaute_sonderzeichen_und_bereinigte_pfade(self, welt: Welt) -> None:
        SessionAgendaItem.objects.filter(pk=welt.top1.pk).update(name="Haushalt 2026/2027: Änderungen?")
        _attach(welt.tenant, "Ärger über Öffnungszeiten <Entwurf>.pdf", _pdf("AERGER"), paper=welt.paper)
        _attach(welt.tenant, "..\\..\\boese.pdf", _pdf("BOESE"), paper=welt.paper)
        _attach(welt.tenant, "Plan.PDF", _pdf("ZWEITER-PLAN"), paper=welt.paper)
        package = _build(welt.meeting, PUBLIC)
        archive = _zip_of(package)
        names = archive.namelist()
        ordner = "1 Haushalt 2026-2027_ Änderungen_/V-2026-0001"
        assert f"{ordner}/Ärger über Öffnungszeiten _Entwurf_.pdf" in names
        assert f"{ordner}/Vorlage V-2026-0001.pdf" in names
        # Namensgleichheit ohne Rücksicht auf Groß-/Kleinschreibung aufgelöst (sortiert nach Name)
        assert f"{ordner}/Plan.PDF" in names
        assert f"{ordner}/Plan (2).pdf" in names
        assert "Einladung und Tagesordnung.pdf" in names
        assert "Weitere Unterlagen/Teilnehmerliste.pdf" in names
        for info in archive.infolist():
            assert not info.filename.startswith("/") and "\\" not in info.filename
            assert ".." not in info.filename.split("/"), info.filename
            if not info.filename.isascii():
                assert info.flag_bits & 0x800, f"UTF-8-Flag fehlt: {info.filename}"
        assert archive.read(f"{ordner}/Plan (2).pdf") == Path(welt.plan_pdf.file.path).read_bytes()
        assert archive.testzip() is None

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Plan.pdf", "Plan.pdf"),
            ("../../etc/passwd", "-..-etc-passwd"),
            ("CON.pdf", "_CON.pdf"),
            ("   ...   ", "Unbenannt"),
            ("Tab\tund\nZeile.txt", "Tab und Zeile.txt"),
            ("Straße.docx", "Straße.docx"),
        ],
    )
    def test_safe_component(self, name: str, expected: str) -> None:
        assert safe_component(name) == expected

    def test_lange_namen_behalten_endung(self) -> None:
        result = safe_component("x" * 300 + ".pdf")
        assert len(result) <= 80 and result.endswith(".pdf")


# =============================================================================
# Hintergrund-Erzeugung (Management-Command)
# =============================================================================


class TestHintergrund:
    def test_anfordern_erzeugt_nichts_das_command_erzeugt(self, welt: Welt) -> None:
        package, created = service.request_package(welt.meeting, PUBLIC, None)
        assert created and package.status == Package.STATUS_REQUESTED and not package.pdf_file
        out = io.StringIO()
        call_command("build_meeting_packages", stdout=out)
        package.refresh_from_db()
        assert package.status == Package.STATUS_READY
        assert package.pdf_file and package.zip_file and package.page_count > 0
        assert "Erzeugt: 1" in out.getvalue()

    def test_fehler_wird_vermerkt_und_lauf_geht_weiter(self, welt: Welt, monkeypatch: pytest.MonkeyPatch) -> None:
        public, _ = service.request_package(welt.meeting, PUBLIC, None)
        internal, _ = service.request_package(welt.meeting, INTERNAL, None)
        calls: list[str] = []
        original = build_pdf

        def flaky(plan: Any, **kwargs: Any) -> Any:
            calls.append(plan.variant)
            if plan.variant == PUBLIC:
                raise RuntimeError("kaputt")
            return original(plan, **kwargs)

        monkeypatch.setattr(service, "build_pdf", flaky)
        result = service.process_requested()
        assert (result.built, result.failed) == (1, 1)
        public.refresh_from_db()
        internal.refresh_from_db()
        assert public.status == Package.STATUS_FAILED and "RuntimeError" in public.error
        assert internal.status == Package.STATUS_READY

    def test_waehrend_der_erzeugung_geloescht_hinterlaesst_keine_dateien(
        self, welt: Welt, media: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        package, _ = service.request_package(welt.meeting, INTERNAL, None)

        def build_and_delete(plan: Any, **kwargs: Any) -> Any:
            result = build_pdf(plan, **kwargs)
            Package.objects.filter(pk=package.pk).delete()
            return result

        monkeypatch.setattr(service, "build_pdf", build_and_delete)
        result = service.process_requested()
        assert (result.built, result.failed) == (0, 1)
        mappen = media / "session" / "files" / "mappen"
        assert not [p for p in mappen.rglob("*") if p.is_file()], "verwaiste Mappen-Dateien"

    def test_abgebrochene_erzeugung_wird_neu_eingereiht(self, welt: Welt) -> None:
        package, _ = service.request_package(welt.meeting, PUBLIC, None)
        vor_zwei_stunden = timezone.now() - timedelta(hours=2)
        Package.objects.filter(pk=package.pk).update(
            status=Package.STATUS_BUILDING, started_at=vor_zwei_stunden, attempts=1
        )
        assert service.reset_stale() == 1
        package.refresh_from_db()
        assert package.status == Package.STATUS_REQUESTED
        Package.objects.filter(pk=package.pk).update(
            status=Package.STATUS_BUILDING, started_at=vor_zwei_stunden, attempts=service.MAX_ATTEMPTS
        )
        service.reset_stale()
        package.refresh_from_db()
        assert package.status == Package.STATUS_FAILED

    def test_uebernahme_nur_einmal(self, welt: Welt) -> None:
        package, _ = service.request_package(welt.meeting, PUBLIC, None)
        assert service.claim(package.pk) is not None
        assert service.claim(package.pk) is None


# =============================================================================
# Oberfläche: Rechte, Polling, Download-Protokoll
# =============================================================================

OEFFENTLICH = {"view_meetings", "view_papers"}


class TestOberflaeche:
    def test_status_und_anforderung_blockieren_nicht(self, welt: Welt) -> None:
        admin = _client(welt.tenant, "admin", set(), admin=True)
        response = admin.get(welt.base)
        assert response.status_code == 200
        body = response.content.decode()
        assert "Öffentliche Fassung" in body and "Nichtöffentliche Fassung" in body
        assert "Sitzungsmappe erstellen" in body and "every" not in body

        response = admin.post(welt.base + "anfordern/", {"variant": PUBLIC}, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        body = response.content.decode()
        assert 'hx-trigger="every 5s"' in body and "wird erstellt" in body
        package = Package.objects.get(meeting=welt.meeting, variant=PUBLIC)
        assert package.status == Package.STATUS_REQUESTED, "Seitenaufruf hat die Mappe erzeugt"
        # Doppelte Anforderung legt keine zweite Fassung an
        admin.post(welt.base + "anfordern/", {"variant": PUBLIC})
        assert Package.objects.filter(meeting=welt.meeting).count() == 1

        service.process_requested()
        body = admin.get(welt.base).content.decode()
        assert "every" not in body and "PDF (" in body and "ZIP (" in body and "Fassung 1" in body

    def test_hinweis_bei_geaenderten_unterlagen(self, welt: Welt) -> None:
        admin = _client(welt.tenant, "admin", set(), admin=True)
        _build(welt.meeting, PUBLIC)
        assert "Unterlagen geändert" not in admin.get(welt.base).content.decode()
        _attach(welt.tenant, "Nachgereicht.pdf", _pdf("NACHGEREICHT"), paper=welt.paper)
        body = admin.get(welt.base).content.decode()
        assert "Seit dieser Fassung wurden Unterlagen geändert." in body and "Neue Fassung erstellen" in body

    def test_rechte_je_fassung(self, welt: Welt) -> None:
        public = _build(welt.meeting, PUBLIC)
        internal = _build(welt.meeting, INTERNAL)
        leser = _client(welt.tenant, "leser", OEFFENTLICH)
        body = leser.get(welt.base).content.decode()
        assert "Öffentliche Fassung" in body and "Nichtöffentliche Fassung" not in body
        assert leser.post(welt.base + "anfordern/", {"variant": INTERNAL}).status_code == 403
        assert _download(leser, f"{welt.base}{internal.pk}/pdf/")[0] == 403
        assert _download(leser, f"{welt.base}{internal.pk}/zip/")[0] == 403
        status, body_bytes = _download(leser, f"{welt.base}{public.pk}/pdf/")
        assert status == 200 and body_bytes.startswith(b"%PDF")

        nur_sitzungen = _client(welt.tenant, "sitzungen", {"view_meetings"})
        assert nur_sitzungen.get(welt.base).status_code == 403
        assert _download(nur_sitzungen, f"{welt.base}{public.pk}/pdf/")[0] == 403

    def test_noe_sitzung_ohne_recht_ist_404(self, welt: Welt) -> None:
        welt.meeting.is_public = False
        welt.meeting.save()
        leser = _client(welt.tenant, "leser", OEFFENTLICH)
        assert leser.get(welt.base).status_code == 404
        assert leser.post(welt.base + "anfordern/", {"variant": PUBLIC}).status_code == 404

    def test_download_wird_protokolliert(self, welt: Welt) -> None:
        package = _build(welt.meeting, INTERNAL)
        admin = _client(welt.tenant, "admin", set(), admin=True)
        status, body = _download(admin, f"{welt.base}{package.pk}/zip/")
        assert status == 200 and body.startswith(b"PK")
        entry = SessionAuditLog.objects.get(action="download", model_name="SessionMeetingPackage")
        assert entry.object_id == package.pk
        assert entry.user is not None and entry.user.user.email == "admin@example.org"
        assert entry.changes["fassung"] == 1
        assert entry.changes["variante"] == "Nichtöffentliche Fassung"
        assert entry.changes["format"] == "ZIP"

    def test_gesperrte_fassung_wird_nicht_ausgeliefert(self, welt: Welt) -> None:
        package = _build(welt.meeting, PUBLIC)
        welt.plan_pdf.is_public = False
        welt.plan_pdf.save()
        admin = _client(welt.tenant, "admin", set(), admin=True)
        response = admin.get(f"{welt.base}{package.pk}/pdf/")
        assert response.status_code == 302
        assert not SessionAuditLog.objects.filter(action="download").exists()
        assert "Nicht mehr abrufbar" in admin.get(welt.base).content.decode()

    def test_fremde_fassung_und_unbekanntes_format(self, welt: Welt) -> None:
        package = _build(welt.meeting, PUBLIC)
        andere = SessionMeeting.objects.create(
            tenant=welt.tenant, name="Andere Sitzung", organization=welt.org, start=timezone.now()
        )
        admin = _client(welt.tenant, "admin", set(), admin=True)
        other_base = f"/session/{welt.tenant.slug}/meetings/{andere.pk}/mappe/"
        assert _download(admin, f"{other_base}{package.pk}/pdf/")[0] == 404
        assert _download(admin, f"{welt.base}{package.pk}/exe/")[0] == 404

    def test_sitzungsdetail_laedt_mappe_nach(self, welt: Welt) -> None:
        detail = f"/session/{welt.tenant.slug}/meetings/{welt.meeting.pk}/"
        admin = _client(welt.tenant, "admin", set(), admin=True)
        assert welt.base in admin.get(detail).content.decode()
        nur_sitzungen = _client(welt.tenant, "sitzungen", {"view_meetings"})
        assert welt.base not in nur_sitzungen.get(detail).content.decode()
