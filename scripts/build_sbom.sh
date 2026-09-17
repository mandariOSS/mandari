#!/bin/sh
# SBOM (CycloneDX, JSON) fuer die drei Bestandteile von mandari erzeugen (Issue #97).
#
#   sh scripts/build_sbom.sh [ausgabeverzeichnis]      # Standard: sbom/
#
# Erzeugt:
#   sbom-mandari-python.cdx.json    Django-Anwendung aus mandari/requirements.lock
#   sbom-ingestor-python.cdx.json   Ingestor aus ingestor/uv.lock (ueber uv export)
#   sbom-mandari-npm.cdx.json       Frontend aus mandari/package-lock.json (ohne dev)
#
# Braucht: python mit cyclonedx-bom (pip install cyclonedx-bom), uv, node/npm (npx).
# Der Release-Workflow ruft dieses Skript auf und haengt die Dateien an das Release.
set -eu

WURZEL=$(cd "$(dirname "$0")/.." && pwd)
AUS="${1:-$WURZEL/sbom}"
mkdir -p "$AUS"
VERSION=$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$WURZEL/mandari/pyproject.toml" | head -1)

echo "SBOM fuer mandari $VERSION -> $AUS"

echo "1/3 Django-Anwendung (requirements.lock)"
python -m cyclonedx_py requirements "$WURZEL/mandari/requirements.lock" \
  --pyproject "$WURZEL/mandari/pyproject.toml" --mc-type application \
  --output-format JSON --output-file "$AUS/sbom-mandari-python.cdx.json"

echo "2/3 Ingestor (uv.lock -> requirements)"
TMP=$(mktemp)
( cd "$WURZEL/ingestor" && uv export --frozen --no-hashes --no-dev --no-emit-project -q -o "$TMP" )
python -m cyclonedx_py requirements "$TMP" \
  --pyproject "$WURZEL/ingestor/pyproject.toml" --mc-type application \
  --output-format JSON --output-file "$AUS/sbom-ingestor-python.cdx.json"
rm -f "$TMP"

echo "3/3 Frontend (package-lock.json)"
( cd "$WURZEL/mandari" && npx --yes @cyclonedx/cyclonedx-npm@4 --package-lock-only --omit dev \
    --mc-type application --output-format JSON --output-file "$AUS/sbom-mandari-npm.cdx.json" )

for f in "$AUS"/sbom-*.cdx.json; do
  n=$(python -c "import json,sys; print(len(json.load(open(sys.argv[1], encoding='utf-8')).get('components', [])))" "$f")
  echo "  $(basename "$f"): $n Komponenten"
done
