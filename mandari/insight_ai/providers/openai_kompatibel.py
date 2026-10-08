# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OpenAI-kompatibler Anbieter für das Bürgerportal (Issue #950).

Ersetzt die feste Nebius-Anbindung. Endpunkt, Modell, Ausweichmodell, Schlüssel und Antwortlänge kommen aus der
zentralen KI-Konfiguration (``apps.common.ki_anbieter.endpunkt_fuer_insight``); die Adresse ist dort gegen die
Positivliste ``KI_ERLAUBTE_HOSTS`` geprüft. Direkte HTTP-Anfragen statt SDK, damit Denktext von Modellen mit
Reasoning (``reasoning_content``/``reasoning``) nie als Antwort gilt.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from apps.common.ki_anbieter import LAENGENLIMIT, KiEndpunkt, ist_laengenlimit

from .base import AbstractAIProvider, ChatMessage, ChatResponse

logger = logging.getLogger(__name__)

#: HTTP-Status, nach denen einmal das Ausweichmodell versucht wird (Modell weg, überlastet, Zeitüberschreitung)
AUSWEICH_STATUS = frozenset({404, 408, 429})


class KiAnbieterError(ValueError):
    """Der KI-Anbieter lieferte keine Antwort. Die Meldung enthält nie Inhalt, Adresse oder Schlüssel."""


class _AusweichbarError(KiAnbieterError):
    """Fehler, nach dem das Ausweichmodell versucht werden darf (404, 408, 429, 5xx, Zeitüberschreitung)."""


class OpenAIKompatiblerProvider(AbstractAIProvider):
    """Chat-Completions an genau einen geprüften Endpunkt."""

    TIMEOUT = httpx.Timeout(300.0, connect=30.0)  # Modelle mit Reasoning brauchen Minuten

    def __init__(self, endpunkt: KiEndpunkt, *, transport: httpx.BaseTransport | None = None) -> None:
        self.endpunkt = endpunkt
        self._model = endpunkt.modell
        # Nur für Tests (httpx.MockTransport); im Betrieb der Standard-Transport von httpx
        self._transport = transport

    def __repr__(self) -> str:
        return f"OpenAIKompatiblerProvider({self.endpunkt!r})"

    def is_available(self) -> bool:
        return bool(self.endpunkt.api_key and self.endpunkt.base_url and self.endpunkt.modell)

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def max_output_tokens(self) -> int:
        return self.endpunkt.max_output_tokens or super().max_output_tokens

    def chat_completion(
        self,
        messages: list[ChatMessage],
        max_tokens: int = 1500,
        temperature: float = 0.3,
    ) -> ChatResponse:
        """
        Antwort aus ``message.content``. Nach 404, 408, 429, 5xx oder Zeitüberschreitung einmal das Ausweichmodell
        am selben Endpunkt mit demselben Schlüssel; sonst ``KiAnbieterError``.
        """
        if self.endpunkt.max_output_tokens > 0:
            max_tokens = min(max_tokens, self.endpunkt.max_output_tokens)
        nachrichten = [{"role": msg.role, "content": msg.content} for msg in messages]
        try:
            return self._anfrage(self.endpunkt.modell, nachrichten, max_tokens, temperature)
        except _AusweichbarError as fehler:
            ausweich = self.endpunkt.ausweichmodell
            if not ausweich or ausweich == self.endpunkt.modell:
                raise KiAnbieterError(str(fehler)) from fehler
            logger.warning(
                "KI-Aufruf: Ausweichmodell nach %s (anbieter=%s host=%s)", fehler, self.endpunkt.anbieter, self.host
            )
            try:
                return self._anfrage(ausweich, nachrichten, max_tokens, temperature)
            except _AusweichbarError as zweiter:
                raise KiAnbieterError(str(zweiter)) from zweiter

    @property
    def host(self) -> str:
        return self.endpunkt.host

    def _anfrage(
        self, modell: str, nachrichten: list[dict[str, str]], max_tokens: int, temperature: float
    ) -> ChatResponse:
        payload = {
            "model": modell,
            "messages": nachrichten,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.endpunkt.api_key}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=self.TIMEOUT, transport=self._transport, follow_redirects=False) as client:
                response = client.post(self.endpunkt.chat_url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise _AusweichbarError("Zeitüberschreitung") from exc
        except httpx.HTTPError as exc:
            raise KiAnbieterError(f"KI-Anbieter nicht erreichbar ({type(exc).__name__})") from exc

        status = response.status_code
        if status in AUSWEICH_STATUS or status >= 500:
            raise _AusweichbarError(f"HTTP {status}")
        if status != 200:
            # Ohne Antworttext: Er kann Teile der Anfrage enthalten
            if ist_laengenlimit(status, response.text):
                logger.warning(
                    "KI-Aufruf abgelehnt (HTTP %d): %s (anbieter=%s host=%s modell=%s max_tokens=%d)",
                    status,
                    LAENGENLIMIT,
                    self.endpunkt.anbieter,
                    self.host,
                    modell,
                    max_tokens,
                )
                raise KiAnbieterError(f"KI-Anbieter meldet HTTP {status}: Längenlimit des Modells überschritten")
            raise KiAnbieterError(f"KI-Anbieter meldet HTTP {status}")
        try:
            daten: Any = response.json()
            message = (daten.get("choices") or [{}])[0].get("message") or {}
        except (ValueError, AttributeError, IndexError, TypeError) as exc:
            raise KiAnbieterError("Antwort des KI-Anbieters nicht lesbar") from exc

        inhalt = message.get("content")
        content = inhalt if isinstance(inhalt, str) else ""
        denktext = message.get("reasoning_content") or message.get("reasoning") or ""
        if denktext:
            logger.debug("KI-Aufruf: Denktext mit %d Zeichen nicht verwendet", len(str(denktext)))
        if not content:
            logger.warning("KI-Aufruf ohne Antworttext (modell=%s, Denktext %d Zeichen)", modell, len(str(denktext)))

        usage = daten.get("usage") or {}
        eingabe = int(usage.get("prompt_tokens") or 0)
        ausgabe = int(usage.get("completion_tokens") or 0)
        gesamt = int(usage.get("total_tokens") or 0) or eingabe + ausgabe
        self._model = modell
        logger.info(
            "KI-Aufruf: anbieter=%s host=%s modell=%s eingabe=%d ausgabe=%d",
            self.endpunkt.anbieter,
            self.host,
            modell,
            eingabe,
            ausgabe,
        )
        return ChatResponse(
            content=content, model=modell, input_tokens=eingabe, output_tokens=ausgabe, total_tokens=gesamt
        )
