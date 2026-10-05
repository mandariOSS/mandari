# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nichtöffentliche Unterlagen einlesen und als nichtöffentliche TOPs übernehmen (Issue #873).

- TOP-Nummer und Titel werden aus Text-PDF und gescannter PDF erkannt; die Texterkennung läuft nur im
  eigenen Betrieb (nie über einen externen Dienst).
- Die Datei liegt in „Nichtöffentliche Vorgänge“; TOPs entstehen erst nach Bestätigung durch Vorsitz,
  stellvertretenden Vorsitz oder Geschäftsführung – nur wenn die Person vereidigt ist.
- Nicht vereidigte Personen und Gäste sehen weder Datei noch Titel der übernommenen TOPs: Sitzungsliste,
  Suche, Detail, TOP-Ansicht, Download, Einladungsmail, Tagesordnung, Kalender und öffentliche Schnittstelle.
"""

from __future__ import annotations

import io
import shutil
from datetime import timedelta
from typing import Any, cast
from unittest import mock

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from mandari_dokumente import OcrResult

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.faction import internal_documents
from apps.work.faction import services as faction_services
from apps.work.faction.feeds import build_personal_feed
from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionMeeting, FactionPublicApiAccess
from apps.work.motions.models import DocumentFolder, Motion, MotionDocument

TITEL_1 = "Grundstuecksangelegenheit Musterweg"
TITEL_2 = "Personalangelegenheit Amtsleitung"
OEFFENTLICH = "Bebauungsplan Nordstadt"

UNTERLAGE_ZEILEN = [
    "Stadt Musterstadt",
    "Tagesordnung der Sitzung des Rates am 12.10.2026",
    "Oeffentlicher Teil",
    f"Ö 1 {OEFFENTLICH}",
    "Nichtöffentlicher Teil",
    f"N 1 {TITEL_1}",
    f"N 2 {TITEL_2}",
]


def _text_pdf(zeilen: list[str] | None = None) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    puffer = io.BytesIO()
    leinwand = canvas.Canvas(puffer, pagesize=A4)
    y = 780
    for zeile in zeilen or UNTERLAGE_ZEILEN:
        leinwand.drawString(72, y, zeile)
        y -= 24
    leinwand.showPage()
    leinwand.save()
    return puffer.getvalue()


def _gescannte_pdf() -> bytes:
    """PDF ohne Textebene (nur Grafik), wie ein Scan."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    puffer = io.BytesIO()
    leinwand = canvas.Canvas(puffer, pagesize=A4)
    leinwand.rect(72, 600, 400, 150, fill=1)
    leinwand.showPage()
    leinwand.save()
    return puffer.getvalue()


def _bild_pdf() -> bytes:
    """Echter Scan: Text als Bild ohne Textebene (für die Texterkennung mit Tesseract)."""
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    bild = Image.new("L", (2000, 900), 255)
    zeichnen = ImageDraw.Draw(bild)
    schrift = ImageFont.load_default(size=56)
    for index, zeile in enumerate(["Nichtoeffentlicher Teil", f"N 1 {TITEL_1}", f"N 2 {TITEL_2}"]):
        zeichnen.text((60, 80 + index * 160), zeile, fill=0, font=schrift)
    puffer = io.BytesIO()
    leinwand = canvas.Canvas(puffer, pagesize=A4)
    leinwand.drawImage(ImageReader(bild), 30, 400, width=535, height=240)
    leinwand.showPage()
    leinwand.save()
    return puffer.getvalue()


@pytest.fixture(autouse=True)
def _medien(settings: Any, tmp_path: Any) -> None:
    settings.MEDIA_ROOT = tmp_path


def _vereidigt(member: Any) -> Any:
    member.is_sworn_in = True
    member.save(update_fields=["is_sworn_in"])
    return member


LESEN = ["faction.view_public", "faction.view_non_public", "motions.view", "protocols.view_public", "dashboard.view"]


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    return _vereidigt(make_member(org, [*LESEN, "agenda.approve", "agenda.view"], email="vorsitz@example.org"))


