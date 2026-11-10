# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüfung des Objektspeichers der Dokumentablage für ``manage.py doctor`` (Issue #964).

Eingeschaltet, aber unvollständig eingestellt, ist der Objektspeicher stillschweigend aus
(``object_storage.enabled``); genau das meldet die Prüfung als Fehler. Vollständig eingestellt fragt sie nur
``HeadBucket``: Sie liest, schreibt und löscht kein Objekt.
"""

from __future__ import annotations

import re
from typing import Final

from django.conf import settings

from apps.common.doctor import Befund, Kontext, fehler, ok, pruefung, uebersprungen
from insight_core.services import object_storage

ZUGANG: Final = ("OBJ_ENDPOINT", "OBJ_BUCKET", "OBJ_KEY", "OBJ_SECRET")
#: Fehlercode des Objektspeichers, wie er in eine Meldung darf (bei HEAD der HTTP-Status)
_CODE: Final = re.compile(r"[A-Za-z0-9]{1,40}")
_NICHT_GEFUNDEN: Final = ("404", "NoSuchBucket", "NotFound")
_ABGELEHNT: Final = ("401", "403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch")
_WO: Final = "OBJ_* in der .env prüfen (docs/FILE_CACHE.md, „Objektspeicher“), danach docker compose up -d."


def _fehlercode(exc: BaseException) -> str:
    """Code aus der Antwort des Objektspeichers; leer, wenn es keine Antwort gab oder der Code kein Code ist."""
    antwort = getattr(exc, "response", None)
    if not isinstance(antwort, dict):
        return ""
    code = str(dict(antwort.get("Error") or {}).get("Code", ""))
    return code if _CODE.fullmatch(code) else ""


@pruefung("objektspeicher", "Objektspeicher (S3)", rang=50)
def pruefe_objektspeicher(kontext: Kontext) -> Befund:
    if not getattr(settings, "OBJ_ENABLED", False):
        return uebersprungen("nicht eingeschaltet (OBJ_ENABLED)")
    fehlt = [name for name in ZUGANG if not getattr(settings, name, "")]
    if fehlt:
        return fehler(
            f"eingeschaltet, aber {', '.join(fehlt)} fehlt: Der Objektspeicher bleibt aus",
            f"Alle vier Angaben setzen ({', '.join(ZUGANG)}). {_WO}",
        )
    try:
        object_storage.probe(kontext.zeitgrenze)
    except Exception as exc:  # noqa: BLE001 - jede Störung ist ein Befund; ihr Text bleibt draußen
        code = _fehlercode(exc)
        if code in _NICHT_GEFUNDEN:
            return fehler(f"Bucket nicht gefunden (Code {code})", f"OBJ_BUCKET und OBJ_ENDPOINT stimmen? {_WO}")
        if code in _ABGELEHNT:
            return fehler(f"Zugang abgelehnt (Code {code})", f"OBJ_KEY, OBJ_SECRET und die Rechte am Bucket? {_WO}")
        art = f"Code {code}" if code else type(exc).__name__
        return fehler(f"nicht erreichbar ({art})", f"OBJ_ENDPOINT, OBJ_REGION und Firewall? {_WO}")
    return ok("Bucket erreichbar (nur gelesen)")
