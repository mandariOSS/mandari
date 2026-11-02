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
from typing import Any

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
HASHPRUEFUNG = REPO / "scripts" / "check_image_hashes.py"

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


# --- Images: nichts ohne Hash-Prüfung (scripts/check_image_hashes.py) ------------------------------------------
#
# Die Images installieren die Abhängigkeiten mit --require-hashes aus uv.lock. Die lokalen Pakete (shared/,
# ingestor/) brauchen beim Bau ihr Build-Backend; in einer isolierten Build-Umgebung holte der Installer es frisch
# aus dem Paketindex, ohne Version und Hash. Jetzt kommt es aus der Gruppe build-backend der Lock-Datei, und die
# lokalen Pakete entstehen ohne Netz.


@pytest.fixture(scope="module")
def hashpruefung() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_image_hashes", HASHPRUEFUNG)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


def test_images_installieren_nur_hash_gepruefte_pakete(hashpruefung: ModuleType) -> None:
    """Beide Dockerfiles und die Gruppe build-backend in mandari/uv.lock und ingestor/uv.lock."""
    probleme = hashpruefung.pruefe()
    assert not probleme, "\n".join(probleme)


# Kopf einer Builder-Stage wie in den Dockerfiles: uv baut nichts (UV_NO_BUILD), exportiert offline, und das
# Build-Backend kommt mit Hashes aus der Gruppe build-backend.
BUILDER = (
    "FROM python:3.14-slim AS builder\n"
    "ENV UV_NO_BUILD=1\n"
    "RUN uv export --quiet --locked --offline --only-group build-backend --no-emit-project \\\n"
    "        --format requirements.txt -o /tmp/build-backend.lock \\\n"
    "    && pip install --no-cache-dir --only-binary :all: --require-hashes -r /tmp/build-backend.lock\n"
)

OHNE_HASH = "ohne `--require-hashes`"
QUELLTEXT = "Quelltextpakete"
NO_BINARY = "`--no-binary`"
NUR_OHNE_NETZ = "nur mit `RUN --network=none`"
HEREDOC = "Heredoc"
AUSSERHALB = "außerhalb einer RUN-Anweisung"
UV_OHNE_NO_BUILD = "ohne `UV_NO_BUILD=1`"
UV_OHNE_OFFLINE = "ohne `--offline`"


def test_hashpruefung_laesst_gesperrten_und_netzlosen_bau_durch(hashpruefung: ModuleType) -> None:
    dockerfile = (
        BUILDER + "# Kommentar zwischen den Anweisungen\n"
        "RUN --network=none pip install --no-cache-dir --prefix=/install --no-deps --no-build-isolation \\\n"
        "    # Kommentar mitten in der Fortsetzung\n"
        "        --no-index /tmp/shared/\n"
        "RUN uv pip install --system --no-build --require-hashes -r /tmp/requirements.lock\n"
        "RUN uv pip install --system --require-hashes -r /tmp/requirements.lock  # --no-build kommt aus ENV\n"
        "RUN --network=none uv pip install --system --no-deps --offline /tmp/wheels/*.whl\n"
        # Ein # mitten im Wort beginnt keinen Kommentar
        "RUN pip install --require-hashes --only-binary=:all: -r /tmp/a#b.lock\n"
        "RUN python -m pip check && rm -rf /root/.cache/pip /root/.cache/uv\n"
        'CMD ["python", "-m", "src.main"]\n'
    )
    assert hashpruefung.pruefe_dockerfile(dockerfile) == []


