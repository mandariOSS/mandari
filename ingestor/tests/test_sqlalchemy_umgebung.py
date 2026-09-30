"""
SQLAlchemy-Umgebung des Ingestors: greenlet ist vorhanden, und die Modelle laden ohne Abkündigungswarnung.

Hintergrund: Ab SQLAlchemy 2.1 kommt ``greenlet`` nicht mehr von selbst mit, sondern nur über das Extra
``sqlalchemy[asyncio]``. Ein Image ohne greenlet startete den Ingestor nicht. Außerdem meldet 2.1 Annotationen,
die im Klassenrumpf statt des Typs eine Spalte treffen (Attribut ``date`` neben dem Typ ``date``).
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

INGESTOR = Path(__file__).resolve().parents[1]


def test_abhaengigkeit_nennt_das_asyncio_extra() -> None:
    projekt = tomllib.loads((INGESTOR / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    abhaengigkeiten = projekt["dependencies"]
    sqlalchemy = [a for a in abhaengigkeiten if a.replace(" ", "").lower().startswith("sqlalchemy")]
    assert len(sqlalchemy) == 1, sqlalchemy
    assert sqlalchemy[0].lower().startswith("sqlalchemy[asyncio]"), (
        "ohne das Extra [asyncio] fehlt greenlet ab SQLAlchemy 2.1 – der Ingestor startet dann nicht"
    )


def test_greenlet_ist_installiert_und_die_asyncio_erweiterung_laedt() -> None:
    import greenlet  # noqa: F401
    import sqlalchemy.ext.asyncio  # noqa: F401
    from sqlalchemy.util import concurrency

    # Ohne greenlet liefert SQLAlchemy 2.0 hier False und scheitert erst beim ersten Datenbankzugriff
    assert getattr(concurrency, "have_greenlet", True) is True


PRUEFSKRIPT = """
import warnings

with warnings.catch_warnings(record=True) as gefangen:
    warnings.simplefilter("always")
    import sqlalchemy.orm
    from sqlalchemy.exc import SADeprecationWarning

    import src.storage.models as m

    sqlalchemy.orm.configure_mappers()

for w in gefangen:
    if issubclass(w.category, SADeprecationWarning):
        print(f"WARNUNG {w.filename}:{w.lineno}: {w.message}")
print("Spaltentyp", type(m.OParlPaper.__table__.c.date.type).__name__)
"""


def test_modelle_laden_ohne_abkuendigungswarnung() -> None:
    """
    Frischer Interpreter, damit der Import der Modelle wirklich ausgeführt wird (nicht aus dem Modul-Cache).
    Die Warnungen werden gesammelt statt in Fehler verwandelt: SQLAlchemy fängt Ausnahmen beim Auswerten
    von Annotationen ab, ein ``-W error`` bliebe wirkungslos.
    """
    ergebnis = subprocess.run(  # noqa: S603 – fester Aufruf im Test
        [sys.executable, "-c", PRUEFSKRIPT],
        cwd=INGESTOR,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert ergebnis.returncode == 0, ergebnis.stderr[-2000:]
    assert "WARNUNG" not in ergebnis.stdout, ergebnis.stdout
    assert "Spaltentyp Date" in ergebnis.stdout