@pytest.fixture
def geschaeftsfuehrung(org: Any, make_member: Any) -> Any:
    return _vereidigt(make_member(org, [*LESEN, "faction.manage"], email="gf@example.org"))


@pytest.fixture
def mitglied_vereidigt(org: Any, make_member: Any) -> Any:
    return _vereidigt(make_member(org, [*LESEN, "agenda.create"], email="mitglied@example.org"))


@pytest.fixture
def vorsitz_nicht_vereidigt(org: Any, make_member: Any) -> Any:
    return make_member(org, [*LESEN, "agenda.approve", "faction.manage"], email="vorsitz-offen@example.org")


@pytest.fixture
def sachkundig(org: Any, make_member: Any) -> Any:
    """Nicht vereidigt, sieht den öffentlichen Teil."""
    return make_member(org, [*LESEN, "agenda.propose"], email="sachkundig@example.org")


@pytest.fixture
def gast(org: Any) -> Any:
    konto = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    return MembershipFactory(user=konto, organization=org, is_guest=True, is_sworn_in=True)  # type: ignore[no-untyped-call]


@pytest.fixture
def sitzung(org: Any, vorsitz: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() + timedelta(days=3),
        status="planned",
        created_by=vorsitz,
    )


def _einlesen_url(org: Any, sitzung: FactionMeeting) -> str:
    return reverse("work:faction_internal_import", kwargs={"org_slug": org.slug, "meeting_id": sitzung.id})


def _uebernehmen_url(org: Any, sitzung: FactionMeeting, motion: Motion) -> str:
    return reverse(
        "work:faction_internal_import_confirm",
        kwargs={"org_slug": org.slug, "meeting_id": sitzung.id, "motion_id": motion.id},
    )


def _hochladen(client: Any, org: Any, sitzung: FactionMeeting, daten: bytes, name: str = "rat-noe.pdf") -> Any:
    return client.post(
        _einlesen_url(org, sitzung), {"datei": SimpleUploadedFile(name, daten, content_type="application/pdf")}
    )


# =============================================================================
# Erkennung
# =============================================================================


def test_erkennt_nur_den_nichtoeffentlichen_abschnitt() -> None:
    vorschlaege = internal_documents.parse_agenda_items(UNTERLAGE_ZEILEN)
    assert [(v.source_number, v.title) for v in vorschlaege] == [("N 1", TITEL_1), ("N 2", TITEL_2)]


@pytest.mark.parametrize(
    ("zeilen", "erwartet"),
    [
        # Nummern ohne Kennung, mit Punkt, Klammer, Unterpunkten und „TOP“
        (
            ["Nicht öffentliche Sitzung", "1. Mitteilungen", "2) Vertragssache", "2.1 Nachtrag", "TOP 3: Anfragen"],
            [("1", "Mitteilungen"), ("2", "Vertragssache"), ("2.1", "Nachtrag"), ("3", "Anfragen")],
        ),
        # Mehrzeiliger Titel mit Trennstrich und Fortsetzung in Kleinbuchstaben
        (
            ["Nichtöffentlicher Teil", "NÖ 4 Vergabe der Unterhaltungs-", "arbeiten an Schulen", "NÖ 5 Sonstiges"],
            [("NÖ 4", "Vergabe der Unterhaltungsarbeiten an Schulen"), ("NÖ 5", "Sonstiges")],
        ),
        # Kopfzeile „nichtöffentlich“ vor dem öffentlichen Teil: es zählt der spätere nichtöffentliche Abschnitt
        (
            ["Vertraulich – nichtöffentlich", "Öffentlicher Teil", "1 Haushalt", "Nichtöffentlicher Teil", "7 Vergabe"],
            [("7", "Vergabe")],
        ),
        # Nur ein öffentlicher Abschnitt: keine Vorschläge
        (["Öffentliche Sitzung", "1 Haushalt", "2 Radwege"], []),
        # Ohne Abschnitt: nur Punkte mit Kennung N, Datum, Uhrzeit, Postleitzahl und Seitenzahl fallen heraus
        (
            [
                "12. Oktober 2026",
                "18:00 Uhr",
                "48143 Musterstadt",
                "Ö 1 Haushalt",
                "N 7 Steuerangelegenheit",
                "Seite 1 von 2",
            ],
            [("N 7", "Steuerangelegenheit")],
        ),
    ],
)
def test_nummern_und_titel_in_verschiedenen_formen(zeilen: list[str], erwartet: list[tuple[str, str]]) -> None:
    vorschlaege = internal_documents.parse_agenda_items(zeilen)
    assert [(v.source_number, v.title) for v in vorschlaege] == erwartet