@pytest.mark.parametrize(
    ("anweisung", "meldung"),
    [
        # Stand vor der Änderung: lokale Pakete mit Build-Isolation
        ("RUN pip install --no-cache-dir --prefix=/install --no-deps /tmp/shared/", OHNE_HASH),
        (
            "RUN uv pip install --system --no-deps /build/shared/ \\\n"
            "    && uv pip install --system --no-deps --no-sources /build/ingestor/",
            OHNE_HASH,
        ),
        # Ohne Hash nur ohne Netz und ohne Index, nicht eines von beiden
        ("RUN pip install --no-index --no-build-isolation /tmp/shared/", OHNE_HASH),
        ("RUN --network=none pip install --no-build-isolation /tmp/shared/", OHNE_HASH),
        ('RUN ["pip", "install", "/tmp/shared/"]', OHNE_HASH),
        ("RUN uv sync --locked --no-dev", OHNE_HASH),
        ("RUN uv pip sync --system /tmp/requirements.lock", OHNE_HASH),
        ("RUN pip3 install /tmp/shared/", OHNE_HASH),
        ("RUN pip3.14 install /tmp/shared/", OHNE_HASH),
        ("RUN python3 -m pip install /tmp/shared/", OHNE_HASH),
        # Hinter einer Shell, einer Befehlsersetzung oder einem anderen Befehl
        ('RUN sh -c "pip install /tmp/shared/"', OHNE_HASH),
        ("RUN echo $(pip install /tmp/shared/)", OHNE_HASH),
        ("RUN echo `pip install /tmp/shared/`", OHNE_HASH),
        ("RUN find /tmp -name '*.whl' -exec pip install {} +", OHNE_HASH),
        # Optionen im Shell-Kommentar zählen nicht; ein # mitten im Wort beendet den Befehl nicht
        ("RUN pip install /tmp/shared/  # --require-hashes --only-binary :all:", OHNE_HASH),
        ("RUN pip install --require-hashes --only-binary :all: -r /tmp/a#b && pip install /tmp/shared/", OHNE_HASH),
        # Hash-geprüft, aber ein Quelltextpaket würde mit frisch geladenem Build-Backend gebaut
        ("RUN pip install --prefix=/install --require-hashes -r /tmp/requirements.lock", QUELLTEXT),
        # pip kürzt Optionen ab: --no-build ist dort --no-build-isolation, kein Verbot von Quelltextpaketen
        ("RUN pip install --require-hashes --no-build -r /tmp/r.lock", QUELLTEXT),
        ("RUN pip install --require-hashes --only-binary :all: --only-binary :none: -r /tmp/r.lock", QUELLTEXT),
        ("RUN UV_NO_BUILD=0 uv pip install --system --require-hashes -r /tmp/r.lock", QUELLTEXT),
        ("RUN UV_NO_BUILD=0 uv pip install --system --no-build-isolation --require-hashes -r /tmp/r.lock", QUELLTEXT),
        ("RUN pip install --require-hashes --only-binary :all: --no-binary :all: -r /tmp/r.lock", NO_BINARY),
        ("RUN pip install --require-hashes --only-binary :all: --no-bin=foo -r /tmp/r.lock", NO_BINARY),
        ("RUN uv pip install --system --require-hashes --no-binary-package foo -r /tmp/r.lock", NO_BINARY),
        # Andere Installer laden ohne Hash-Prüfung: nur ohne Netz
        ("RUN python -m build --wheel /tmp/shared/", NUR_OHNE_NETZ),
        ("RUN pyproject-build /tmp/shared/", NUR_OHNE_NETZ),
        ("RUN uvx hatchling build", NUR_OHNE_NETZ),
        ("RUN pipx install hatch", NUR_OHNE_NETZ),
        # Nicht zeilenweise prüfbar: sicher abweisen
        ("RUN <<EOF\npip install /tmp/shared/\nEOF", HEREDOC),
        ("RUN <<-'EOF' bash\n\tset -e\n\techo ok\n\tEOF", HEREDOC),
        ("ONBUILD RUN pip install /tmp/shared/", AUSSERHALB),
        # uv nur mit UV_NO_BUILD=1 (ENV oder vor dem Befehl) oder --no-build
        ("RUN UV_NO_BUILD=0 uv export --locked --offline -o /tmp/r.lock", UV_OHNE_NO_BUILD),
        ("ENV UV_NO_BUILD=false\nRUN uv export --locked --offline -o /tmp/r.lock", UV_OHNE_NO_BUILD),
        ("FROM python:3.14-slim AS zweite\nRUN uv export --locked --offline -o /tmp/r.lock", UV_OHNE_NO_BUILD),
        # Für Metadaten, die nicht feststehen, baut uv lokale Pakete auch mit UV_NO_BUILD: Auflösen nur offline
        ("RUN uv export --locked --only-group build-backend -o /tmp/b.lock", UV_OHNE_OFFLINE),
        ("RUN uv lock --check", UV_OHNE_OFFLINE),
        ("RUN uv pip compile pyproject.toml -o /tmp/r.lock", UV_OHNE_OFFLINE),
        ("RUN UV_OFFLINE=0 uv tree", UV_OHNE_OFFLINE),
    ],
)
def test_hashpruefung_meldet_installation_ohne_hash(hashpruefung: ModuleType, anweisung: str, meldung: str) -> None:
    probleme = hashpruefung.pruefe_dockerfile(BUILDER + anweisung + "\n")
    assert any(meldung in problem for problem in probleme), probleme


