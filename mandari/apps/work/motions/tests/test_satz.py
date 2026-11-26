# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Satz-Kette der Dokumente (Issue #1584): Schriftsatz, Positivliste, Satz-Konfiguration, Druckpaket und der PDF-Weg
über Chromium mit Rückfall auf xhtml2pdf.

Die Kette selbst (messen, umbrechen, Lücken) prüfen frontend/satz/test/umbruch.test.mjs und
tests_e2e/test_editor_seiten.py (Editor und Druck brechen gleich um).
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, cast

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse

from apps.common import pdf_chromium, schriften
from apps.work.motions import druck
from apps.work.motions.export_service import motion_export_service
from apps.work.motions.models import Motion, OrganizationLetterhead
from apps.work.motions.satz import FOLGE_OBEN_GENERIERT, STANDARD_RAENDER, satz_konfig
from apps.work.sanitize import sanitize_editor_html

STATIC = Path(settings.BASE_DIR) / "static"
WURZEL = Path(settings.BASE_DIR).parent


# --- Schriftsatz --------------------------------------------------------------------------------------------------


def test_office_namen_werden_auf_kompatible_schriften_abgebildet() -> None:
    assert schriften.aufloesen("Arial").schluessel == "arimo"
    assert schriften.aufloesen("Times New Roman").schluessel == "tinos"
    assert schriften.aufloesen("calibri").schluessel == "carlito"
    assert schriften.aufloesen('"Cambria", serif').schluessel == "caladea"
    assert schriften.aufloesen("Comic Sans MS").schluessel == schriften.STANDARD
    assert schriften.aufloesen(None).schluessel == schriften.STANDARD
    assert schriften.schrift("tinos") is not None
    assert schriften.schrift("tinos").fuer_office == "Times New Roman"  # type: ignore[union-attr]
    assert schriften.schrift("noto-serif").fuer_office == "Noto Serif"  # type: ignore[union-attr]


def test_jede_schriftdatei_liegt_vor_und_steht_in_css_und_reuse() -> None:
    css = (STATIC / "vendor" / "schriften" / "schriften.css").read_text(encoding="utf-8")
    reuse = (WURZEL / "REUSE.toml").read_text(encoding="utf-8")
    for schrift in schriften.SCHRIFTEN:
        assert len(schrift.schnitte) == 4
        for schnitt in schrift.schnitte:
            datei = STATIC / schrift.datei(schnitt)
            assert datei.exists(), datei
            assert datei.stat().st_size > 5_000
            assert f"{schrift.schluessel}/{datei.name}" in css
        assert f'path = "mandari/static/vendor/schriften/{schrift.schluessel}/**"' in reuse


def test_schriften_ohne_reservierten_namen_oder_unveraendert() -> None:
    # Untermengen tragen den Namen weiter: nur bei Schriften ohne „Reserved Font Name“ (Carlito: Originaldateien)
    for schrift in schriften.SCHRIFTEN:
        assert schrift.lizenz == "OFL-1.1"
        if schrift.schluessel == "carlito":
            assert schrift.unveraendert
        for schnitt in schrift.schnitte:
            assert schrift.datei(schnitt).endswith(".ttf" if schrift.unveraendert else ".woff2")


# --- Positivliste ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stil", "erwartet"),
    [
        ("font-family: Arimo", "font-family: Arimo"),
        ('font-family: "Open Sans"', 'font-family: "Open Sans"'),
        ("font-family: 'Noto Serif'", "font-family: 'Noto Serif'"),
        ("font-family: Comic Sans MS", None),
        ('font-family: "Arimo", sans-serif', None),
        ("font-family: Arimo; color: red", "font-family: Arimo; color: red"),
        ("font-size: 12pt", "font-size: 12pt"),
        ("font-size: 10.5pt", "font-size: 10.5pt"),
        ("font-size: 99pt", None),
        ("font-size: 12px", None),
        ("font-size: expression(alert(1))", None),
    ],
)
def test_positivliste_schrift_und_groesse(stil: str, erwartet: str | None) -> None:
    ergebnis = sanitize_editor_html(f'<p><span style="{html.escape(stil)}">x</span></p>')
    if erwartet is None:
        assert "font" not in ergebnis
    else:
        assert f'style="{html.escape(erwartet)}"' in ergebnis


# --- Satz-Konfiguration ------------------------------------------------------------------------------------------


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["motions.view", "motions.edit"], email="satz@example.org")


def _generiert(org: Any, **felder: Any) -> OrganizationLetterhead:
    werte = {
        "organization": org,
        "name": "Fraktion",
        "kind": "generated",
        "address_block": "Fraktion Muster\nRathausplatz 1",
        "sender_line": "Fraktion Muster · Rathausplatz 1",
        "footer_text": "Zeile eins\n\nZeile zwei",
        "content_margin_top": 55,
        "font_family": "Times New Roman",
        "font_size": 12,
    }
    werte.update(felder)
    return OrganizationLetterhead.objects.create(**werte)


