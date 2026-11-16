# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Grenze je Person und Token-Kontingent der Organisation bei KI-Aufrufen, die sich überlappen.

Beides wird vor dem Anbieteraufruf belegt (``AIRateLimiter.reserve``, ``MotionAIService._reserve_org_tokens``) und
danach auf den echten Verbrauch berichtigt. Die Tests stellen die Überlappung ohne Threads nach: Der Ersatz für den
Anbieter stellt, während der erste Aufruf „läuft“, selbst weitere Aufrufe. HTTP läuft nur über
``httpx.MockTransport``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from django.core.cache import cache

from apps.common.models import AISettings
from apps.tenants.models import Organization
from apps.work.motions.ai_security import AIRateLimiter
from apps.work.motions.models import OrganizationAITokenUsage
from apps.work.motions.services import AIResponse, MotionAIService

pytestmark = pytest.mark.django_db

STACKIT = "https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1"
NACHRICHTEN = [{"role": "user", "content": "Bitte kürzen"}]
#: Schätzung für ``NACHRICHTEN`` mit ``max_tokens=100``: 12 Zeichen durch vier, dazu die Antwortlänge
GESCHAETZT = 103


@pytest.fixture(autouse=True)
def _plattform(settings: Any) -> None:
    settings.KI_ERLAUBTE_HOSTS = [urlsplit(STACKIT).hostname]
    cache.delete(AISettings.CACHE_KEY)
    ki = AISettings.get_settings()
    ki.provider = "stackit"
    ki.enabled = True
    ki.model_name = "modell-work"
    ki.set_api_key("work-testschluessel-geheim-0123456789")
    ki.save()


@pytest.fixture
def mitglied(org: Organization, make_member: Any) -> Any:
    org.ai_enabled = True
    org.save(update_fields=["ai_enabled"])
    return make_member(org, ["motions.view"], email="ki-grenzen@example.org")


