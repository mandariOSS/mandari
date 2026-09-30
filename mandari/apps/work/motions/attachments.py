# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anhänge an Dokumenten hochladen, umbenennen und entfernen (Issue #584).

Rechte: wie das Bearbeiten des Dokuments (``Motion.can_edit`` – ohne Status-Sperre, die nur den
Inhalt betrifft). Prüfung der Datei über den gemeinsamen Baustein ``apps.common.uploads``
(Dateityp, Größe), Ablage unter einem Zufallsnamen (``apps.work.files``), Auslieferung nur über
die zugriffsgeprüfte Download-View. Den Inhaltstyp bestimmt der Server aus der Endung.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.common.uploads import DOCUMENTS, MB, validate_upload

if TYPE_CHECKING:
    from apps.tenants.models import Membership

    from .models import Motion, MotionDocument

logger = logging.getLogger(__name__)

#: Einheitliche Grenze je Anhang (wie Session-Anlagen)
ATTACHMENT_MAX_BYTES = 50 * MB
#: Höchstzahl der Anhänge je Dokument
MAX_ATTACHMENTS = 30
#: Längster Anzeigename (Modellfeld ``filename``)
MAX_NAME_LENGTH = 255
#: ``accept``-Angabe für das Dateifeld (dieselbe Liste prüft der Server)
ACCEPT = ",".join(sorted(DOCUMENTS))


def mime_type_for(name: str) -> str:
    """Inhaltstyp aus der Endung – nie die Angabe des Browsers."""
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _clean_name(name: str) -> str:
    """Anzeigename ohne Pfadanteile und Steuerzeichen."""
    base = PurePosixPath((name or "").replace("\\", "/")).name
    return "".join(ch for ch in base if ch.isprintable()).strip()[:MAX_NAME_LENGTH]


def add_attachments(motion: Motion, membership: Membership, files: list[Any]) -> tuple[list[MotionDocument], list[str]]:
    """Dateien prüfen und anhängen. Liefert (angelegte Anhänge, Fehlermeldungen je abgelehnter Datei)."""
    from .models import MotionDocument

    created: list[MotionDocument] = []
    errors: list[str] = []
    existing = motion.documents.count()
    for upload in files:
        name = _clean_name(getattr(upload, "name", "") or "")
        if existing + len(created) >= MAX_ATTACHMENTS:
            errors.append(f"„{name}“: Ein Dokument kann höchstens {MAX_ATTACHMENTS} Anhänge haben.")
            continue
        try:
            validate_upload(upload, allowed=DOCUMENTS, max_bytes=ATTACHMENT_MAX_BYTES, bezeichnung="Datei")
        except ValidationError as exc:
            errors.append(f"„{name}“: {' '.join(exc.messages)}")
            continue
        with transaction.atomic():
            document = MotionDocument(
                motion=motion,
                file=upload,
                filename=name or "Anhang",
                mime_type=mime_type_for(name),
                file_size=upload.size,
                uploaded_by=membership,
            )
            document.save()
        created.append(document)
        logger.info("Anhang %s an Dokument %s angelegt (%s Bytes)", document.id, motion.id, upload.size)
    return created, errors


def rename_attachment(document: MotionDocument, new_name: str) -> str | None:
    """Anzeigenamen ändern; die Endung bleibt (sie bestimmt Typ und Auslieferung).

    Liefert eine Fehlermeldung für Nutzer:innen oder ``None``.
    """
    cleaned = _clean_name(new_name)
    suffix = PurePosixPath(document.filename or "").suffix
    if not cleaned or cleaned.lower() == suffix.lower():
        return "Bitte einen Namen angeben."
    if suffix and not cleaned.lower().endswith(suffix.lower()):
        cleaned = (cleaned[: MAX_NAME_LENGTH - len(suffix)]).rstrip(". ") + suffix
    document.filename = cleaned
    document.save(update_fields=["filename"])
    return None


def delete_attachment(document: MotionDocument) -> None:
    """Anhang entfernen; die Datei im Speicher erst nach erfolgreicher Transaktion."""
    storage = document.file.storage
    name = document.file.name
    document.delete()

    def remove_file() -> None:
        try:
            if name and storage.exists(name):
                storage.delete(name)
        except OSError:
            logger.warning("Datei eines entfernten Anhangs konnte nicht gelöscht werden: %s", name)

    transaction.on_commit(remove_file)
