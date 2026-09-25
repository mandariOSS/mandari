# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsmappe als Gesamtdokument (Issue #218): Anforderung, Hintergrund-Erzeugung, Fassungen.

Ablauf:
1. Die Oberfläche fordert eine Fassung an (``request_package``). Das legt nur eine Zeile an
   oder findet die passende vorhandene – Seitenaufrufe blockieren nie.
2. Das Management-Command ``build_meeting_packages`` (Cron) erzeugt angeforderte Mappen
   (``process_requested``): Gesamt-PDF und ZIP-Paket in ein temporäres Verzeichnis, danach in
   den Speicher unter ``session/files/mappen/`` (nie direkt über /media/ abrufbar).
3. Der Download läuft über eine zugriffsgeprüfte View und wird im Audit-Log protokolliert.

Fassungen: Der Fingerabdruck der Eingaben (siehe ``meeting_package_plan``) entscheidet, ob eine
Anforderung die vorhandene Fassung wiederverwendet oder eine neue mit eigenem Stand anlegt.
Ältere Fassungen bleiben abrufbar – außer ein enthaltener Teil wurde inzwischen gelöscht oder
(öffentliche Fassung) nichtöffentlich; dann wird die ältere Fassung gesperrt (``blocked_packages``).

Der Django-Tasks-Mechanismus läuft in Produktion synchron und wird hier bewusst nicht genutzt.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from django.core.files import File
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.text import slugify

from apps.session import audit
from apps.session.models import (
    SessionAgendaItem,
    SessionFile,
    SessionMeeting,
    SessionMeetingPackage,
    SessionPaper,
    SessionUser,
)
from apps.session.services import file_service
from apps.session.services.meeting_package_pdf import build_pdf
from apps.session.services.meeting_package_plan import (
    AGENDA_ZIP_PATH,
    PUBLIC,
    VARIANT_LABELS,
    VARIANT_PERMISSIONS,
    MeetingPackagePlan,
    PlannedFile,
    build_plan,
)

logger = logging.getLogger(__name__)

Package = SessionMeetingPackage
_log_event = cast(Any, audit).log_event

#: Gilt eine Erzeugung nach dieser Zeit noch als „in Arbeit“, ist ihr Lauf abgebrochen (Absturz, OOM)
STALE_AFTER = timedelta(minutes=60)
#: Nach so vielen Fehlversuchen bleibt eine Fassung fehlgeschlagen, bis sie neu angefordert wird
MAX_ATTEMPTS = 3

#: Bereits komprimierte Formate werden im ZIP nur gespeichert, nicht erneut komprimiert
_STORED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}


# =============================================================================
# Anforderung
# =============================================================================


def request_package(meeting: SessionMeeting, variant: str, requested_by: SessionUser | None) -> tuple[Package, bool]:
    """
    Fassung anfordern.

    Returns:
        (Mappe, neu_angefordert). Stimmt der Fingerabdruck mit der jüngsten Fassung überein,
        wird diese zurückgegeben (fertig oder in Arbeit). Eine noch nicht begonnene oder
        gescheiterte jüngste Fassung wird mit dem aktuellen Stand erneut eingereiht, sonst
        entsteht eine neue Fassung mit der nächsten Nummer.
    """
    plan = build_plan(meeting, variant)
    fingerprint = plan.fingerprint()
    try:
        with transaction.atomic():
            # Gleichzeitige Anforderungen derselben Sitzung nacheinander (PostgreSQL; SQLite sperrt ohnehin)
            SessionMeeting.objects.select_for_update().filter(pk=meeting.pk).first()
            latest = Package.objects.filter(meeting=meeting, variant=variant).order_by("-version").first()
            if latest is not None and latest.fingerprint == fingerprint and latest.status != Package.STATUS_FAILED:
                return latest, False
            if latest is not None and latest.status in (Package.STATUS_REQUESTED, Package.STATUS_FAILED):
                latest.fingerprint = fingerprint
                latest.status = Package.STATUS_REQUESTED
                latest.error = ""
                latest.attempts = 0
                latest.requested_by = requested_by
                latest.requested_at = timezone.now()
                latest.save(
                    update_fields=["fingerprint", "status", "error", "attempts", "requested_by", "requested_at"]
                )
                package, created = latest, True
            else:
                package = Package.objects.create(
                    tenant=meeting.tenant,
                    meeting=meeting,
                    variant=variant,
                    version=(latest.version + 1) if latest is not None else 1,
                    fingerprint=fingerprint,
                    requested_by=requested_by,
                )
                created = True
    except IntegrityError:
        # Parallel angelegt: die dabei entstandene Fassung verwenden
        existing = Package.objects.filter(meeting=meeting, variant=variant).order_by("-version").first()
        if existing is None:
            raise
        return existing, False
    _log_event(
        "create",
        package,
        tenant=meeting.tenant,
        user=requested_by,
        changes={"fassung": package.version, "variante": VARIANT_LABELS[variant], "vorgang": "angefordert"},
    )
    return package, created


