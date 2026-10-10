# SPDX-License-Identifier: AGPL-3.0-or-later
"""Roh-Manifeste unter deploy/kubernetes/manifests aus dem Helm-Chart erzeugen (Issue #963).

    python deploy/kubernetes/manifeste_erzeugen.py            # Dateien neu schreiben
    python deploy/kubernetes/manifeste_erzeugen.py --pruefen  # nur vergleichen (CI), Abweichung = Exit 1

Braucht helm im PATH (oder HELM=/pfad/zu/helm). Gerendert wird mit den Standardwerten des Charts,
Release und Namespace "mandari". Statt Zugangsdaten stehen Platzhalter "BITTE-ERSETZEN-*" im Secret.
Die Migrations-Jobs landen in job-migrate.yaml, alles andere in mandari.yaml.
"""

from __future__ import annotations

import base64
import difflib
import os
import re
import subprocess
import sys
from pathlib import Path

HIER = Path(__file__).resolve().parent
CHART = HIER / "helm" / "mandari"
ZIEL = HIER / "manifests"

# Der Chart verlangt für encryption-key Base64 von genau 32 Byte. Gerendert wird mit einem
# Platzhalter dieser Länge, in der Datei steht danach ein bewusst ungültiger Wert: Wer ihn nicht
# ersetzt, bekommt einen Startfehler statt einer Installation mit bekanntem Schlüssel.
SCHLUESSEL_RENDER = base64.b64encode(b"BITTE-ERSETZEN-encryption-key-32").decode()
# Werte unter data: sind noch einmal Base64-kodiert
SCHLUESSEL_RENDER_DATA = base64.b64encode(SCHLUESSEL_RENDER.encode()).decode()
SCHLUESSEL_DATEI_DATA = base64.b64encode(b"BITTE-ERSETZEN-encryption").decode()

WERTE = {
    "secrets.secretKey": "BITTE-ERSETZEN-secret-key",
    "secrets.encryptionKey": SCHLUESSEL_RENDER,
    "secrets.websiteSecretKey": "BITTE-ERSETZEN-website",
    "postgres.password": "BITTE-ERSETZEN-db",
    "redis.password": "BITTE-ERSETZEN-redis",
}

KOPF_MANDARI = """\
# =============================================================================
# mandari – Kubernetes-Manifeste (ohne Helm)
# =============================================================================
# ERZEUGT aus dem Helm-Chart deploy/kubernetes/helm/mandari, nicht von Hand ändern:
#   python deploy/kubernetes/manifeste_erzeugen.py
# Eigene Anpassungen gehören in ein Kustomize-Overlay (siehe kustomization.yaml).
#
# Vor dem Einspielen anpassen:
#   1. Alle Werte "BITTE-ERSETZEN-*" im Secret durch eigene ersetzen (Werte unter data: sind
#      Base64-kodiert; encryption-key: openssl rand -base64 32 | tr -d '\\n' | base64)
#   2. mandari.example.com durch die eigene Domain ersetzen
#   3. Bei Bedarf Speicherklassen und Größen anpassen
#   4. Mailversand: Message-ID und EHLO tragen den Host aus SITE_URL. Anderer Absender
#      oder andere Absenderdomain: DEFAULT_FROM_EMAIL bzw. EMAIL_MESSAGE_ID_DOMAIN
#      (vollständiger Domainname) im env der Anwendung und der Worker ergänzen
#      (DEPLOYMENT.md, „Mailversand: Message-ID und EHLO“)
#
# Einspielen:
#   kubectl create namespace mandari
#   kubectl kustomize deploy/kubernetes/manifests | kubectl apply -f -
#   kubectl -n mandari apply -f deploy/kubernetes/manifests/job-migrate.yaml
# Anwendung, Worker und Ingestor warten per initContainer, bis der Migrations-Job fertig ist.
#
# Empfehlung: Für Updates und Konfiguration ist das Helm-Chart komfortabler.
# =============================================================================
"""

