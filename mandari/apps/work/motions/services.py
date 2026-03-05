# SPDX-License-Identifier: AGPL-3.0-or-later
"""
AI-powered document assistant service for Work DMS.

Key goals:
- OpenAI-compatible provider integration (Nebius default)
- Organization-level API keys and model/provider overrides
- Hard token budgets per organization (day/week/month)
- Context-aware chat for collaborative document editing
"""

import json
import logging
import re
from dataclasses import dataclass

import httpx
from django.conf import settings

from apps.common.models import SiteSettings

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

    PROVIDER_DEFAULTS = {
        "nebius": {
            "base_url": "https://api.tokenfactory.nebius.com/v1/",
            "model": "openai/gpt-oss-120b",
        },
        "ovh": {
            "base_url": "",
            "model": "openai/gpt-oss-120b",
        },
        "ionos": {
            "base_url": "",
            "model": "openai/gpt-oss-120b",
        },
    }

    def __init__(self, organization=None, user_id: int | None = None):
        self.organization = organization
        self.user_id = user_id

    def _resolve_provider_config(self) -> dict:
        provider = (getattr(self.organization, "ai_provider", "") or "nebius").lower()
        defaults = self.PROVIDER_DEFAULTS.get(provider, self.PROVIDER_DEFAULTS["nebius"])

        model = getattr(self.organization, "ai_model", "") or defaults["model"]
        base_url = ""
        api_key = ""

        if self.organization:
            base_url = self.organization.get_effective_ai_base_url()
            api_key = self.organization.get_ai_api_key()

        # For Nebius we also support global fallback key.
        if not api_key and provider == "nebius":
            api_key = SiteSettings.get_nebius_api_key() or getattr(settings, "NEBIUS_API_KEY", "")

        if not base_url:
            base_url = defaults["base_url"]

        return {
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
        }

    def _check_rate_limit(self) -> tuple[bool, str]:
        if not self.user_id:
            return True, ""
        organization_id = str(self.organization.id) if self.organization else None
        return AIRateLimiter.check_limit(self.user_id, organization_id=organization_id)

    def _increment_rate_limit(self) -> None:
        if self.user_id:
            organization_id = str(self.organization.id) if self.organization else None
            AIRateLimiter.increment(self.user_id, organization_id=organization_id)

    def _check_org_token_limits(self, estimated_tokens: int) -> tuple[bool, str]:
        if not self.organization:
            return True, ""

        day_used = OrganizationAITokenUsage.get_tokens_used(self.organization, OrganizationAITokenUsage.PERIOD_DAY)
        week_used = OrganizationAITokenUsage.get_tokens_used(self.organization, OrganizationAITokenUsage.PERIOD_WEEK)
        month_used = OrganizationAITokenUsage.get_tokens_used(self.organization, OrganizationAITokenUsage.PERIOD_MONTH)

        if day_used + estimated_tokens > self.organization.ai_token_limit_daily:
            return False, "Tageslimit für KI-Tokens erreicht."
        if week_used + estimated_tokens > self.organization.ai_token_limit_weekly:
            return False, "Wochenlimit für KI-Tokens erreicht."
        if month_used + estimated_tokens > self.organization.ai_token_limit_monthly:
            return False, "Monatslimit für KI-Tokens erreicht."
        return True, ""

    def _record_token_usage(self, total_tokens: int) -> None:
        if self.organization and total_tokens > 0:
            OrganizationAITokenUsage.increment_usage(self.organization, total_tokens)

    def _call_api(
        self,
        messages: list[dict],
        max_tokens: int = 2000,
        temperature: float = 0.5,
    ) -> AIResponse:
        allowed, limit_message = self._check_rate_limit()
        if not allowed:
            return AIResponse(success=False, error=limit_message)

        cfg = self._resolve_provider_config()
        if not cfg["api_key"]:
            return AIResponse(success=False, error="Kein KI API Key konfiguriert.")
        if not cfg["base_url"]:
            return AIResponse(success=False, error="Kein KI Endpoint (Base URL) konfiguriert.")

        estimated_prompt_tokens = sum(_estimate_tokens(str(m.get("content", ""))) for m in messages)
        estimated_total = estimated_prompt_tokens + max_tokens
        allowed, budget_message = self._check_org_token_limits(estimated_total)
        if not allowed:
            return AIResponse(success=False, error=budget_message)

        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        }

        try:
            timeout = httpx.Timeout(90.0, connect=15.0)
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
            data = response.json()

            content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
            usage = data.get("usage") or {}
            total_tokens = int(usage.get("total_tokens") or 0)
            if total_tokens <= 0:
                total_tokens = _estimate_tokens(content) + estimated_prompt_tokens

            # Strong post-check to enforce budget strictly.
            allowed, budget_message = self._check_org_token_limits(total_tokens)
            if not allowed:
                return AIResponse(success=False, error=budget_message)

            self._record_token_usage(total_tokens)
            self._increment_rate_limit()

            safe_content = AIOutputFilter.filter(content, allow_html=True)
            return AIResponse(success=True, content=safe_content, total_tokens=total_tokens)
        except httpx.HTTPStatusError as e:
            logger.warning("AI provider HTTP error: %s - %s", e.response.status_code, e.response.text[:500])
            return AIResponse(success=False, error=f"KI-Provider Fehler: {e.response.status_code}")
        except Exception as e:
            logger.exception("AI provider call failed: %s", e)
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
        cfg = self._resolve_provider_config()
        return bool(cfg["api_key"] and cfg["base_url"] and cfg["model"])