def current_fingerprint(meeting: SessionMeeting, variant: str) -> str:
    """Fingerabdruck des aktuellen Inhalts (für den Hinweis „Unterlagen geändert“)."""
    return build_plan(meeting, variant).fingerprint()


@dataclass
class VariantOverview:
    """Stand einer Fassung für die Anzeige an der Sitzung."""

    variant: str
    label: str
    latest: Package | None
    current: Package | None
    current_blocked: bool
    older: list[tuple[Package, bool]]
    outdated: bool

    @property
    def in_progress(self) -> bool:
        return self.latest is not None and self.latest.in_progress

    @property
    def failed(self) -> bool:
        return self.latest is not None and self.latest.status == Package.STATUS_FAILED


def overview(meeting: SessionMeeting, variants: list[str]) -> list[VariantOverview]:
    """
    Je abrufbarer Fassung: jüngste Anforderung, aktuelle fertige Fassung, frühere Fassungen und
    ob sich die Unterlagen seit der aktuellen Fassung geändert haben.
    """
    packages = list(
        Package.objects.filter(meeting=meeting, variant__in=variants)
        .select_related("meeting")
        .order_by("variant", "-version")
    )
    blocked = blocked_packages(packages)
    result = []
    for variant in variants:
        own = [p for p in packages if p.variant == variant]
        latest = own[0] if own else None
        ready = [p for p in own if p.status == Package.STATUS_READY]
        current = ready[0] if ready else None
        outdated = False
        if current is not None and latest is not None and latest.status == Package.STATUS_READY:
            outdated = current_fingerprint(meeting, variant) != current.fingerprint
        result.append(
            VariantOverview(
                variant=variant,
                label=VARIANT_LABELS[variant],
                latest=latest,
                current=current,
                current_blocked=current is not None and current.pk in blocked,
                older=[(p, p.pk in blocked) for p in ready[1:]],
                outdated=outdated,
            )
        )
    return result


# =============================================================================
# Sperre älterer Fassungen
# =============================================================================


def blocked_packages(packages: Iterable[Package]) -> set[Any]:
    """
    Fertige Fassungen, die nicht mehr ausgeliefert werden dürfen (Primärschlüssel).

    Gesperrt ist eine Fassung, wenn ein enthaltener TOP, eine Vorlage oder Anlage gelöscht
    wurde; die öffentliche Fassung außerdem, wenn die Sitzung, ein enthaltener TOP, eine
    Vorlage oder Anlage inzwischen nichtöffentlich ist. Geprüft wird mit denselben Regeln
    wie beim Erzeugen (``file_service.file_visible``) – in drei Abfragen für alle Fassungen.
    """
    packages = [p for p in packages if p.status == Package.STATUS_READY]
    if not packages:
        return set()
    item_ids: set[str] = set()
    paper_ids: set[str] = set()
    file_ids: set[str] = set()
    for package in packages:
        contents = package.contents or {}
        item_ids.update(contents.get("items", []))
        paper_ids.update(contents.get("papers", []))
        file_ids.update(contents.get("files", []))

    items = {
        str(pk): public
        for pk, public in SessionAgendaItem.objects.filter(pk__in=item_ids).values_list("pk", "is_public")
    }
    papers = {
        str(pk): public for pk, public in SessionPaper.objects.filter(pk__in=paper_ids).values_list("pk", "is_public")
    }
    public_permissions = VARIANT_PERMISSIONS[PUBLIC]
    files = {
        str(f.pk): file_service.file_visible(public_permissions, f)
        for f in SessionFile.objects.filter(pk__in=file_ids).select_related("paper", "agenda_item__meeting", "meeting")
    }

    blocked: set[Any] = set()
    for package in packages:
        contents = package.contents or {}
        wanted = [
            (items, contents.get("items", [])),
            (papers, contents.get("papers", [])),
            (files, contents.get("files", [])),
        ]
        if any(pk not in existing for existing, ids in wanted for pk in ids):
            blocked.add(package.pk)
            continue
        if package.variant == PUBLIC:
            still_public = package.meeting.is_public and all(existing[pk] for existing, ids in wanted for pk in ids)
            if not still_public:
                blocked.add(package.pk)
    return blocked