KOPF_JOB = """\
# =============================================================================
# mandari – Migrations-Job (ohne Helm)
# =============================================================================
# ERZEUGT aus dem Helm-Chart, nicht von Hand ändern: python deploy/kubernetes/manifeste_erzeugen.py
#
# Nach dem ersten Apply und nach jedem Update ausführen; vorher den alten Job entfernen
# (fertige Jobs löscht Kubernetes nach einem Tag selbst):
#   kubectl -n mandari delete job mandari-migrate --ignore-not-found
#   kubectl -n mandari apply -f deploy/kubernetes/manifests/job-migrate.yaml
# Ohne Helm gibt es nur diesen einen Schritt (migrate). Das Chart migriert bei Upgrades in zwei
# Schritten (safemigrate vor, migrate nach dem Ausrollen) wie update.sh.
# =============================================================================
"""


def rendern() -> str:
    helm = os.environ.get("HELM", "helm")
    befehl = [helm, "template", "mandari", str(CHART), "--namespace", "mandari", "--no-hooks"]
    for schluessel, wert in WERTE.items():
        befehl += ["--set-string", f"{schluessel}={wert}"]
    return subprocess.run(befehl, check=True, capture_output=True, text=True, encoding="utf-8").stdout


def aufteilen(ausgabe: str) -> tuple[list[str], list[str]]:
    haupt: list[str] = []
    job: list[str] = []
    for teil in ausgabe.replace("\r\n", "\n").split("\n---\n"):
        zeilen = teil.strip("\n").split("\n")
        quelle = next((z for z in zeilen if z.startswith("# Source: ")), "")
        rumpf = "\n".join(z for z in zeilen if not z.startswith("# Source: ") and z != "---").strip("\n")
        if not rumpf.strip():
            continue
        rumpf = rumpf.replace(f"encryption-key: {SCHLUESSEL_RENDER_DATA}", f"encryption-key: {SCHLUESSEL_DATEI_DATA}")
        # Die Prüfsumme hängt an den Zeilenenden der Vorlage (Windows-Checkout: CRLF) und hat ohne Helm
        # keine Wirkung; ein fester Wert hält die Datei auf jedem System gleich.
        rumpf = re.sub(r"(checksum/secret: )[0-9a-f]{64}", r"\g<1>ohne-helm", rumpf)
        (job if quelle.endswith("/job-migrate.yaml") else haupt).append(rumpf)
    if not job:
        raise SystemExit("Kein Migrations-Job im gerenderten Chart gefunden.")
    if not any(f"encryption-key: {SCHLUESSEL_DATEI_DATA}" in d for d in haupt):
        raise SystemExit("Platzhalter für encryption-key nicht ersetzt: Secret-Vorlage geändert?")
    return haupt, job


def zusammensetzen(kopf: str, dokumente: list[str]) -> str:
    return kopf + "\n---\n".join(dokumente) + "\n"


def main() -> int:
    pruefen = "--pruefen" in sys.argv[1:]
    haupt, job = aufteilen(rendern())
    dateien = {
        ZIEL / "mandari.yaml": zusammensetzen(KOPF_MANDARI, haupt),
        ZIEL / "job-migrate.yaml": zusammensetzen(KOPF_JOB, job),
    }
    abweichend = False
    for pfad, inhalt in dateien.items():
        bisher = pfad.read_text(encoding="utf-8") if pfad.exists() else ""
        if bisher == inhalt:
            continue
        abweichend = True
        if pruefen:
            diff = difflib.unified_diff(
                bisher.splitlines(keepends=True),
                inhalt.splitlines(keepends=True),
                f"{pfad.name} (im Repository)",
                f"{pfad.name} (aus dem Chart)",
                n=2,
            )
            sys.stdout.writelines(list(diff)[:200])
        else:
            pfad.write_text(inhalt, encoding="utf-8", newline="\n")
            print(f"geschrieben: {pfad.relative_to(HIER.parent.parent)}")
    if pruefen and abweichend:
        print(
            "\nDie Manifeste entsprechen nicht dem Chart. Neu erzeugen mit:\n"
            "  python deploy/kubernetes/manifeste_erzeugen.py",
            file=sys.stderr,
        )
        return 1
    if pruefen:
        print("Manifeste entsprechen dem Chart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
