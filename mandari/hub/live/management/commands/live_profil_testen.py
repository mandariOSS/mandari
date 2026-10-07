# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einblendungsprofil an einem lokalen Bild oder Video prüfen (Issue #915). Speichert nichts.

    python manage.py live_profil_testen <bild.png|video.mp4> [--sekunde 1800] \\
        [--profil balken_unten_dreizeilig | --profil-datei profil.json | --quelle <uuid>]

Bei einem Video wird das Bild an ``--sekunde`` im Speicher dekodiert (PyAV); es entsteht keine Datei. Ausgabe:
die Lesung als JSON und die Dauer der Texterkennung.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from PIL import Image, UnidentifiedImageError

from ...lesung import lies_bild
from ...models import BroadcastSource
from ...ocr import OcrError, verfuegbar
from ...profil import VORLAGEN, ProfilError, lade_profil, vorlage


def _bild_aus_video(pfad: Path, sekunde: float) -> Image.Image:
    import av

    try:
        with av.open(str(pfad)) as container:
            spur = container.streams.video[0]
            if spur.time_base is not None:
                container.seek(int(sekunde / spur.time_base), stream=spur)
            for bild in container.decode(spur):
                ergebnis: Image.Image = bild.to_image()  # type: ignore[no-untyped-call]
                return ergebnis
    except (av.FFmpegError, IndexError) as fehler:
        raise CommandError("Weder Bild noch lesbares Video") from fehler
    raise CommandError("Kein Bild an dieser Stelle")


class Command(BaseCommand):
    help = "Einblendungsprofil an einem lokalen Bild oder Video prüfen; speichert nichts (Issue #915)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("datei", help="Bild (PNG, JPEG) oder Video")
        parser.add_argument("--sekunde", type=float, default=0.0, help="Stelle im Video (Sekunden)")
        profil = parser.add_mutually_exclusive_group()
        profil.add_argument("--profil", choices=sorted(VORLAGEN), help="Profilvorlage")
        profil.add_argument("--profil-datei", help="Profil als JSON-Datei")
        profil.add_argument("--quelle", help="UUID einer Übertragungsquelle (deren Profil)")

    def handle(self, *args: Any, **optionen: Any) -> None:
        if optionen["quelle"]:
            quelle = BroadcastSource.objects.filter(pk=uuid.UUID(optionen["quelle"])).first()
            if quelle is None:
                raise CommandError("Quelle nicht gefunden")
            daten: Any = quelle.overlay_profile
        elif optionen["profil_datei"]:
            try:
                daten = json.loads(Path(optionen["profil_datei"]).read_text(encoding="utf-8"))
            except (OSError, ValueError) as fehler:
                raise CommandError("Profildatei nicht lesbar oder kein JSON") from fehler
        else:
            daten = vorlage(optionen["profil"] or "balken_unten_dreizeilig")
        try:
            profil = lade_profil(daten)
        except ProfilError as fehler:
            raise CommandError(f"Profil ungültig: {'; '.join(fehler.probleme)}") from fehler
        if not verfuegbar():
            raise CommandError("Tesseract nicht gefunden (LIVE_TESSERACT_CMD)")

        pfad = Path(optionen["datei"])
        if not pfad.is_file():
            raise CommandError("Datei nicht gefunden")
        try:
            bild: Image.Image = Image.open(pfad)
            bild.load()
        except (UnidentifiedImageError, OSError):
            bild = _bild_aus_video(pfad, optionen["sekunde"])
        beginn = time.monotonic()
        try:
            lesung = lies_bild(bild.convert("RGB"), profil)
        except OcrError as fehler:
            raise CommandError(f"Texterkennung gescheitert: {fehler}") from fehler
        finally:
            groesse = bild.size
            bild.close()
        ergebnis = {**lesung.als_dict(), "breite": groesse[0], "hoehe": groesse[1]}
        ergebnis["ms"] = int((time.monotonic() - beginn) * 1000)
        self.stdout.write(json.dumps(ergebnis, ensure_ascii=False))
