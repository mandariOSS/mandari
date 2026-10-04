# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignis ``ris.file.text_extracted`` aus dem Auftrag ``file.extract_text`` (Issue #821).

Die Texterkennung läuft im OCR-Worker des Ingestors oder als Auftrag im Worker der Anwendung
(Schalter ``TEXT_EXTRACTION_RUNNER``, Issue #530). Beide melden einen erkannten Text mit demselben
Ereignis und denselben Regeln wie der Ingestor (``ingestor/src/storage/ris_events.py``,
``text_extracted_events``; ein Test vergleicht beide):

- **Sichtbarkeit ``intern``** laut Vertrag: eine Anreicherung des Bestands, keine Veröffentlichung der
  Quelle. Die Nutzlast nennt Datei, Verfahren (als Code) und Länge des Texts, nie den Text selbst.
- **Nur mit Text.** Eine Erkennung ohne Text ändert nichts, was ein Empfänger lesen könnte.
- **Herkunft und Schalter wie beim Ingestor** (``hub.ris.retraction``): Mandant ist die Quelle der
  Kommune (``source:<uuid>``), ``INGESTOR_EVENTS_ENABLED`` gilt, eine Quelle mit
  ``sync_config["events_enabled"] = false`` bleibt ausgenommen.
- **In der laufenden Transaktion** (``publish()``): Wer auf das Ereignis hin den Bestand liest, sieht den
  Text.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Final

from apps.events import CanonicalRef, publish, tenant_ref
from apps.events.models import Visibility
from hub.ris.retraction import event_source_id, events_enabled

#: Schemaversion des Ereignisses
VERSION: Final = 1
TEXT_EXTRACTED: Final = "ris.file.text_extracted"
#: Verfahren als Code (Vertrag, Feld ``method``); gleich ``_METHOD`` im Ingestor
_METHOD: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
#: Verfahren, wenn der Code nicht darstellbar ist (gleich ``METHOD_UNKNOWN`` im Ingestor)
METHOD_UNKNOWN: Final = "unbekannt"


def method_code(method: str | None) -> str:
    """Verfahren der Texterkennung als Code des Vertrags; nicht darstellbare werden ``METHOD_UNKNOWN``."""
    code = (method or "").strip().lower()
    return code if _METHOD.fullmatch(code) else METHOD_UNKNOWN


def payload(file_id: uuid.UUID, method: str | None, characters: int | None) -> dict[str, Any]:
    """Nutzlast wie ``text_extracted_events`` im Ingestor."""
    nutzlast: dict[str, Any] = {"file": str(file_id), "method": method_code(method)}
    if characters is not None and characters >= 0:
        nutzlast["characters"] = characters
    return nutzlast


def report_text_extracted(
    file_id: uuid.UUID, body_id: uuid.UUID | None, *, method: str | None, characters: int
) -> bool:
    """Meldet den erkannten Text einer Datei in der laufenden Transaktion; ``True``, wenn geschrieben."""
    if characters <= 0 or not events_enabled():
        return False
    source_id = event_source_id(body_id)
    if source_id is None:
        return False
    publish(
        TEXT_EXTRACTED,
        version=VERSION,
        aggregate=CanonicalRef("File", file_id),
        tenant=tenant_ref("source", source_id),
        body_id=body_id,
        visibility=Visibility.INTERN,
        payload=payload(file_id, method, characters),
    )
    return True
