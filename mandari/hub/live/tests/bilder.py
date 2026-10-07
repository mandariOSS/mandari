# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetische Einzelbilder für die Tests (Issue #915): Pillow zeichnet Balken und Text, nichts kommt aus Aufnahmen."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

#: Petrol wie in der Vorlage „balken_unten_dreizeilig“ (Blau und Grün deutlich über Rot)
BALKEN = (0, 96, 112)


def einblendung(
    *,
    top: str = "TOP 5",
    name: str = "Erika Muster",
    fraktion: str = "Fraktion A",
    titel: str = "Neubau einer Grundschule",
    balken: bool = True,
    groesse: tuple[int, int] = (1920, 1080),
    titel_links: float = 0.57,
) -> Image.Image:
    """
    Bild mit Einblendung im Layout der Vorlage (Balken unten, Name links, TOP Mitte, Titel rechts); ``titel_links``
    ist der Beginn des Titels relativ zur Bildbreite.
    """
    breite, hoehe = groesse
    bild = Image.new("RGB", groesse, (40, 40, 40))
    if not balken:
        return bild
    zeichnen = ImageDraw.Draw(bild)
    zeichnen.rectangle((0, int(hoehe * 0.815), breite, int(hoehe * 0.96)), fill=BALKEN)
    gross = ImageFont.load_default(size=int(hoehe * 0.036))
    klein = ImageFont.load_default(size=int(hoehe * 0.03))
    weiss = (255, 255, 255)
    zeichnen.text((int(breite * 0.06), int(hoehe * 0.83)), name, font=gross, fill=weiss)
    zeichnen.text((int(breite * 0.06), int(hoehe * 0.88)), fraktion, font=klein, fill=weiss)
    zeichnen.text((int(breite * 0.37), int(hoehe * 0.885)), top, font=gross, fill=weiss)
    zeichnen.text((int(breite * titel_links), int(hoehe * 0.835)), titel, font=klein, fill=weiss)
    return bild


def mpegts(bilder: int = 3, groesse: tuple[int, int] = (320, 180)) -> bytes:
    """Kurzes MPEG-TS-Segment im Speicher (PyAV, MPEG-2), als Ersatz für ein HLS-Segment."""
    import av

    puffer = io.BytesIO()
    with av.open(puffer, mode="w", format="mpegts") as container:
        spur = container.add_stream("mpeg2video", rate=25)
        spur.width, spur.height = groesse
        spur.pix_fmt = "yuv420p"
        for nummer in range(bilder):
            bild = Image.new("RGB", groesse, (nummer * 40, 96, 112))
            for paket in spur.encode(av.VideoFrame.from_image(bild)):  # type: ignore[no-untyped-call]
                container.mux(paket)
        for paket in spur.encode():
            container.mux(paket)
    return puffer.getvalue()