# =============================================================================
# Hintergrund-Erzeugung
# =============================================================================


@dataclass
class RunResult:
    built: int = 0
    failed: int = 0
    reset: int = 0


def reset_stale(now: datetime | None = None) -> int:
    """Abgebrochene Erzeugungen erneut einreihen bzw. nach MAX_ATTEMPTS als gescheitert markieren."""
    now = now or timezone.now()
    count = 0
    for package in Package.objects.filter(status=Package.STATUS_BUILDING, started_at__lt=now - STALE_AFTER):
        if package.attempts >= MAX_ATTEMPTS:
            package.status = Package.STATUS_FAILED
            package.error = "Die Erzeugung wurde wiederholt abgebrochen."
        else:
            package.status = Package.STATUS_REQUESTED
        package.save(update_fields=["status", "error"])
        count += 1
    return count


def claim(package_id: Any) -> Package | None:
    """Angeforderte Mappe atomar übernehmen (nur ein Lauf erzeugt sie)."""
    now = timezone.now()
    claimed = Package.objects.filter(pk=package_id, status=Package.STATUS_REQUESTED).update(
        status=Package.STATUS_BUILDING, started_at=now
    )
    if not claimed:
        return None
    return Package.objects.select_related("meeting__tenant", "meeting__organization", "meeting__legislative_term").get(
        pk=package_id
    )


def process_requested(limit: int = 10, max_seconds: float | None = None) -> RunResult:
    """Angeforderte Mappen der Reihe nach erzeugen (älteste Anforderung zuerst)."""
    result = RunResult(reset=reset_stale())
    started = time.monotonic()
    pending = list(
        Package.objects.filter(status=Package.STATUS_REQUESTED)
        .order_by("requested_at")
        .values_list("pk", flat=True)[: max(0, limit)]
    )
    for package_id in pending:
        if max_seconds is not None and time.monotonic() - started > max_seconds:
            break
        package = claim(package_id)
        if package is None:
            continue
        if build_package(package):
            result.built += 1
        else:
            result.failed += 1
    return result


def build_package(package: Package) -> bool:
    """Eine übernommene Mappe erzeugen. Fehler werden am Datensatz vermerkt, nie weitergereicht."""
    Package.objects.filter(pk=package.pk).update(attempts=F("attempts") + 1)
    try:
        return _build(package)
    except Exception as exc:  # noqa: BLE001 — der Lauf soll die übrigen Mappen weiter erzeugen
        logger.exception("Sitzungsmappe %s konnte nicht erzeugt werden.", package.pk)
        # update() statt save(): Die Fassung kann inzwischen mit ihrer Sitzung gelöscht sein
        Package.objects.filter(pk=package.pk).update(
            status=Package.STATUS_FAILED,
            error=f"Die Sitzungsmappe konnte nicht erstellt werden ({type(exc).__name__}).",
            finished_at=timezone.now(),
        )
        return False


def _build(package: Package) -> bool:
    meeting = package.meeting
    as_of = timezone.now()
    plan = build_plan(meeting, package.variant)
    storage = package.pdf_file.storage
    with tempfile.TemporaryDirectory(prefix="mandari-mappe-") as workdir:
        pdf_path = Path(workdir) / "mappe.pdf"
        zip_path = Path(workdir) / "mappe.zip"
        result = build_pdf(plan, version=package.version, as_of=as_of, target=pdf_path)
        write_zip(plan, result.generated, zip_path)

        stem = f"sitzungsmappe-{package.pk}"
        with open(pdf_path, "rb") as handle:
            package.pdf_file.save(f"{stem}.pdf", File(handle), save=False)
        with open(zip_path, "rb") as handle:
            package.zip_file.save(f"{stem}.zip", File(handle), save=False)
        pdf_size = pdf_path.stat().st_size
        zip_size = zip_path.stat().st_size

    names = [str(package.pdf_file.name), str(package.zip_file.name)]
    updated = Package.objects.filter(pk=package.pk, status=Package.STATUS_BUILDING).update(
        status=Package.STATUS_READY,
        fingerprint=plan.fingerprint(),
        contents=plan.contents(),
        content_as_of=as_of,
        pdf_file=names[0],
        zip_file=names[1],
        pdf_size=pdf_size,
        zip_size=zip_size,
        page_count=result.page_count,
        embedded_count=result.embedded,
        referenced_count=result.referenced,
        error="",
        finished_at=timezone.now(),
    )
    if not updated:
        # Fassung inzwischen gelöscht (etwa mit ihrer Sitzung) oder als abgebrochen zurückgesetzt:
        # keine verwaisten Dateien mit womöglich nichtöffentlichem Inhalt liegen lassen
        for name in names:
            storage.delete(name)
        return False
    return True


