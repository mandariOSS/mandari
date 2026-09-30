# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kanonisches JSON nach RFC 8785 (JSON Canonicalization Scheme) und Inhalts-Hash.

Der Inhalts-Hash einer Quittung ist SHA-256 über das kanonische JSON des Befehlsinhalts. Damit
Einreichende und Fremdsysteme ihn mit einer beliebigen JCS-Bibliothek nachrechnen können, folgt die
Darstellung RFC 8785: keine Leerzeichen, Schlüssel nach UTF-16-Codeeinheiten sortiert, Zeichenketten
nur mit den nötigen Escapes, Zahlen wie in ECMAScript (``Number.prototype.toString``).

Zwei Arten von Werten lehnt ``canonical_json`` mit ``ValueError`` ab, weil fremde Bibliotheken dafür
einen anderen Hash rechnen würden oder gar nicht rechnen können:

- Ganzzahlen außerhalb von ±(2^53 − 1). RFC 8785 behandelt jede Zahl als IEEE-754-Double; größere
  Ganzzahlen sind dort nicht exakt (``10**21`` würde zu ``1e+21``). Große Zahlen gehören als
  Zeichenkette in den Inhalt (RFC 7493, I-JSON).
- Zeichenketten mit einem einzelnen Surrogat (in JSON als ``"\\ud800"`` schreibbar): kein gültiges
  Unicode, nicht als UTF-8 darstellbar.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Final

#: Größte Ganzzahl, die jeder JSON-Leser exakt darstellt (IEEE 754, RFC 7493): 2^53 − 1
MAX_SAFE_INTEGER: Final = 2**53 - 1


def canonical_json(value: object) -> bytes:
    """
    ``value`` als kanonisches JSON (UTF-8).

    ``ValueError`` bei Werten, die JSON nicht kennt oder die sich nicht eindeutig darstellen lassen
    (siehe Moduldokumentation). Die Meldung nennt nie den Wert.
    """
    try:
        return _serialize(value).encode("utf-8")
    except UnicodeEncodeError:
        # Die ursprüngliche Ausnahme trägt die ganze Zeichenkette mit sich (``exc.object``).
        raise ValueError("Zeichenkette mit einzelnem Surrogat ist kein gültiges Unicode") from None


def content_hash(value: object) -> str:
    """SHA-256 (hexadezimal, klein) über das kanonische JSON von ``value``."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _serialize(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError("Ganzzahl außerhalb von ±(2^53 − 1)")
        return str(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        keys = list(value)
        if not all(isinstance(key, str) for key in keys):
            raise ValueError("JSON-Objekte haben nur Zeichenketten als Schlüssel")
        members = sorted(keys, key=lambda key: key.encode("utf-16-be"))
        return "{" + ",".join(f"{_serialize(key)}:{_serialize(value[key])}" for key in members) + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(_serialize(item) for item in value) + "]"
    raise ValueError(f"kein JSON-Wert: {type(value).__name__}")


def _number(value: float) -> str:
    """Zahl wie ECMAScript ``Number.prototype.toString`` (RFC 8785, Abschnitt 3.2.2.3)."""
    if not math.isfinite(value):
        raise ValueError("NaN und Unendlich sind kein JSON")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    # Kürzeste Ziffernfolge, die den Wert eindeutig darstellt (wie repr): Wert = Ziffern × 10^exponent
    _, digit_tuple, exponent = Decimal(repr(abs(value))).as_tuple()
    digits = "".join(str(digit) for digit in digit_tuple)
    stripped = digits.rstrip("0")
    exponent = int(exponent) + len(digits) - len(stripped)
    digits = stripped
    k = len(digits)
    n = exponent + k  # Wert = 0.d1…dk × 10^n
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    mantissa = digits[0] + ("." + digits[1:] if k > 1 else "")
    shift = n - 1
    return f"{sign}{mantissa}e{'+' if shift >= 0 else '-'}{abs(shift)}"
