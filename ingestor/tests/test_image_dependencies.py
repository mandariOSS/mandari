# SPDX-License-Identifier: AGPL-3.0-or-later
"""Das Ingestor-Image enthält die Abhängigkeiten aus ``uv.lock`` (Issue #636).

Früher löste der Docker-Build die Untergrenzen aus ``pyproject.toml`` bei jedem Bauen gegen den neuesten
PyPI-Stand auf. So kam SQLAlchemy 2.1 ins Image – ohne ``greenlet``, das ab 2.1 nicht mehr automatisch
mitkommt – und ``import src.main`` scheiterte beim Start. Getestet wurde derweil der Stand aus dem Lockfile.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement

INGESTOR = Path(__file__).resolve().parent.parent
DOCKERFILE = INGESTOR / "Dockerfile"


def _anweisungen() -> list[str]:
    """Dockerfile-Anweisungen ohne Kommentare, Fortsetzungszeilen zusammengefügt."""
    zeilen = [z.strip() for z in DOCKERFILE.read_text(encoding="utf-8").splitlines()]
    text = "\n".join(z for z in zeilen if z and not z.startswith("#"))
    return [" ".join(a.split()) for a in text.replace("\\\n", " ").splitlines()]


def test_sqlalchemy_mit_asyncio_extra() -> None:
    """Der Ingestor nutzt ``sqlalchemy.ext.asyncio``; dafür braucht es ``greenlet`` (Extra ``asyncio``)."""
    daten = tomllib.loads((INGESTOR / "pyproject.toml").read_text(encoding="utf-8"))
    anforderungen = {r.name.lower(): r for r in map(Requirement, daten["project"]["dependencies"])}
    assert "asyncio" in anforderungen["sqlalchemy"].extras


def test_image_installiert_aus_dem_lockfile() -> None:
    anweisungen = _anweisungen()
    assert any(a.startswith("COPY") and "ingestor/uv.lock" in a for a in anweisungen)
    assert any("uv export --frozen" in a for a in anweisungen), "Abhängigkeiten nicht aus uv.lock exportiert"
    # Jede Installation eines Projektverzeichnisses ohne --no-deps löste die Abhängigkeiten wieder live auf
    for anweisung in anweisungen:
        if anweisung.startswith("RUN") and "uv pip install" in anweisung:
            for teil in anweisung.split("&&"):
                if "uv pip install" in teil and " -r " not in teil:
                    assert "--no-deps" in teil, f"Installation ohne Lockfile: {teil.strip()}"


def test_build_prueft_die_importierbarkeit() -> None:
    """Ein Image, dessen Paket sich nicht importieren lässt, darf gar nicht erst entstehen."""
    assert 'RUN python -c "import src.main"' in _anweisungen()
