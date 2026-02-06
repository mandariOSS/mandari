# SPDX-License-Identifier: AGPL-3.0-or-later
"""
AI-powered motion assistant service.

Provides AI assistance for motion creation:
- Text improvement with specific instructions
- Formal correctness checking
- Suggestion generation
- Title generation from content
- Bullet point expansion
"""

import json
import logging
from dataclasses import dataclass

from django.conf import settings

from .ai_security import AIInputSanitizer, AIOutputFilter, AIRateLimiter

logger = logging.getLogger(__name__)


@dataclass
class AIResponse:
    """Standardized AI response."""

    success: bool
    content: str = ""
    error: str = ""
    suggestions: list = None

    def __post_init__(self):
        if self.suggestions is None:
            self.suggestions = []


class MotionAIService:
    """
    AI-powered assistance for motion/document creation.

    Uses the Groq API for fast inference.
    """

    SYSTEM_PROMPT = """Du bist Experte für kommunalpolitische Anträge und Anfragen in Deutschland.
Deine Aufgabe ist es, Anträge formal korrekt, klar und überzeugend zu formulieren.

Wichtige Regeln:
- Verwende formale Sprache, aber bleibe verständlich
- Beachte die typische Struktur kommunaler Anträge (Betreff, Antrag/Beschlussvorschlag, Begründung)
- Sei präzise und verzichte auf unnötige Floskeln
- Beachte die politische Neutralität - der Nutzer bestimmt die Ausrichtung

Format-Hinweise:
- Verwende klare Absätze
- Nummeriere bei mehreren Beschlusspunkten
- Halte die Begründung sachlich"""

    MOTION_TYPES = {
        "motion": "Antrag",
        "inquiry": "Anfrage",
        "statement": "Stellungnahme",
        "amendment": "Änderungsantrag",
    }

    def __init__(self, user_id: int = None):
        """
        Initialize the AI service.

        Args:
            user_id: Optional user ID for rate limiting
        """
        self.api_key = getattr(settings, "GROQ_API_KEY", None)
        self.model = getattr(settings, "GROQ_MODEL", "llama-3.1-70b-versatile")
        self.user_id = user_id

    def _get_client(self):
        """Get the Groq client (lazy loading)."""
        if not self.api_key:
            return None

        try:
            from groq import Groq

            return Groq(api_key=self.api_key)
        except ImportError:
            logger.warning("Groq library not installed")
            return None

    def _check_rate_limit(self) -> tuple[bool, str]:
        """Check rate limit for current user."""
        if not self.user_id:
            return True, ""
        return AIRateLimiter.check_limit(self.user_id)

    def _increment_rate_limit(self) -> None:
        """Increment rate limit counter after successful request."""
        if self.user_id:
            AIRateLimiter.increment(self.user_id)

    def _call_api(self, messages: list, max_tokens: int = 2000) -> str | None:
        """Make API call to Groq with security checks."""
        # Check rate limit
        allowed, error_message = self._check_rate_limit()
        if not allowed:
            logger.warning(f"Rate limit exceeded for user {self.user_id}: {error_message}")
            return None

        client = self._get_client()
        if not client:
            return None

        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.7,
            )
            result = response.choices[0].message.content

            # Increment rate limit on success
            self._increment_rate_limit()

            # Filter output for safety
            return AIOutputFilter.filter(result, allow_html=True)
        except Exception as e:
            logger.error(f"Groq API error: {e}")
            return None

    def improve_text(self, text: str, instruction: str, motion_type: str = "motion", context: str = "") -> AIResponse:
        """
        Improve text based on specific instruction.

        Args:
            text: The text to improve
            instruction: What to improve (e.g., "make more formal", "shorten")
            motion_type: Type of motion (motion, inquiry, etc.)
            context: Additional context about the motion

        Returns:
            AIResponse with improved text
        """
        if not text.strip():
            return AIResponse(success=False, error="Kein Text zum Verbessern")

        # Sanitize inputs
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

Antworte nur mit dem verbesserten Text, ohne Erklärungen.""",
            },
        ]

        result = self._call_api(messages)
        if result:
            return AIResponse(success=True, content=result.strip())

        return AIResponse(success=False, error="AI-Service nicht verfügbar")

    def check_formalities(self, content: str, motion_type: str = "motion") -> AIResponse:
        """
        Check a motion for formal correctness.

        Returns list of issues and suggestions.
        """
        if not content.strip():
            return AIResponse(success=False, error="Kein Inhalt zum Prüfen")

        # Sanitize input
        content = AIInputSanitizer.sanitize(content)

        type_name = self.MOTION_TYPES.get(motion_type, "Antrag")

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""Prüfe den folgenden {type_name} auf formale Korrektheit.

Prüfe besonders:
1. Ist ein klarer Beschlussvorschlag vorhanden?
2. Ist der Betreff aussagekräftig?
3. Enthält die Begründung alle wichtigen Punkte?
4. Ist die Sprache formal und verständlich?
5. Gibt es logische Lücken oder Widersprüche?

{type_name}:
{content}

Antworte im JSON-Format:
{{
    "issues": ["Problem 1", "Problem 2"],
    "suggestions": ["Verbesserungsvorschlag 1", "Verbesserungsvorschlag 2"],
    "score": 85,
    "summary": "Kurze Zusammenfassung"
}}""",
            },
        ]

        result = self._call_api(messages)
        if result:
            try:
                # Try to parse JSON
                data = json.loads(result)
                return AIResponse(
                    success=True,
                    content=data.get("summary", ""),
                    suggestions=data.get("issues", []) + data.get("suggestions", []),
                )
            except json.JSONDecodeError:
                # Return as plain text
                return AIResponse(success=True, content=result)

        return AIResponse(success=False, error="AI-Service nicht verfügbar")

    def suggest_improvements(self, content: str) -> AIResponse:
        """
        Generate improvement suggestions for the motion.

        Returns a list of actionable suggestions.
        """
        if not content.strip():
            return AIResponse(success=False, error="Kein Inhalt")

        # Sanitize input
        content = AIInputSanitizer.sanitize(content)

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""Analysiere diesen Antrag und gib konkrete Verbesserungsvorschläge.

