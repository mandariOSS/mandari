# SPDX-License-Identifier: AGPL-3.0-or-later
"""
AI-powered document assistant service for Work DMS.

Key goals:
- Ein OpenAI-kompatibler Endpunkt aus der zentralen KI-Konfiguration (``apps.common.ki_anbieter``, Issue #950):
  kein fest eingebauter Anbieter, kein Rückfall, nur Hosts aus ``KI_ERLAUBTE_HOSTS``
- Organization-level API keys and model/provider overrides
- Hard token budgets per organization (day/week/month)
- Context-aware chat for collaborative document editing
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpRequest, JsonResponse

from apps.common.ki_anbieter import KiEndpunkt, endpunkt_fuer_work, laengenlimit_hinweis
from apps.common.models import AISettings

from .ai_security import AIInputSanitizer, AIOutputFilter, AIRateLimiter
from .models import OrganizationAITokenUsage

logger = logging.getLogger(__name__)


def _strip_html(value: str) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _estimate_tokens(value: str) -> int:
    # Good enough estimation for budget pre-checking.
    return max(1, len(value or "") // 4)


@dataclass
class AIResponse:
    """Standardized AI response."""

    success: bool
    content: str = ""
    error: str = ""
    suggestions: list = None
    total_tokens: int = 0

    def __post_init__(self):
        if self.suggestions is None:
            self.suggestions = []


class MotionAIService:
    """
    AI-powered assistance for collaborative document editing.
    """

    SYSTEM_PROMPT = """Du bist ein deutscher Assistent für kommunalpolitische Dokumente.
Du hilfst beim Schreiben, Umformulieren, Prüfen und Strukturieren.
Nutze den bereitgestellten Dokumentkontext präzise und antworte konkret."""

    CHAT_SYSTEM_PROMPT = """Du bist der KI-Co-Editor in einem kollaborativen Dokument (Beta).