def test_konfig_ohne_briefpapier() -> None:
    konfig = satz_konfig(None)
    assert konfig["raender"]["oben"] == STANDARD_RAENDER["oben"]
    assert konfig["raender"]["oben_folge"] == STANDARD_RAENDER["oben"]
    assert konfig["schrift"]["schluessel"] == schriften.STANDARD
    assert konfig["briefpapier"]["art"] == ""
    assert konfig["seite"] == {"breite_mm": 210, "hoehe_mm": 297}


@pytest.mark.django_db
def test_konfig_generierter_briefkopf(org: Any) -> None:
    konfig = satz_konfig(_generiert(org))
    assert konfig["briefpapier"]["art"] == "generiert"
    assert "Rathausplatz 1" in konfig["briefpapier"]["kopf_html"]
    assert konfig["briefpapier"]["fuss_zeilen"] == ["Zeile eins", "Zeile zwei"]
    assert konfig["briefpapier"]["seitenzahlen"] is True
    assert konfig["raender"]["oben"] == 55
    assert konfig["raender"]["oben_folge"] == FOLGE_OBEN_GENERIERT
    assert konfig["schrift"]["schluessel"] == "tinos"
    assert konfig["schrift"]["groesse_pt"] == 12


@pytest.mark.django_db
def test_konfig_akzentfarbe_nur_als_hexfarbe(org: Any) -> None:
    org.primary_color = "red;x"
    org.save(update_fields=["primary_color"])
    briefpapier = satz_konfig(_generiert(org))["briefpapier"]
    assert "red;x" not in briefpapier["kopf_html"]
    assert briefpapier["akzent"] == "#4b5563"


@pytest.mark.django_db
def test_konfig_pdf_briefpapier(org: Any) -> None:
    from django.core.files.base import ContentFile

    briefpapier = OrganizationLetterhead(organization=org, name="PDF", kind="pdf", content_margin_top=40)
    briefpapier.pdf_file.save("kopf.pdf", ContentFile(b"%PDF-1.4"), save=True)
    konfig = satz_konfig(briefpapier, pdf_url="/datei/")
    assert konfig["briefpapier"]["art"] == "pdf"
    assert konfig["briefpapier"]["pdf_url"] == "/datei/"
    assert konfig["raender"]["oben_folge"] == 40


# --- Druckpaket ---------------------------------------------------------------------------------------------------


def _dokument(org: Any, autorin: Any, inhalt: str, letterhead: OrganizationLetterhead | None = None) -> Motion:
    motion = Motion(organization=org, author=autorin, title="Antrag", visibility="organization", letterhead=letterhead)
    cast(Any, motion).set_content_encrypted(inhalt)
    motion.save()
    return motion


def test_verwendete_schriften_aus_dem_text() -> None:
    inhalt = sanitize_editor_html(
        '<p><span style="font-family: &quot;Noto Serif&quot;">a</span><span style="font-family: Tinos">b</span></p>'
    )
    namen = [s.schluessel for s in druck.verwendete_schriften(inhalt, "arimo")]
    assert namen == ["arimo", "noto-serif", "tinos"]


def test_schriften_css_mit_flachen_namen() -> None:
    css = druck.schriften_css([schriften.aufloesen("Arial"), schriften.aufloesen("Calibri")])
    assert 'src: url("arimo-400.woff2") format("woff2")' in css
    assert 'src: url("carlito-700-kursiv.ttf") format("truetype")' in css
    assert "/" not in css.replace("font-family", "").split("url(")[1].split(")")[0].strip('"')


@pytest.mark.django_db
def test_druckpaket(org: Any, autorin: Any, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "manifest.json").write_text("{}", encoding="utf-8")
    (dist / "satz-druck.js").write_text("window.satzFertig = true", encoding="utf-8")
    motion = _dokument(org, autorin, '<p>Text <span style="font-family: Tinos">Tinos</span></p>', _generiert(org))
    vite = {"default": {**settings.DJANGO_VITE["default"], "manifest_path": dist / "manifest.json"}}
    with override_settings(DJANGO_VITE=vite):
        dateien = druck.druck_dateien(motion)
    assert {"index.html", "dokument-satz.css", "schriften.css", "satz-druck.js", "tinos-400.woff2"} <= set(dateien)
    html = dateien["index.html"].decode("utf-8")
    assert '<script src="satz-druck.js"></script>' in html
    konfig = json.loads(html.split('id="satz-konfig" type="application/json">')[1].split("</script>")[0])
    assert {s["schluessel"] for s in konfig["schriften"]} == {"tinos"}
    assert konfig["briefpapier"]["pdf_url"] == ""


@pytest.mark.django_db
def test_druckpaket_ohne_gebautes_frontend(org: Any, autorin: Any, tmp_path: Path) -> None:
    motion = _dokument(org, autorin, "<p>Text</p>")
    vite = {"default": {**settings.DJANGO_VITE["default"], "manifest_path": tmp_path / "fehlt" / "manifest.json"}}
    with override_settings(DJANGO_VITE=vite), pytest.raises(druck.DruckNichtMoeglichError):
        druck.druck_dateien(motion)


