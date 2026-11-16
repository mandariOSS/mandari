# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kalenderdateien: Text aus Feldern bleibt im Wert genau einer Eigenschaft.

Außer den CRLF-Zeilenenden und der Faltung enthält die Ausgabe keinen Zeilentrenner, gleich welches Zeichen im Text
steht: Zeilenumbrüche in jeder Schreibweise werden maskiert, übrige Steuerzeichen entfallen.
"""

from __future__ import annotations

import email
from datetime import UTC, datetime

import pytest
from django.core.mail import EmailMessage

from apps.common.ical import build_ics_event, build_ics_feed

BEGINN = datetime(2027, 1, 12, 18, 0, tzinfo=UTC)
#: Alle Steuerzeichen (C0, DEL, C1) sowie die Unicode-Zeilen- und Absatztrenner
ZEICHEN = [*map(chr, range(0xA0)), " ", " "]


def _nur_crlf(daten: bytes) -> None:
    """Die Datei zerfällt an denselben Stellen in Zeilen, gleich wer sie liest (Bytes oder Text)."""
    assert daten.splitlines() == daten.split(b"\r\n")[:-1]
    text = daten.decode("utf-8")
    assert text.splitlines() == text.split("\r\n")[:-1]


def _eigenschaften(daten: bytes) -> list[str]:
    """Namen der Inhaltszeilen (Fortsetzungen einer gefalteten Zeile zählen nicht)."""
    zeilen = [zeile for zeile in daten.decode("utf-8").splitlines() if not zeile.startswith(" ")]
    return [zeile.split(":", 1)[0].split(";", 1)[0] for zeile in zeilen]


@pytest.mark.parametrize("zeichen", ZEICHEN, ids=lambda z: f"U+{ord(z):04X}")
def test_termin_hat_nur_seine_eigenen_zeilen(zeichen: str) -> None:
    ort = f"Rathaus{zeichen}URL:https://example.org/anders"
    daten = build_ics_event(
        uid="sitzung-1@mandari",
        summary=f"Fraktion{zeichen}STATUS:CANCELLED",
        start=BEGINN,
        location=ort,
        description=f"Hinweis{zeichen}ATTACH:https://example.org/datei",
        organizer_name=f"Büro{zeichen}X-ANDERS:1",
        organizer_email="buero@example.org",
    )

    _nur_crlf(daten)
    assert _eigenschaften(daten) == [
        "BEGIN",
        "VERSION",
        "PRODID",
        "METHOD",
        "BEGIN",
        "UID",
        "DTSTAMP",
        "DTSTART",
        "SUMMARY",
        "LOCATION",
        "DESCRIPTION",
        "ORGANIZER",
        "SEQUENCE",
        "STATUS",
        "END",
        "END",
    ]


@pytest.mark.parametrize("zeichen", ["\r", "\n", "\r\n", "\x0b", "\x0c", "\x1c", "\x85", " "])
def test_feed_hat_nur_seine_eigenen_zeilen(zeichen: str) -> None:
    daten = build_ics_feed(
        [
            {
                "uid": "sitzung-1@mandari",
                "summary": f"Rat{zeichen}END:VEVENT",
                "start": BEGINN,
                "location": f"Saal{zeichen}URL:https://example.org/anders",
                "url": f"https://example.org/sitzung{zeichen}X-ANDERS:1",
            }
        ],
        name=f"Sitzungen{zeichen}X-ANDERS:1",
    )

    _nur_crlf(daten)
    namen = _eigenschaften(daten)
    assert namen.count("BEGIN") == 2 and namen.count("END") == 2, "ein Kalender mit einem Termin"
    assert namen.count("URL") == 1
    assert "X-ANDERS" not in namen


def test_zeilenumbruch_im_text_bleibt_als_umbruch_erhalten() -> None:
    """Ob CR, LF oder beides: Im Kalender steht ein maskierter Umbruch."""
    for umbruch in ("\r", "\n", "\r\n"):
        daten = build_ics_event(uid="1@mandari", summary="Titel", start=BEGINN, location=f"Rathaus{umbruch}Saal 2")
        assert b"LOCATION:Rathaus\\nSaal 2\r\n" in daten


def test_anhang_einer_mail_behaelt_einen_termin() -> None:
    """Auf dem Mailweg trennt die E-Mail-Bibliothek den Anhang neu in Zeilen, auch an einem einzelnen CR."""
    daten = build_ics_event(
        uid="sitzung-1@mandari",
        summary="Fraktionssitzung",
        start=BEGINN,
        location="Rathaus\rURL:https://example.org/anders\rEND:VEVENT\rBEGIN:VEVENT\rUID:zwei@example.org",
    )
    nachricht = EmailMessage("Einladung", "Text", "von@example.org", ["an@example.org"])
    nachricht.attach("sitzung.ics", daten.decode("utf-8"), "text/calendar")

    gelesen = email.message_from_bytes(nachricht.message().as_bytes())
    [anhang] = [teil for teil in gelesen.walk() if teil.get_content_type() == "text/calendar"]
    inhalt = anhang.get_payload(decode=True)
    assert isinstance(inhalt, bytes)

    zeilen = inhalt.splitlines()
    assert zeilen.count(b"BEGIN:VEVENT") == 1
    assert not [zeile for zeile in zeilen if zeile.startswith((b"URL:", b"UID:zwei"))]