def test_ohne_punkte_keine_vorschlaege() -> None:
    assert internal_documents.parse_agenda_items(["Einladung", "Sehr geehrte Damen und Herren,"]) == []


# =============================================================================
# Einlesen und Übernehmen
# =============================================================================


@pytest.mark.django_db
@override_settings(MISTRAL_API_KEY="nie-verwenden")
def test_text_pdf_wird_abgelegt_und_vorgeschlagen(
    org: Any, sitzung: FactionMeeting, vorsitz: Any, client_for: Any
) -> None:
    with mock.patch("mandari_dokumente.texterkennung.extract_text_with_mistral") as extern:
        antwort = _hochladen(client_for(vorsitz.user), org, sitzung, _text_pdf())

    extern.assert_not_called()
    assert antwort.status_code == 200
    text = antwort.content.decode()
    assert TITEL_1 in text and TITEL_2 in text and OEFFENTLICH not in text
    unterlage = Motion.objects.get(organization=org)
    assert unterlage.is_sworn_in_only()
    assert MotionDocument.objects.filter(motion=unterlage, mime_type="application/pdf").count() == 1
    # Noch keine TOPs: erst nach Bestätigung
    assert not FactionAgendaItem.objects.filter(meeting=sitzung).exists()


@pytest.mark.django_db
@override_settings(MISTRAL_API_KEY="nie-verwenden")
def test_gescannte_pdf_nur_mit_eigener_texterkennung(
    org: Any, sitzung: FactionMeeting, vorsitz: Any, client_for: Any
) -> None:
    erkannt = OcrResult(text="\n".join(UNTERLAGE_ZEILEN), pages_rendered=1)
    with (
        mock.patch("mandari_dokumente.texterkennung.ocr_pdf", return_value=erkannt) as ocr,
        mock.patch("mandari_dokumente.texterkennung.extract_text_with_mistral") as extern,
    ):
        antwort = _hochladen(client_for(vorsitz.user), org, sitzung, _gescannte_pdf())

    ocr.assert_called_once()
    extern.assert_not_called()
    assert antwort.status_code == 200
    assert TITEL_1 in antwort.content.decode()


@pytest.mark.django_db
@pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("pdftoppm")), reason="Tesseract/Poppler fehlen")
def test_gescannte_pdf_mit_tesseract(org: Any, sitzung: FactionMeeting, vorsitz: Any, client_for: Any) -> None:
    antwort = _hochladen(client_for(vorsitz.user), org, sitzung, _bild_pdf())

    assert antwort.status_code == 200
    unterlage = Motion.objects.get(organization=org)
    vorschlaege, _hinweis = internal_documents.proposals_for(unterlage)
    assert any("Grundst" in v.title for v in vorschlaege), vorschlaege


