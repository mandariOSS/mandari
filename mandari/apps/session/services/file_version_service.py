# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fassungen von Anlagen und Speicherkonzept (Issue #226).

Speicherkonzept
- Jeder Dateiinhalt liegt je Mandant genau einmal im Speicher (``SessionFileBlob``, SHA-256).
  Hochladen, Ersetzen und Wiederherstellen suchen zuerst einen vorhandenen Inhalt und
  verweisen darauf, statt die Datei erneut abzulegen. Mandantenübergreifend wird bewusst
  nicht zusammengelegt: Mandanten bleiben auch im Speicher getrennt und einzeln löschbar.
- Die Anlage (``SessionFile.file``) zeigt auf den Speichernamen ihres aktuellen Inhalts, jede
  Fassung (``SessionFileVersion``) auf ihren Inhalt. Beim Ersetzen bleibt die bisherige
  Fassung samt Datei abrufbar.
- Anlagen aus der Zeit vor der Versionierung werden beim ersten Bedarf erfasst
  (``ensure_current_version``): Die Prüfsumme entsteht aus der gespeicherten Datei, gleiche
  Inhalte werden dabei zusammengeführt.

Löschen
- Eine Anlage zu löschen entfernt sie samt ihrem Verlauf. Inhalte, die in einer Fassung der
  Vorlage stecken, bleiben dort erhalten; alles Übrige räumt ``collect_garbage`` nach dem
  Commit aus dem Speicher. Ein Inhalt verschwindet erst, wenn nichts mehr auf ihn verweist.
- Datenschutz-Löschung (``purge_content``): entfernt einen Inhalt endgültig aus dem Speicher,
  auch aus gesicherten Fassungen. Ausgenommen sind der aktuelle Inhalt einer Anlage (erst
  ersetzen oder löschen) und die beschlossene Fassung einer Vorlage (amtlicher Stand).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Sequence
from typing import IO, Any, cast

from django.core.files.base import File
from django.core.files.uploadedfile import UploadedFile
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError, RestrictedError
from django.utils import timezone

from apps.session import audit
from apps.session.models import (
    SessionFile,
    SessionFileBlob,
    SessionFileVersion,
    SessionPaper,
    SessionPaperVersionFile,
    SessionTenant,
    SessionUser,
)
from apps.session.services import file_service

logger = logging.getLogger(__name__)

_log_event = cast(Any, audit).log_event

_CHUNK = 1024 * 1024
#: Hinweis an Fassungen, die nachträglich erfasst wurden (Bestand vor der Versionierung)
NOTE_BACKFILLED = "Nachträglich erfasst"


class PurgeRefusedError(Exception):
    """Datenschutz-Löschung nicht möglich (nutzerfreundliche Meldung)."""


# =============================================================================
# Prüfsummen und Speicher
# =============================================================================


