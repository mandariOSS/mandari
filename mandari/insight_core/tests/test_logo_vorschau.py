# SPDX-License-Identifier: AGPL-3.0-or-later
"""
WebP-Vorschaubilder der Kommunen-Logos (insight_core/logo_vorschau.py).

Insight zeigt Logos mit 27 bis 52 CSS-Pixeln an, hochgeladen sind sie oft mit 800 × 800 Pixeln. Beim
Speichern eines Logos entstehen 64- und 128-Pixel-Fassungen mit Inhalts-Hash im Namen, das Original bleibt.
Die Seiten binden sie mit ``srcset``/``width``/``height`` ein und fallen ohne Fassung auf das Original zurück.
"""

from __future__ import annotations

from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import Client, override_settings
from PIL import Image

from insight_core.logo_vorschau import ORDNER
from insight_core.models import OParlBody, OParlSource
from mandari.media import IMMUTABLE_MEDIA_RE

pytestmark = pytest.mark.django_db

RIS = "https://ris.logo.example/oparl"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'


@pytest.fixture(autouse=True)
def media_root(tmp_path: Path) -> Any:
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield tmp_path


def _bild(breite: int, hoehe: int, fmt: str = "PNG", modus: str = "RGBA") -> bytes:
    puffer = BytesIO()
    Image.new(modus, (breite, hoehe), (200, 30, 30, 255) if modus == "RGBA" else (200, 30, 30)).save(puffer, fmt)
    return puffer.getvalue()


def _kommune(nummer: int = 1, **felder: Any) -> OParlBody:
    source, _ = OParlSource.objects.get_or_create(url=f"{RIS}/system", defaults={"name": "Logo-RIS"})
    return OParlBody.objects.create(
        external_id=f"{RIS}/body/{nummer}", source=source, name=f"Logostadt {nummer}", is_listed=True, **felder
    )


def _mit_logo(dateiname: str, inhalt: bytes, nummer: int = 1) -> OParlBody:
    body = _kommune(nummer)
    body.logo.save(dateiname, ContentFile(inhalt))  # speichert die Kommune mit
    return body


def _daten(body: OParlBody) -> dict[str, Any]:
    assert isinstance(body.logo_thumbnails, dict)
    return body.logo_thumbnails


def _datei(media_root: Path, name: str) -> Path:
    return media_root / name


def test_fassungen_in_anzeigegroesse_als_webp(media_root: Path) -> None:
    body = _mit_logo("wappen.png", _bild(800, 400))

    daten = body.logo_thumbnails
    assert daten is not None
    assert [(g["width"], g["height"]) for g in daten["sizes"]] == [(64, 32), (128, 64)]
    for groesse in daten["sizes"]:
        assert groesse["name"].startswith(f"{ORDNER}/wappen-")
        assert IMMUTABLE_MEDIA_RE.match(groesse["name"]), "Name muss zum Cache-Muster in mandari/media.py passen"
        with Image.open(_datei(media_root, groesse["name"])) as bild:
            assert bild.format == "WEBP"
            assert bild.size == (groesse["width"], groesse["height"])
    assert _datei(media_root, str(body.logo.name)).exists(), "Das Original bleibt"
    body.refresh_from_db()
    assert body.logo_thumbnails == daten


def test_img_attribute_mit_srcset_und_massen() -> None:
    body = _mit_logo("wappen.jpg", _bild(800, 800, "JPEG", "RGB"))
    klein, gross = _daten(body)["sizes"]

    attrs = str(body.logo_img_attrs)

    assert f'src="/media/{klein["name"]}"' in attrs
    assert f'srcset="/media/{klein["name"]} 1x, /media/{gross["name"]} 2x"' in attrs
    assert 'width="64" height="64"' in attrs


def test_svg_bleibt_svg() -> None:
    body = _mit_logo("wappen.svg", SVG)

    assert _daten(body)["sizes"] == []
    assert str(body.logo_img_attrs) == f'src="/media/{body.logo.name}"'


def test_kleines_logo_ohne_vergroesserte_kopie() -> None:
    body = _mit_logo("klein.png", _bild(50, 40))

    assert [(g["width"], g["height"]) for g in _daten(body)["sizes"]] == [(50, 40)]
    assert " 2x" not in str(body.logo_img_attrs)


