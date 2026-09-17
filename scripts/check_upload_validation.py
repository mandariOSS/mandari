# SPDX-License-Identifier: AGPL-3.0-or-later
"""
CI-Gate: Jede Stelle, die hochgeladene Dateien entgegennimmt, benutzt die gemeinsame
Prüfung aus ``apps/common/uploads.py`` (Issue #260, BSI CON.10.A5 / APP.3.1.A4).

    python scripts/check_upload_validation.py

Geprüft werden alle Module unter ``mandari/apps`` (ohne Tests und Migrationen), die
``request.FILES`` lesen oder ein ``forms.FileField``/``forms.ImageField`` deklarieren.
Jedes davon muss ``validate_upload`` importieren – oder in ``DELEGIERT`` stehen, mit
der Begründung, an welche prüfende Stelle es die Datei weiterreicht.

Die Liste darf nur schrumpfen: Ein Eintrag, dessen Modul keine Upload-Stelle mehr ist,
ist ein Fehler und wird entfernt. Eine neue Upload-Stelle ohne Prüfung lässt den
Lauf scheitern, bevor sie die Lücke aus #260 zum dritten Mal aufmacht.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
APPS = WURZEL / "mandari" / "apps"

MUSTER_ANNAHME = re.compile(r"request\.FILES|forms\.(File|Image)Field\(")
MUSTER_PRUEFUNG = re.compile(r"from apps\.common\.uploads import [^\n]*validate_upload|uploads\.validate_upload")

#: Module, die die Datei ungeprüft an eine prüfende Stelle weiterreichen.
DELEGIERT: dict[str, str] = {
    "work/meetings/views/api_documents.py": "reicht an meetings/services.add_document_upload weiter (validate_upload)",
    "work/organization/views/profile.py": "reicht an organization/services.update_profile weiter (_pruefe_bild)",
    "work/organization/views/team.py": "reicht an organization/services.update_general_settings weiter (_pruefe_bild)",
    "work/tasks/views/panel.py": "nutzt TaskAttachmentForm.clean_file (validate_upload)",
    "session/views/files.py": "nutzt session/services/file_service.validate_upload",
}


def upload_module() -> list[Path]:
    treffer = []
    for datei in sorted(APPS.rglob("*.py")):
        rel = datei.relative_to(APPS).as_posix()
        if "/tests/" in f"/{rel}" or "/migrations/" in f"/{rel}" or rel.startswith("common/uploads.py"):
            continue
        if MUSTER_ANNAHME.search(datei.read_text(encoding="utf-8")):
            treffer.append(datei)
    return treffer


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    fehler: list[str] = []
    gesehen: set[str] = set()
    geprueft = 0

    for datei in upload_module():
        rel = datei.relative_to(APPS).as_posix()
        gesehen.add(rel)
        quelle = datei.read_text(encoding="utf-8")
        if MUSTER_PRUEFUNG.search(quelle):
            geprueft += 1
            if rel in DELEGIERT:
                fehler.append(f"{rel}: prüft selbst – Eintrag in DELEGIERT ist überflüssig, bitte entfernen")
            continue
        if rel in DELEGIERT:
            geprueft += 1
            continue
        fehler.append(
            f"{rel}: nimmt Dateien an, ohne validate_upload aus apps.common.uploads zu benutzen "
            "(oder mit Begründung in DELEGIERT einzutragen)"
        )

    for rel in sorted(set(DELEGIERT) - gesehen):
        fehler.append(f"{rel}: steht in DELEGIERT, ist aber keine Upload-Stelle mehr – Eintrag entfernen")

    if fehler:
        print("Upload-Stellen ohne gemeinsame Prüfung:\n")
        for f in fehler:
            print(f"  ✗ {f}")
        print("\nRegel: docs/UPLOADS.md – jede Upload-Stelle ruft validate_upload mit einem Profil auf.")
        return 1
    print(f"OK: {geprueft} Upload-Stellen benutzen die gemeinsame Prüfung ({len(DELEGIERT)} davon über eine prüfende Stelle).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
