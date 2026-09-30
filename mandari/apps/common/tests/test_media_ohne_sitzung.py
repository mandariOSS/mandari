# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Medien brauchen weder Sitzung noch Datenbank (Issue #667).

Beim ersten Aufruf von „Frage stellen“ lädt der Browser Dutzende Personenfotos zugleich. Jede dieser
Anfragen lief durch die Sitzungs-Middleware, die wegen ``SESSION_SAVE_EVERY_REQUEST`` die Sitzung
in die Datenbank zurückschrieb – eine Verbindung aus dem Pool je Bild, bei Angemeldeten zusätzlich
das Laden des Kontos. Über ``max_size`` plus ``max_waiting`` hinaus scheiterte das sofort mit
``TooManyRequests``; Django meldet einen Fehler beim Zurückschreiben irreführend als „The request's
session was deleted before the request completed“ (``SessionInterrupted``, 400).

Die Tests lassen jede Datenbankabfrage scheitern wie bei erschöpftem Pool und nutzen die
Sitzungsablage der Produktion (``cached_db``): Die Sitzung selbst kommt dort aus dem Cache.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.db import OperationalError, connection
from django.test import Client

from apps.common.models import SiteSettings
from apps.common.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

FOTO = "persons/photos/0f46a4c9-e428-5cad-8f24-cc27645e69ac.jpg"
INHALT = b"\xff\xd8\xff\xe0-foto"


@pytest.fixture
def media(tmp_path: Path, settings: Any) -> Path:
    settings.MEDIA_ROOT = tmp_path
    for relativ in (FOTO, "sonstiges/notiz.txt", "bodies/dokumente/vorlage.pdf"):
        ziel = tmp_path / relativ
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(INHALT)
    return tmp_path


@pytest.fixture(autouse=True)
def _sitzungen_wie_in_produktion(settings: Any) -> None:
    settings.SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"


class PoolErschoepft:
    """Lässt jede Abfrage scheitern wie ein Pool, vor dem schon ``max_waiting`` Anfragen warten."""

    def __init__(self) -> None:
        self.abfragen: list[str] = []

    def __call__(self, execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any) -> Any:
        self.abfragen.append(sql)
        raise OperationalError("the pool 'pool-1' has already 20 requests waiting")


def _besucher_mit_sitzung(client: Client) -> None:
    """Eine Sitzung wie nach der Wahl einer Kommune (``/insight/kommune/<id>/``)."""
    sitzung = client.session
    sitzung["active_body_id"] = "3ae1b038-cf57-5305-a49b-83108c0d6b96"
    sitzung.save()


def _geliefert(antwort: Any) -> bool:
    return bool(antwort.status_code == 200 and b"".join(antwort.streaming_content) == INHALT)


def test_foto_ohne_datenbank_trotz_sitzung(media: Path, client: Client, settings: Any) -> None:
    _besucher_mit_sitzung(client)
    pool = PoolErschoepft()

    with connection.execute_wrapper(pool):
        antwort = client.get(f"/media/{FOTO}")

    assert antwort.status_code == 200, antwort.status_code
    assert _geliefert(antwort)
    assert pool.abfragen == []
    # Weder Sitzung geschrieben noch Cookie erneuert: öffentlich cachebar, für alle gleich
    assert settings.SESSION_COOKIE_NAME not in antwort.cookies
    assert "Cookie" not in antwort.get("Vary", "")
    assert antwort["Cache-Control"] == "public, max-age=3600"
    assert antwort["X-Content-Type-Options"] == "nosniff"
    # Die Metriken zählen die Auslieferung weiter unter „media“ statt unter „unresolved“
    treffer = antwort.wsgi_request.resolver_match
    assert treffer is not None
    assert treffer.url_name == "media"


def test_foto_ohne_datenbank_auch_angemeldet(media: Path, client: Client) -> None:
    client.force_login(cast(Any, UserFactory)())
    pool = PoolErschoepft()

    with connection.execute_wrapper(pool):
        antwort = client.get(f"/media/{FOTO}")

    assert _geliefert(antwort)
    assert pool.abfragen == []


def test_anmeldepflichtige_medien_pruefen_weiter_die_anmeldung(media: Path, client: Client) -> None:
    _besucher_mit_sitzung(client)
    assert client.get("/media/sonstiges/notiz.txt").status_code == 404

    client.force_login(cast(Any, UserFactory)())
    antwort = client.get("/media/sonstiges/notiz.txt")
    assert _geliefert(antwort)
    assert antwort["Cache-Control"] == "private, no-store"


def test_geschuetzte_ablage_unter_oeffentlichem_praefix_bleibt_geschuetzt(
    media: Path, client: Client, settings: Any
) -> None:
    """Die geschützten Präfixe gelten vor den öffentlichen – auch auf dem kurzen Weg."""
    settings.OPARL_FILES_ROOT = str(media / "bodies" / "dokumente")
    antwort = client.get("/media/bodies/dokumente/vorlage.pdf")
    assert antwort.status_code == 404
    assert not _geliefert(antwort)


def test_fehlendes_foto_ergibt_404(media: Path, client: Client) -> None:
    assert client.get("/media/persons/photos/gibt-es-nicht.jpg").status_code == 404


def test_wartungsmodus_sperrt_auch_oeffentliche_medien(media: Path, client: Client, settings: Any) -> None:
    settings.MAINTENANCE_MODE_ENFORCEMENT = True
    cache.delete(SiteSettings.CACHE_KEY)
    einstellungen = SiteSettings.get_settings()
    einstellungen.maintenance_mode = True
    einstellungen.save()
    try:
        assert client.get(f"/media/{FOTO}").status_code == 503
    finally:
        cache.delete(SiteSettings.CACHE_KEY)