@pytest.mark.django_db
def test_signatur_steht_maskiert_im_druck(org: Any, autorin: Any) -> None:
    from apps.work.motions.models import MotionTemplate

    vorlage = MotionTemplate.objects.create(
        organization=org, name="V", signature_block="Gez. <b>{{ organisation }}</b>"
    )
    motion = _dokument(org, autorin, "<p>Text</p>")
    motion.template = vorlage
    motion.save(update_fields=["template"])
    inhalt = druck.druck_inhalt(motion)
    assert "&lt;b&gt;" in inhalt
    assert org.name in inhalt


# --- PDF über Chromium mit Rückfall -------------------------------------------------------------------------------


class _Antwort:
    def __init__(self, status: int, inhalt: bytes) -> None:
        self.status_code = status
        self.content = inhalt


class _Client:
    """Ersatz für httpx.Client: merkt sich die Anfrage, antwortet fest."""

    anfragen: list[dict[str, Any]] = []
    antwort = _Antwort(200, b"%PDF-1.7 chromium")

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def post(self, url: str, data: dict[str, str], files: list[Any]) -> _Antwort:
        _Client.anfragen.append({"url": url, "data": data, "files": [f[1][0] for f in files]})
        return _Client.antwort


@pytest.fixture
def chromium(monkeypatch: pytest.MonkeyPatch) -> type[_Client]:
    _Client.anfragen = []
    _Client.antwort = _Antwort(200, b"%PDF-1.7 chromium")
    monkeypatch.setattr("apps.common.pdf_chromium.httpx.Client", _Client)
    monkeypatch.setattr(druck, "druck_skript", lambda: b"// satz")
    return _Client


@pytest.mark.django_db
def test_ohne_dienst_bleibt_der_bisherige_weg(org: Any, autorin: Any, chromium: type[_Client]) -> None:
    with override_settings(GOTENBERG_URL=""):
        pdf = motion_export_service.export_to_pdf(_dokument(org, autorin, "<p>Text</p>"))
    assert pdf.startswith(b"%PDF")
    assert chromium.anfragen == []


@pytest.mark.django_db
def test_pdf_ueber_chromium(org: Any, autorin: Any, chromium: type[_Client]) -> None:
    with override_settings(GOTENBERG_URL="http://gotenberg:3000"):
        pdf = motion_export_service.export_to_pdf(_dokument(org, autorin, "<p>Text</p>"))
    assert pdf == b"%PDF-1.7 chromium"
    anfrage = chromium.anfragen[0]
    assert anfrage["url"] == "http://gotenberg:3000/forms/chromium/convert/html"
    assert anfrage["data"]["waitForExpression"] == "window.satzFertig === true"
    assert anfrage["data"]["failOnConsoleExceptions"] == "true"
    assert "index.html" in anfrage["files"]


@pytest.mark.django_db
def test_fehler_im_dienst_faellt_auf_den_bisherigen_weg_zurueck(
    org: Any, autorin: Any, chromium: type[_Client]
) -> None:
    chromium.antwort = _Antwort(409, b"Konsole: Ausnahme im Dokument")
    with override_settings(GOTENBERG_URL="http://gotenberg:3000"):
        pdf = motion_export_service.export_to_pdf(_dokument(org, autorin, "<p>Text</p>"))
    assert pdf.startswith(b"%PDF")
    assert pdf != b"%PDF-1.7 chromium"


@pytest.mark.django_db
def test_druckansicht_im_browser(client: Any, org: Any, make_member: Any) -> None:
    mitglied = make_member(org, ["motions.view"], email="druck@example.org")
    motion = _dokument(org, mitglied, "<p>Ansicht</p>")
    client.force_login(mitglied.user)
    url = reverse("work:document_export", kwargs={"org_slug": org.slug, "motion_id": motion.id}) + "?format=druck"
    antwort = client.get(url)
    assert antwort.status_code == 200
    html = antwort.content.decode("utf-8")
    assert 'id="satz-konfig"' in html
    assert "<p>Ansicht</p>" in html
    assert "/static/dist/satz-druck.js" in html


# --- Briefpapier: Schriftauswahl -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_briefpapier_speichert_schrift_des_satzes(client: Any, org: Any, make_member: Any) -> None:
    verwaltung = make_member(org, ["organization.edit"], email="briefpapier@example.org")
    client.force_login(verwaltung.user)
    url = reverse("work:document_letterhead_create", kwargs={"org_slug": org.slug})
    seite = client.get(url).content.decode("utf-8")
    assert "Carlito – wie Calibri" in seite
    antwort = client.post(url, {"name": "Neu", "kind": "generated", "font_family": "Calibri", "font_size": "11"})
    assert antwort.status_code == 302
    assert OrganizationLetterhead.objects.get(organization=org, name="Neu").font_family == "Carlito"
    client.post(url, {"name": "Fremd", "kind": "generated", "font_family": "Comic Sans MS", "font_size": "11"})
    assert OrganizationLetterhead.objects.get(organization=org, name="Fremd").font_family == "Arimo"