@pytest.mark.parametrize(
    "anweisungen",
    [
        "FROM builder AS weiter\nRUN uv export --locked --offline -o /tmp/r.lock",
        "FROM python:3.14-slim\nRUN uv export --locked --offline --no-build -o /tmp/r.lock",
        "FROM python:3.14-slim\nRUN UV_NO_BUILD=1 uv export --locked --offline -o /tmp/r.lock",
        "FROM python:3.14-slim\nENV UV_NO_BUILD 1\nRUN uv export --locked --offline -o /tmp/r.lock",
        "FROM python:3.14-slim\nENV UV_NO_BUILD=1 UV_OFFLINE=1\nRUN uv export --locked -o /tmp/r.lock",
        "FROM python:3.14-slim\nENV UV_NO_BUILD=1\nRUN --network=none uv lock --check",
    ],
)
def test_hashpruefung_uv_mit_uv_no_build(hashpruefung: ModuleType, anweisungen: str) -> None:
    """ENV gilt in der Stage und in Stages, die auf ihr aufbauen; eine neue Basis beginnt ohne."""
    assert hashpruefung.pruefe_dockerfile(BUILDER + anweisungen + "\n") == []


def test_hashpruefung_findet_jedes_dockerfile_mit_installation(hashpruefung: ModuleType, tmp_path: Path) -> None:
    """Ein Dockerfile mit pip-/uv-Aufruf, das nicht in IMAGES steht, ist ein Fehler (außer unter docs/examples)."""
    mit_pip = "FROM python:3.14-slim\nRUN pip install --require-hashes --only-binary :all: -r /tmp/r.lock\n"
    dateien = {
        "dienst/Dockerfile": mit_pip,
        "dienst/Dockerfile.dev": "FROM python:3.14-slim\nENV UV_NO_BUILD=1\nRUN uv export -o /tmp/r.lock\n",
        "worker/gpu.Dockerfile": mit_pip,
        "backup/Dockerfile": "FROM postgres:16-alpine\nRUN apk add --no-cache openssh-client-default\n",
        "docs/examples/beispiel.Dockerfile": "FROM python:3.14-slim\nRUN pip install uv\n",
        "docs/examples/Dockerfile": "FROM python:3.14-slim\nRUN pip install uv\n",
        "mandari/node_modules/paket/Dockerfile": "FROM python:3.14-slim\nRUN pip install uv\n",
        ".venv/Dockerfile": "FROM python:3.14-slim\nRUN pip install uv\n",
    }
    for name, inhalt in dateien.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(inhalt, encoding="utf-8")

    probleme = hashpruefung.pruefe(images=(), wurzel=tmp_path)
    assert sorted(problem.split(":", 1)[0] for problem in probleme) == [
        "dienst/Dockerfile",
        "dienst/Dockerfile.dev",
        "worker/gpu.Dockerfile",
    ], probleme
    assert all("nicht in IMAGES" in problem for problem in probleme), probleme


