# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kennung unserer automatischen Abrufe bei Ratsinformationssystemen.

Ingestor (OParl-Schnittstelle, Textextraktion) und Django (Dokument-Cache, Textextraktion, Vorschau)
melden sich mit demselben Produkt-Token. Betreiber sehen daran, wer abruft, finden über die Infoseite
unsere Regeln und erreichen uns über die Kontaktadresse. Dasselbe Token wertet die robots.txt-Prüfung
aus (:mod:`mandari_oparl.robots`): Eine Regel für ``mandari-ingestor`` gilt für alle unsere Abrufe.
"""

from __future__ import annotations

#: Produkt-Token im User-Agent und in robots.txt-Gruppen (RFC 9309: nur Buchstaben, ``-`` und ``_``)
PRODUCT_TOKEN = "mandari-ingestor"

#: Infoseite für Betreiber: was wir abrufen, wie oft, wie man uns drosselt oder ausschließt
INFO_URL = "https://mandari.de/crawler/"

#: Kontaktadresse für Betreiber
CONTACT = "support@mandari.de"


def user_agent(version: str | None = None) -> str:
    """User-Agent mit Produkt-Token, Version (falls bekannt), Infoseite und Kontaktadresse."""
    product = f"{PRODUCT_TOKEN}/{version}" if version else PRODUCT_TOKEN
    return f"{product} (+{INFO_URL}; {CONTACT})"


def product_token(agent: str) -> str:
    """
    Produkt-Token eines User-Agents (Teil vor ``/`` bzw. dem ersten Leerzeichen), kleingeschrieben.

    Je Quelle lässt sich ein abweichender User-Agent einstellen; robots.txt-Regeln gelten dann für
    dessen Token.
    """
    token = (agent or "").strip().split("/", 1)[0].split(None, 1)
    return token[0].lower() if token else PRODUCT_TOKEN
