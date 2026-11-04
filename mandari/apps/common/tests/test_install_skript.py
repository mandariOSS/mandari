# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``install.sh`` und Release-Workflow: Jedes angebotene Image-Tag gibt es für alle drei Anwendungs-Images (Issue #698).

``docker-compose.yml`` zieht mandari, ingestor und website mit einem gemeinsamen ``IMAGE_TAG``. Früher bot der
Installer den Kanal ``beta`` an, den es für kein Image gab, und ``--tag v0.11.0`` scheiterte an der Website, die
keine Versions-Tags bekam. Jetzt prüft der Installer das Tag, bevor er etwas anlegt oder löscht.

Das Skript läuft gegen ein nachgebautes ``docker`` (protokolliert nur die Aufrufe), damit die CI ohne Docker prüft.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[4]
SKRIPT = WURZEL / "install.sh"
COMPOSE = WURZEL / "docker-compose.yml"
RELEASE_WORKFLOW = WURZEL / ".github" / "workflows" / "release.yml"
ENV_BEISPIEL = WURZEL / ".env.example"
CHANGELOG = WURZEL / "CHANGELOG.md"
# Dateien, die Selbstbetreibern Image-Tags anbieten (Installer, Update, Kubernetes, Anleitungen)
ANGEBOTENE_TAGS_IN = (
    SKRIPT,
    WURZEL / "update.sh",
    WURZEL / "install-k8s.sh",
    ENV_BEISPIEL,
    WURZEL / "DEPLOYMENT.md",
    WURZEL / "README.md",
    WURZEL / "deploy" / "kubernetes" / "README.md",
    *sorted((WURZEL / "deploy" / "kubernetes" / "helm" / "mandari").glob("values*.yaml")),
)

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$DOCKER_LOG"
case "$1" in
  version) echo "29.0.0" ;;
  volume) exit 1 ;;
  manifest)
    # FAKE_FEHLT: Images, die es in der Registry nicht gibt (durch Leerzeichen getrennt)
    case " ${FAKE_FEHLT:-} " in
      *" $3 "*) echo "manifest unknown" >&2; exit 1 ;;
    esac
    ;;
  # Jeder Container läuft und ist gesund (Installation bis zum Ende)
  inspect)
    case "$*" in
      *"{{.State.Status}}"*) echo "running" ;;
      *) echo "healthy" ;;
    esac
    ;;
  exec)
    # FAKE_PASSWORT_ABGELEHNT: die Passwortprüfung der Anwendung lehnt ab
    case "$*" in
      *validate_password*)
        if [ -n "${FAKE_PASSWORT_ABGELEHNT:-}" ]; then
          echo "django.core.exceptions.ValidationError: ['Dieses Passwort ist zu üblich.']" >&2
          exit 1
        fi
        ;;
    esac
    ;;
esac
exit 0
"""

# crontab nur nachgebaut: Der Test darf die Crontab des Rechners nie ändern
FAKE_CRONTAB = """#!/usr/bin/env bash
case "$1" in
  -l) exit 1 ;;
  -) cat > "$CRONTAB_DATEI" ;;
