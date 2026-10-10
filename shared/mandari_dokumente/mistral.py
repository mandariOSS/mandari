"""
Die eine Anbindung an die Mistral-API für die Texterkennung (Issue #530).

Bisher gab es zwei: eine asynchrone mit Begrenzung je Minute in der Anwendung und eine synchrone im
Ingestor. Übrig bleibt eine synchrone Anfrage (sie läuft im Arbeitsfaden bzw. Auftrag) mit einer einfachen
Begrenzung der Anfragen je Minute und Prozess. Fehler werfen eine Ausnahme; die Texterkennung fällt dann
auf Tesseract zurück. Der API-Schlüssel erscheint nie in Meldungen.

Seit Issue #950 gibt es keinen festen Endpunkt mehr: Die Adresse kommt aus ``MISTRAL_BASE_URL`` (OpenAI-
kompatible Basis-URL, etwa ``https://…/v1``), und ihr Host muss in der Positivliste ``KI_ERLAUBTE_HOSTS``
stehen (``ki_hosts``, leer ist nichts erlaubt). Ein gesetzter Schlüssel allein schickt also nichts nach außen.
"""

from __future__ import annotations

import base64
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .ki_hosts import STANDARD_ERLAUBTE_HOSTS, ist_erlaubter_host

DEFAULT_MODEL = "pixtral-12b-2409"

_PROMPT = (
    "Extrahiere den vollständigen Text aus diesem PDF-Dokument. "
    "Gib nur den extrahierten Text zurück, ohne Kommentare oder Formatierung. "
    "Behalte Absätze und Strukturierung bei. "
    "Falls das Dokument auf Deutsch ist, behalte die deutsche Sprache bei."
)


@dataclass(frozen=True)
class MistralConfig:
    """
    Zugang und Grenzen. Eingeschaltet nur mit Schlüssel, Adresse und erlaubtem Host.

    ``url`` ist die OpenAI-kompatible Basis-URL (``…/v1``) oder schon die volle Adresse der
    Chat-Schnittstelle (``…/chat/completions``); ``erlaubte_hosts`` die Positivliste (``KI_ERLAUBTE_HOSTS``),
    im Standard leer: Ohne ausdrücklich erlaubten Host bleibt die externe Texterkennung aus.
    """

    api_key: str = field(default="", repr=False)
    url: str = ""
    erlaubte_hosts: tuple[str, ...] = STANDARD_ERLAUBTE_HOSTS
    model: str = DEFAULT_MODEL
    timeout: float = 120.0
    #: Anfragen je Minute und Prozess; 0 = ohne Begrenzung
    requests_per_minute: int = 60

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and bool(self.url) and ist_erlaubter_host(self.url, self.erlaubte_hosts)

    @property
    def endpoint(self) -> str:
        """Adresse der Chat-Schnittstelle mit Bildeingabe."""
        basis = self.url.strip().rstrip("/")
        return basis if basis.endswith("/chat/completions") else basis + "/chat/completions"


class MistralError(Exception):
    """Mistral lieferte keinen Text (Fehler der Schnittstelle, Netz, Begrenzung)."""


class MistralRateLimitError(MistralError):
    """Begrenzung je Minute erreicht (eigene oder von Mistral, HTTP 429)."""


class _RateLimiter:
    """Gleitendes Fenster von einer Minute je Prozess."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._times: deque[float] = deque()

    def acquire(self, per_minute: int, now: float | None = None) -> bool:
        if per_minute <= 0:
            return True
        now = time.monotonic() if now is None else now
        with self._lock:
            while self._times and now - self._times[0] >= 60.0:
                self._times.popleft()
            if len(self._times) >= per_minute:
                return False
            self._times.append(now)
            return True


_limiter = _RateLimiter()


def extract_text_with_mistral(data: bytes, config: MistralConfig, file_name: str = "") -> str:
    """Text eines PDFs über die Mistral-API; wirft ``MistralError`` statt leeren Text bei Fehlern."""
    import httpx

    if not config.enabled:
        raise MistralError("Mistral nicht eingerichtet")
    if not _limiter.acquire(config.requests_per_minute):
        raise MistralRateLimitError("Begrenzung je Minute erreicht")
    payload = {
        "model": config.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:application/pdf;base64," + base64.b64encode(data).decode("ascii")},
                    },
                ],
            }
        ],
        "max_tokens": 32000,
    }
    try:
        response = httpx.post(
            config.endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=config.timeout,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise MistralError(f"Mistral nicht erreichbar ({type(exc).__name__})") from exc
    if response.status_code == 429:
        raise MistralRateLimitError("Mistral meldet HTTP 429")
    if response.status_code != 200:
        raise MistralError(f"Mistral meldet HTTP {response.status_code}")
    try:
        result = response.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    except (ValueError, AttributeError, IndexError) as exc:
        raise MistralError("Antwort von Mistral nicht lesbar") from exc
    return str(content).strip()
