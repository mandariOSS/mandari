# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schriftdateien für Dokumente bauen (Issue #1584).

    python scripts/schriften_bauen.py [--nur arimo,tinos] [--quellen <Ordner>]

Liest den Schriftsatz aus ``mandari/apps/common/schriften.py``, lädt die Quelldateien aus einem festen Stand von
github.com/google/fonts (``GOOGLE_FONTS_STAND``) und schreibt je Schnitt eine woff2-Datei nach
``mandari/static/vendor/schriften/<schlüssel>/`` sowie die ``@font-face``-Regeln nach
``mandari/static/vendor/schriften/schriften.css``.

Je Schnitt:

1. Variable Quelldateien werden auf feste Achsenwerte gesetzt (Gewicht 400 bzw. 700, übrige Achsen laut Satz).
2. Untermenge Lateinisch (Basis, Latin-1, Erweitert A/B, Satzzeichen, Währungen, Pfeile, Aufzählungszeichen) mit
   allen OpenType-Merkmalen, ohne Hinting: Browser und Chromium setzen damit identisch, die Dateien bleiben klein.
3. woff2 (Brotli). Browser und die PDF-Erzeugung lesen dieselbe Datei.

Schriften mit reserviertem Namen (OFL) werden nicht verändert: Ihre Originaldateien (TrueType) werden byte-gleich
übernommen (``unveraendert`` im Schriftsatz).

Benötigt ``fonttools`` und ``brotli`` (nicht Teil der Laufzeit). Die Quellen sind fremde Dateien: Sie landen in einem
eigenen Ordner und werden nur von fontTools gelesen. Am Ende stehen die Urheberangaben aus den Schriftdateien,
damit REUSE.toml sie korrekt nennt.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
import urllib.request
from pathlib import Path

#: Stand von github.com/google/fonts, aus dem gebaut wird (Commit)
GOOGLE_FONTS_STAND = "5026800714d07231937d92eb4cceabf06e05d74e"
QUELLE_URL = "https://raw.githubusercontent.com/google/fonts/{stand}/{verzeichnis}/{datei}"

WURZEL = Path(__file__).resolve().parent.parent
ZIEL = WURZEL / "mandari" / "static" / "vendor" / "schriften"

#: Unicode-Bereiche der Untermenge
BEREICHE: tuple[tuple[int, int], ...] = (
    (0x0020, 0x007E),  # Basis-Lateinisch
    (0x00A0, 0x00FF),  # Latin-1 (Umlaute, ß, §, «»)
    (0x0100, 0x017F),  # Lateinisch Erweitert A (osteuropäische Namen)
    (0x0180, 0x024F),  # Lateinisch Erweitert B
    (0x0259, 0x0259),  # ə
    (0x02BB, 0x02BC),
    (0x02C6, 0x02C7),
    (0x02DA, 0x02DD),
    (0x0300, 0x0308),  # kombinierende Zeichen (häufige)
    (0x1E00, 0x1EFF),  # Lateinisch Erweitert Zusatz
    (0x2000, 0x206F),  # Satzzeichen („“ – — … • ‰ ′ ″)
    (0x2070, 0x209F),  # Hoch- und Tiefstellungen
    (0x20A0, 0x20CF),  # Währungen (€)
    (0x2100, 0x214F),  # Buchstabenähnliches (№, ™, ℓ)
    (0x2150, 0x218F),  # Brüche
    (0x2190, 0x21FF),  # Pfeile
    (0x2212, 0x2212),  # Minus
    (0x2215, 0x2215),
    (0x2219, 0x221A),
    (0x221E, 0x221E),
    (0x2248, 0x2248),
    (0x2260, 0x2265),
    (0x25A0, 0x25CF),  # Formen für Aufzählungen (■ □ ▪ ● ○)
    (0x2610, 0x2612),  # Kästchen ☐ ☑ ☒
    (0x2713, 0x2714),  # Haken ✓ ✔
    (0xFB01, 0xFB02),  # Ligaturen fi, fl
    (0xFEFF, 0xFEFF),
    (0xFFFD, 0xFFFD),
)


def _schriften_modul():
    pfad = WURZEL / "mandari" / "apps" / "common" / "schriften.py"
    spec = importlib.util.spec_from_file_location("schriften", pfad)
    assert spec and spec.loader
    modul = importlib.util.module_from_spec(spec)
    sys.modules["schriften"] = modul
    spec.loader.exec_module(modul)
    return modul


def _laden(verzeichnis: str, datei: str, quellen: Path) -> Path:
    # Je Stand ein eigener Ordner: Ein Zwischenspeicher eines älteren Stands baut sonst andere Schriften
    ziel = quellen / GOOGLE_FONTS_STAND / verzeichnis.replace("/", "_") / datei
    if ziel.exists():
        return ziel
    ziel.parent.mkdir(parents=True, exist_ok=True)
    url = QUELLE_URL.format(stand=GOOGLE_FONTS_STAND, verzeichnis=verzeichnis, datei=urllib.request.quote(datei))
    with urllib.request.urlopen(url, timeout=60) as antwort:  # noqa: S310 – feste https-Adresse
        daten = antwort.read()
    # Erst vollständig schreiben, dann bereitstellen: Ein abgebrochener Lauf hinterlässt keine halbe Datei
    vorlaeufig = ziel.with_name(ziel.name + ".teil")
    vorlaeufig.write_bytes(daten)
    vorlaeufig.replace(ziel)
    return ziel


