# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Live-Seite einer Sitzung (Issue #915): Status, Zwei-Klick-Player, TOP und Person mit Links, Verlauf,
Aktualisierung per htmx, ``noindex``, 404 ohne Quelle, kein Link aus der Sitzungsseite.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from hub.live import selectors
from hub.live.models import (
    Broadcast,
    BroadcastSection,
    BroadcastSpeech,
    BroadcastStatus,
    SpeechAssignment,
)
from hub.live.tests.conftest import EMBED_ID, Welt, kennung
from insight_core.models import OParlMeeting, OParlOrganization

pytestmark = pytest.mark.django_db


def _url(meeting: OParlMeeting) -> str:
    return reverse("insight_core:insight:meeting_live", args=[meeting.pk])


@pytest.fixture
def laufend(welt: Welt) -> Broadcast:
    broadcast = Broadcast.objects.create(
        source=welt.quelle,
        meeting=welt.sitzung,
        status=BroadcastStatus.LIVE,
        started_at=timezone.localtime(welt.jetzt).replace(hour=16, minute=15),
    )
    abschnitt = BroadcastSection.objects.create(
        broadcast=broadcast, agenda_item=welt.top5, number="5", started_at=welt.jetzt
    )
    unbekannt = BroadcastSpeech.objects.create(
        broadcast=broadcast,
        section=abschnitt,
        name_read="Gisela Gast",
        function_read="Bürgermeisterin",
        started_at=welt.jetzt,
        assignment=SpeechAssignment.KEINE,
    )
    am_wort = BroadcastSpeech.objects.create(
        broadcast=broadcast,
        section=abschnitt,
        person=welt.muster,
        name_read="Erika Muster",
        faction_read="Fraktion A",
        started_at=welt.jetzt + timedelta(minutes=2),
        assignment=SpeechAssignment.EINDEUTIG,
        readings=3,
    )
    assert unbekannt.pk != am_wort.pk
    broadcast.current_section = abschnitt
    broadcast.current_speech = am_wort
    broadcast.save()
    return broadcast


def test_laufende_uebertragung(client: Client, welt: Welt, laufend: Broadcast) -> None:
    antwort = client.get(_url(welt.sitzung))
    assert antwort.status_code == 200
    html = antwort.content.decode()
    assert "Die Übertragung läuft seit 16:15 Uhr." in html
    assert "TOP 5 – Neubau einer Grundschule" in html
    # TOP mit Vorlage → Link auf die Vorlage; Person eindeutig → Link auf die Personenseite
    assert reverse("insight_core:insight:paper_detail", args=[welt.vorlage.pk]) in html
    assert reverse("insight_core:insight:person_detail", args=[welt.muster.pk]) in html
    assert "Erika Muster</a> (Fraktion A)" in html
    # ohne Zuordnung nur Text, Funktion statt Fraktion
    assert "Gisela Gast, Bürgermeisterin" in html
    assert 'aria-live="polite"' in html
    assert 'hx-trigger="every 15s"' in html


def test_noindex_und_kein_cache(client: Client, welt: Welt, laufend: Broadcast) -> None:
    antwort = client.get(_url(welt.sitzung))
    assert antwort["X-Robots-Tag"] == "noindex, nofollow"
    assert 'content="noindex, nofollow"' in antwort.content.decode()
    assert antwort["Cache-Control"] == "no-cache"


def test_player_erst_nach_klick(client: Client, welt: Welt, laufend: Broadcast) -> None:
    einbettung = f"https://playout.3qsdn.com/embed/{EMBED_ID}"
    html = client.get(_url(welt.sitzung)).content.decode()
    # Die Seitenvorlage hat eigene iframes (Dokumentvorschau); der Player fehlt bis zum Klick
    assert einbettung not in html and 'title="Übertragung:' not in html
    assert "Übertragung laden" in html and "IP-Adresse" in html

    antwort = client.get(_url(welt.sitzung), {"player": "1"})
    html = antwort.content.decode()
    assert f'<iframe src="{einbettung}"' in html
    richtlinie = antwort["Content-Security-Policy"]
    assert re.search(r"frame-src [^;]*https://playout\.3qsdn\.com", richtlinie)


def test_aktualisierung_nur_der_live_teil_und_204_ohne_neues(client: Client, welt: Welt, laufend: Broadcast) -> None:
    antwort = client.get(_url(welt.sitzung), {"teil": "stand"})
    assert antwort.status_code == 200
    html = antwort.content.decode()
    assert "<html" not in html and "Die Übertragung läuft" in html
    assert 'hx-swap-oob="innerHTML:#live-verlauf"' in html
    version = re.search(r"v=([^\"&]+)\"", html)
    assert version is not None
    assert client.get(_url(welt.sitzung), {"teil": "stand", "v": version.group(1)}).status_code == 204

    BroadcastSpeech.objects.create(
        broadcast=laufend,
        section=laufend.current_section,
        name_read="Max Beispiel",
        started_at=welt.jetzt + timedelta(minutes=5),
    )
    assert client.get(_url(welt.sitzung), {"teil": "stand", "v": version.group(1)}).status_code == 200


