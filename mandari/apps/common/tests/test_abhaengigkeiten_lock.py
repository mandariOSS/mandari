# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Python-Abhängigkeiten der Django-Anwendung: eine Quelle (``pyproject.toml``), eine Lock-Datei
(``uv.lock``), aus der Image, CI, pip-audit und SBOM lesen (Issue #682).

Vorher standen die Abhängigkeiten in ``requirements.txt`` (Untergrenzen) und ``requirements.lock``.
Dependabot hob nur die Untergrenzen an, das Lockfile blieb alt, und jeder seiner PRs scheiterte.
Die Tests halten fest, was die Umstellung zusichert – ohne Netz; ob ``uv.lock`` im Ganzen zu
``pyproject.toml`` passt, prüft die CI mit ``uv lock --check``.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO = Path(__file__).resolve().parents[4]
PYPROJECT = REPO / "mandari" / "pyproject.toml"
UV_LOCK = REPO / "mandari" / "uv.lock"
DEPENDABOT = REPO / ".github" / "dependabot.yml"
EXPORT = REPO / "scripts" / "export_requirements.sh"
VERSIONSPRUEFUNG = REPO / "scripts" / "check_version_consistency.py"

# Lokales Paket aus ../shared: steht in uv.lock, wird aber getrennt installiert
LOKAL = {"mandari-oparl"}


def _direkte() -> list[Requirement]:
    projekt = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    return [Requirement(eintrag) for eintrag in projekt["dependencies"]]


def _gesperrt() -> dict[str, set[str]]:
    pakete = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))["package"]
    versionen: dict[str, set[str]] = {}
    for paket in pakete:
        if "version" in paket:
            versionen.setdefault(canonicalize_name(paket["name"]), set()).add(paket["version"])
    return versionen


def _alle_anforderungen() -> list[Requirement]:
    """Direkte Abhängigkeiten samt der Extras (Entwicklungswerkzeuge) – alles, was Dependabot in pyproject.toml liest."""
    projekt = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    eintraege = list(projekt["dependencies"])
    for extra in projekt.get("optional-dependencies", {}).values():
        eintraege.extend(extra)
    return [Requirement(eintrag) for eintrag in eintraege]


UPDATE_ARTEN = ("major", "minor", "patch")


def _ueberschreitende_update_arten(anforderung: Requirement) -> set[str]:
    """
    Update-Arten, die die Grenze der Anforderung überschreiten: ``<9`` → major, ``<6.2`` → major und minor,
    feste Version → alle. Ohne Obergrenze: keine.
    """
    arten: set[str] = set()
    for grenze in anforderung.specifier:
        if grenze.operator in ("==", "==="):
            return set(UPDATE_ARTEN)
        if grenze.operator in ("<", "<="):
            stellen = Version(grenze.version).release
        elif grenze.operator == "~=":
            # ~=1.4.2 heißt <1.5, ~=1.4 heißt <2
            stellen = Version(grenze.version).release[:-1]
        else:
            continue
        while len(stellen) > 1 and stellen[-1] == 0:
            stellen = stellen[:-1]
        arten.update(UPDATE_ARTEN[: len(stellen)])
    return arten


def _bei_dependabot_gesperrt() -> dict[str, set[str]]:
    """``ignore`` des uv-Eintrags für /mandari: Paket → Update-Arten, die Dependabot nicht vorschlägt."""
    eintraege = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))["updates"]
    (eintrag,) = [e for e in eintraege if e["package-ecosystem"] == "uv" and e.get("directory") == "/mandari"]
    gesperrt: dict[str, set[str]] = {}
    for regel in eintrag.get("ignore", []):
        assert "versions" not in regel, "Versionsbereiche unter ignore wertet dieser Test nicht aus"
        # Ohne update-types gilt die Regel für jede neue Version
        arten = {art.removeprefix("version-update:semver-") for art in regel.get("update-types", [])} or set(
            UPDATE_ARTEN
        )
        gesperrt.setdefault(canonicalize_name(regel["dependency-name"]), set()).update(arten)
    return gesperrt


def test_alte_requirements_dateien_sind_weg() -> None:
    # Eine zweite Liste neben pyproject.toml liefe wieder auseinander (und Dependabot läse sie mit).
    for name in ("requirements.txt", "requirements.lock"):
        assert not (REPO / "mandari" / name).exists(), (
            f"mandari/{name} ist zurück – Abhängigkeiten gehören in mandari/pyproject.toml, danach `uv lock`"
        )