def test_neues_logo_raeumt_alte_fassungen_ab(media_root: Path) -> None:
    body = _mit_logo("alt.png", _bild(300, 300))
    alte = [g["name"] for g in _daten(body)["sizes"]]

    body.logo.save("neu.png", ContentFile(_bild(300, 150, modus="RGB")))

    neue = [g["name"] for g in _daten(body)["sizes"]]
    assert neue and set(neue).isdisjoint(alte)
    assert all(_datei(media_root, n).exists() for n in neue)
    assert not any(_datei(media_root, n).exists() for n in alte)


def test_logo_entfernt(media_root: Path) -> None:
    body = _mit_logo("weg.png", _bild(300, 300))
    namen = [g["name"] for g in _daten(body)["sizes"]]

    body.logo = None
    body.save()

    assert body.logo_thumbnails is None
    assert not any(_datei(media_root, n).exists() for n in namen)
    body.refresh_from_db()
    assert body.logo_thumbnails is None


def test_defektes_bild_verhindert_speichern_nicht() -> None:
    body = _mit_logo("kaputt.png", b"kein Bild")

    assert _daten(body)["sizes"] == []
    assert _daten(body)["error"] is True
    assert str(body.logo_img_attrs) == f'src="/media/{body.logo.name}"'


def test_ohne_vorschau_zeigt_das_original() -> None:
    body = _mit_logo("bestand.png", _bild(400, 400))
    OParlBody.objects.filter(pk=body.pk).update(logo_thumbnails=None)
    body.refresh_from_db()

    assert str(body.logo_img_attrs) == f'src="/media/{body.logo.name}"'


def test_befehl_holt_bestand_nach_und_ist_idempotent(media_root: Path) -> None:
    mit_logo = _mit_logo("bestand.png", _bild(400, 400))
    _kommune(2)  # ohne Logo
    OParlBody.objects.filter(pk=mit_logo.pk).update(logo_thumbnails=None)

    call_command("build_logo_thumbnails", stdout=StringIO())
    mit_logo.refresh_from_db()
    namen = [g["name"] for g in _daten(mit_logo)["sizes"]]
    assert len(namen) == 2
    zeiten = [_datei(media_root, n).stat().st_mtime_ns for n in namen]

    ausgabe = StringIO()
    call_command("build_logo_thumbnails", stdout=ausgabe)
    assert "0 Kommunen aktualisiert, 1 unverändert" in ausgabe.getvalue()

    call_command("build_logo_thumbnails", "--force", stdout=StringIO())
    mit_logo.refresh_from_db()
    assert [g["name"] for g in _daten(mit_logo)["sizes"]] == namen
    assert [_datei(media_root, n).stat().st_mtime_ns for n in namen] == zeiten, "gleicher Inhalt: nicht neu schreiben"


def test_cache_header_vorschau_ein_jahr_original_eine_stunde() -> None:
    body = _mit_logo("cache.png", _bild(400, 400))
    vorschau = _daten(body)["sizes"][0]["name"]
    client = Client()

    antwort = client.get(f"/media/{vorschau}")
    assert antwort.status_code == 200
    assert antwort["Cache-Control"] == "public, max-age=31536000, immutable"

    original = client.get(f"/media/{body.logo.name}")
    assert original.status_code == 200
    assert original["Cache-Control"] == "public, max-age=3600"


def test_kommunenwahl_und_seitenleiste_binden_die_vorschau_ein() -> None:
    body = _mit_logo("wahl.png", _bild(800, 800))
    _kommune(2)  # bei nur einer Kommune entfällt die Auswahl
    klein = _daten(body)["sizes"][0]["name"]
    client = Client()

    wahl = client.get("/insight/").content.decode()
    assert f'src="/media/{klein}"' in wahl
    assert 'width="64" height="64"' in wahl
    assert f'src="/media/{body.logo.name}"' not in wahl, "Das Original wird nicht mehr geladen"

    client.get(f"/insight/kommune/{body.id}/")
    start = client.get("/insight/").content.decode()
    assert f'srcset="/media/{klein} 1x' in start