def test_beendet_mit_verlauf_ohne_aktualisierung(client: Client, welt: Welt, laufend: Broadcast) -> None:
    laufend.status = BroadcastStatus.BEENDET
    laufend.ended_at = timezone.localtime(welt.jetzt).replace(hour=18, minute=25)
    laufend.save()
    html = client.get(_url(welt.sitzung)).content.decode()
    assert "Die Übertragung wurde um 18:25 Uhr beendet." in html
    assert "Am Wort" not in html, "nach dem Ende spricht niemand mehr"
    assert "Gisela Gast" in html and "Erika Muster" in html, "der Verlauf bleibt"
    assert "hx-trigger" not in html


def test_geplant_ohne_uebertragung(client: Client, welt: Welt) -> None:
    html = client.get(_url(welt.sitzung)).content.decode()
    assert "Die Übertragung hat noch nicht begonnen." in html
    assert "Noch keine Einträge." in html


def test_vergangene_sitzung_ohne_uebertragung_nicht_uebertragen(client: Client, welt: Welt) -> None:
    """Ohne Übertragungsdatensatz und lange nach dem Beginn: „nicht übertragen“, keine Abfrage alle 15 s mehr."""
    welt.sitzung.start = timezone.now() - timedelta(days=90)
    welt.sitzung.save()
    html = client.get(_url(welt.sitzung)).content.decode()
    assert "Diese Sitzung wurde nicht übertragen." in html
    assert "hx-trigger" not in html
    welt.sitzung.start = timezone.now() - timedelta(hours=2)
    welt.sitzung.save()
    html = client.get(_url(welt.sitzung)).content.decode()
    assert "Die Übertragung hat noch nicht begonnen." in html, "kurz nach dem Beginn kann sie noch kommen"
    assert "hx-trigger" in html


def test_kopf_nennt_das_gremium_nur_einmal(client: Client, welt: Welt) -> None:
    """Die Überschrift ist meist schon der Gremiumsname; die Zeile darunter wiederholt ihn dann nicht."""

    def kopf() -> str:
        html = client.get(_url(welt.sitzung)).content.decode()
        treffer = re.search(r'<header class="mb-6.*?</header>', html, re.DOTALL)
        assert treffer is not None
        return treffer.group(0)

    assert re.findall(r">\s*Rat\s*<", kopf()) == [">Rat<"], "nur die Überschrift"
    zweites = OParlOrganization.objects.create(external_id=kennung("organizations"), body=welt.body, name="Ausschuss")
    welt.sitzung.organizations.add(zweites)
    assert "<span>Rat</span>" in kopf(), "gemeinsame Sitzung: die Zeile nennt das Gremium der Übertragung"


def test_ohne_quelle_404(client: Client, welt: Welt) -> None:
    andere = OParlOrganization.objects.create(external_id=kennung("organizations"), body=welt.body, name="Ausschuss")
    sitzung = OParlMeeting.objects.create(external_id=kennung("meetings"), body=welt.body, start=timezone.now())
    sitzung.organizations.set([andere])
    assert client.get(_url(sitzung)).status_code == 404


def test_inaktive_quelle_ohne_uebertragung_404(client: Client, welt: Welt, settings: Any) -> None:
    welt.quelle.active = False
    welt.quelle.save()
    assert client.get(_url(welt.sitzung)).status_code == 404
    settings.LIVE_UEBERTRAGUNG_AKTIV = False
    welt.quelle.active = True
    welt.quelle.save()
    assert client.get(_url(welt.sitzung)).status_code == 404, "Schalter aus: keine Seite ohne Übertragung"


def test_sitzungsseite_verlinkt_die_live_seite_nicht(client: Client, welt: Welt, laufend: Broadcast) -> None:
    html = client.get(reverse("insight_core:insight:meeting_detail", args=[welt.sitzung.pk])).content.decode()
    assert _url(welt.sitzung) not in html


def test_wortmeldung_fuer_abonnenten(welt: Welt, laufend: Broadcast) -> None:
    daten = selectors.wortmeldung(laufend.current_speech_id)  # type: ignore[arg-type]
    assert daten is not None
    assert (daten.person_id, daten.body_id, daten.organization_id) == (welt.muster.pk, welt.body.pk, welt.rat.pk)
    assert (daten.fraktion_gelesen, daten.zuordnung, daten.lesungen) == ("Fraktion A", "eindeutig", 3)
    assert daten.eindeutig
    unsicher = BroadcastSpeech.objects.get(name_read="Gisela Gast")
    gast = selectors.wortmeldung(unsicher.pk)
    assert gast is not None and not gast.eindeutig and gast.funktion_gelesen == "Bürgermeisterin"
    assert selectors.wortmeldung("00000000-0000-0000-0000-000000000000") is None
