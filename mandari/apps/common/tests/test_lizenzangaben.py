# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lizenzangaben (Issue #578): Oberfläche und Vorlagen nennen „AGPL-3.0-or-later“, die
Markenlizenz ``LicenseRef-Mandari-Brand`` gilt in ``REUSE.toml`` nur für Kennzeichen.

Urheberangaben (Issue #736): einheitlich Sven Konopka, zentral in ``REUSE.toml`` und in den
Paketmetadaten; keine Sammelangaben in Dateiköpfen.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from fnmatch import fnmatch
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
TEMPLATES = REPO / "mandari" / "templates"

#: „AGPL-3.0“ ohne Zusatz, „AGPL 3.0“, „AGPLv3“ – nicht aber „AGPL-3.0-or-later“/„-only“
UNGENAU = re.compile(r"AGPL[- ]?(?:v)?3(?:\.0)?(?!\.0)(?!-or-later|-only)", re.IGNORECASE)

#: Sichtbare Stellen außerhalb der Templates (Banner der Installationsskripte)
WEITERE_OBERFLAECHEN = ("install.sh", "install-k8s.sh")


#: SPDX-Kopfzeile, die bei der Suche nach ungenauen Angaben nicht zählt. Zusammengesetzt, damit
#: ``reuse lint`` die Zeichenkette nicht als Lizenzangabe dieser Datei liest (Issue #690).
SPDX_KOPF = "SPDX-License-" + "Identifier: AGPL-3.0-or-later"


def _fundstellen(pfad: Path) -> list[str]:
    text = pfad.read_text(encoding="utf-8")
    return [
        f"{pfad.relative_to(REPO).as_posix()}:{nr}: {zeile.strip()}"
        for nr, zeile in enumerate(text.splitlines(), start=1)
        if UNGENAU.search(zeile.replace(SPDX_KOPF, ""))
    ]


@pytest.mark.parametrize(
    "beispiel",
    ["Open Source unter AGPL-3.0 ·", "AGPL-3.0 Lizenz", "AGPL 3.0", "AGPLv3", "unter AGPL-3.0."],
)
def test_muster_erkennt_ungenaue_angaben(beispiel: str) -> None:
    assert UNGENAU.search(beispiel)


@pytest.mark.parametrize("beispiel", ["AGPL-3.0-or-later", "AGPL-3.0-only", "Lizenz AGPL-3.0-or-later."])
def test_muster_laesst_genaue_angaben_durch(beispiel: str) -> None:
    assert not UNGENAU.search(beispiel)


def test_vorlagen_und_banner_nennen_agpl_or_later() -> None:
    dateien = [p for p in TEMPLATES.rglob("*") if p.suffix in {".html", ".txt", ".xml"}]
    dateien += [REPO / name for name in WEITERE_OBERFLAECHEN]
    fundstellen = [f for pfad in dateien for f in _fundstellen(pfad)]
    assert not fundstellen, "Lizenzangabe ohne „-or-later“:\n" + "\n".join(fundstellen)


@pytest.mark.parametrize(
    "vorlage",
    ["components/footer.html", "accounts/base_auth.html", "emails/base_email.html"],
)
def test_fusszeilen_nennen_die_lizenz(vorlage: str) -> None:
    assert "AGPL-3.0-or-later" in (TEMPLATES / vorlage).read_text(encoding="utf-8")


def _marken_muster() -> list[str]:
    daten = tomllib.loads((REPO / "REUSE.toml").read_text(encoding="utf-8"))
    muster: list[str] = []
    for eintrag in daten["annotations"]:
        if eintrag["SPDX-License-Identifier"] == "LicenseRef-Mandari-Brand":
            pfade = eintrag["path"]
            muster += [pfade] if isinstance(pfade, str) else list(pfade)
    return muster


def _unter_marke(pfad: str) -> bool:
    return any(fnmatch(pfad, muster) for muster in _marken_muster())


def test_markenlizenz_gilt_nur_fuer_kennzeichen() -> None:
    kennzeichen = [
        "mandari/static/brand/favicon.svg",
        "mandari/static/brand/icon-512.png",
        "mandari/static/images/favicon.ico",
        "mandari/static/images/favicon.svg",
        "docs/assets/logo.svg",
    ]
    for pfad in kennzeichen:
        assert (REPO / pfad).exists(), pfad
        assert _unter_marke(pfad), f"{pfad} ist ein Kennzeichen und gehört unter die Markenlizenz"

    assert not _unter_marke("mandari/static/images/og-default.png"), "Vorschaubild ist kein Kennzeichen"
    # Keine ganzen Bildordner mehr unter der Markenlizenz, außer dem Ordner nur für Kennzeichen
    assert all(m == "mandari/static/brand/**" or not m.endswith("/**") for m in _marken_muster())


# --- Urheberangaben (Issue #736) -----------------------------------------------------------------

#: Urheber des Projekts. Die Angabe steht zentral in ``REUSE.toml``; Fremdbibliotheken behalten ihre.
URHEBER = "Sven Konopka"

#: Zusammengesetzt wie ``SPDX_KOPF``, damit ``reuse lint`` die Muster nicht als Urheberangabe dieser
#: Datei liest: SPDX-Zeile, Schreibweise mit „(C)“ und das Zeichen U+00A9.
URHEBER_ZEILE = re.compile(
    "(?:SPDX-File" + "CopyrightText:|Copy" + r"right\s*\([Cc]\)|" + chr(0xA9) + ")(?P<angabe>.*)"
)

#: So viele Zeilen gelten als Dateikopf
KOPF_ZEILEN = 10

#: Pfade, deren Angaben nicht vom Projekt stammen (Fremdbibliotheken, Lizenztexte, Testdaten)
FREMDE_PFADE = ("LICENSES/", "mandari/static/vendor/", "ingestor/tests/fixtures/")


def _eigene_annotationen() -> list[dict[str, object]]:
    daten = tomllib.loads((REPO / "REUSE.toml").read_text(encoding="utf-8"))
    eigene: list[dict[str, object]] = []
    for eintrag in daten["annotations"]:
        pfade = eintrag["path"]
        pfade = [pfade] if isinstance(pfade, str) else list(pfade)
        if not all(p.startswith("mandari/static/vendor/") for p in pfade):
            eigene.append(eintrag)
    return eigene


def test_reuse_toml_nennt_den_urheber() -> None:
    daten = tomllib.loads((REPO / "REUSE.toml").read_text(encoding="utf-8"))
    assert daten["SPDX-PackageSupplier"] == URHEBER

    eigene = _eigene_annotationen()
    assert eigene, "REUSE.toml ohne eigene Zuordnungen?"
    for eintrag in eigene:
        angabe = eintrag["SPDX-FileCopyrightText"]
        assert isinstance(angabe, str)
        assert re.fullmatch(r"\d{4}(?:-\d{4})? " + URHEBER, angabe), (eintrag["path"], angabe)


@pytest.mark.parametrize(
    "pyproject",
    ["pyproject.toml", "mandari/pyproject.toml", "ingestor/pyproject.toml", "shared/pyproject.toml"],
)
def test_paketmetadaten_nennen_den_urheber(pyproject: str) -> None:
    daten = tomllib.loads((REPO / pyproject).read_text(encoding="utf-8"))
    assert daten["project"]["authors"] == [{"name": URHEBER}]


#: Urheberzeichen in Vorlagen, als Zeichen oder als HTML-Entität
URHEBERZEICHEN = re.compile("(?:" + chr(0xA9) + "|&copy;)")

#: Fremde Namensnennungen in Vorlagen (Kartendaten von OpenStreetMap)
FREMDE_NAMENSNENNUNG = re.compile(r"openstreetmap", re.IGNORECASE)


@pytest.mark.parametrize(
    "vorlage",
    ["components/footer.html", "accounts/base_auth.html", "emails/base_email.html", "errors/base_error.html"],
)
def test_fusszeilen_nennen_den_urheber(vorlage: str) -> None:
    assert URHEBER in (TEMPLATES / vorlage).read_text(encoding="utf-8")


def test_urheberzeichen_in_vorlagen_nennen_den_urheber() -> None:
    """Jedes Urheberzeichen in einer Vorlage nennt den Urheber – oder gehört zu einer fremden Namensnennung."""
    fundstellen: list[str] = []
    for pfad in TEMPLATES.rglob("*"):
        if pfad.suffix not in {".html", ".txt", ".xml"}:
            continue
        text = pfad.read_text(encoding="utf-8")
        for treffer in URHEBERZEICHEN.finditer(text):
            umgebung = text[treffer.start() : treffer.start() + 120]
            if URHEBER in umgebung or FREMDE_NAMENSNENNUNG.search(umgebung):
                continue
            zeile = text.count("\n", 0, treffer.start()) + 1
            fundstellen.append(f"{pfad.relative_to(REPO).as_posix()}:{zeile}: {umgebung.splitlines()[0].strip()}")
    assert not fundstellen, "Urheberzeichen ohne Urheber:\n" + "\n".join(fundstellen)


def test_muster_erkennt_urheberangaben_im_kopf() -> None:
    assert URHEBER_ZEILE.search("# SPDX-File" + "CopyrightText: 2027 Erika Mustermann")
    assert URHEBER_ZEILE.search("# Copy" + "right (C) 2024 Mandari Contributors")
    assert URHEBER_ZEILE.search("<!-- " + chr(0xA9) + " 2026 Beispiel -->")
    assert not URHEBER_ZEILE.search("# SPDX-License-" + "Identifier: AGPL-3.0-or-later")


def _versionierte_dateien() -> list[str]:
    if shutil.which("git") is None:
        pytest.skip("git nicht verfügbar")
    ergebnis = subprocess.run(  # noqa: S603 — fester Aufruf im Test
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    if ergebnis.returncode != 0:
        pytest.skip("kein Git-Arbeitsverzeichnis")
    return [p for p in ergebnis.stdout.decode("utf-8").split("\0") if p]


def test_dateikoepfe_ohne_abweichende_sammelangabe() -> None:
    """Keine Sammelangabe „… Contributors“ in Dateiköpfen; Beitragende stehen mit Namen da."""
    fundstellen: list[str] = []
    for pfad in _versionierte_dateien():
        if pfad.startswith(FREMDE_PFADE):
            continue
        datei = REPO / pfad
        try:
            with datei.open(encoding="utf-8") as f:
                kopf = [next(f, "") for _ in range(KOPF_ZEILEN)]
        except (UnicodeDecodeError, OSError):
            continue  # Binärdateien und nicht lesbare Dateien tragen keinen Kopf
        for nr, zeile in enumerate(kopf, start=1):
            treffer = URHEBER_ZEILE.search(zeile)
            if treffer and "contributors" in treffer.group("angabe").lower():
                fundstellen.append(f"{pfad}:{nr}: {zeile.strip()}")
    assert not fundstellen, "Sammelangabe statt Urheber im Dateikopf:\n" + "\n".join(fundstellen)
