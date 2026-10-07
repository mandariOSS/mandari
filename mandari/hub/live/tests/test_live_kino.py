# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Live-Seite und Kinomodus (Issue #915): Knopf und Player nur bei laufender Übertragung (auch mit ``?player=1``),
Link auf die offizielle Übertragung in jedem Zustand, Player-Bereich in der Aktualisierung nur beim Statuswechsel
(auch beim Wiederanlaufen nach dem Ende), Kinoansicht ohne Rahmen von Insight mit Links in neuem Tab.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from hub.live.models import Broadcast, BroadcastStatus
from hub.live.tests.conftest import EMBED_ID, Welt
from insight_core.models import OParlMeeting

pytestmark = pytest.mark.django_db

EINBETTUNG = f"https://playout.3qsdn.com/embed/{EMBED_ID}"
QUELLE = "https://www.musterstadt.example/live"

Adresse = Callable[[OParlMeeting], str]


def _live(meeting: OParlMeeting) -> str:
    return reverse("insight_core:insight:meeting_live", args=[meeting.pk])


def _kino(meeting: OParlMeeting) -> str:
    return reverse("insight_core:insight:meeting_live_kino", args=[meeting.pk])


def _status_setzen(welt: Welt, status: str) -> None:
    """Sitzung in den Zustand bringen; ``geplant`` und ``nicht_uebertragen`` ohne Übertragungsdatensatz."""
    if status == "nicht_uebertragen":
        welt.sitzung.start = timezone.now() - timedelta(days=90)
        welt.sitzung.save()
    elif status == BroadcastStatus.BEENDET:
        Broadcast.objects.create(
            source=welt.quelle,
            meeting=welt.sitzung,
            status=BroadcastStatus.BEENDET,
            started_at=welt.jetzt - timedelta(hours=2),
            ended_at=welt.jetzt - timedelta(minutes=10),
        )


NICHT_LIVE = ["geplant", "beendet", "nicht_uebertragen"]


@pytest.mark.parametrize("status", NICHT_LIVE)
@pytest.mark.parametrize("url", [_live, _kino])
def test_kein_knopf_und_kein_player_ohne_laufende_uebertragung(
    client: Client, welt: Welt, status: str, url: Adresse
) -> None:
    _status_setzen(welt, status)
    for parameter in ({}, {"player": "1"}):
        antwort = client.get(url(welt.sitzung), parameter)
        assert antwort.status_code == 200
        html = antwort.content.decode()
        assert EINBETTUNG not in html and 'title="Übertragung:' not in html, "kein Player, auch nicht mit ?player=1"
        assert "Übertragung laden" not in html and "IP-Adresse" not in html
        assert _kino(welt.sitzung) not in html, "kein Kinomodus-Knopf"
        assert 'data-testid="live-status"' in html


def test_live_seite_knopf_und_kinomodus_bei_laufender_uebertragung(
    client: Client, welt: Welt, laufend: Broadcast
) -> None:
    html = client.get(_live(welt.sitzung)).content.decode()
    assert "Übertragung laden" in html and EINBETTUNG not in html
    assert f'href="{_kino(welt.sitzung)}?player=1"' in html, "Kinomodus zählt als Zustimmung"

    html = client.get(_live(welt.sitzung), {"player": "1"}).content.decode()
    assert f'<iframe src="{EINBETTUNG}"' in html
    kino = re.search(r'<a href="' + re.escape(_kino(welt.sitzung)) + r'\?player=1".*?</a>', html, re.DOTALL)
    assert kino is not None and "Kinomodus" in kino.group(0)
    assert "teil=stand&amp;player=1&amp;v=" in html, "die Aktualisierung behält die Zustimmung"


@pytest.mark.parametrize("status", ["live", *NICHT_LIVE])
@pytest.mark.parametrize("url", [_live, _kino])
def test_offizielle_quelle_in_jedem_zustand(client: Client, welt: Welt, status: str, url: Adresse) -> None:
    if status == "live":
        Broadcast.objects.create(
            source=welt.quelle, meeting=welt.sitzung, status=BroadcastStatus.LIVE, started_at=welt.jetzt
        )
    else:
        _status_setzen(welt, status)
    html = client.get(url(welt.sitzung)).content.decode()
    link = re.search(r'<a href="' + re.escape(QUELLE) + r'"[^>]*>(.*?)</a>', html, re.DOTALL)
    assert link is not None
    assert 'target="_blank"' in link.group(0) and 'rel="noopener noreferrer"' in link.group(0)
    assert re.sub(r"\s+", " ", link.group(1)).strip().startswith("Offizielle Übertragung: Musterstadt")


def test_ohne_seite_der_kommune_kein_link(client: Client, welt: Welt) -> None:
    welt.quelle.page_url = ""
    welt.quelle.save()
    for url in (_live, _kino):
        assert "Offizielle Übertragung" not in client.get(url(welt.sitzung)).content.decode()


def _version(html: str) -> str:
    treffer = re.search(r"v=([^\"&]+)\"", html)
    assert treffer is not None
    return treffer.group(1)


