# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generiert die PWA-App-Icons (Android/Manifest) aus dem Brand-Favicon.

Quelle: mandari/static/brand/favicon-512.png (transparentes "m." in Indigo,
erzeugt von marketing-website/scripts/generate_logo.py).

Erzeugt vollflaechige Icons (weisses "m." auf Indigo #4F46E5), die sowohl
als purpose "any" wie auch "maskable" taugen: Der Schriftzug ist so
skaliert, dass er komplett in der Maskable-Safe-Zone (Kreis mit 80 %
Durchmesser) liegt.

    python scripts/generate_pwa_icons.py
"""

import math
from pathlib import Path

from PIL import Image

BRAND_DIR = Path(__file__).resolve().parent.parent / "mandari" / "static" / "brand"
INDIGO = (79, 70, 229, 255)  # #4F46E5 (theme_color)
WHITE = (255, 255, 255, 255)


def build_icon(size: int) -> Image.Image:
    source = Image.open(BRAND_DIR / "favicon-512.png").convert("RGBA")
    alpha = source.split()[3]
    bbox = alpha.getbbox()
    glyph_alpha = alpha.crop(bbox)
    gw, gh = glyph_alpha.size

    # Safe-Zone: Diagonale des Schriftzugs muss in den Kreis mit Radius 40 %
    # der Kantenlaenge passen (Maskable-Spezifikation).
    radius = 0.40 * size
    scale = (2 * radius) / math.hypot(gw, gh)
    tw, th = max(1, round(gw * scale)), max(1, round(gh * scale))
    glyph_alpha = glyph_alpha.resize((tw, th), Image.LANCZOS)

    icon = Image.new("RGBA", (size, size), INDIGO)
    glyph = Image.new("RGBA", (tw, th), WHITE)
    icon.paste(glyph, ((size - tw) // 2, (size - th) // 2), glyph_alpha)
    return icon


def main() -> None:
    for size in (192, 512):
        icon = build_icon(size)
        for name in (f"icon-{size}.png", f"icon-maskable-{size}.png"):
            icon.save(BRAND_DIR / name, optimize=True)
            print("geschrieben:", BRAND_DIR / name)


if __name__ == "__main__":
    main()