esac
"""

#: Umgebung des Installers, die der Test selbst setzt (nie aus dem Rechner übernehmen)
INSTALLER_UMGEBUNG = {"COMPOSE_PROJECT_NAME", "IMAGE_TAG", "DOMAIN", "ACME_EMAIL", "ADMIN_EMAIL", "ADMIN_PASSWORD"}


def _bash() -> str | None:
    if os.name == "nt":
        # Git Bash; das bash.exe aus System32 wäre WSL
        kandidat = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git" / "usr" / "bin" / "bash.exe"
        return str(kandidat) if kandidat.exists() else None
    return shutil.which("bash")


BASH = _bash()


@dataclass
class Lauf:
    rc: int
    aufrufe: list[str]
    ausgabe: str
    arbeit: Path


def _lauf(
    tmp_path: Path,
    *args: str,
    fehlt: str = "",
    env_datei: str | None = None,
    eingabe: str = "",
    umgebung: dict[str, str] | None = None,
    dateien: dict[str, str] | None = None,
) -> Lauf:
    if BASH is None:
        pytest.skip("bash nicht vorhanden")
    arbeit = tmp_path / "mandari"
    arbeit.mkdir()
    # LF erzwingen (Checkout unter Windows kann CRLF liefern)
    (arbeit / "install.sh").write_bytes(SKRIPT.read_bytes().replace(b"\r\n", b"\n"))
    if env_datei is not None:
        (arbeit / ".env").write_text(env_datei, encoding="utf-8")
    for name, inhalt in (dateien or {}).items():
        (arbeit / name).write_text(inhalt, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, inhalt in (("docker", FAKE_DOCKER), ("crontab", FAKE_CRONTAB)):
        programm = bin_dir / name
        programm.write_bytes(inhalt.encode())
        programm.chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()

    env = {k: v for k, v in os.environ.items() if k not in INSTALLER_UMGEBUNG}
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "DOCKER_LOG": str(log),
            "CRONTAB_DATEI": str(tmp_path / "crontab"),
            "FAKE_FEHLT": fehlt,
            **(umgebung or {}),
        }
    )
    ergebnis = subprocess.run(  # noqa: S603 — fester Aufruf im Test
        [BASH, "install.sh", *args],
        cwd=arbeit,
        env=env,
        # Antworten auf Rückfragen; leer = sofort Dateiende. Als Bytes, damit "\n" nicht zu "\r\n" wird (Windows).
        input=eingabe.encode(),
        capture_output=True,
        timeout=120,
        check=False,
    )
    return Lauf(
        rc=ergebnis.returncode,
        aufrufe=log.read_text(encoding="utf-8").splitlines(),
        ausgabe=(ergebnis.stdout + ergebnis.stderr).decode("utf-8", errors="replace"),
        arbeit=arbeit,
    )


def test_fehlendes_image_bricht_vor_jeder_aenderung_ab(tmp_path: Path) -> None:
    lauf = _lauf(tmp_path, "--unattended", "--tag", "v9.9.9", fehlt="ghcr.io/mandarioss/website:v9.9.9")

    assert lauf.rc == 1, lauf.ausgabe
    assert "website" in lauf.ausgabe and "fehlt" in lauf.ausgabe, lauf.ausgabe
    assert "nichts verändert" in lauf.ausgabe, lauf.ausgabe
    assert "v0.11.0" in lauf.ausgabe and "/releases" in lauf.ausgabe, "die Meldung nennt gültige Werte"
    assert not (lauf.arbeit / ".env").exists()
    for image in ("mandari", "ingestor", "website"):
        assert any(f"manifest inspect ghcr.io/mandarioss/{image}:v9.9.9" in z for z in lauf.aufrufe), lauf.aufrufe
    assert not any(z.startswith(("compose up", "compose pull", "compose down", "volume")) for z in lauf.aufrufe)


def test_neuinstallation_loescht_nichts_wenn_das_tag_fehlt(tmp_path: Path) -> None:
    """Mit ``--reinstall-destroy-data`` würden Container und Volumes entfernt – erst, wenn das Tag existiert."""
    lauf = _lauf(
        tmp_path,
        "--unattended",
        "--reinstall-destroy-data",
        "--tag",
        "v9.9.9",
        fehlt="ghcr.io/mandarioss/mandari:v9.9.9",
        env_datei="IMAGE_TAG=v0.11.0\n",
    )

    assert lauf.rc == 1, lauf.ausgabe
    assert (lauf.arbeit / ".env").read_text(encoding="utf-8") == "IMAGE_TAG=v0.11.0\n"
    assert not list(lauf.arbeit.glob(".env.vor-neuinstallation-*"))
    assert not any("compose down" in z for z in lauf.aufrufe), lauf.aufrufe


def test_interaktive_neuinstallation_klaert_die_version_vor_dem_loeschen(tmp_path: Path) -> None:
    """Interaktiv ohne ``--tag``: Nach „Neu installieren“ kommt erst die Kanalwahl samt Prüfung, dann ``down -v``."""
    lauf = _lauf(
        tmp_path,
        fehlt="ghcr.io/mandarioss/website:latest",
        env_datei="IMAGE_TAG=latest\n",
        eingabe="1\n1\n",  # 1) Neu installieren, 1) latest
    )

    assert lauf.rc == 1, lauf.ausgabe
    assert "Release-Kanal wählen" in lauf.ausgabe, lauf.ausgabe
    assert "website" in lauf.ausgabe and "fehlt" in lauf.ausgabe, lauf.ausgabe
    assert (lauf.arbeit / ".env").read_text(encoding="utf-8") == "IMAGE_TAG=latest\n"
    assert not list(lauf.arbeit.glob(".env.vor-neuinstallation-*"))
    assert not any(z.startswith(("compose down", "volume rm")) for z in lauf.aufrufe), lauf.aufrufe


def test_interaktive_neuinstallation_mit_vorhandener_version_raeumt_erst_danach_ab(tmp_path: Path) -> None:
    """Gegenprobe: Gibt es die gewählte Version, wird die alte Installation wie bisher entfernt – nach der Prüfung."""
    lauf = _lauf(tmp_path, env_datei="IMAGE_TAG=latest\n", eingabe="1\n2\n")  # 1) Neu installieren, 2) dev

    aufrufe = lauf.aufrufe
    pruefung = aufrufe.index("manifest inspect ghcr.io/mandarioss/website:dev")
    abbau = next(i for i, z in enumerate(aufrufe) if z.startswith("compose down -v"))
    assert pruefung < abbau, aufrufe
    assert lauf.ausgabe.count("Release-Kanal wählen") == 1, "nach der Neuinstallations-Frage nicht erneut fragen"
    assert list(lauf.arbeit.glob(".env.vor-neuinstallation-*"))


def test_vorhandenes_tag_wird_geprueft_und_der_installer_laeuft_weiter(tmp_path: Path) -> None:
    """Unbeaufsichtigt ohne ``--tag``: geprüft wird latest; danach greift die Prüfung auf eine bestehende Installation."""
    lauf = _lauf(tmp_path, "--unattended", env_datei="IMAGE_TAG=latest\n")

    assert any("manifest inspect ghcr.io/mandarioss/website:latest" in z for z in lauf.aufrufe), lauf.aufrufe
    assert "bereits eine Installation" in lauf.ausgabe, lauf.ausgabe
    assert lauf.rc == 1


def _kanaele_im_installer() -> set[str]:
    text = SKRIPT.read_text(encoding="utf-8")
    menue = text[text.index("Release-Kanal wählen") :]
    menue = menue[: menue.index("esac")]
    return set(re.findall(r'IMAGE_TAG="([a-z]+)"', menue))


def _bewegliche_tags_im_release_workflow() -> set[str]:
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    return set(re.findall(r"/mandari:([a-z]+)[,\"]", text))


def test_jeder_kanal_des_installers_entsteht_im_release_workflow() -> None:
    kanaele = _kanaele_im_installer()
    assert kanaele == {"latest", "dev"}, kanaele
    assert kanaele <= _bewegliche_tags_im_release_workflow()


def test_website_bekommt_im_release_workflow_dieselben_tags() -> None:
    """Ein IMAGE_TAG für alle drei Images: Die Website-Tags leiten sich aus den mandari-Tags ab und werden geprüft."""
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"WEBSITE_TAGS=.*/mandari:#.*/website:#", text), "Website-Tags aus den mandari-Tags"
    assert "docker buildx imagetools create" in text
    assert "scripts/check_image_tags.py" in text, "nach dem Veröffentlichen anonym prüfen"
    assert "- beta" not in text and ":beta" not in text


def test_kein_angebot_eines_nicht_existierenden_kanals_beta() -> None:
    for datei in ANGEBOTENE_TAGS_IN:
        text = datei.read_text(encoding="utf-8")
        assert not re.search(r"\bbeta\b", text, flags=re.IGNORECASE), datei.name


def _veroeffentlichte_versionen() -> set[str]:
    """Versionen mit Datum im CHANGELOG (``## [0.11.0] – 2026-09-27``); ``[Unreleased]`` zählt nicht."""
    text = CHANGELOG.read_text(encoding="utf-8")
    return set(re.findall(r"^## \[(\d+\.\d+\.\d+[^\]]*)\]\s+[–-]\s+\d{4}-\d{2}-\d{2}", text, flags=re.MULTILINE))


def test_beispielversionen_sind_veroeffentlicht() -> None:
    """Beispiele wie ``--tag v1.2.3`` oder ``image.tag=v0.12.0`` zeigten auf Tags, die es nicht gibt."""
    veroeffentlicht = _veroeffentlichte_versionen()
    assert "0.11.0" in veroeffentlicht, veroeffentlicht
    erfundene = sorted(
        f"{datei.relative_to(WURZEL).as_posix()}: v{version}"
        for datei in ANGEBOTENE_TAGS_IN
        for version in re.findall(r"\bv(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)\b", datei.read_text(encoding="utf-8"))
        if version not in veroeffentlicht
    )
    assert not erfundene, f"Beispiel-Tag ohne Release (siehe CHANGELOG.md): {erfundene}"


# --- Unbeaufsichtigte Installation bis zum Ende (Issue #962) -------------------------------------------------------

ADMIN = {"ADMIN_EMAIL": "admin@ris.meine-kommune.de", "ADMIN_PASSWORD": "EinLangesPasswort"}


def _env_zeile(arbeit: Path, name: str) -> str | None:
    for zeile in (arbeit / ".env").read_text(encoding="utf-8").splitlines():
        if zeile.startswith(f"{name}="):
            return zeile.split("=", 1)[1]
    return None


def _compose_container() -> set[str]:
    namen = re.findall(r"container_name:\s*\$\{COMPOSE_PROJECT_NAME:-mandari\}(\S*)", COMPOSE.read_text("utf-8"))
    return {f"mandari{rest}" for rest in namen}


@pytest.mark.parametrize(
    ("acme", "erwartet"),
    [
        ("", ""),
        ("admin@example.com", ""),
        ("noreply@EXAMPLE.org", ""),
        ("betrieb@ris.meine-kommune.de", "betrieb@ris.meine-kommune.de"),
    ],
)
def test_acme_adresse_ohne_platzhalter(tmp_path: Path, acme: str, erwartet: str) -> None:
    """Ohne Eingabe schrieb der Installer noreply@example.com; Let's Encrypt lehnt solche Adressen ab."""
    lauf = _lauf(tmp_path, "--unattended", umgebung={"DOMAIN": "localhost", "ACME_EMAIL": acme, **ADMIN})

    assert lauf.rc == 0, lauf.ausgabe
    assert _env_zeile(lauf.arbeit, "ACME_EMAIL") == erwartet


def test_unbeaufsichtigte_installation_bis_zum_ende(tmp_path: Path) -> None:
    lauf = _lauf(tmp_path, "--unattended", umgebung={"DOMAIN": "localhost", **ADMIN})

    assert lauf.rc == 0, lauf.ausgabe
    # Sicherung mit Konfiguration: backup.sh liest die Passphrase aus ./.backup-passphrase
    passphrase = (lauf.arbeit / ".backup-passphrase").read_text(encoding="utf-8").strip()
    assert len(passphrase) >= 32, "Passphrase zu kurz"
    if os.name != "nt":
        assert (lauf.arbeit / ".backup-passphrase").stat().st_mode & 0o077 == 0, "nur für den Eigentümer lesbar"
    assert passphrase not in (lauf.arbeit / ".env").read_text(encoding="utf-8")
    assert ".backup-passphrase" in lauf.ausgabe and "außerhalb des Servers" in lauf.ausgabe

    # Das Passwort prüft die Anwendung vor createsuperuser, Fehler werden nicht verschluckt
    protokoll = "\n".join(lauf.aufrufe)
    pruefung = protokoll.index("mandari python manage.py shell -c")
    assert "validate_password" in protokoll[pruefung:]
    assert pruefung < protokoll.index("mandari python manage.py createsuperuser --noinput")

    # Die Prüfung am Ende nennt jeden Dienst außer dem optionalen PgBouncer, auch den Orchestrator
    geprueft = {z.split()[-1] for z in lauf.aufrufe if z.startswith("inspect --format={{.State.Status}}")}
    assert geprueft == _compose_container() - {"mandari-pgbouncer"}, geprueft
    assert "Orchestrator" in lauf.ausgabe
    # Hinweise für den ersten Login
    assert "Zwei-Faktor" in lauf.ausgabe and "Mailversand" in lauf.ausgabe


def test_vorhandene_passphrase_bleibt(tmp_path: Path) -> None:
    """Ältere Sicherungen brauchen die bisherige Passphrase: Eine vorhandene Datei wird nie ersetzt."""
    lauf = _lauf(
        tmp_path,
        "--unattended",
        umgebung={"DOMAIN": "localhost", **ADMIN},
        dateien={".backup-passphrase": "bisherige-passphrase-mit-genug-zeichen\n"},
    )

    assert lauf.rc == 0, lauf.ausgabe
    inhalt = (lauf.arbeit / ".backup-passphrase").read_text(encoding="utf-8")
    assert inhalt == "bisherige-passphrase-mit-genug-zeichen\n"


def test_zu_kurzes_admin_passwort_bricht_vor_jeder_aenderung_ab(tmp_path: Path) -> None:
    """Die Passwortrichtlinie verlangt 12 Zeichen; createsuperuser --noinput prüft das nicht."""
    lauf = _lauf(
        tmp_path,
        "--unattended",
        "--reinstall-destroy-data",
        umgebung={"DOMAIN": "localhost", "ADMIN_EMAIL": "a@ris.meine-kommune.de", "ADMIN_PASSWORD": "elfZeichen1"},
        env_datei="IMAGE_TAG=latest\n",
    )

    assert lauf.rc == 1, lauf.ausgabe
    assert "12 Zeichen" in lauf.ausgabe and "nichts verändert" in lauf.ausgabe
    assert lauf.aufrufe == [], "vor jedem Aufruf von docker"
    assert (lauf.arbeit / ".env").read_text(encoding="utf-8") == "IMAGE_TAG=latest\n"


def test_abgelehntes_passwort_legt_keinen_admin_an_und_sagt_es(tmp_path: Path) -> None:
    lauf = _lauf(
        tmp_path,
        "--unattended",
        umgebung={"DOMAIN": "localhost", "FAKE_PASSWORT_ABGELEHNT": "1", **ADMIN},
    )

    assert lauf.rc == 0, lauf.ausgabe
    assert not any("createsuperuser --noinput" in z for z in lauf.aufrufe)
    assert "Passwortrichtlinie" in lauf.ausgabe and "zu üblich" in lauf.ausgabe
    assert "createsuperuser" in lauf.ausgabe, "der Weg von Hand steht in der Meldung"


def test_installer_nennt_die_zahl_der_quellen() -> None:
    """Die Kommunenauswahl nennt so viele Quellen, wie import_oparl_sources anlegt, und nur vorhandene Großstädte."""
    from insight_core.management.commands.import_oparl_sources import OPARL_SOURCES

    text = SKRIPT.read_text(encoding="utf-8")
    namen = {name for name, _url in OPARL_SOURCES}
    zahlen = {int(z) for z in re.findall(r"(\d+) Kommunen", text[text.index("configure_oparl_sources() {") :])}
    grossstaedte = re.findall(r"'([^']+)'", text[text.index("names = [") : text.index("]", text.index("names = ["))])
    assert zahlen == {len(OPARL_SOURCES), len(grossstaedte)}, zahlen
    assert set(grossstaedte) <= namen, set(grossstaedte) - namen
