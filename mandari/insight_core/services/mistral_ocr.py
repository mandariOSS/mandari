"""
Mistral OCR Service.

Verwendet die Mistral AI API für hochwertige OCR-Extraktion aus PDFs.
Fallback-Option zwischen pypdf (schnell, nur Text-PDFs) und Tesseract (lokal).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Optional

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


class MistralOCRError(Exception):
    """Basisklasse für Mistral OCR Fehler."""


class RateLimitError(MistralOCRError):
    """Rate Limit erreicht."""


class APINotConfiguredError(MistralOCRError):
    """API nicht konfiguriert."""


class OCRExtractionError(MistralOCRError):
    """Fehler bei der OCR-Extraktion."""


@dataclass
class RateLimiter:
    """Einfacher Rate Limiter für API-Anfragen."""

    requests_per_minute: int = 60
    _timestamps: list = None
    _lock: Lock = None

    def __post_init__(self):
        self._timestamps = []
        self._lock = Lock()

    def acquire(self) -> bool:
        """
        Versucht eine Rate-Limit-Slot zu bekommen.

        Returns:
            True wenn erlaubt, False wenn Rate Limit erreicht
        """
        with self._lock:
            now = time.time()
            # Entferne Timestamps älter als 1 Minute
            self._timestamps = [t for t in self._timestamps if now - t < 60]

            if len(self._timestamps) >= self.requests_per_minute:
                return False

            self._timestamps.append(now)
            return True

    def wait_time(self) -> float:
        """Berechnet wie lange gewartet werden muss bis nächste Anfrage möglich."""
        with self._lock:
            if not self._timestamps:
                return 0.0

            now = time.time()
            oldest = min(self._timestamps)
            wait = 60 - (now - oldest)
            return max(0.0, wait)


class MistralOCRService:
    """
    Service für Mistral AI basierte OCR-Extraktion.

    Nutzt die Mistral Document API (pixtral-12b-2409 oder später).
    """

    # Mistral API Endpoint
    BASE_URL = "https://api.mistral.ai/v1"

    # Model für OCR/Vision Tasks
    OCR_MODEL = "pixtral-12b-2409"

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialisiert den Mistral OCR Service.

        Args:
            api_key: Optional API Key, sonst aus settings.MISTRAL_API_KEY
        """
        self.api_key = api_key or getattr(settings, "MISTRAL_API_KEY", "")
        self.rate_limiter = RateLimiter(
            requests_per_minute=getattr(settings, "MISTRAL_OCR_RATE_LIMIT", 60)
        )
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def is_configured(self) -> bool:
        """Prüft ob der Service konfiguriert ist."""
        return bool(self.api_key)

    async def _get_client(self) -> httpx.AsyncClient:
        """Gibt den HTTP-Client zurück (lazy initialization)."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=120.0,  # OCR kann dauern
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def close(self):
        """Schließt den HTTP-Client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def is_available(self) -> bool:
        """
        Prüft ob der Service verfügbar ist.

        Returns:
            True wenn API erreichbar und konfiguriert
        """
        if not self.is_configured:
            return False

        try:
            client = await self._get_client()
            response = await client.get(f"{self.BASE_URL}/models")
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Mistral API health check fehlgeschlagen: {e}")
            return False

    async def extract_text(
        self,
        pdf_bytes: bytes,
        file_name: str = "document.pdf",
    ) -> str:
        """
        Extrahiert Text aus einer PDF-Datei mittels Mistral Vision.

        Args:
            pdf_bytes: PDF als Bytes
            file_name: Optionaler Dateiname für Logging

        Returns:
            Extrahierter Text

        Raises:
            APINotConfiguredError: Wenn API Key fehlt
            RateLimitError: Wenn Rate Limit erreicht
            OCRExtractionError: Bei Extraktionsfehlern
        """
        if not self.is_configured:
            raise APINotConfiguredError("MISTRAL_API_KEY nicht konfiguriert")

        # Rate Limiting prüfen
        if not self.rate_limiter.acquire():
            wait_time = self.rate_limiter.wait_time()
            raise RateLimitError(
                f"Rate Limit erreicht. Bitte {wait_time:.1f}s warten."
            )

        try:
            # PDF zu Base64 konvertieren
            pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")

            # API Request erstellen
            client = await self._get_client()

            # Verwende Chat Completions API mit Vision
            payload = {
                "model": self.OCR_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Extrahiere den vollständigen Text aus diesem PDF-Dokument. "
                                    "Gib nur den extrahierten Text zurück, ohne Kommentare oder Formatierung. "
                                    "Behalte Absätze und Strukturierung bei. "
                                    "Falls das Dokument auf Deutsch ist, behalte die deutsche Sprache bei."
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:application/pdf;base64,{pdf_base64}"
                                },
                            },
                        ],
                    }
                ],
                "max_tokens": 32000,  # Maximale Textlänge
            }

            response = await client.post(
                f"{self.BASE_URL}/chat/completions",
                json=payload,
            )

            if response.status_code == 429:
                raise RateLimitError("Mistral API Rate Limit erreicht")

            if response.status_code != 200:
                error_detail = response.text
                logger.error(f"Mistral OCR Fehler: {response.status_code} - {error_detail}")
                raise OCRExtractionError(
                    f"API Fehler {response.status_code}: {error_detail[:200]}"
                )

            result = response.json()
            text = result.get("choices", [{}])[0].get("message", {}).get("content", "")

            if not text.strip():
                logger.warning(f"Mistral OCR lieferte leeren Text für {file_name}")

            logger.info(f"Mistral OCR erfolgreich: {file_name} ({len(text)} Zeichen)")
            return text.strip()

        except (RateLimitError, APINotConfiguredError):
            raise
        except Exception as e:
            logger.exception(f"Mistral OCR Fehler für {file_name}: {e}")
            raise OCRExtractionError(f"Extraktion fehlgeschlagen: {e}") from e

    async def extract_text_sync(
        self,
        pdf_bytes: bytes,
        file_name: str = "document.pdf",
    ) -> str:
        """
        Synchrone Wrapper-Methode für extract_text.

        Für Verwendung in synchronem Code.
        """
        return await self.extract_text(pdf_bytes, file_name)


