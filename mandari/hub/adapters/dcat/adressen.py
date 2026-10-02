# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Adressen im Katalog: Basis der Installation, Webadressen und E-Mail-Adressen aus fremden Angaben.

Der Katalog übernimmt Webseiten und Kontaktadressen von Kommunen und Mandanten (``Body.website`` aus fremden
Ratsinformationssystemen, Angaben in den Einstellungen). Im Graphen werden sie zu IRIs (``foaf:homepage``,
``vcard:hasURL``, ``vcard:hasEmail``). Eine IRI mit Leerzeichen, spitzen Klammern, Anführungszeichen oder
Steuerzeichen lässt sich nicht als Turtle schreiben: rdflib bricht die Ausgabe ab, und der ganze Katalog – bei einer
Kommune im Gesamtkatalog auch der aller übrigen – wäre nicht abrufbar. Deshalb nimmt der Katalog nur Angaben, die
als IRI taugen; alles andere gilt als „keine Angabe“.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

#: Zeichen, die in einer IRI nicht vorkommen dürfen (Turtle ``IRIREF``, RFC 3987): Leer- und Steuerzeichen,
#: spitze und geschweifte Klammern, Anführungszeichen, senkrechter Strich, Backslash, Zirkumflex, Gravis sowie in
#: XML unzulässige Zeichen (Ersatzzeichen, ``U+FFFE``, ``U+FFFF``)
_UNZULAESSIG: Final = re.compile(r'[\x00-\x20<>"{}|\\^`\x7f-\x9f\ud800-\udfff\ufffe\uffff]')

#: Schemata, die der Katalog als Webadresse nimmt
_SCHEMATA: Final = ("http", "https")


def site() -> str:
    """Basis aller Adressen der Installation (``SITE_URL`` ohne Schrägstrich am Ende)."""
    return str(settings.SITE_URL).rstrip("/")


def iri(angabe: str | None) -> str | None:
    """Die Angabe, wenn sie als IRI taugt (keine unzulässigen Zeichen) – sonst ``None``."""
    text = (angabe or "").strip()
    if not text or _UNZULAESSIG.search(text):
        return None
    return text


def webadresse(angabe: str | None) -> str | None:
    """
    Vollständige Webadresse (``http``/``https`` mit Host), die als IRI taugt – sonst ``None``.

    Eine Angabe ohne Schema, ohne Host oder mit unzulässigen Zeichen (etwa einem Leerzeichen) ist im Katalog keine
    gültige IRI und gilt als „keine Angabe“.
    """
    text = iri(angabe)
    if text is None:
        return None
    try:
        teile = urlsplit(text)
    except ValueError:
        return None
    if teile.scheme.lower() not in _SCHEMATA or not teile.hostname:
        return None
    return text


def email(angabe: str | None) -> str | None:
    """E-Mail-Adresse, die als ``mailto:``-IRI taugt – sonst ``None``."""
    text = iri(angabe)
    if text is None:
        return None
    try:
        validate_email(text)
    except ValidationError:
        return None
    return text