def write_zip(plan: MeetingPackagePlan, generated: dict[str, bytes], target: Path) -> None:
    """
    ZIP-Paket: Einladung, je TOP ein Ordner ``<TOP-Nr> <TOP-Titel>``, darin je Vorlage ein Ordner
    ``<Aktenzeichen>`` mit dem Vorlagendokument und ihren Anlagen im Originalformat.

    Dateinamen mit Umlauten erhalten das UTF-8-Flag (``zipfile`` setzt es für Nicht-ASCII-Namen);
    Anlagen werden einzeln in Blöcken kopiert, nie vollständig in den Speicher geladen.
    """
    generated_time = _zip_time(timezone.now())

    def add_generated(archive: zipfile.ZipFile, name: str) -> None:
        if name in generated:
            info = zipfile.ZipInfo(name, date_time=generated_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, generated[name])

    def add_file(archive: zipfile.ZipFile, planned: PlannedFile) -> None:
        session_file = planned.file
        info = zipfile.ZipInfo(planned.zip_path, date_time=_zip_time(session_file.updated_at))
        suffix = Path(planned.zip_path).suffix.lower()
        info.compress_type = zipfile.ZIP_STORED if suffix in _STORED_EXTENSIONS else zipfile.ZIP_DEFLATED
        try:
            source = session_file.file.storage.open(str(session_file.file.name), "rb")
        except (OSError, ValueError):
            logger.warning("Sitzungsmappe: Anlage %s fehlt im Speicher.", session_file.pk)
            return
        with source:
            # Bekannte Größe: zipfile entscheidet selbst über ZIP64 (nur bei sehr großen Dateien)
            info.file_size = int(source.size or 0)
            with archive.open(info, "w") as dest:
                shutil.copyfileobj(source, dest, 1024 * 1024)

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        add_generated(archive, AGENDA_ZIP_PATH)
        for top in plan.all_tops():
            if top.paper is not None:
                add_generated(archive, top.paper.document_zip_path)
                for planned in top.paper.files:
                    add_file(archive, planned)
            for planned in top.files:
                add_file(archive, planned)
        for planned in plan.meeting_files:
            add_file(archive, planned)


def _zip_time(moment: datetime | None) -> tuple[int, int, int, int, int, int]:
    if moment is None:
        return (1980, 1, 1, 0, 0, 0)
    local = timezone.localtime(moment)
    if local.year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    return (local.year, local.month, local.day, local.hour, local.minute, local.second)


# =============================================================================
# Download
# =============================================================================


def download_filename(package: Package, extension: str) -> str:
    """Sprechender Dateiname, z. B. „Sitzungsmappe Hauptausschuss 2026-10-01 Fassung 2 öffentlich.pdf“."""
    meeting = package.meeting
    organization = meeting.organization.short_name or meeting.organization.name
    date = timezone.localtime(meeting.start).strftime("%Y-%m-%d")
    variant = "nichtöffentlich" if package.is_internal else "öffentlich"
    base = f"Sitzungsmappe {organization} {date} Fassung {package.version} {variant}"
    cleaned = "".join(ch for ch in base if ch.isalnum() or ch in " -_äöüÄÖÜß").strip()
    return f"{cleaned or slugify(base)}.{extension}"


def log_download(package: Package, file_format: str, *, user: SessionUser | None, request: Any = None) -> None:
    """Download revisionssicher protokollieren (wer, wann, welche Fassung, welche Variante)."""
    _log_event(
        "download",
        package,
        tenant=package.tenant,
        user=user,
        request=request,
        changes={
            "fassung": package.version,
            "variante": VARIANT_LABELS[package.variant],
            "format": file_format.upper(),
            "stand": timezone.localtime(package.content_as_of).strftime("%d.%m.%Y %H:%M")
            if package.content_as_of
            else "",
        },
    )