# Singleton-Instanz
_mistral_service: Optional[MistralOCRService] = None


def get_mistral_ocr_service() -> MistralOCRService:
    """Gibt die Singleton-Instanz des Mistral OCR Service zurück."""
    global _mistral_service
    if _mistral_service is None:
        _mistral_service = MistralOCRService()
    return _mistral_service


def extract_text_with_mistral(pdf_bytes: bytes, file_name: str = "document.pdf") -> str:
    """
    Convenience-Funktion für synchrone Mistral OCR Extraktion.

    Args:
        pdf_bytes: PDF als Bytes
        file_name: Optionaler Dateiname

    Returns:
        Extrahierter Text oder leerer String bei Fehler
    """
    service = get_mistral_ocr_service()

    if not service.is_configured:
        logger.debug("Mistral OCR nicht konfiguriert, überspringe")
        return ""

    try:
        # In neuem Event Loop ausführen falls nicht bereits async
        try:
            loop = asyncio.get_running_loop()
            # Bereits in async context - direkt awaiten
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    service.extract_text(pdf_bytes, file_name)
                )
                return future.result(timeout=120)
        except RuntimeError:
            # Kein running loop - neuen erstellen
            return asyncio.run(service.extract_text(pdf_bytes, file_name))

    except RateLimitError:
        logger.warning("Mistral Rate Limit erreicht, Fallback auf Tesseract")
        return ""
    except Exception as e:
        logger.warning(f"Mistral OCR fehlgeschlagen: {e}")
        return ""