def test_hashpruefung_verlangt_den_export_der_gruppe(hashpruefung: ModuleType) -> None:
    """Ein Kommentar mit dem Befehl zählt nicht."""
    dockerfile = "FROM python:3.14-slim\n# uv export --only-group build-backend\nRUN echo ok\n"
    (problem,) = hashpruefung.pruefe_dockerfile(dockerfile)
    assert "exportiert die Gruppe `build-backend` nicht" in problem


def _lock_paket(name: str, *abhaengigkeiten: str, wheels: bool = True) -> dict[str, Any]:
    paket: dict[str, Any] = {"name": name, "version": "1.0", "source": {"registry": "https://pypi.org/simple"}}
    paket["dependencies"] = [{"name": abhaengigkeit} for abhaengigkeit in abhaengigkeiten]
    if wheels:
        paket["wheels"] = [{"url": f"https://example.invalid/{name}-1.0-py3-none-any.whl", "hash": "sha256:00"}]
    else:
        paket["sdist"] = {"url": f"https://example.invalid/{name}-1.0.tar.gz", "hash": "sha256:00"}
    return paket


def test_hashpruefung_meldet_build_backend_ausserhalb_der_gruppe(hashpruefung: ModuleType) -> None:
    pyproject = {"project": {"name": "mandari"}}
    lock = {"package": [{"name": "mandari", "source": {"virtual": "."}}]}
    lokal = {"shared": {"build-system": {"requires": ["hatchling"]}}}
    (problem,) = hashpruefung.pruefe_build_backend(pyproject, lock, lokal, "mandari")
    assert "`hatchling` von shared fehlt in [dependency-groups] build-backend" in problem


def test_hashpruefung_meldet_abhaengigkeit_des_build_backends_ohne_wheel(hashpruefung: ModuleType) -> None:
    pyproject = {"project": {"name": "mandari"}, "dependency-groups": {"build-backend": ["hatchling"]}}
    projekt = {
        "name": "mandari",
        "source": {"virtual": "."},
        "dev-dependencies": {"build-backend": [{"name": "hatchling"}]},
    }
    lock = {"package": [projekt, _lock_paket("hatchling", "pluggy"), _lock_paket("pluggy", wheels=False)]}
    lokal = {"shared": {"build-system": {"requires": ["hatchling>=1.26"]}}}
    assert hashpruefung.pruefe_build_backend(pyproject, lock, lokal, "mandari") == [
        "mandari/uv.lock: `pluggy` (Gruppe build-backend) ohne Wheels mit Hash"
    ]


def test_hashpruefung_laeuft_in_der_ci_und_vor_dem_bau_der_release_images() -> None:
    """Der Job „Test“ läuft bei einer reinen Dockerfile-Änderung nicht, der Job „Qualität“ schon."""
    workflows = REPO / ".github" / "workflows"
    pr_check = yaml.safe_load((workflows / "pr-check.yml").read_text(encoding="utf-8"))
    qualitaet = [str(schritt.get("run", "")) for schritt in pr_check["jobs"]["qualitaet"]["steps"]]
    assert any("scripts/check_image_hashes.py" in run for run in qualitaet), "Schritt im Job qualitaet fehlt"

    release = yaml.safe_load((workflows / "release.yml").read_text(encoding="utf-8"))
    schritte = release["jobs"]["build"]["steps"]
    pruefung = [i for i, s in enumerate(schritte) if "scripts/check_image_hashes.py" in str(s.get("run", ""))]
    bauten = [i for i, s in enumerate(schritte) if str(s.get("uses", "")).startswith("docker/build-push-action")]
    assert pruefung and bauten and pruefung[0] < bauten[0], "Release prüft vor dem ersten Image-Bau"


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