def _sha256_of_upload(uploaded: UploadedFile[bytes]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    uploaded.seek(0)
    for chunk in uploaded.chunks():
        digest.update(chunk)
        size += len(chunk)
    uploaded.seek(0)
    return digest.hexdigest(), size


def _sha256_of_handle(handle: IO[bytes]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(_CHUNK), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _file_field() -> Any:
    return SessionFile._meta.get_field("file")


def _storage() -> Any:
    return _file_field().storage


def _store(uploaded: UploadedFile[bytes]) -> str:
    """Upload unter ``session/files/<Jahr>/<Monat>/`` ablegen; liefert den Speichernamen."""
    field = _file_field()
    name = field.generate_filename(None, uploaded.name or "anlage")
    uploaded.seek(0)
    return str(field.storage.save(name, uploaded, max_length=field.max_length))


def _exists(name: str) -> bool:
    try:
        return bool(name) and bool(_storage().exists(name))
    except (OSError, ValueError):
        return False


def _find_blob(tenant_id: Any, sha256: str) -> SessionFileBlob | None:
    return SessionFileBlob.objects.filter(tenant_id=tenant_id, sha256=sha256, purged_at__isnull=True).first()


def _blob_for_upload(tenant: SessionTenant, uploaded: UploadedFile[bytes], sha256: str, size: int) -> SessionFileBlob:
    """Vorhandenen Inhalt wiederverwenden, sonst ablegen – keine Datei doppelt im Speicher."""
    blob = _find_blob(tenant.pk, sha256)
    if blob is not None and _exists(str(blob.file.name)):
        return blob
    stored = _store(uploaded)
    if blob is not None:
        # Eintrag ohne Datei im Speicher (etwa nach einer Rücksicherung): mit dem neuen Upload heilen
        blob.file.name = stored
        blob.size = size
        blob.save(update_fields=["file", "size"])
        return blob
    try:
        with transaction.atomic():
            return SessionFileBlob.objects.create(tenant=tenant, sha256=sha256, size=size, file=stored)
    except IntegrityError:
        # Denselben Inhalt hat gerade jemand anderes abgelegt: dessen Eintrag nutzen, eigene Kopie freigeben
        release_storage_names([stored])
        return SessionFileBlob.objects.get(tenant=tenant, sha256=sha256, purged_at__isnull=True)


def open_blob(blob: SessionFileBlob | None) -> File[Any] | None:
    """Inhalt zum Lesen öffnen; None, wenn gelöscht oder im Speicher nicht vorhanden."""
    if blob is None or not blob.is_available:
        return None
    try:
        return cast("File[Any]", _storage().open(str(blob.file.name), "rb"))
    except (OSError, ValueError):
        return None


def read_text(blob: SessionFileBlob, mime_type: str, name: str) -> str:
    """Text für die Suche aus einem gespeicherten Inhalt (best effort, wie beim Upload)."""
    if blob.size > file_service.TEXT_EXTRACTION_MAX_SIZE_MB * 1024 * 1024:
        return ""
    handle = open_blob(blob)
    if handle is None:
        return ""
    with handle:
        data = handle.read()
    return file_service.extract_text(data, mime_type, name)


# =============================================================================
# Fassungen einer Anlage
# =============================================================================


def _record(
    session_file: SessionFile, blob: SessionFileBlob, *, user: SessionUser | None, note: str = ""
) -> SessionFileVersion:
    return SessionFileVersion.objects.create(
        tenant_id=session_file.tenant_id,
        session_file=session_file,
        number=session_file.version,
        blob=blob,
        name=session_file.name,
        mime_type=session_file.mime_type,
        size=blob.size,
        note=note[:200],
        created_by=user,
    )


def attach_upload(
    session_file: SessionFile, uploaded: UploadedFile[bytes], *, user: SessionUser | None
) -> SessionFileVersion:
    """Neue Anlage mit ihrem ersten Inhalt speichern (Fassung ``session_file.version``)."""
    sha256, size = _sha256_of_upload(uploaded)
    with transaction.atomic():
        blob = _blob_for_upload(session_file.tenant, uploaded, sha256, size)
        session_file.file.name = str(blob.file.name)
        session_file.size = size
        session_file.save()
        return _record(session_file, blob, user=user)


def ensure_current_version(
    session_file: SessionFile, versions: Sequence[SessionFileVersion] | None = None
) -> SessionFileVersion | None:
    """
    Fassung zum aktuellen Inhalt der Anlage – bei Bedarf nachträglich erfasst.

    ``versions`` (neueste zuerst) spart die Abfrage, wenn sie schon geladen sind. Liefert None,
    wenn der Inhalt im Speicher nicht lesbar ist; die Anlage bleibt dann unverändert.
    """
    if versions is None:
        latest = session_file.versions.select_related("blob").order_by("-number").first()
    else:
        latest = versions[0] if versions else None
    name = str(session_file.file.name or "")
    if latest is not None and latest.number == session_file.version and str(latest.blob.file.name) == name:
        return latest
    if not name:
        return None
    try:
        with _storage().open(name, "rb") as handle:
            sha256, size = _sha256_of_handle(handle)
    except (OSError, ValueError):
        logger.warning("Anlage %s: Inhalt im Speicher nicht lesbar – Fassung ohne Inhalt.", session_file.pk)
        return None

    with transaction.atomic():
        blob = _find_blob(session_file.tenant_id, sha256)
        if blob is None:
            try:
                with transaction.atomic():
                    blob = SessionFileBlob.objects.create(
                        tenant_id=session_file.tenant_id, sha256=sha256, size=size, file=name
                    )
            except IntegrityError:
                blob = SessionFileBlob.objects.get(tenant_id=session_file.tenant_id, sha256=sha256, purged_at=None)
        if str(blob.file.name) != name:
            if _exists(str(blob.file.name)):
                # Derselbe Inhalt liegt schon im Speicher: Anlage darauf umstellen, eigene Kopie freigeben
                SessionFile.objects.filter(pk=session_file.pk).update(file=blob.file.name)
                session_file.file.name = str(blob.file.name)
                release_storage_names([name])
            else:
                blob.file.name = name
                blob.save(update_fields=["file"])

        number = session_file.version if latest is None else max(session_file.version, latest.number + 1)
        if number != session_file.version:
            SessionFile.objects.filter(pk=session_file.pk).update(version=number)
            session_file.version = number
        return _record(session_file, blob, user=None, note=NOTE_BACKFILLED)


def _switch_content(
    session_file: SessionFile,
    blob: SessionFileBlob,
    *,
    user: SessionUser | None,
    name: str,
    mime_type: str,
    text_content: str,
    note: str = "",
) -> SessionFileVersion:
    latest = session_file.versions.order_by("-number").values_list("number", flat=True).first() or 0
    session_file.file.name = str(blob.file.name)
    session_file.name = name[:500]
    session_file.mime_type = mime_type[:100]
    session_file.size = blob.size
    session_file.text_content = text_content
    session_file.version = max(session_file.version, latest) + 1
    session_file.save()
    return _record(session_file, blob, user=user, note=note)


def replace_content(
    session_file: SessionFile,
    uploaded: UploadedFile[bytes],
    *,
    user: SessionUser | None,
    mime_type: str,
    text_content: str,
) -> SessionFileVersion | None:
    """
    Anlage durch eine neue Datei ersetzen: neue Fassung, die bisherige bleibt abrufbar.

    Liefert None, wenn die Datei dem aktuellen Inhalt gleicht – dann ändert sich nichts.
    """
    sha256, size = _sha256_of_upload(uploaded)
    with transaction.atomic():
        current = ensure_current_version(session_file)
        if current is not None and current.blob.sha256 == sha256 and current.blob.is_available:
            return None
        blob = _blob_for_upload(session_file.tenant, uploaded, sha256, size)
        return _switch_content(
            session_file,
            blob,
            user=user,
            name=uploaded.name or session_file.name,
            mime_type=mime_type,
            text_content=text_content,
        )


def restore_content(
    session_file: SessionFile,
    blob: SessionFileBlob,
    *,
    user: SessionUser | None,
    name: str,
    mime_type: str,
    note: str,
) -> SessionFileVersion | None:
    """Früheren Inhalt als neue Fassung der Anlage einsetzen (ohne Kopie); None, wenn schon aktuell."""
    with transaction.atomic():
        current = ensure_current_version(session_file)
        if current is not None and current.blob_id == blob.pk:
            if session_file.name != name:
                session_file.name = name[:500]
                session_file.save()
            return None
        return _switch_content(
            session_file,
            blob,
            user=user,
            name=name,
            mime_type=mime_type,
            text_content=read_text(blob, mime_type, name),
            note=note,
        )


def recreate_attachment(
    paper: SessionPaper,
    attachment_id: Any,
    blob: SessionFileBlob,
    *,
    user: SessionUser | None,
    name: str,
    mime_type: str,
    note: str,
) -> SessionFile:
    """
    Gelöschte Anlage aus einer Fassung wieder anlegen – unter ihrer bisherigen Kennung, damit
    Vergleich und Audit-Log sie wiedererkennen. Sie kommt bewusst nichtöffentlich zurück:
    Wer sie gelöscht hatte, hatte womöglich einen Grund; die Freigabe entscheidet ein Mensch.
    """
    with transaction.atomic():
        session_file = SessionFile(
            id=attachment_id,
            tenant=paper.tenant,
            paper=paper,
            name=name[:500],
            file=str(blob.file.name),
            mime_type=mime_type[:100],
            size=blob.size,
            is_public=False,
            created_by=user,
            text_content=read_text(blob, mime_type, name),
        )
        session_file.save(force_insert=True)
        _record(session_file, blob, user=user, note=note)
    return session_file


def history(session_file: SessionFile) -> list[SessionFileVersion]:
    """Frühere Fassungen einer Anlage (ohne die aktuelle), neueste zuerst."""
    current_name = str(session_file.file.name or "")
    result = []
    for version in session_file.versions.select_related("blob", "created_by__user").order_by("-number"):
        if version.number == session_file.version and str(version.blob.file.name) == current_name:
            continue
        result.append(version)
    return result


# =============================================================================
# Aufräumen und Datenschutz-Löschung
# =============================================================================


def release_storage_names(names: Iterable[str]) -> None:
    """Speichernamen nach dem Commit löschen, sofern weder eine Anlage noch ein Inhalt darauf verweist."""
    pending = [name for name in names if name]
    if not pending:
        return

    def run() -> None:
        storage = _storage()
        for name in pending:
            if SessionFile.objects.filter(file=name).exists() or SessionFileBlob.objects.filter(file=name).exists():
                continue
            try:
                storage.delete(name)
            except OSError:
                logger.warning("Speichername %s konnte nicht gelöscht werden.", name)

    transaction.on_commit(run)


def collect_garbage(blob_ids: Iterable[Any]) -> None:
    """Inhalte ohne Verweis (keine Anlagen-Fassung, keine Vorlagen-Fassung) nach dem Commit entfernen."""
    pending = {blob_id for blob_id in blob_ids if blob_id}
    if not pending:
        return

    def run() -> None:
        for blob in SessionFileBlob.objects.filter(pk__in=pending):
            if blob.file_versions.exists() or blob.paper_version_files.exists():
                continue
            try:
                with transaction.atomic():
                    blob.delete()  # Signal gibt den Speichernamen frei
            except (ProtectedError, RestrictedError):
                continue  # inzwischen wieder in Gebrauch

    transaction.on_commit(run)


def purge_blocker(blob: SessionFileBlob) -> str | None:
    """Grund, warum ein Inhalt nicht endgültig gelöscht werden darf; None, wenn es geht."""
    if blob.purged_at is not None:
        return "Der Inhalt ist bereits gelöscht."
    if blob.file and SessionFile.objects.filter(tenant_id=blob.tenant_id, file=blob.file.name).exists():
        return "Der Inhalt ist die aktuelle Datei einer Anlage – bitte die Anlage zuerst ersetzen oder löschen."
    if blob.paper_version_files.filter(version__is_resolved=True).exists():
        return "Der Inhalt gehört zur beschlossenen Fassung einer Vorlage und bleibt unverändert erhalten."
    return None


def purgeable_blob_ids(blobs: Iterable[SessionFileBlob | None]) -> set[Any]:
    """Inhalte, deren Datenschutz-Löschung zulässig ist – für die Anzeige; verbindlich prüft ``purge_blocker``."""
    candidates = {blob.pk: blob for blob in blobs if blob is not None and blob.purged_at is None}
    if not candidates:
        return set()
    names = {str(blob.file.name) for blob in candidates.values() if blob.file}
    in_use = set(SessionFile.objects.filter(file__in=names).values_list("file", flat=True))
    resolved = set(
        SessionPaperVersionFile.objects.filter(blob_id__in=candidates, version__is_resolved=True).values_list(
            "blob_id", flat=True
        )
    )
    return {pk for pk, blob in candidates.items() if str(blob.file.name) not in in_use and pk not in resolved}


def purge_content(blob: SessionFileBlob, *, user: SessionUser | None, reason: str) -> None:
    """
    Inhalt endgültig aus dem Speicher löschen (Datenschutz), auch aus gesicherten Fassungen.

    Prüfsumme und Größe bleiben als Nachweis; Fassungen zeigen „Inhalt gelöscht“. Der Grund steht
    im Audit-Log. Raises PurgeRefusedError, wenn ``purge_blocker`` einen Grund nennt.
    """
    reason = reason.strip()[:300]
    if not reason:
        raise PurgeRefusedError("Bitte einen Grund für die Löschung angeben.")
    with transaction.atomic():
        locked = SessionFileBlob.objects.select_for_update().get(pk=blob.pk)
        blocker = purge_blocker(locked)
        if blocker:
            raise PurgeRefusedError(blocker)
        name = str(locked.file.name or "")
        locked.purged_at = timezone.now()
        locked.purged_by = user
        locked.purge_reason = reason
        locked.file.name = ""
        locked.save(update_fields=["purged_at", "purged_by", "purge_reason", "file"])
        _log_event(
            "delete",
            locked,
            user=user,
            changes={"inhalt": "endgültig gelöscht (Datenschutz)", "sha256": locked.sha256, "grund": reason},
        )
        release_storage_names([name])
