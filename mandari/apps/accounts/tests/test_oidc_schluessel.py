# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Signaturschlüssel des OpenID-Connect-Anbieters aus der Umgebung (Issue #1206, ``apps/accounts/oidc/signing_keys.py``).

Datei oder Umgebungsvariable, bisherige Schlüssel für den Wechsel, nur RSA ab 2048 Bit, unverschlüsselt;
Fehlermeldungen nennen nie den Inhalt eines Schlüssels.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from apps.accounts.oidc import signing_keys
from apps.accounts.tests.oidc_hilfen import rsa_schluessel


def _pem(key: object, passwort: bytes | None = None) -> str:
    verschluesselung = serialization.BestAvailableEncryption(passwort) if passwort else serialization.NoEncryption()
    pem = cast(Any, key).private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, verschluesselung)
    return str(pem.decode())


def test_ohne_einstellung_kein_schluessel() -> None:
    assert signing_keys.load({}) == ("", [])


def test_aus_datei_und_bisheriger_schluessel(tmp_path: Path) -> None:
    aktiv, bisher = tmp_path / "aktiv.pem", tmp_path / "bisher.pem"
    aktiv.write_text(rsa_schluessel(1), encoding="utf-8")
    bisher.write_text(rsa_schluessel(2), encoding="utf-8")
    schluessel, vorige = signing_keys.load(
        {"OIDC_RSA_PRIVATE_KEY_FILE": str(aktiv), "OIDC_RSA_PRIVATE_KEY_PREVIOUS_FILE": f" {bisher} ,"}
    )
    assert schluessel.strip() == rsa_schluessel(1).strip()
    assert [pem.strip() for pem in vorige] == [rsa_schluessel(2).strip()]


def test_aus_umgebungsvariable_mit_zeilenumbruechen_als_text() -> None:
    einzeilig = rsa_schluessel(1).strip().replace("\n", "\\n")
    schluessel, vorige = signing_keys.load({"OIDC_RSA_PRIVATE_KEY": einzeilig})
    assert schluessel.strip() == rsa_schluessel(1).strip() and vorige == []


def test_aktiver_schluessel_nicht_doppelt_im_jwks() -> None:
    _schluessel, vorige = signing_keys.load(
        {"OIDC_RSA_PRIVATE_KEY": rsa_schluessel(1), "OIDC_RSA_PRIVATE_KEY_PREVIOUS": rsa_schluessel(1)}
    )
    assert vorige == []


def test_fehlende_datei(tmp_path: Path) -> None:
    with pytest.raises(signing_keys.SigningKeyError, match="nicht lesbar"):
        signing_keys.load({"OIDC_RSA_PRIVATE_KEY_FILE": str(tmp_path / "fehlt.pem")})


@pytest.mark.parametrize(
    ("pem", "meldung"),
    [
        ("kein schluessel", "kein lesbarer"),
        (_pem(ec.generate_private_key(ec.SECP256R1())), "RSA-Schlüssel"),
        (_pem(rsa.generate_private_key(public_exponent=65537, key_size=1024)), "1024 Bit"),
        (_pem(rsa.generate_private_key(public_exponent=65537, key_size=2048), b"passwort"), "unverschlüsselter"),
    ],
)
def test_untaugliche_schluessel(pem: str, meldung: str) -> None:
    with pytest.raises(signing_keys.SigningKeyError, match=meldung) as fehler:
        signing_keys.load({"OIDC_RSA_PRIVATE_KEY": pem})
    assert "PRIVATE KEY" not in str(fehler.value)