def _anbieter(
    waehrenddessen: Callable[[], None] | None = None, *, status: int = 200, tokens: int = 42
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Ersatz für den Anbieter; ``waehrenddessen`` läuft einmal, solange der erste Aufruf noch nicht gebucht ist."""
    anfragen: list[httpx.Request] = []

    def antwort(request: httpx.Request) -> httpx.Response:
        anfragen.append(request)
        if len(anfragen) == 1 and waehrenddessen is not None:
            waehrenddessen()
        if status != 200:
            return httpx.Response(status, json={"error": "weg"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Kurzer Titel"}}], "usage": {"total_tokens": tokens}}
        )

    return httpx.MockTransport(antwort), anfragen


def _verbrauch(org: Organization, period: str = OrganizationAITokenUsage.PERIOD_DAY) -> tuple[int, int]:
    zeile = OrganizationAITokenUsage.objects.filter(organization=org, period_type=period).first()
    return (int(zeile.tokens_used), int(zeile.requests_count)) if zeile else (0, 0)


def test_grenze_je_person_gilt_fuer_ueberlappende_aufrufe(org: Organization, mitglied: Any) -> None:
    """Zwölf Aufrufe, von denen elf während des ersten eintreffen: Höchstens zehn je Minute gehen zum Anbieter."""
    ueberlappend: list[AIResponse] = []
    dienst: MotionAIService

    def weitere() -> None:
        ueberlappend.extend(dienst.generate_title(f"Antrag Nummer {n} zum Radweg") for n in range(11))

    transport, anfragen = _anbieter(weitere)
    dienst = MotionAIService(organization=org, user_id=mitglied.user_id, transport=transport)

    erste = dienst.generate_title("Antrag zum Radweg an der Hauptstraße")

    assert erste.success
    assert len(anfragen) == AIRateLimiter.LIMITS["per_minute"] == 10
    abgewiesen = [antwort for antwort in ueberlappend if not antwort.success]
    assert [antwort.error for antwort in abgewiesen] == ["Zu viele Anfragen. Bitte warten Sie eine Minute."] * 2
    # Gezählt sind genau die zehn Aufrufe, die liefen; die abgewiesenen zählen wie bisher nicht
    assert AIRateLimiter.get_remaining(mitglied.user_id, "per_minute") == 0
    assert AIRateLimiter.get_remaining(mitglied.user_id, "per_hour") == 90
    assert _verbrauch(org) == (10 * 42, 10)


def test_kontingent_der_organisation_gilt_fuer_ueberlappende_aufrufe(org: Organization, mitglied: Any) -> None:
    """Das Tageskontingent reicht für zwei geschätzte Aufrufe: Von vier überlappenden gehen zwei zum Anbieter."""
    org.ai_token_limit_daily = 2 * GESCHAETZT + 10
    org.save(update_fields=["ai_token_limit_daily"])
    ueberlappend: list[AIResponse] = []
    dienst: MotionAIService

    def weitere() -> None:
        ueberlappend.extend(dienst._call_api(NACHRICHTEN, max_tokens=100) for _ in range(3))

    transport, anfragen = _anbieter(weitere)
    dienst = MotionAIService(organization=org, user_id=mitglied.user_id, transport=transport)

    erste = dienst._call_api(NACHRICHTEN, max_tokens=100)

    assert erste.success and len(anfragen) == 2
    assert [antwort.error for antwort in ueberlappend if not antwort.success] == [
        "Tageslimit für KI-Tokens erreicht."
    ] * 2
    # Gebucht bleibt der echte Verbrauch der beiden Aufrufe, in allen drei Zeiträumen; abgewiesene Aufrufe
    # belegen weder Kontingent noch die Grenze je Person
    for period in (
        OrganizationAITokenUsage.PERIOD_DAY,
        OrganizationAITokenUsage.PERIOD_WEEK,
        OrganizationAITokenUsage.PERIOD_MONTH,
    ):
        assert _verbrauch(org, period) == (2 * 42, 2)
    assert AIRateLimiter.get_remaining(mitglied.user_id, "per_minute") == 8


def test_gescheiterter_aufruf_gibt_die_belegung_zurueck(org: Organization, mitglied: Any) -> None:
    transport, anfragen = _anbieter(status=500)
    dienst = MotionAIService(organization=org, user_id=mitglied.user_id, transport=transport)

    antwort = dienst._call_api(NACHRICHTEN, max_tokens=100)

    assert not antwort.success and len(anfragen) == 1
    assert _verbrauch(org) == (0, 0)
    assert AIRateLimiter.get_remaining(mitglied.user_id, "per_minute") == 10


def test_ohne_endpunkt_zaehlt_kein_aufruf(org: Organization, mitglied: Any) -> None:
    ki = AISettings.get_settings()
    ki.enabled = False
    ki.save()
    transport, anfragen = _anbieter()
    dienst = MotionAIService(organization=org, user_id=mitglied.user_id, transport=transport)

    antwort = dienst._call_api(NACHRICHTEN, max_tokens=100)

    assert antwort.error == MotionAIService.NICHT_EINGERICHTET and anfragen == []
    assert AIRateLimiter.get_remaining(mitglied.user_id, "per_minute") == 10 and _verbrauch(org) == (0, 0)


def test_mehrverbrauch_bleibt_gebucht_auch_wenn_die_antwort_verworfen_wird(org: Organization, mitglied: Any) -> None:
    """Verbraucht ein Aufruf mehr als geschätzt und überschreitet damit das Kontingent, zählt er trotzdem."""
    org.ai_token_limit_daily = GESCHAETZT + 10
    org.save(update_fields=["ai_token_limit_daily"])
    transport, anfragen = _anbieter(tokens=500)
    dienst = MotionAIService(organization=org, user_id=mitglied.user_id, transport=transport)

    erste = dienst._call_api(NACHRICHTEN, max_tokens=100)
    zweite = dienst._call_api(NACHRICHTEN, max_tokens=100)

    assert erste.error == zweite.error == "Tageslimit für KI-Tokens erreicht."
    # Der zweite Aufruf geht nicht mehr zum Anbieter: Der Verbrauch des ersten steht im Kontingent
    assert len(anfragen) == 1 and _verbrauch(org) == (500, 1)


def test_reserve_zaehlt_und_prueft_in_einem_schritt() -> None:
    person = 987654321
    for period in AIRateLimiter.LIMITS:
        cache.delete(AIRateLimiter._get_key(person, period))

    ergebnisse = [AIRateLimiter.reserve(person) for _ in range(12)]

    assert [erlaubt for erlaubt, _ in ergebnisse] == [True] * 10 + [False] * 2
    assert ergebnisse[-1][1] == "Zu viele Anfragen. Bitte warten Sie eine Minute."
    # Abgewiesene Aufrufe nehmen ihre Zählung in allen Zeiträumen zurück
    assert AIRateLimiter.get_remaining(person, "per_minute") == 0
    assert AIRateLimiter.get_remaining(person, "per_hour") == 90
    assert AIRateLimiter.get_remaining(person, "per_day") == 490
    AIRateLimiter.release(person)
    assert AIRateLimiter.get_remaining(person, "per_minute") == 1
    assert AIRateLimiter.reserve(person) == (True, "")