def _unicodes() -> list[int]:
    return [code for anfang, ende in BEREICHE for code in range(anfang, ende + 1)]


def _schnitt_bauen(quelle: Path, schnitt, ziel: Path) -> str:
    from fontTools import subset
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    font = TTFont(quelle)
    if "fvar" in font:
        achsen = {achse.axisTag: achse.defaultValue for achse in font["fvar"].axes}
        achsen.update(dict(schnitt.achsen))
        achsen["wght"] = schnitt.gewicht
        font = instancer.instantiateVariableFont(font, achsen, updateFontNames=True)

    optionen = subset.Options()
    optionen.layout_features = ["*"]
    optionen.name_IDs = [0, 1, 2, 3, 4, 5, 6, 13, 14, 16, 17]
    optionen.name_languages = [0x0409]
    optionen.hinting = False
    optionen.desubroutinize = True
    optionen.notdef_outline = True
    optionen.recalc_bounds = True
    optionen.flavor = "woff2"
    teiler = subset.Subsetter(options=optionen)
    teiler.populate(unicodes=_unicodes())
    teiler.subset(font)

    ziel.parent.mkdir(parents=True, exist_ok=True)
    font.flavor = "woff2"
    font.save(ziel)
    urheber = font["name"].getDebugName(0) or ""
    return urheber


def _urheber(datei: Path) -> str:
    from fontTools.ttLib import TTFont

    return TTFont(datei)["name"].getDebugName(0) or ""


def _css(modul) -> str:
    zeilen = [
        # Zusammengesetzt, damit die REUSE-Prüfung die Zeile nicht als Lizenzangabe dieses Skripts liest
        "/* SPDX-License-Identifier: AGPL-3.0-or-later */",
        "/* Erzeugt von scripts/schriften_bauen.py aus mandari/apps/common/schriften.py – nicht von Hand ändern.",
        "   Schriften für Dokumente (Editor, Seitenansicht, PDF); Lizenzen der Schriftdateien: REUSE.toml. */",
    ]
    for schrift in modul.SCHRIFTEN:
        for schnitt in schrift.schnitte:
            datei = Path(schrift.datei(schnitt)).name
            format_ = "truetype" if schrift.unveraendert else "woff2"
            zeilen += [
                "@font-face {",
                f'    font-family: "{schrift.name}";',
                f"    font-style: {'italic' if schnitt.kursiv else 'normal'};",
                f"    font-weight: {schnitt.gewicht};",
                # block: Der Satz misst erst, wenn die Schrift da ist (document.fonts.load); kein Springen
                "    font-display: block;",
                f'    src: url("{schrift.schluessel}/{datei}") format("{format_}");',
                "}",
            ]
    return "\n".join(zeilen) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--nur", help="nur diese Schlüssel (Komma-getrennt)")
    parser.add_argument("--quellen", type=Path, help="Ordner für die geladenen Quelldateien (Standard: temporär)")
    args = parser.parse_args()

    modul = _schriften_modul()
    nur = {s.strip() for s in args.nur.split(",") if s.strip()} if args.nur else None
    if nur is not None:
        unbekannt = sorted(nur - {s.schluessel for s in modul.SCHRIFTEN})
        if unbekannt or not nur:
            parser.error(f"unbekannte Schlüssel in --nur: {', '.join(unbekannt) or '(leer)'}")
    quellen = args.quellen or Path(tempfile.mkdtemp(prefix="mandari-schriften-"))
    urheber: dict[str, set[str]] = {}
    for schrift in modul.SCHRIFTEN:
        if nur and schrift.schluessel not in nur:
            continue
        for schnitt in schrift.schnitte:
            quelle = _laden(schrift.verzeichnis, schnitt.quelle, quellen)
            ziel = WURZEL / "mandari" / "static" / schrift.datei(schnitt)
            if schrift.unveraendert:
                # Reservierter Schriftname: Originaldatei byte-gleich übernehmen
                ziel.parent.mkdir(parents=True, exist_ok=True)
                ziel.write_bytes(quelle.read_bytes())
                angabe = _urheber(quelle)
            else:
                angabe = _schnitt_bauen(quelle, schnitt, ziel)
            urheber.setdefault(schrift.schluessel, set()).add(angabe)
            print(f"{ziel.relative_to(WURZEL)}  {ziel.stat().st_size // 1024} KB")
    (ZIEL / "schriften.css").write_text(_css(modul), encoding="utf-8", newline="\n")
    print("\nUrheberangaben aus den Schriftdateien (für REUSE.toml):")
    for schluessel, angaben in urheber.items():
        for angabe in sorted(angaben):
            print(f"  {schluessel}: {angabe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
