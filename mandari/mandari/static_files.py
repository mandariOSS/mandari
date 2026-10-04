# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Statische Dateien: Vite-Bundles unter ihrem eigenen Namen ausliefern und dauerhaft cachen.

Vite benennt jede Datei unter ``static/dist/assets/`` schon nach ihrem Inhalt (``main-BXRI7s5k.js``).
Der Manifest-Storage von Django hängte bisher einen zweiten Hash an (``main-BXRI7s5k.67da78e2dddf.js``);
``{% vite_asset %}`` lud den Einstieg und die ``modulepreload``-Links unter diesem Namen. Die Importe
*innerhalb* der Bundles zeigen aber auf den Vite-Namen (``./csrf-PtHGiBr-.js``). Der Browser lud die
Chunks dadurch zweimal: einmal vergeblich vorab, dann erst nach dem Einstieg – ein zusätzlicher
Roundtrip auf dem kritischen Pfad. WhiteNoise erkannte die Vite-Namen außerdem nicht als unveränderlich
und lieferte sie mit ``max-age=60`` aus.

- :class:`ManifestStaticFilesStorage` gibt für Vite-Dateien den Vite-Namen zurück; Einstieg,
  Vorab-Links und Importe nutzen damit dieselbe Adresse.
- :func:`immutable_file_test` (``WHITENOISE_IMMUTABLE_FILE_TEST``) erkennt die Vite-Namen zusätzlich
  zu den Namen des Manifest-Storage.
"""

from __future__ import annotations

import os
import re
from posixpath import basename

from whitenoise.storage import CompressedManifestStaticFilesStorage

#: Ausgabe von ``npm run build`` (vite.config.ts: ``base``/``outDir``, Standardmuster ``[name]-[hash]``):
#: acht Zeichen Base64url als Inhalts-Hash, z. B. ``csrf-PtHGiBr-.js`` oder ``pdf.worker.min-Dswkl-cV.mjs``.
VITE_ASSET_RE = re.compile(r"^dist/assets/[^/]+-[A-Za-z0-9_-]{8}\.[A-Za-z0-9]+$")


def is_vite_asset(name: str) -> bool:
    """``True`` für eine von Vite inhaltsgehashte Datei (Pfad relativ zu ``STATIC_ROOT``)."""
    return bool(VITE_ASSET_RE.match(name.lstrip("/")))


class ManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):  # type: ignore[misc]  # WhiteNoise ohne Typen
    """Manifest-Storage von WhiteNoise, der Vite-Dateien unter ihrem Vite-Namen adressiert."""

    def stored_name(self, name: str) -> str:
        if is_vite_asset(name):
            return name
        return str(super().stored_name(name))


def _static_prefix() -> str:
    from django.conf import settings

    prefix = "/" + (settings.STATIC_URL or "/static/").strip("/") + "/"
    return prefix.replace("//", "/")


def immutable_file_test(path: str, url: str) -> bool:
    """Darf WhiteNoise die Datei unter ``url`` dauerhaft cachen lassen (``immutable``)?

    Ersetzt den eingebauten Test von ``WhiteNoiseMiddleware``, darum steht dessen Regel hier mit:
    Ein Name mit 12-stelligem Hash des Manifest-Storage (``styles.64f2a878acae.css``) gilt, wenn der
    Storage den Namen ohne Hash genau auf ihn abbildet. Neu: Vite-Dateien unter ``dist/assets/``.
    Alles andere (z. B. ``dist/manifest.json``, ungehashte Originale) behält ``WHITENOISE_MAX_AGE``.
    """
    prefix = _static_prefix()
    if not url.startswith(prefix):
        return False
    name = url[len(prefix) :]
    if is_vite_asset(name):
        return True
    name_with_hash, ext = os.path.splitext(name)
    name_without_hash = os.path.splitext(name_with_hash)[0] + ext
    if name_without_hash == name:
        return False
    from django.contrib.staticfiles.storage import staticfiles_storage

    try:
        static_url = staticfiles_storage.url(name_without_hash)
    except ValueError:
        return False
    return bool(static_url) and basename(static_url) == basename(url)