Antrag:
{content}

Gib maximal 5 Vorschläge. Antworte im JSON-Format:
[
    {{"type": "struktur", "suggestion": "Vorschlag 1"}},
    {{"type": "sprache", "suggestion": "Vorschlag 2"}},
    {{"type": "inhalt", "suggestion": "Vorschlag 3"}}
]""",
            },
        ]

        result = self._call_api(messages)
        if result:
            try:
                suggestions = json.loads(result)
                return AIResponse(success=True, suggestions=[s.get("suggestion", str(s)) for s in suggestions])
            except json.JSONDecodeError:
                # Parse as plain text list
                lines = [line.strip("- ").strip() for line in result.split("\n") if line.strip()]
                return AIResponse(success=True, suggestions=lines[:5])

        return AIResponse(success=False, error="AI-Service nicht verfügbar")

    def generate_title(self, content: str) -> AIResponse:
        """
        Generate a suitable title from the motion content.
        """
        if not content.strip():
            return AIResponse(success=False, error="Kein Inhalt")

        # Sanitize input
        content = AIInputSanitizer.sanitize(content)

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""Erstelle einen passenden Betreff/Titel für diesen Antrag.

Der Titel sollte:
- Prägnant sein (max. 100 Zeichen)
- Den Kerninhalt widerspiegeln
- Formal korrekt sein

Antrag:
{content[:2000]}

Antworte nur mit dem Titel, ohne Erklärungen.""",
            },
        ]

        result = self._call_api(messages, max_tokens=200)
        if result:
            # Clean up result
            title = result.strip().strip('"').strip()
            return AIResponse(success=True, content=title[:500])

        return AIResponse(success=False, error="AI-Service nicht verfügbar")

    def expand_bullet_points(self, bullet_points: str, motion_type: str = "motion", context: str = "") -> AIResponse:
        """
        Expand bullet points into a full motion text.

        Args:
            bullet_points: Bullet points or notes to expand
            motion_type: Type of motion
            context: Additional context

        Returns:
            AIResponse with expanded text
        """
        if not bullet_points.strip():
            return AIResponse(success=False, error="Keine Stichpunkte")

        # Sanitize inputs
        bullet_points = AIInputSanitizer.sanitize(bullet_points)
        context = AIInputSanitizer.sanitize(context) if context else ""

        type_name = self.MOTION_TYPES.get(motion_type, "Antrag")

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""Formuliere aus diesen Stichpunkten einen vollständigen {type_name}.

{f"Kontext: {context}" if context else ""}

Stichpunkte:
{bullet_points}

Erstelle einen gut strukturierten {type_name} mit:
- Klarem Beschlussvorschlag (bei Anträgen) oder klarer Fragestellung (bei Anfragen)
- Sachlicher Begründung

Antworte nur mit dem ausformulierten {type_name}.""",
            },
        ]

        result = self._call_api(messages, max_tokens=3000)
        if result:
            return AIResponse(success=True, content=result.strip())

        return AIResponse(success=False, error="AI-Service nicht verfügbar")

    def generate_summary(self, content: str, max_length: int = 300) -> AIResponse:
        """
        Generate a public summary of the motion.
        """
        if not content.strip():
            return AIResponse(success=False, error="Kein Inhalt")

        # Sanitize input
        content = AIInputSanitizer.sanitize(content)

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""Erstelle eine öffentliche Kurzzusammenfassung dieses Antrags.

Die Zusammenfassung sollte:
- Maximal {max_length} Zeichen lang sein
- Den Kern des Antrags erfassen
- Verständlich für die Öffentlichkeit sein

Antrag:
{content[:3000]}

Antworte nur mit der Zusammenfassung.""",
            },
        ]

        result = self._call_api(messages, max_tokens=500)
        if result:
            summary = result.strip()[:max_length]
            return AIResponse(success=True, content=summary)

        return AIResponse(success=False, error="AI-Service nicht verfügbar")

    def is_available(self) -> bool:
        """Check if the AI service is available."""
        return bool(self.api_key)


# Singleton instance
motion_ai_service = MotionAIService()