Verhalte dich wie ein pragmatischer Redaktionsassistent:
- Beziehe dich auf den bereitgestellten Dokumentkontext
- Erkläre kurz und klar
- Gib konkrete Formulierungsvorschläge in Deutsch
- Erfinde keine Fakten
- Wenn Informationen fehlen, stelle Rückfragen"""

    MOTION_TYPES = {
        "motion": "Antrag",
        "inquiry": "Anfrage",
        "statement": "Stellungnahme",
        "amendment": "Änderungsantrag",
    }

    #: Meldung, wenn kein freigegebener Endpunkt eingerichtet ist
    NICHT_EINGERICHTET = "KI ist nicht eingerichtet."

    def __init__(self, organization=None, user_id: int | None = None, *, transport: httpx.BaseTransport | None = None):
        self.organization = organization
        self.user_id = user_id
        # Nur für Tests (httpx.MockTransport); im Betrieb der Standard-Transport von httpx
        self._transport = transport

    def _resolve_provider_config(self) -> KiEndpunkt | None:
        """
        Endpunkt aus der zentralen KI-Konfiguration (``endpunkt_fuer_work``), bei jedem Aufruf neu geprüft.

        1. Organisation mit eigenem API Key (Organization → KI): ihre Konfiguration.
        2. Sonst die KI-Einstellungen, wenn „KI in Work aktiviert“ und ein Key gesetzt ist.
        3. Sonst ``None``: Die KI ist aus. Es gibt keinen Rückfall auf einen fest eingebauten Anbieter.
        """
        return endpunkt_fuer_work(self.organization)

    def _check_rate_limit(self) -> tuple[bool, str]:
        if not self.user_id:
            return True, ""
        organization_id = str(self.organization.id) if self.organization else None
        return AIRateLimiter.check_limit(self.user_id, organization_id=organization_id)

    def _increment_rate_limit(self) -> None:
        if self.user_id:
            organization_id = str(self.organization.id) if self.organization else None
            AIRateLimiter.increment(self.user_id, organization_id=organization_id)

    QUOTA_EXCEEDED_MESSAGE = (
        "KI-Kontingent aufgebraucht — im nächsten Monat wieder verfügbar. "
        "Das Limit kann im Admin (KI-Einstellungen bzw. Organisation) erhöht werden."
    )

    def _effective_monthly_limit(self) -> int | None:
        """
        Effektives Monats-Token-Limit der Organisation.

        Org-Override (``ai_token_limit_monthly``) gewinnt; ``None`` dort bedeutet
        Default aus den globalen ``AISettings``. Rückgabe 0 = KI deaktiviert.
        """
        if not self.organization:
            return None
        org_limit = self.organization.ai_token_limit_monthly
        if org_limit is not None:
            return org_limit
        return AISettings.get_settings().default_org_monthly_token_limit

    def _check_org_token_limits(self, estimated_tokens: int) -> tuple[bool, str]:
        if not self.organization:
            return True, ""

        monthly_limit = self._effective_monthly_limit()
        if monthly_limit == 0:
            return False, "KI ist für diese Organisation deaktiviert."

        day_used = OrganizationAITokenUsage.get_tokens_used(self.organization, OrganizationAITokenUsage.PERIOD_DAY)
        week_used = OrganizationAITokenUsage.get_tokens_used(self.organization, OrganizationAITokenUsage.PERIOD_WEEK)
        month_used = OrganizationAITokenUsage.get_tokens_used(self.organization, OrganizationAITokenUsage.PERIOD_MONTH)

        if day_used + estimated_tokens > self.organization.ai_token_limit_daily:
            return False, "Tageslimit für KI-Tokens erreicht."
        if week_used + estimated_tokens > self.organization.ai_token_limit_weekly:
            return False, "Wochenlimit für KI-Tokens erreicht."
        if monthly_limit is not None and month_used + estimated_tokens > monthly_limit:
            return False, self.QUOTA_EXCEEDED_MESSAGE
        return True, ""

    def get_quota_status(self) -> dict:
        """
        Monats-Kontingent der Organisation für die Anzeige im KI-Panel.

        Returns dict mit ``limit`` (None = unbegrenzt), ``used`` und
        ``remaining`` (None = unbegrenzt).
        """
        if not self.organization:
            return {"limit": None, "used": 0, "remaining": None}
        limit = self._effective_monthly_limit()
        used = OrganizationAITokenUsage.get_tokens_used(self.organization, OrganizationAITokenUsage.PERIOD_MONTH)
        remaining = max(0, limit - used) if limit is not None else None
        return {"limit": limit, "used": used, "remaining": remaining}

    def _record_token_usage(self, total_tokens: int) -> None:
        if self.organization and total_tokens > 0:
            OrganizationAITokenUsage.increment_usage(self.organization, total_tokens)

    def _call_api(
        self,
        messages: list[dict],
        max_tokens: int = 2000,
        temperature: float = 0.5,
    ) -> AIResponse:
        if getattr(settings, "DEMO_INSTANCE", False):
            # Öffentliche Demo (Issue #99): keine Aufrufe an KI-Anbieter oder frei eintragbare Adressen.
            return AIResponse(success=False, error="KI-Funktionen sind in der Demo-Umgebung abgeschaltet.")
        if self.organization is not None and not self.organization.ai_enabled:
            return AIResponse(success=False, error="KI ist für diese Organisation deaktiviert.")
        allowed, limit_message = self._check_rate_limit()
        if not allowed:
            return AIResponse(success=False, error=limit_message)

        endpunkt = self._resolve_provider_config()
        if endpunkt is None:
            return AIResponse(success=False, error=self.NICHT_EINGERICHTET)

        # Obergrenze für die Antwortlänge (AISettings.max_output_tokens; 0 = keine)
        if endpunkt.max_output_tokens > 0:
            max_tokens = min(max_tokens, endpunkt.max_output_tokens)

        estimated_prompt_tokens = sum(_estimate_tokens(str(m.get("content", ""))) for m in messages)
        estimated_total = estimated_prompt_tokens + max_tokens
        allowed, budget_message = self._check_org_token_limits(estimated_total)
        if not allowed:
            return AIResponse(success=False, error=budget_message)

        # OpenAI-kompatible Chat-Completions; die Adresse ist geprüft (KI_ERLAUBTE_HOSTS)
        payload = {
            "model": endpunkt.modell,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {endpunkt.api_key}",
            "Content-Type": "application/json",
        }

        try:
            timeout = httpx.Timeout(90.0, connect=15.0)
            with httpx.Client(timeout=timeout, transport=self._transport, follow_redirects=False) as client:
                response = client.post(endpunkt.chat_url, json=payload, headers=headers)
                response.raise_for_status()
            data = response.json()

            message = ((data.get("choices") or [{}])[0]).get("message") or {}
            content = message.get("content") or ""
            if not isinstance(content, str):
                content = ""
            usage = data.get("usage") or {}
            total_tokens = int(usage.get("total_tokens") or 0)
            if total_tokens <= 0:
                total_tokens = _estimate_tokens(content) + estimated_prompt_tokens
            # Nachweis je Aufruf: Anbieter, Host, Modell, Tokens; nie Inhalt oder Schlüssel
            logger.info(
                "KI-Aufruf: anbieter=%s host=%s modell=%s tokens=%d",
                endpunkt.anbieter,
                endpunkt.host,
                endpunkt.modell,
                total_tokens,
            )

            # Strong post-check to enforce budget strictly.
            allowed, budget_message = self._check_org_token_limits(total_tokens)
            if not allowed:
                return AIResponse(success=False, error=budget_message)

            self._record_token_usage(total_tokens)
            self._increment_rate_limit()

            safe_content = AIOutputFilter.filter(content, allow_html=True)
            return AIResponse(success=True, content=safe_content, total_tokens=total_tokens)
        except httpx.HTTPStatusError as e:
            # Ohne Antworttext: Er kann Teile der Anfrage enthalten
            hinweis = laengenlimit_hinweis(e.response.status_code, e.response.text)
            if hinweis is not None:
                logger.warning(
                    "KI-Aufruf abgelehnt (HTTP %d): %s (anbieter=%s host=%s modell=%s max_tokens=%d)",
                    e.response.status_code,
                    hinweis,
                    endpunkt.anbieter,
                    endpunkt.host,
                    endpunkt.modell,
                    max_tokens,
                )
                return AIResponse(success=False, error="Die Anfrage ist für das KI-Modell zu lang.")
            logger.warning(
                "KI-Aufruf gescheitert: anbieter=%s host=%s modell=%s status=%s",
                endpunkt.anbieter,
                endpunkt.host,
                endpunkt.modell,
                e.response.status_code,
            )
            return AIResponse(success=False, error=f"KI-Provider Fehler: {e.response.status_code}")
        except Exception as e:
            logger.warning(
                "KI-Aufruf gescheitert: anbieter=%s host=%s modell=%s fehler=%s",
                endpunkt.anbieter,
                endpunkt.host,
                endpunkt.modell,
                type(e).__name__,
            )
            return AIResponse(success=False, error="KI-Service nicht verfügbar.")

    def improve_text(self, text: str, instruction: str, motion_type: str = "motion", context: str = "") -> AIResponse:
        if not text.strip():
            return AIResponse(success=False, error="Kein Text zum Verbessern")

        text = AIInputSanitizer.sanitize(text)
        instruction = AIInputSanitizer.sanitize(instruction)
        context = AIInputSanitizer.sanitize(context) if context else ""
        type_name = self.MOTION_TYPES.get(motion_type, "Antrag")

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""Verbessere den folgenden Text eines {type_name}s.
Anweisung: {instruction}
{f"Kontext: {context}" if context else ""}
Text:
{text}
Antworte nur mit dem verbesserten Text.""",
            },
        ]
        return self._call_api(messages, max_tokens=2000, temperature=0.4)

    def check_formalities(self, content: str, motion_type: str = "motion") -> AIResponse:
        if not content.strip():
            return AIResponse(success=False, error="Kein Inhalt zum Prüfen")

        content = AIInputSanitizer.sanitize(content)
        type_name = self.MOTION_TYPES.get(motion_type, "Antrag")
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""Prüfe den folgenden {type_name} auf formale Korrektheit.
Antworte als JSON:
{{"issues": [], "suggestions": [], "summary": ""}}
Inhalt:
{content}""",
            },
        ]
        result = self._call_api(messages, max_tokens=1200, temperature=0.2)
        if not result.success:
            return result
        try:
            data = json.loads(result.content)
            return AIResponse(
                success=True,
                content=data.get("summary", ""),
                suggestions=data.get("issues", []) + data.get("suggestions", []),
                total_tokens=result.total_tokens,
            )
        except json.JSONDecodeError:
            return result

    def suggest_improvements(self, content: str) -> AIResponse:
        if not content.strip():
            return AIResponse(success=False, error="Kein Inhalt")
        content = AIInputSanitizer.sanitize(content)
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""Analysiere den Text und nenne maximal 5 konkrete Verbesserungen als JSON-Array.
Text:
{content}""",
            },
        ]
        result = self._call_api(messages, max_tokens=900, temperature=0.3)
        if not result.success:
            return result
        try:
            suggestions = json.loads(result.content)
            return AIResponse(
                success=True,
                suggestions=[s.get("suggestion", str(s)) for s in suggestions],
                total_tokens=result.total_tokens,
            )
        except json.JSONDecodeError:
            lines = [line.strip("- ").strip() for line in result.content.split("\n") if line.strip()]
            return AIResponse(success=True, suggestions=lines[:5], total_tokens=result.total_tokens)

    def generate_title(self, content: str) -> AIResponse:
        if not content.strip():
            return AIResponse(success=False, error="Kein Inhalt")
        content = AIInputSanitizer.sanitize(content)
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""Erstelle einen prägnanten Titel (max. 100 Zeichen) für den Text.
Text:
{content[:3000]}""",
            },
        ]
        result = self._call_api(messages, max_tokens=180, temperature=0.2)
        if result.success:
            result.content = result.content.strip().strip('"')[:500]
        return result

    def expand_bullet_points(self, bullet_points: str, motion_type: str = "motion", context: str = "") -> AIResponse:
        if not bullet_points.strip():
            return AIResponse(success=False, error="Keine Stichpunkte")
        bullet_points = AIInputSanitizer.sanitize(bullet_points)
        context = AIInputSanitizer.sanitize(context) if context else ""
        type_name = self.MOTION_TYPES.get(motion_type, "Antrag")
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""Formuliere aus den Stichpunkten einen vollständigen {type_name}.
{f"Kontext: {context}" if context else ""}
Stichpunkte:
{bullet_points}""",
            },
        ]
        return self._call_api(messages, max_tokens=2600, temperature=0.5)

    def generate_summary(self, content: str, max_length: int = 300) -> AIResponse:
        if not content.strip():
            return AIResponse(success=False, error="Kein Inhalt")
        content = AIInputSanitizer.sanitize(content)
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""Erstelle eine öffentliche Zusammenfassung mit max. {max_length} Zeichen.
Text:
{content[:4000]}""",
            },
        ]
        result = self._call_api(messages, max_tokens=500, temperature=0.3)
        if result.success:
            result.content = result.content[:max_length]
        return result

    def chat_with_document(
        self,
        document_html: str,
        user_message: str,
        selected_text: str = "",
        history: list[dict] | None = None,
    ) -> AIResponse:
        if not user_message.strip():
            return AIResponse(success=False, error="Leere Nachricht")

        user_message = AIInputSanitizer.sanitize(user_message)
        selected_text = AIInputSanitizer.sanitize(selected_text or "")
        document_text = AIInputSanitizer.sanitize(_strip_html(document_html))[:16000]

        messages = [{"role": "system", "content": self.CHAT_SYSTEM_PROMPT}]
        if document_text:
            messages.append(
                {
                    "role": "system",
                    "content": f"Dokumentkontext (gekürzt):\n{document_text}",
                }
            )
        if selected_text:
            messages.append(
                {
                    "role": "system",
                    "content": f"Aktuell markierter Text:\n{selected_text[:2000]}",
                }
            )

        # Keep conversation history short for cost control.
        for msg in (history or [])[-8:]:
            role = msg.get("role")
            content = AIInputSanitizer.sanitize(str(msg.get("content", "")))
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content[:4000]})

        messages.append({"role": "user", "content": user_message})
        return self._call_api(messages, max_tokens=1400, temperature=0.4)

    def is_available(self) -> bool:
        if self.organization and not self.organization.ai_enabled:
            return False
        # Effektives Monatslimit 0 = KI für diese Organisation deaktiviert.
        if self.organization and self._effective_monthly_limit() == 0:
            return False
        return self._resolve_provider_config() is not None


# ---------------------------------------------------------------------------
# Speichern ohne Kollaborationsverbindung (#184)
# ---------------------------------------------------------------------------

#: Hinweis, wenn ein Speichern auf einen inzwischen geänderten Stand trifft.
KONFLIKT_HINWEIS = (
    "Das Dokument wurde inzwischen an anderer Stelle geändert. "
    "Laden Sie die Seite neu, um den aktuellen Stand zu sehen, oder überschreiben Sie ihn bewusst."
)


def speicherkonflikt(request: HttpRequest, old_content: str, new_content: str) -> JsonResponse | str | None:
    """
    Prüft, ob ein POST-Speichern einen neueren Stand still überschreiben würde (#184).

    Der Editor schickt ohne Kollaborationsverbindung ``base_content_hash`` mit, den
    Fingerabdruck des Stands, von dem er ausgeht. Passt er nicht mehr zum gespeicherten
    Inhalt und unterscheidet sich der neue Inhalt, hat inzwischen jemand anderes
    gespeichert. Nur ein ausdrückliches ``force=1`` überschreibt dann.

    Liefert ``None`` (kein Konflikt), eine 409-Antwort für den Editor (AJAX) oder den
    Hinweistext für ein normales Formular.
    """
    from .models import content_fingerprint

    base_hash = request.POST.get("base_content_hash", "").strip()
    if not base_hash or request.POST.get("force") == "1" or old_content == new_content:
        return None
    if base_hash == content_fingerprint(old_content):
        return None
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse(
            {"error": "conflict", "message": KONFLIKT_HINWEIS, "content_hash": content_fingerprint(old_content)},
            status=409,
        )
    return KONFLIKT_HINWEIS


# ---------------------------------------------------------------------------
# Metadaten aus dem Editor: nur Objekte der eigenen Organisation
# ---------------------------------------------------------------------------


class FremdeAuswahlError(ValueError):
    """Eine im Formular übergebene ID gehört nicht zur Organisation (oder ist ungültig)."""


def _aus_organisation(model: Any, organization: Any, raw_id: str) -> Any:
    """Objekt der Organisation zu einer Formular-ID; leer ergibt ``None``, fremd/ungültig ``FremdeAuswahlError``."""
    if not raw_id:
        return None
    try:
        found = model.objects.filter(id=raw_id, organization=organization).first()
    except (ValueError, ValidationError):
        found = None
    if found is None:
        raise FremdeAuswahlError(raw_id)
    return found


def document_type_for(organization: Any, raw_id: str) -> Any:
    """Dokumenttyp der Organisation (Editor-Auswahl)."""
    from .models import MotionType

    return _aus_organisation(MotionType, organization, raw_id)


def letterhead_for(organization: Any, raw_id: str) -> Any:
    """Briefkopf der Organisation (Editor-Auswahl)."""
    from .models import OrganizationLetterhead

    return _aus_organisation(OrganizationLetterhead, organization, raw_id)


def withdraw_pending_approvals(membership: Any) -> int:
    """
    Offene Freigabe-Anfragen an ein Mitglied löschen (Issue #420).

    Wird beim Entfernen der Mitgliedschaft aufgerufen: Eine entfernte Person kann nicht mehr
    entscheiden, die Anfrage bliebe sonst für immer offen. Bereits getroffene Entscheidungen
    gehören zum Dokument und bleiben mit geleertem Verweis erhalten. Liefert die Anzahl.
    """
    from .models import MotionApproval

    count, _ = MotionApproval.objects.filter(approver=membership, approved__isnull=True).delete()
    return int(count)