@pytest.mark.parametrize(("url", "bereich"), [(_live, "live-player"), (_kino, "kino-bild")])
def test_aktualisierung_tauscht_player_bereich_nur_beim_statuswechsel(
    client: Client, welt: Welt, url: Adresse, bereich: str
) -> None:
    # Seite offen, Übertragung noch nicht begonnen
    html = client.get(url(welt.sitzung), {"player": "1"}).content.decode()
    geplant = _version(html)
    assert geplant.startswith("geplant.")

    # Übertragung beginnt: der Player-Bereich kommt außer der Reihe mit, mit Zustimmung gleich der Player
    broadcast = Broadcast.objects.create(
        source=welt.quelle, meeting=welt.sitzung, status=BroadcastStatus.LIVE, started_at=welt.jetzt
    )
    teil = client.get(url(welt.sitzung), {"teil": "stand", "player": "1", "v": geplant}).content.decode()
    assert f'id="{bereich}"' in teil and 'hx-swap-oob="true"' in teil
    assert f'<iframe src="{EINBETTUNG}"' in teil
    ohne_zustimmung = client.get(url(welt.sitzung), {"teil": "stand", "v": geplant}).content.decode()
    assert "Übertragung laden" in ohne_zustimmung and EINBETTUNG not in ohne_zustimmung

    # Neuer Stand bei gleichem Status: Leiste neu, der laufende Player bleibt unberührt
    live = _version(teil)
    broadcast.speeches.create(name_read="Max Beispiel", started_at=welt.jetzt + timedelta(minutes=1))
    teil = client.get(url(welt.sitzung), {"teil": "stand", "player": "1", "v": live}).content.decode()
    assert "<html" not in teil and 'data-testid="live-status"' in teil
    assert f'id="{bereich}"' not in teil and "<iframe" not in teil

    # Übertragung endet: der Player verschwindet ohne Neuladen der Seite
    broadcast.status = BroadcastStatus.BEENDET
    broadcast.ended_at = welt.jetzt + timedelta(hours=1)
    broadcast.save()
    teil = client.get(url(welt.sitzung), {"teil": "stand", "player": "1", "v": _version(teil)}).content.decode()
    assert f'id="{bereich}"' in teil and 'hx-swap-oob="true"' in teil
    assert "<iframe" not in teil and "Übertragung laden" not in teil
    assert "hx-trigger" in teil, "nach einer langen Pause kann die Übertragung wieder anlaufen"

    # Übertragung läuft nach der Pause wieder an: Knopf bzw. Player kommen ohne Neuladen der Seite zurück
    beendet = _version(teil)
    broadcast.status = BroadcastStatus.LIVE
    broadcast.save()
    teil = client.get(url(welt.sitzung), {"teil": "stand", "player": "1", "v": beendet}).content.decode()
    assert f'id="{bereich}"' in teil and 'hx-swap-oob="true"' in teil
    assert f'<iframe src="{EINBETTUNG}"' in teil
    ohne_zustimmung = client.get(url(welt.sitzung), {"teil": "stand", "v": beendet}).content.decode()
    assert "Übertragung laden" in ohne_zustimmung and EINBETTUNG not in ohne_zustimmung


def test_kinoansicht(client: Client, welt: Welt, laufend: Broadcast) -> None:
    antwort = client.get(_kino(welt.sitzung))
    assert antwort.status_code == 200
    assert antwort["X-Robots-Tag"] == "noindex, nofollow" and antwort["Cache-Control"] == "no-cache"
    html = antwort.content.decode()
    assert 'content="noindex, nofollow"' in html
    # ohne Rahmen von Insight: keine Seitenleiste, Kopfzeile, Brotkrumen, Fußzeile oder Leiste unten
    for rahmen in ('id="insight-navigation"', 'aria-label="Brotkrumen"', "<footer", 'aria-label="Hauptbereiche"'):
        assert rahmen not in html
    # Zwei-Klick: ohne ?player=1 zuerst der Hinweis
    assert "IP-Adresse" in html and "Übertragung laden" in html and EINBETTUNG not in html
    assert "Läuft seit 16:15 Uhr" in html
    assert 'hx-trigger="every 15s"' in html and 'aria-live="polite"' in html
    assert 'data-action="fullscreen" data-target="#kino"' in html

    antwort = client.get(_kino(welt.sitzung), {"player": "1"})
    html = antwort.content.decode()
    assert f'<iframe src="{EINBETTUNG}"' in html
    assert re.search(r"frame-src [^;]*https://playout\.3qsdn\.com", antwort["Content-Security-Policy"])
    # Links öffnen in neuem Tab, damit das Video weiterläuft; der TOP führt zum Tagesordnungspunkt der Sitzung
    top = reverse("insight_core:insight:meeting_detail", args=[welt.sitzung.pk]) + f"#top-{welt.top5.pk}"
    assert re.search(r'<a href="' + re.escape(top) + r'"[^>]*>TOP 5 – Neubau einer Grundschule</a>', html)
    for ziel in (
        top,
        reverse("insight_core:insight:paper_detail", args=[welt.vorlage.pk]),
        reverse("insight_core:insight:person_detail", args=[welt.muster.pk]),
        QUELLE,
    ):
        link = re.search(r'<a href="' + re.escape(ziel) + r'"[^>]*>', html)
        assert link is not None, ziel
        assert 'target="_blank"' in link.group(0) and 'rel="noopener' in link.group(0)
    assert "TOP 5 – Neubau einer Grundschule" in html and "Erika Muster</a> (Fraktion A)" in html
    # Verlassen führt zurück zur Live-Seite, die Zustimmung bleibt
    assert f'<a href="{_live(welt.sitzung)}?player=1"' in html and "Kinomodus verlassen" in html


def test_kinoansicht_ohne_quelle_404(client: Client, welt: Welt) -> None:
    welt.quelle.delete()
    assert client.get(_kino(welt.sitzung)).status_code == 404
