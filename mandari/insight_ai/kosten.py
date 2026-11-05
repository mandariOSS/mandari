# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kosten und Monatsbudget der Zusammenfassungen mit der Claude API.

Gerechnet wird aus den Token jeder Antwort (``usage``) mit den Listenpreisen von Anthropic (USD je Million Token,
Stand 10/2026, https://platform.claude.com/docs/en/about-claude/pricing). Message Batches kosten die Hälfte. Ein
unbekanntes Modell wird mit dem höchsten Listenpreis gerechnet, damit das Budget eher zu früh als zu spät greift.

Das Monatsbudget (``AISettings.summary_monthly_budget_usd``) gilt je Kalendermonat in ``TIME_ZONE``. Verbraucht ist,
was ``KiVerbrauch`` für den Monat verbucht, zuzüglich der Schätzung offener Batches (reserviert, bis ihre Ergebnisse
übernommen sind). Die Grenze ist eine eigene Bremse; das Ausgabenlimit in der Konsole von Anthropic bleibt davon
unberührt und greift zusätzlich.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from django.db.models import Sum
from django.utils import timezone

if TYPE_CHECKING:
    from apps.common.ki_anbieter import ClaudeEndpunkt

#: Ab dieser Länge des Prompts (Eingabe einschließlich Cache, Token) gilt bei Claude Haiku 5.5 der höhere Preis
GRENZE_LANGER_PROMPT: Final = 100_000

#: USD je Million Token: Eingabe und Ausgabe bis ``GRENZE_LANGER_PROMPT``, Eingabe und Ausgabe darüber
PREISE: Final[dict[str, tuple[Decimal, Decimal, Decimal, Decimal]]] = {
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50"), Decimal("0.50"), Decimal("2.50")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5"), Decimal("1"), Decimal("5")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10"), Decimal("2"), Decimal("10")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10"), Decimal("2"), Decimal("10")),
    "claude-opus-5-5": (Decimal("4"), Decimal("20"), Decimal("4"), Decimal("20")),
}
#: Unbekanntes Modell: höchster Listenpreis (Claude Fable)
PREIS_UNBEKANNT: Final = (Decimal("10"), Decimal("50"), Decimal("10"), Decimal("50"))
#: Message Batches: halber Preis auf alle Token
BATCH_FAKTOR: Final = Decimal("0.5")
#: Gelesener Cache kostet ein Zehntel, geschriebener (5 Minuten) das 1,25-Fache der Eingabe
CACHE_LESEN_FAKTOR: Final = Decimal("0.1")
CACHE_SCHREIBEN_FAKTOR: Final = Decimal("1.25")
#: Schätzung vor dem Einreichen: höchstens so viele Zeichen je Token. Bewusst niedrig (eher zu viele Token): Zahlen,
#: Tabellen und OCR-Rauschen brauchen mehr Token als Fließtext, und ab ``GRENZE_LANGER_PROMPT`` rechnet auch die
#: Schätzung mit dem höheren Preis
ZEICHEN_JE_TOKEN: Final = 2
#: Zuschlag für Rollen und Formatierung der Anfrage (Token)
GRUNDLAST_TOKEN: Final = 200
_MILLION: Final = Decimal(1_000_000)


def preise(modell: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    return PREISE.get((modell or "").strip(), PREIS_UNBEKANNT)


def kosten(
    modell: str,
    *,
    eingabe: int,
    ausgabe: int,
    cache_lesen: int = 0,
    cache_schreiben: int = 0,
    batch: bool = False,
) -> Decimal:
    """Kosten eines Aufrufs in USD aus seinen Token."""
    kurz_ein, kurz_aus, lang_ein, lang_aus = preise(modell)
    prompt = eingabe + cache_lesen + cache_schreiben
    preis_ein, preis_aus = (lang_ein, lang_aus) if prompt > GRENZE_LANGER_PROMPT else (kurz_ein, kurz_aus)
    betrag = (
        Decimal(eingabe) * preis_ein
        + Decimal(cache_lesen) * preis_ein * CACHE_LESEN_FAKTOR
        + Decimal(cache_schreiben) * preis_ein * CACHE_SCHREIBEN_FAKTOR
        + Decimal(ausgabe) * preis_aus
    ) / _MILLION
    return betrag * BATCH_FAKTOR if batch else betrag


def schaetzung(modell: str, *, zeichen: int, max_ausgabe: int, batch: bool) -> Decimal:
    """Obergrenze der Kosten einer Anfrage vor dem Senden: Eingabe aus der Textlänge, Ausgabe voll ausgeschöpft."""
    eingabe = max(0, zeichen) // ZEICHEN_JE_TOKEN + GRUNDLAST_TOKEN
    return kosten(modell, eingabe=eingabe, ausgabe=max(0, max_ausgabe), batch=batch)


def _token(usage: Any, name: str) -> int:
    try:
        return max(0, int(getattr(usage, name, 0) or 0))
    except (TypeError, ValueError):
        return 0


def kosten_aus_usage(modell: str, usage: Any, *, batch: bool) -> Decimal:
    """Kosten aus dem ``usage``-Objekt einer Antwort der Claude API."""
    return kosten(
        modell,
        eingabe=_token(usage, "input_tokens"),
        ausgabe=_token(usage, "output_tokens"),
        cache_lesen=_token(usage, "cache_read_input_tokens"),
        cache_schreiben=_token(usage, "cache_creation_input_tokens"),
        batch=batch,
    )


def verbuchen(*, art: str, modell: str, usage: Any, vorgang_id: Any = None) -> Decimal:
    """Verbrauch einer Antwort festhalten; Rückgabe: ihre Kosten in USD."""
    from .models import KiVerbrauch

    betrag = kosten_aus_usage(modell, usage, batch=art == KiVerbrauch.Art.BATCH)
    KiVerbrauch.objects.create(
        art=art,
        modell=modell,
        vorgang_id=vorgang_id or None,
        eingabe_tokens=_token(usage, "input_tokens"),
        ausgabe_tokens=_token(usage, "output_tokens"),
        cache_lesen_tokens=_token(usage, "cache_read_input_tokens"),
        cache_schreiben_tokens=_token(usage, "cache_creation_input_tokens"),
        kosten_usd=betrag,
    )
    return betrag


def monatsbeginn(jetzt: datetime | None = None) -> datetime:
    """Beginn des laufenden Kalendermonats in ``TIME_ZONE``."""
    lokal = timezone.localtime(jetzt or timezone.now())
    return lokal.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def monatsverbrauch(jetzt: datetime | None = None) -> Decimal:
    """Verbuchte Kosten des laufenden Monats in USD."""
    from .models import KiVerbrauch

    summe = KiVerbrauch.objects.filter(zeitpunkt__gte=monatsbeginn(jetzt)).aggregate(summe=Sum("kosten_usd"))["summe"]
    return Decimal(summe or 0)


def reserviert() -> Decimal:
    """Geschätzte Kosten der Batches, deren Ergebnisse noch nicht übernommen sind."""
    from .models import ZusammenfassungsBatch

    summe = ZusammenfassungsBatch.objects.filter(status=ZusammenfassungsBatch.Status.EINGEREICHT).aggregate(
        summe=Sum("geschaetzt_usd")
    )["summe"]
    return Decimal(summe or 0)


def budget_rest(endpunkt: ClaudeEndpunkt, jetzt: datetime | None = None) -> Decimal:
    """Was vom Monatsbudget bleibt (USD, kann negativ sein)."""
    return Decimal(endpunkt.monatsbudget_usd) - monatsverbrauch(jetzt) - reserviert()
