#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüft, ob die Anwendungs-Images für die genannten Tags anonym abrufbar sind (Issue #698).

``docker-compose.yml`` und das Helm-Chart ziehen mandari, ingestor und website mit einem gemeinsamen Tag.
Fehlt eines der drei Images für ein Tag, scheitert ``./install.sh --tag …`` bzw. ``./update.sh --tag …``.
Der Release-Workflow ruft dieses Skript nach dem Veröffentlichen auf – ohne Anmeldung, also so, wie ein
Selbstbetreiber die Images abruft.

    python scripts/check_image_tags.py v0.11.0 latest
    python scripts/check_image_tags.py --images mandari,ingestor dev

Exit 0, wenn jedes Image für jedes Tag vorhanden ist; sonst 1 mit einer Liste der fehlenden.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable

REGISTRY = "ghcr.io"
OWNER = "mandarioss"
IMAGES = ("mandari", "ingestor", "website")
MANIFEST_TYPES = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

VORHANDEN = "vorhanden"
FEHLT = "fehlt"
UNKLAR = "nicht prüfbar"

Oeffner = Callable[[urllib.request.Request], int]


class RegistryError(Exception):
    """Antwort der Registry, die weder „vorhanden“ noch „fehlt“ bedeutet."""


def _http_status(anfrage: urllib.request.Request) -> int:
    try:
        with urllib.request.urlopen(anfrage, timeout=30) as antwort:  # noqa: S310 – feste https-Adresse
            return int(antwort.status)
    except urllib.error.HTTPError as fehler:
        return int(fehler.code)


def _anonymes_token(registry: str, repository: str) -> str:
    adresse = f"https://{registry}/token?" + urllib.parse.urlencode({"scope": f"repository:{repository}:pull"})
    with urllib.request.urlopen(adresse, timeout=30) as antwort:  # noqa: S310 – feste https-Adresse
        daten = json.load(antwort)
    token = daten.get("token") or daten.get("access_token")
    if not token:
        raise RegistryError(f"kein Token für {repository}")
    return str(token)


def manifest_status(
    registry: str,
    repository: str,
    tag: str,
    *,
    token: str,
    oeffner: Oeffner = _http_status,
) -> str:
    """HEAD auf das Manifest: 200 = vorhanden, 404 = fehlt, alles andere ist nicht prüfbar."""
    anfrage = urllib.request.Request(
        f"https://{registry}/v2/{repository}/manifests/{urllib.parse.quote(tag, safe='')}",
        method="HEAD",
        headers={"Authorization": f"Bearer {token}", "Accept": MANIFEST_TYPES},
    )
    status = oeffner(anfrage)
    if status == 200:
        return VORHANDEN
    if status == 404:
        return FEHLT
    raise RegistryError(f"{repository}:{tag}: HTTP {status}")


def pruefe(
    tags: Iterable[str],
    images: Iterable[str] = IMAGES,
    *,
    registry: str = REGISTRY,
    owner: str = OWNER,
    token_holen: Callable[[str, str], str] | None = None,
    oeffner: Oeffner | None = None,
    versuche: int = 3,
    pause: float = 10.0,
) -> dict[str, str]:
    """Status je ``<image>:<tag>``; nicht prüfbare Antworten werden ``versuche``-mal wiederholt."""
    token_holen = token_holen or _anonymes_token
    oeffner = oeffner or _http_status
    ergebnis: dict[str, str] = {}
    for image in images:
        repository = f"{owner}/{image}"
        for tag in tags:
            for versuch in range(1, versuche + 1):
                try:
                    token = token_holen(registry, repository)
                    ergebnis[f"{image}:{tag}"] = manifest_status(
                        registry, repository, tag, token=token, oeffner=oeffner
                    )
                    break
                except (RegistryError, OSError, ValueError) as fehler:
                    ergebnis[f"{image}:{tag}"] = f"{UNKLAR} ({type(fehler).__name__})"
                    if versuch < versuche:
                        time.sleep(pause)
    return ergebnis


def _liste(wert: str) -> list[str]:
    return [teil.strip() for teil in wert.replace(",", " ").split() if teil.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tags", nargs="+", help="Tags, z. B. v0.11.0 latest dev (auch komma-getrennt)")
    parser.add_argument("--images", default=",".join(IMAGES), help="Images, komma-getrennt (Standard: alle drei)")
    parser.add_argument("--registry", default=REGISTRY)
    parser.add_argument("--owner", default=OWNER)
    args = parser.parse_args(argv)

    tags = [tag for wert in args.tags for tag in _liste(wert)]
    ergebnis = pruefe(tags, _liste(args.images), registry=args.registry, owner=args.owner)
    breite = max(len(name) for name in ergebnis)
    for name, status in ergebnis.items():
        print(f"{args.registry}/{args.owner}/{name:<{breite}}  {status}")
    fehlend = [name for name, status in ergebnis.items() if status != VORHANDEN]
    if fehlend:
        print(f"\nNicht abrufbar: {', '.join(fehlend)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
