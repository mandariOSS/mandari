# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eingriffe in die Zustellung im Sicherheitsprotokoll (Ereignis ``betrieb``, ``record_operation``).

Laut Spezifikation der Datendrehscheibe (Abschnitt 6, „Nachspielen und Admin“) steht jeder Eingriff
mit Audit-Eintrag im Protokoll: aus dem Admin (``apps.events.admin``, mit Konto und Herkunft der
Anfrage) ebenso wie von der Kommandozeile (``events_dispatch --replay``/``--retry-parked``/
``--discard-parked``, ``suchindex_schatten loeschen``). Von der Kommandozeile gibt es kein Konto;
der Eintrag nennt deshalb die Quelle ``kommandozeile`` und den Befehl.

Festgehalten werden nur Kennungen, Zahlen und Codes, nie Inhalte. Wie im Admin schreibt der Aufrufer
Eingriff und Eintrag in **einer** Transaktion: Scheitert das Protokoll, unterbleibt der Eingriff.
"""

from __future__ import annotations

from typing import Any

from apps.accounts.security_audit import record_operation

from .models import ParkedEvent


def parked_identifiers(geparkt: ParkedEvent) -> dict[str, Any]:
    """Was das Protokoll zu einem geparkten Ereignis festhält: Kennungen und Codes, keine Inhalte."""
    return {
        "abonnement": geparkt.subscription,
        "folgenummer": geparkt.event_seq,
        "objekt": str(geparkt.aggregate_id),
        "zustand": geparkt.state,
        "versuche": geparkt.attempts,
        "fehlercode": geparkt.error_code or "",
    }


def record_command(command: str, action: str, **details: Any) -> None:
    """Eingriff über einen Verwaltungsbefehl festhalten (ohne Anfrage und Konto)."""
    record_operation(None, action, quelle="kommandozeile", befehl=command, **details)
