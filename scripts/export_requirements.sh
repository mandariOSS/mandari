#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Schreibt die gesperrten Laufzeit-Abhängigkeiten der Django-Anwendung als requirements-Datei.
#
#   sh scripts/export_requirements.sh <zieldatei>
#   pip install -r <zieldatei>
#
# Quelle ist mandari/uv.lock; mandari/pyproject.toml nennt die direkten Abhängigkeiten. CI-Jobs, pip-audit
# und die SBOM nutzen dieses Skript, damit überall derselbe Stand geprüft wird wie im Image
# (mandari/Dockerfile exportiert selbst, dort mit Hashes und --locked).
#
# --frozen: uv.lock wird gelesen, wie sie ist. Ob sie zu pyproject.toml passt, prüft die CI mit
# `uv lock --check` (Job "Abhängigkeiten prüfen") und der Image-Build mit --locked.
# Das lokale Paket ../shared (mandari-oparl) installieren die Aufrufer getrennt.
set -eu

ZIEL="${1:?Aufruf: sh scripts/export_requirements.sh <zieldatei>}"
WURZEL=$(cd "$(dirname "$0")/.." && pwd)

uv export --project "$WURZEL/mandari" --quiet --frozen --no-dev --no-emit-project \
    --no-emit-package mandari-oparl --no-hashes --format requirements.txt -o "$ZIEL"
