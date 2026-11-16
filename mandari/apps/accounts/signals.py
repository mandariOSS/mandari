# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Signale der Kontoverwaltung.

Die Plattform kennt die Fachmodule nicht (Schichtregeln). Was dort auf ein Ereignis am Konto reagieren muss, etwa
offene WebSocket-Verbindungen, hängt sich an diese Signale.
"""

from __future__ import annotations

from typing import Any

from django.dispatch import Signal

#: Sitzungen eines Kontos gelten nicht mehr: abgemeldet, in der Geräteliste beendet oder das Passwort ist neu.
#: Argument ``user``. Gesendet wird, nachdem die Sitzung beendet bzw. das Passwort gespeichert ist.
sessions_ended = Signal()


def announce_sessions_ended(user: Any) -> None:
    """``sessions_ended`` senden; ein Fehler bei einem Empfänger hält den auslösenden Vorgang nie auf."""
    sessions_ended.send_robust(sender=None, user=user)
