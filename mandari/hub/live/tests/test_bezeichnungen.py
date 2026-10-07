# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gelesene Fraktionen unscharf auf bekannte Bezeichnungen abbilden (Issue #915): Umlautfehler der Texterkennung,
abgeschnittene lange Bezeichnungen, bekannte Bezeichnungen der Kommune aus Profil und bisherigen Lesungen.
"""

from __future__ import annotations

import pytest
from django.utils import timezone

from hub.live.bezeichnungen import bekannte_fraktionen, kanonisch, ohne_abgeschnittene
from hub.live.models import Broadcast, BroadcastSpeech, BroadcastStatus
from hub.live.tests.conftest import Welt

BEKANNT = ["Fraktion Grün-Süd", "Liste Musterstadt", "Internationale Fraktion Beispielpartei"]


@pytest.mark.parametrize(
    ("gelesen", "erwartet"),
    [
        # gleich in der Vergleichsform: die Schreibweise mit Umlauten gilt
        ("Fraktion Grun-Sud", "Fraktion Grün-Süd"),
        ("Fraktion Gru�n-Su�d", "Fraktion Grün-Süd"),
        ("fraktion grün süd", "Fraktion Grün-Süd"),
        # abgeschnitten: Anfang genau einer bekannten (auch mit Auslassungszeichen)
        ("Internationale Fraktion Beisp", "Internationale Fraktion Beispielpartei"),
        ("Internationale Fraktion Bei…", "Internationale Fraktion Beispielpartei"),
        # abgeschnitten und verlesen
        ("Internationale Fraktlon Beisp", "Internationale Fraktion Beispielpartei"),
        # ähnlich (ein verlesener Buchstabe)
        ("Liste Musterstaclt", "Liste Musterstadt"),
        # länger als eine bekannte, die ihr Anfang ist: die vollständigere Lesung
        ("Liste Musterstadt Nord", "Liste Musterstadt Nord"),
        # Lesefehler in langen Wörtern (doppelt gelesener Buchstabe)
        ("Liste Mussterstadt", "Liste Musterstadt"),
        # unbekannt oder zu kurz für einen Anfang: unverändert; Kürzel müssen genau gleich sein
        ("Fraktion B", "Fraktion B"),
        ("Liste Musterstadt ABC", "Liste Musterstadt ABC"),
        ("Inter", "Inter"),
        ("", ""),
        (None, None),
    ],
)
def test_kanonisch(gelesen: str | None, erwartet: str | None) -> None:
    assert kanonisch(gelesen, BEKANNT) == erwartet


def test_mehrdeutiger_anfang_bleibt_unveraendert() -> None:
    bekannte = ["Fraktion Musterliste Nord", "Fraktion Musterliste Süd"]
    assert kanonisch("Fraktion Musterliste", bekannte) == "Fraktion Musterliste"


def test_doppelt_gelesener_umlaut() -> None:
    """Die Texterkennung liest „ü“ gelegentlich als „uü“: in langen Wörtern ein Lesefehler."""
    assert kanonisch("Die Suüdliste Musterstadt", ["Die Südliste Musterstadt"]) == "Die Südliste Musterstadt"


def test_kuerzel_werden_nie_verwechselt() -> None:
    """Ein Buchstabe Unterschied in einem Kürzel ist eine andere Fraktion, kein Lesefehler."""
    bekannte = ["Fraktion ABC", "Fraktion A"]
    assert kanonisch("Fraktion ABD", bekannte) == "Fraktion ABD"
    assert kanonisch("Fraktion B", bekannte) == "Fraktion B"
    assert kanonisch("Fraktion ABC", bekannte) == "Fraktion ABC"


def test_ohne_abgeschnittene_und_mit_umlauten() -> None:
    gelesene = [
        "Fraktion Grun",
        "Internationale Fraktion Bei",
        "Internationale Fraktion Beispielpartei",
        "Fraktion Grün",
    ]
    assert ohne_abgeschnittene(["Liste Musterstadt"], gelesene) == [
        "Liste Musterstadt",
        "Fraktion Grün",
        "Internationale Fraktion Beispielpartei",
    ]


@pytest.mark.django_db
def test_bekannte_fraktionen_der_kommune(welt: Welt) -> None:
    broadcast = Broadcast.objects.create(source=welt.quelle, meeting=welt.sitzung, status=BroadcastStatus.BEENDET)
    jetzt = timezone.now()
    for name, fraktion in [
        ("Erika Muster", "Fraktion Grün-Süd"),
        ("Max Beispiel", "Fraktion Grün-Süd"),
        ("Paula Probe", "Internationale Fraktion Beisp"),
        ("Otto Test", "Internationale Fraktion Beispielpartei"),
        ("Gisela Gast", ""),
    ]:
        BroadcastSpeech.objects.create(broadcast=broadcast, name_read=name, faction_read=fraktion, started_at=jetzt)
    assert bekannte_fraktionen(welt.body.pk, ["Liste Musterstadt"]) == [
        "Liste Musterstadt",
        "Fraktion Grün-Süd",
        "Internationale Fraktion Beispielpartei",
    ]
    assert bekannte_fraktionen(None) == []