def test_kein_ci_schritt_installiert_aus_einer_datei_die_es_nicht_gibt() -> None:
    """
    Ein parallel entstandener Job mit ``pip install -r mandari/requirements.lock`` lässt sich textuell
    konfliktfrei zusammenführen und scheitert erst danach (so geschehen beim OParl-Validator). Erlaubt sind
    Dateien im Repo und solche, die der Schritt selbst erzeugt (``/tmp/…`` aus ``export_requirements.sh``).
    """
    dateien = [
        *sorted((REPO / ".github" / "workflows").glob("*.y*ml")),
        *sorted((REPO / "scripts").glob("*.sh")),
        REPO / "Makefile",
        REPO / "mandari" / "Dockerfile",
        REPO / "ingestor" / "Dockerfile",
    ]
    fehlend = []
    for datei in dateien:
        for nummer, zeile in enumerate(datei.read_text(encoding="utf-8").splitlines(), start=1):
            if zeile.lstrip().startswith("#") or not re.search(r"\bpip\b", zeile):
                continue  # nur pip install, uv pip install und pip-audit
            for pfad in re.findall(r"\s-r\s+([^\s\"';|&)]+)", zeile):
                if pfad.startswith(("/tmp/", "$")):  # noqa: S108 – Pfad im CI-Schritt, kein Dateizugriff
                    continue
                if not any((basis / pfad).is_file() for basis in (REPO, REPO / "mandari", REPO / "ingestor")):
                    fehlend.append(f"{datei.relative_to(REPO).as_posix()}:{nummer}: -r {pfad}")
    assert not fehlend, (
        "Installation aus einer Datei, die es nicht gibt – für die Django-Anwendung stattdessen "
        "`sh scripts/export_requirements.sh /tmp/requirements.lock`:\n" + "\n".join(fehlend)
    )


def test_jede_direkte_abhaengigkeit_ist_gesperrt_und_erfuellt_ihre_grenze() -> None:
    gesperrt = _gesperrt()
    probleme = []
    for anforderung in _direkte():
        versionen = gesperrt.get(canonicalize_name(anforderung.name))
        if not versionen:
            probleme.append(f"{anforderung.name}: fehlt in uv.lock")
        elif not all(anforderung.specifier.contains(Version(v), prereleases=True) for v in versionen):
            probleme.append(
                f"{anforderung.name}: uv.lock hat {sorted(versionen)}, verlangt ist {anforderung.specifier}"
            )
    assert not probleme, "uv.lock passt nicht zu pyproject.toml (`uv lock` im Ordner mandari):\n" + "\n".join(probleme)


def test_jede_obergrenze_steht_bei_dependabot_unter_ignore() -> None:
    """
    Dependabot weitet Obergrenzen im PR auf, statt sie zu beachten (``<9`` wurde im Gruppen-PR zu ``<10``,
    ``<2.1`` zu ``<2.2``). Eine Grenze in pyproject.toml hält deshalb nur, wenn die passende Update-Art für
    das Paket unter ``ignore`` steht – sonst nimmt die nächste Gruppe z. B. Django 6.2 als Minor-Update mit.
    Umgekehrt bliebe ein ``ignore`` ohne Grenze unbemerkt stehen und das Paket bekäme keine Updates mehr.
    """
    gesperrt = _bei_dependabot_gesperrt()
    noetig: dict[str, set[str]] = {}
    for anforderung in _alle_anforderungen():
        arten = _ueberschreitende_update_arten(anforderung)
        if arten:
            noetig.setdefault(canonicalize_name(anforderung.name), set()).update(arten)

    fehlt = {name: sorted(arten - gesperrt.get(name, set())) for name, arten in noetig.items()}
    fehlt = {name: arten for name, arten in fehlt.items() if arten}
    assert not fehlt, (
        "Obergrenze oder feste Version in mandari/pyproject.toml ohne passenden Eintrag unter `ignore` "
        f"(uv, /mandari) in .github/dependabot.yml – Paket: fehlende Update-Arten: {fehlt}"
    )
    zu_viel = {name: sorted(arten - noetig.get(name, set())) for name, arten in gesperrt.items()}
    zu_viel = {name: arten for name, arten in zu_viel.items() if arten}
    assert not zu_viel, (
        "`ignore` in .github/dependabot.yml sperrt mehr, als mandari/pyproject.toml begrenzt – "
        f"Grenze dort eintragen oder den Eintrag entfernen: {zu_viel}"
    )


@pytest.mark.parametrize(
    ("anforderung", "erwartet"),
    [
        ("paket>=1.0", set()),
        ("paket>=8.12.0,<9", {"major"}),
        ("paket>=6.1,<6.2", {"major", "minor"}),
        ("paket>=2.0.36,<2.1.0", {"major", "minor"}),
        ("paket<=1.4.2", {"major", "minor", "patch"}),
        ("paket~=1.4", {"major"}),
        ("paket~=1.4.2", {"major", "minor"}),
        ("paket==0.14.6", {"major", "minor", "patch"}),
    ],
)
def test_update_arten_die_eine_grenze_ueberschreiten(anforderung: str, erwartet: set[str]) -> None:
    assert _ueberschreitende_update_arten(Requirement(anforderung)) == erwartet


