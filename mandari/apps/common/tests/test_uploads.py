# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame Upload-Prüfung und Auslieferung von Medien (Issue #260).

BSI CON.10.A5 und APP.3.1.A4 verlangen die Einschränkung nach Dateigröße **und**
Dateityp. Geprüft wird beides — und zusätzlich, dass eine Datei, die sich doch
einmal in den Medienordner verirrt, nicht eingebettet ausgeliefert wird.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.common.uploads import DATA, DOCUMENTS, IMAGES, MB, is_embeddable, validate_upload


def datei(name: str, groesse: int = 1024) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"x" * groesse)


class TestValidateUpload:
    @pytest.mark.parametrize("name", ["bericht.pdf", "Tabelle.XLSX", "notiz.txt", "bild.png", "anlage.zip"])
    def test_erlaubte_dokumente_gehen_durch(self, name: str) -> None:
        validate_upload(datei(name), allowed=DOCUMENTS, max_bytes=20 * MB)

    @pytest.mark.parametrize(
        "name",
        [
            "angriff.html",
            "angriff.htm",
            "angriff.svg",
            "angriff.SVG",
            "angriff.js",
            "angriff.xhtml",
            "angriff.php",
            "angriff.exe",
            "angriff.vbs",
        ],
    )
    def test_aktive_inhalte_werden_abgelehnt(self, name: str) -> None:
        """Genau die Typen, die ein Browser ausführen würde."""
        with pytest.raises(ValidationError):
            validate_upload(datei(name), allowed=DOCUMENTS, max_bytes=20 * MB)

    def test_svg_auch_im_bildprofil_abgelehnt(self) -> None:
        """SVG ist ein Bildformat, das Skript tragen darf — deshalb nirgends erlaubt."""
        assert ".svg" not in IMAGES
        with pytest.raises(ValidationError):
            validate_upload(datei("logo.svg"), allowed=IMAGES, max_bytes=2 * MB)

    def test_never_sticht_auch_wenn_ein_profil_es_erlaubt(self) -> None:
        """Ein versehentlich zu weites Profil darf die Sperrliste nicht aushebeln."""
        zu_weit = frozenset({".pdf", ".html"})
        with pytest.raises(ValidationError):
            validate_upload(datei("angriff.html"), allowed=zu_weit, max_bytes=20 * MB)

    def test_doppelte_endung_wird_an_der_letzten_gemessen(self) -> None:
        validate_upload(datei("bericht.html.pdf"), allowed=DOCUMENTS, max_bytes=20 * MB)
        with pytest.raises(ValidationError):
            validate_upload(datei("bericht.pdf.html"), allowed=DOCUMENTS, max_bytes=20 * MB)

    def test_pfadangaben_im_namen_aendern_die_endung_nicht(self) -> None:
        with pytest.raises(ValidationError):
            validate_upload(datei(r"..\..\angriff.svg"), allowed=DOCUMENTS, max_bytes=20 * MB)

    def test_zu_gross_wird_abgelehnt(self) -> None:
        with pytest.raises(ValidationError) as exc:
            validate_upload(datei("bericht.pdf", groesse=3 * MB), allowed=DOCUMENTS, max_bytes=2 * MB)
        assert "2 MB" in exc.value.messages[0]

    def test_leer_und_ohne_endung_werden_abgelehnt(self) -> None:
        with pytest.raises(ValidationError):
            validate_upload(datei("leer.pdf", groesse=0), allowed=DOCUMENTS, max_bytes=20 * MB)
        with pytest.raises(ValidationError):
            validate_upload(datei("ohneendung"), allowed=DOCUMENTS, max_bytes=20 * MB)
        with pytest.raises(ValidationError):
            validate_upload(None, allowed=DOCUMENTS, max_bytes=20 * MB)

    def test_meldung_nennt_die_erlaubten_typen(self) -> None:
        with pytest.raises(ValidationError) as exc:
            validate_upload(datei("liste.csv"), allowed=IMAGES, max_bytes=2 * MB)
        meldung = exc.value.messages[0]
        assert "csv" in meldung and "png" in meldung

    def test_datenprofil_erlaubt_import_formate(self) -> None:
        validate_upload(datei("import.csv"), allowed=DATA, max_bytes=5 * MB)
        validate_upload(datei("import.json"), allowed=DATA, max_bytes=5 * MB)


class TestEinbettbarkeit:
    @pytest.mark.parametrize("pfad", ["organizations/logos/a.png", "avatars/b.JPG", "demo/c.webp"])
    def test_bilder_duerfen_eingebettet_werden(self, pfad: str) -> None:
        assert is_embeddable(pfad)

    @pytest.mark.parametrize(
        "pfad",
        ["faction/attachments/a.pdf", "support/attachments/b.html", "x/c.svg", "y/d.docx", "z/e"],
    )
    def test_alles_andere_geht_als_download(self, pfad: str) -> None:
        assert not is_embeddable(pfad)


@pytest.mark.django_db
class TestAuslieferung:
    """serve_media als zweite Verteidigungslinie."""

    def test_nicht_bild_wird_als_download_ausgeliefert(self, client: Client, tmp_path: Path, settings: Any) -> None:
        settings.MEDIA_ROOT = tmp_path
        ziel = tmp_path / "demo"
        ziel.mkdir()
        (ziel / "anlage.pdf").write_bytes(b"%PDF-1.4 test")
        antwort = client.get("/media/demo/anlage.pdf")
        assert antwort.status_code == 200
        assert antwort["Content-Disposition"].startswith("attachment;")
        assert antwort["X-Content-Type-Options"] == "nosniff"

    def test_bild_bleibt_eingebettet(self, client: Client, tmp_path: Path, settings: Any) -> None:
        settings.MEDIA_ROOT = tmp_path
        ziel = tmp_path / "demo"
        ziel.mkdir()
        (ziel / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        antwort = client.get("/media/demo/logo.png")
        assert antwort.status_code == 200
        # Django liefert bei FileResponse immer eine Disposition; entscheidend ist "inline"
        assert antwort["Content-Disposition"].startswith("inline")
        assert antwort["X-Content-Type-Options"] == "nosniff"


class TestXmlNurAlsImportdaten:
    """XML ist Datenformat der Import-Schnittstellen (nur geparst), aber kein Dokument."""

    def test_xml_im_datenprofil_erlaubt(self) -> None:
        validate_upload(datei("aufgaben.xml"), allowed=DATA, max_bytes=MB, bezeichnung="Importdatei")

    def test_xml_im_dokumentprofil_abgelehnt(self) -> None:
        with pytest.raises(ValidationError):
            validate_upload(datei("anlage.xml"), allowed=DOCUMENTS, max_bytes=MB, bezeichnung="Anlage")

    def test_xsl_bleibt_ueberall_gesperrt(self) -> None:
        with pytest.raises(ValidationError):
            validate_upload(datei("boese.xsl"), allowed=DATA, max_bytes=MB, bezeichnung="Importdatei")
