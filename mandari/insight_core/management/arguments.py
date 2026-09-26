# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gemeinsame Argumente der Extraktions-Befehle ``extract_texts`` und ``extract_locations``."""

from django.core.management.base import CommandParser


def add_extraction_arguments(
    parser: CommandParser,
    *,
    noun: str,
    batch_size: int,
    workers: int,
    workers_note: str = "",
) -> None:
    """
    ``--limit``, ``--batch-size``, ``--workers``, ``--body``, ``--verbose``, ``--reprocess`` und ``--dry-run``.

    ``noun`` benennt die verarbeiteten Objekte in den Hilfetexten („Dateien“, „Papers“),
    ``workers_note`` ergänzt den Hilfetext von ``--workers``.
    """
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help=f"Maximale Anzahl zu verarbeitender {noun} (0 = unbegrenzt)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=batch_size,
        help=f"Anzahl {noun} pro Batch (Standard: {batch_size})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=workers,
        help=f"Anzahl paralleler Worker (Standard: {workers}{workers_note})",
    )
    parser.add_argument(
        "--body",
        type=str,
        default=None,
        help=f"UUID der Kommune (nur {noun} dieser Kommune verarbeiten)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Detaillierte Ausgabe",
    )
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help=f"Auch bereits verarbeitete {noun} neu extrahieren",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Nur zählen, keine Extraktion durchführen",
    )
