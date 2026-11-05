# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Signaturschlüssel des OpenID-Connect-Anbieters (RS256, Issue #1206).

Der Schlüssel kommt wie die übrigen Geheimnisse aus der Umgebung, nie aus dem Repository:

- ``OIDC_RSA_PRIVATE_KEY_FILE``: Pfad zu einer PEM-Datei (bevorzugt, etwa als eingebundene Datei im Container)
- ``OIDC_RSA_PRIVATE_KEY``: der PEM-Text selbst; ``\\n`` als Zeichenfolge wird zum Zeilenumbruch

Nur während eines Schlüsselwechsels zusätzlich der bisherige Schlüssel (``OIDC_RSA_PRIVATE_KEY_PREVIOUS_FILE``,
mehrere Pfade durch Komma getrennt, oder ``OIDC_RSA_PRIVATE_KEY_PREVIOUS``). Mit ihm wird nichts mehr signiert;
das JWKS veröffentlicht seinen öffentlichen Teil, damit Anwendungen ID-Tokens, die vor dem Wechsel ausgestellt
wurden, bis zu deren Ablauf weiter prüfen können. Ablauf in ``docs/schnittstelle/anmeldung.md``.

Verlangt wird ein unverschlüsselter RSA-Schlüssel mit mindestens 2048 Bit. Fehlermeldungen nennen nie den
Inhalt eines Schlüssels. Das Modul hat keine Abhängigkeiten zu Django, weil ``settings.py`` es liest.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

ENV_KEY = "OIDC_RSA_PRIVATE_KEY"
ENV_KEY_FILE = "OIDC_RSA_PRIVATE_KEY_FILE"
ENV_PREVIOUS = "OIDC_RSA_PRIVATE_KEY_PREVIOUS"
ENV_PREVIOUS_FILE = "OIDC_RSA_PRIVATE_KEY_PREVIOUS_FILE"

#: Mindestlänge des RSA-Schlüssels in Bit
MIN_BITS = 2048


class SigningKeyError(ValueError):
    """Der konfigurierte Schlüssel fehlt, ist unlesbar oder zu schwach (Meldung ohne Schlüsselinhalt)."""


def _from_env(text: str) -> str:
    return text.strip().replace("\\n", "\n")


def _from_file(path: str, variable: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise SigningKeyError(f"{variable}: Datei {path} ist nicht lesbar ({exc.strerror or 'Fehler'}).") from None


def validate(pem: str, variable: str) -> str:
    """Prüft, dass ``pem`` ein unverschlüsselter RSA-Schlüssel mit mindestens ``MIN_BITS`` ist; liefert ihn zurück."""
    try:
        key = load_pem_private_key(pem.encode("utf-8"), password=None)
    except (TypeError, ValueError):
        raise SigningKeyError(
            f"{variable}: kein lesbarer, unverschlüsselter privater Schlüssel im PEM-Format."
        ) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise SigningKeyError(f"{variable}: RS256 braucht einen RSA-Schlüssel.")
    if key.key_size < MIN_BITS:
        raise SigningKeyError(f"{variable}: Der RSA-Schlüssel hat {key.key_size} Bit, verlangt sind {MIN_BITS}.")
    return pem.strip() + "\n"


def load(environ: Mapping[str, str]) -> tuple[str, list[str]]:
    """
    Aktiver Schlüssel und bisherige Schlüssel aus der Umgebung.

    Returns: ``(aktiv, bisherige)``; ``aktiv`` ist leer, wenn nichts eingestellt ist.

    Raises: :class:`SigningKeyError`, wenn ein eingestellter Schlüssel nicht taugt.
    """
    active = ""
    if environ.get(ENV_KEY_FILE, "").strip():
        active = validate(_from_file(environ[ENV_KEY_FILE].strip(), ENV_KEY_FILE), ENV_KEY_FILE)
    elif environ.get(ENV_KEY, "").strip():
        active = validate(_from_env(environ[ENV_KEY]), ENV_KEY)

    previous: list[str] = []
    for path in (part.strip() for part in environ.get(ENV_PREVIOUS_FILE, "").split(",")):
        if path:
            previous.append(validate(_from_file(path, ENV_PREVIOUS_FILE), ENV_PREVIOUS_FILE))
    if environ.get(ENV_PREVIOUS, "").strip():
        previous.append(validate(_from_env(environ[ENV_PREVIOUS]), ENV_PREVIOUS))
    # Der aktive Schlüssel steht nicht zugleich unter den bisherigen (das JWKS nennte ihn sonst doppelt)
    return active, [pem for pem in previous if pem != active]
