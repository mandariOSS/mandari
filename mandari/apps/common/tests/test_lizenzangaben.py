# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lizenzangaben (Issue #578): Oberfläche und Vorlagen nennen „AGPL-3.0-or-later“, die
Markenlizenz ``LicenseRef-Mandari-Brand`` gilt in ``REUSE.toml`` nur für Kennzeichen.
"""

from __future__ import annotations

import re
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


def _fundstellen(pfad: Path) -> list[str]:
    text = pfad.read_text(encoding="utf-8")
    return [
        f"{pfad.relative_to(REPO).as_posix()}:{nr}: {zeile.strip()}"
        for nr, zeile in enumerate(text.splitlines(), start=1)
        if UNGENAU.search(zeile.replace("SPDX-License-Identifier: AGPL-3.0-or-later", ""))
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
