# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gemeinsame Optionen der Befehle ``events_tasks`` und ``events_worker`` (kein eigener Befehl: Unterstrich)."""

from __future__ import annotations

from collections.abc import Collection

from django.core.management.base import CommandError


def liste(wert: str | None) -> list[str]:
    """Kommagetrennte Angabe als Liste ohne Leereinträge."""
    return [teil.strip() for teil in (wert or "").split(",") if teil.strip()]


def parallelitaet(angaben: list[str], queues: Collection[str]) -> dict[str, int]:
    """``--concurrency WARTESCHLANGE=N`` (mehrfach) für gewählte Warteschlangen."""
    ergebnis: dict[str, int] = {}
    for angabe in angaben:
        name, _, zahl = angabe.partition("=")
        name = name.strip()
        if name not in queues or not zahl.strip().isdigit():
            raise CommandError(f"--concurrency erwartet WARTESCHLANGE=N mit einer gewählten Warteschlange: {angabe}")
        ergebnis[name] = int(zahl)
    return ergebnis


def nicht_negativ(**werte: int | None) -> None:
    """Grenzen wie ``--max-tasks`` dürfen nicht negativ sein (0 = keine Grenze)."""
    for name, wert in werte.items():
        if wert is not None and wert < 0:
            raise CommandError(f"--{name.replace('_', '-')} darf nicht negativ sein.")