@pytest.mark.django_db
def test_bestaetigung_legt_nichtoeffentliche_tops_an(
    org: Any, sitzung: FactionMeeting, vorsitz: Any, client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    _hochladen(client, org, sitzung, _text_pdf())
    unterlage = Motion.objects.get(organization=org)

    # Rückkehr zur Auswahl zeigt die Vorschläge erneut
    assert TITEL_2 in client.get(_uebernehmen_url(org, sitzung, unterlage)).content.decode()
    antwort = client.post(
        _uebernehmen_url(org, sitzung, unterlage),
        {"nr": ["N 1", "N 2"], "titel": [f"{TITEL_1} (korrigiert)", TITEL_2], "auswahl": ["0"]},
    )

    assert antwort.status_code == 302
    top = FactionAgendaItem.objects.get(meeting=sitzung)
    assert (top.number, top.title, top.visibility) == ("NÖ 1", f"{TITEL_1} (korrigiert)", "internal")
    assert "N 1" in (cast(Any, top).get_description_decrypted() or "")
    assert list(top.related_motions.all()) == [unterlage]


@pytest.mark.django_db
def test_geschaeftsfuehrung_darf_einlesen(
    org: Any, sitzung: FactionMeeting, geschaeftsfuehrung: Any, client_for: Any
) -> None:
    assert _hochladen(client_for(geschaeftsfuehrung.user), org, sitzung, _text_pdf()).status_code == 200


@pytest.mark.django_db
def test_nur_vereidigte_mit_genehmigungsrecht(
    org: Any,
    sitzung: FactionMeeting,
    vorsitz: Any,
    mitglied_vereidigt: Any,
    vorsitz_nicht_vereidigt: Any,
    sachkundig: Any,
    gast: Any,
    make_member: Any,
    client_for: Any,
) -> None:
    admin = make_member(org, [], email="admin@example.org", is_admin=True)
    _hochladen(client_for(vorsitz.user), org, sitzung, _text_pdf())
    unterlage = Motion.objects.get(organization=org)

    for person in (mitglied_vereidigt, vorsitz_nicht_vereidigt, sachkundig, gast, admin):
        # Gäste leitet Work auf ihre Freigaben-Übersicht um, alle anderen erhalten 403
        abgewiesen = (302,) if person.is_guest else (403, 404)
        client = client_for(person.user)
        assert client.get(_einlesen_url(org, sitzung)).status_code in abgewiesen, person.user.email
        assert _hochladen(client, org, sitzung, _text_pdf()).status_code in abgewiesen, person.user.email
        bestaetigen = client.post(
            _uebernehmen_url(org, sitzung, unterlage), {"nr": ["N 1"], "titel": [TITEL_1], "auswahl": ["0"]}
        )
        assert bestaetigen.status_code in abgewiesen, person.user.email
    assert Motion.objects.filter(organization=org).count() == 1
    assert not FactionAgendaItem.objects.filter(meeting=sitzung).exists()


@pytest.mark.django_db
def test_nur_unterlagen_aus_dem_ordner_und_offene_sitzungen(
    org: Any, sitzung: FactionMeeting, vorsitz: Any, client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    normal = Motion.objects.create(organization=org, author=vorsitz, title="Antrag", visibility="organization")
    falsch = client.post(_uebernehmen_url(org, sitzung, normal), {"nr": ["1"], "titel": ["X"], "auswahl": ["0"]})
    text_datei = client.post(
        _einlesen_url(org, sitzung), {"datei": SimpleUploadedFile("notiz.txt", b"N 1 Test", content_type="text/plain")}
    )
    sitzung.status = "completed"
    sitzung.save(update_fields=["status"])
    beendet = _hochladen(client, org, sitzung, _text_pdf())

    assert falsch.status_code == 404
    assert text_datei.status_code == 400
    assert beendet.status_code == 302
    assert not DocumentFolder.objects.filter(organization=org, sworn_in_only=True).exists()
    assert not FactionAgendaItem.objects.filter(meeting=sitzung).exists()


# =============================================================================
# Lesewege der übernommenen TOPs
# =============================================================================


@pytest.fixture
def uebernommen(org: Any, sitzung: FactionMeeting, vorsitz: Any, sachkundig: Any, client_for: Any) -> Motion:
    client = client_for(vorsitz.user)
    _hochladen(client, org, sitzung, _text_pdf())
    unterlage = Motion.objects.get(organization=org)
    client.post(
        _uebernehmen_url(org, sitzung, unterlage),
        {"nr": ["N 1", "N 2"], "titel": [TITEL_1, TITEL_2], "auswahl": ["0", "1"]},
    )
    assert FactionAgendaItem.objects.filter(meeting=sitzung, visibility="internal").count() == 2
    for person in (vorsitz, sachkundig):
        FactionAttendance.objects.create(meeting=sitzung, membership=person, status="invited")
    return unterlage


@pytest.mark.django_db
def test_liste_suche_detail_und_top_ansicht(
    org: Any,
    sitzung: FactionMeeting,
    uebernommen: Motion,
    vorsitz: Any,
    sachkundig: Any,
    gast: Any,
    client_for: Any,
) -> None:
    liste = reverse("work:faction", kwargs={"org_slug": org.slug})
    detail = reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": sitzung.id})
    top = FactionAgendaItem.objects.filter(meeting=sitzung).first()
    assert top is not None
    panel = reverse(
        "work:faction_item_panel", kwargs={"org_slug": org.slug, "meeting_id": sitzung.id, "item_id": top.id}
    )

    client = client_for(sachkundig.user)
    for antwort in (client.get(liste), client.get(liste, {"q": "Musterweg"}), client.get(detail)):
        text = antwort.content.decode()
        assert TITEL_1 not in text and TITEL_2 not in text and uebernommen.title not in text
    assert client.get(panel).status_code == 403
    gast_client = client_for(gast.user)
    for url in (liste, detail, panel):
        antwort = gast_client.get(url)
        assert antwort.status_code in (302, 403, 404) and TITEL_1 not in antwort.content.decode()

    vorsitz_client = client_for(vorsitz.user)
    assert TITEL_1 in vorsitz_client.get(detail).content.decode()
    assert uebernommen.title in vorsitz_client.get(panel).content.decode()


@pytest.mark.django_db
def test_einladungsmail_und_tagesordnung(
    monkeypatch: pytest.MonkeyPatch, sitzung: FactionMeeting, uebernommen: Motion
) -> None:
    monkeypatch.setattr(faction_services, "html_to_pdf", lambda html: html.encode())

    faction_services.FactionMeetingEmailService().send_invitations(sitzung)

    nachrichten = {nachricht.to[0]: nachricht for nachricht in mail.outbox}
    offen = nachrichten["sachkundig@example.org"]
    vertraulich = nachrichten["vorsitz@example.org"]
    for nachricht in (offen, vertraulich):
        # Die Unterlage selbst geht nie per Mail hinaus
        assert sorted(name for name, _inhalt, _typ in nachricht.attachments) == ["sitzung.ics", "tagesordnung.pdf"]
    assert TITEL_1 not in offen.body and TITEL_1 not in str(getattr(offen, "alternatives", ""))
    for _name, inhalt, _typ in offen.attachments:
        assert TITEL_1 not in (inhalt.decode("utf-8", "replace") if isinstance(inhalt, bytes) else str(inhalt))
    assert TITEL_1 in vertraulich.body or TITEL_1 in str(getattr(vertraulich, "alternatives", ""))


@pytest.mark.django_db
def test_kalender_und_oeffentliche_schnittstelle(
    org: Any, client: Any, sitzung: FactionMeeting, uebernommen: Motion, vorsitz: Any, sachkundig: Any
) -> None:
    for person in (vorsitz, sachkundig):
        assert TITEL_1.encode() not in build_personal_feed(person.user)

    zugang = FactionPublicApiAccess.objects.create(organization=org, is_enabled=True, show_agenda=True)
    antwort = client.get(f"/api/public/v1/fraktionen/{zugang.token}/sitzungen/{sitzung.id}/")
    assert antwort.status_code == 200
    assert TITEL_1 not in antwort.content.decode() and uebernommen.title not in antwort.content.decode()
