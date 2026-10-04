# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Worker lebt“ als Meldung an die Statusseite (Issue #574): externer Endpunkt von Gatus.

Die Prüfung ``/health/worker/`` fragt die Anwendung; fällt die ganze Installation aus, meldet sie
nichts mehr. Deshalb meldet sich der Worker zusätzlich selbst. Bleibt die Meldung aus (Prozess tot,
Container gestoppt, Server weg), alarmiert die Statusseite nach ihrer eigenen Frist.

- ``WORKER_PUSH_URL``: vollständige Adresse des externen Endpunkts, bei Gatus
  ``https://<statusseite>/api/v1/endpoints/<gruppe>_<name>/external``; leer = keine Meldung.
- ``WORKER_PUSH_TOKEN``: Bearer-Token dieses Endpunkts.
- ``WORKER_PUSH_INTERVAL``: Sekunden zwischen zwei Meldungen (Standard 60). Ändert sich der Zustand,
  wird sofort gemeldet.

Gemeldet wird ``success=true``, solange jede Rolle arbeitet, sonst ``success=false`` mit den Rollen
ohne Lebenszeichen. Es meldet nur ein Worker mit der Rolle ``scheduler`` (bei der Compose-Vorlage der
Dienst ``worker``, nicht ``worker-heavy``), damit ein zweiter Worker den Ausfall nicht verdeckt. Die
Meldung läuft in einem eigenen kurzen Faden: Eine langsame Statusseite hält das Lebenszeichen nicht
auf. Fehler beim Melden landen nur im Protokoll.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Final

from django.conf import settings

logger = logging.getLogger(__name__)

#: Zeitgrenze einer Meldung in Sekunden
TIMEOUT: Final = 5.0
#: Abstand zweier Meldungen ohne Zustandswechsel, wenn nichts eingestellt ist
DEFAULT_INTERVAL: Final = 60.0

Sender = Callable[[str, str, bool, str], None]


def send(url: str, token: str, ok: bool, error: str) -> None:
    """Eine Meldung an den externen Endpunkt (``POST …?success=…&error=…``)."""
    abfrage = {"success": "true" if ok else "false"}
    if error:
        abfrage["error"] = error
    trenner = "&" if "?" in url else "?"
    anfrage = urllib.request.Request(f"{url}{trenner}{urllib.parse.urlencode(abfrage)}", data=b"", method="POST")
    if token:
        anfrage.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(anfrage, timeout=TIMEOUT) as antwort:  # noqa: S310 – Adresse aus den Einstellungen
        antwort.read()


class Push:
    """Meldet den Zustand des Workers höchstens alle ``interval`` Sekunden, bei einem Wechsel sofort."""

    def __init__(
        self,
        url: str,
        token: str = "",
        interval: float = DEFAULT_INTERVAL,
        *,
        sender: Sender = send,
        clock: Callable[[], float] = time.monotonic,
        background: bool = True,
    ) -> None:
        self.url = url
        self.token = token
        self.interval = interval
        self._sender = sender
        self._clock = clock
        self._background = background
        self._zuletzt: float | None = None
        self._zuletzt_ok: bool | None = None
        self._faden: threading.Thread | None = None

    def report(self, ok: bool, error: str = "") -> bool:
        """Meldet, wenn es Zeit ist oder sich der Zustand geändert hat; ``True``, wenn gemeldet wird."""
        jetzt = self._clock()
        wechsel = ok != self._zuletzt_ok
        if not wechsel and self._zuletzt is not None and jetzt - self._zuletzt < self.interval:
            return False
        if self._faden is not None and self._faden.is_alive():
            return False  # die vorige Meldung hängt noch; nicht stapeln
        self._zuletzt, self._zuletzt_ok = jetzt, ok
        if not self._background:
            self._senden(ok, error)
            return True
        self._faden = threading.Thread(target=self._senden, args=(ok, error), name="worker-push", daemon=True)
        self._faden.start()
        return True

    def _senden(self, ok: bool, error: str) -> None:
        try:
            self._sender(self.url, self.token, ok, error)
        except Exception as exc:  # noqa: BLE001 – eine fehlende Statusseite darf den Worker nicht stören
            logger.warning("Worker: Meldung an die Statusseite gescheitert (%s)", type(exc).__name__)


def push_from_settings() -> Push | None:
    """``Push`` nach ``WORKER_PUSH_*``; ``None``, wenn keine Adresse eingestellt ist."""
    url = str(getattr(settings, "WORKER_PUSH_URL", "") or "").strip()
    if not url:
        return None
    token = str(getattr(settings, "WORKER_PUSH_TOKEN", "") or "")
    interval = float(getattr(settings, "WORKER_PUSH_INTERVAL", DEFAULT_INTERVAL) or DEFAULT_INTERVAL)
    return Push(url, token, interval)