def test_sqlalchemy_bringt_greenlet_ueber_das_extra_mit() -> None:
    """
    Das Image führt den Ingestor-Code aus /ingestor selbst aus (Abgleich aus dem Admin, ``sync_daemon``).
    Ab SQLAlchemy 2.1 kommt greenlet nur noch über das Extra ``[asyncio]``; ohne greenlet scheitert der
    Abgleich. Die Django-Tests ersetzen den Orchestrator und merkten das nicht.
    """
    (anforderung,) = [a for a in _direkte() if canonicalize_name(a.name) == "sqlalchemy"]
    assert "asyncio" in anforderung.extras, "sqlalchemy ohne [asyncio]: greenlet fiele mit SQLAlchemy 2.1 weg"

    pakete = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))["package"]
    (projekt,) = [paket for paket in pakete if paket["name"] == "mandari"]
    (eintrag,) = [a for a in projekt["dependencies"] if a["name"] == "sqlalchemy"]
    assert "asyncio" in eintrag.get("extra", []), "uv.lock folgt dem Extra nicht – `uv lock` im Ordner mandari"
    (sqlalchemy,) = [paket for paket in pakete if paket["name"] == "sqlalchemy"]
    assert {"name": "greenlet"} in sqlalchemy["optional-dependencies"]["asyncio"]
    assert "greenlet" in _gesperrt()


def test_greenlet_ist_installiert_und_die_asyncio_erweiterung_laedt() -> None:
    import greenlet  # noqa: F401
    import sqlalchemy.ext.asyncio  # noqa: F401


def test_kein_pymupdf_in_den_abhaengigkeiten() -> None:
    """Fitnessfunktion aus docs/adr/20260930-pdf-seitenanalyse-bibliothek.md: PyMuPDF (AGPL/kommerziell) bleibt draußen."""
    verboten = {"pymupdf", "pymupdfb", "fitz"}
    assert not verboten & set(_gesperrt()), "PyMuPDF ist gesperrt – siehe ADR zur PDF-Seitenanalyse"
    assert not verboten & {canonicalize_name(a.name) for a in _direkte()}


def test_projekt_ist_kein_paket_und_nutzt_das_lokale_shared_paket() -> None:
    uv = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["uv"]
    assert uv["package"] is False, "mandari ist eine Anwendung; uv darf sie nicht als Paket bauen"
    quelle = uv["sources"]["mandari-oparl"]
    assert quelle["path"] == "../shared", "mandari-oparl muss aus dem Repo kommen, nicht aus einer Paketquelle"


@pytest.fixture(scope="module")
def versionspruefung() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_version_consistency", VERSIONSPRUEFUNG)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


def test_versionspruefung_liest_die_projektversion_aus_uv_lock(versionspruefung: ModuleType) -> None:
    assert versionspruefung.aus_uv_lock() == versionspruefung.aus_pyproject()


def test_versionspruefung_meldet_abweichende_lock_datei(
    versionspruefung: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Version in pyproject.toml angehoben, ``uv lock`` vergessen: Der Image-Build bräche sonst erst später ab."""
    aktuell = versionspruefung.aus_pyproject()
    veraltet = tmp_path / "uv.lock"
    veraltet.write_text(
        UV_LOCK.read_text(encoding="utf-8").replace(
            f'name = "mandari"\nversion = "{aktuell}"', 'name = "mandari"\nversion = "0.0.1"'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(versionspruefung, "UV_LOCK", veraltet)
    monkeypatch.setattr(sys, "argv", ["check_version_consistency.py"])

    assert versionspruefung.main() == 1
    assert "mandari/uv.lock sagt 0.0.1" in capsys.readouterr().out


def _sh() -> str | None:
    if sys.platform == "win32":
        # Git Bash; das sh.exe aus System32 gibt es nicht, bash.exe dort wäre WSL
        kandidat = Path(r"C:\Program Files\Git\usr\bin\sh.exe")
        return str(kandidat) if kandidat.exists() else None
    return shutil.which("sh")


@pytest.mark.skipif(shutil.which("uv") is None or _sh() is None, reason="uv oder sh nicht vorhanden")
def test_export_liefert_nur_laufzeitpakete_mit_fester_version(tmp_path: Path) -> None:
    """Was CI, pip-audit und SBOM installieren bzw. prüfen: alle direkten Abhängigkeiten, keine Werkzeuge."""
    ziel = tmp_path / "requirements.lock"
    sh = _sh()
    assert sh is not None
    subprocess.run([sh, str(EXPORT), str(ziel)], check=True, capture_output=True, timeout=120)  # noqa: S603

    zeilen = [z for z in ziel.read_text(encoding="utf-8").splitlines() if z and not z.lstrip().startswith("#")]
    assert zeilen, "Export ist leer"
    namen = set()
    for zeile in zeilen:
        treffer = re.match(r"^([A-Za-z0-9._-]+)==[^\s;]+(\s*;.*)?$", zeile)
        assert treffer, f"keine feste Version: {zeile}"
        namen.add(canonicalize_name(treffer.group(1)))

    erwartet = {canonicalize_name(a.name) for a in _direkte()} - LOKAL
    assert erwartet <= namen, f"fehlen im Export: {sorted(erwartet - namen)}"
    assert not namen & LOKAL, "das lokale Paket aus ../shared gehört nicht in den Export"
    assert "greenlet" in namen, "greenlet fehlt im Export – der eingebaute Abgleich braucht es"
    assert not namen & {"pytest", "mypy", "ruff", "djlint"}, "Entwicklungswerkzeuge gehören nicht ins Image"
