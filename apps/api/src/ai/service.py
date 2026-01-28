"""
AI Service - Groq-based AI for summaries, chat, and location extraction.

Uses the Groq API with Kimi K2 model for fast inference.
"""

import json
import re
from typing import Any

import httpx
from groq import Groq
from pydantic import BaseModel

from src.core.config import settings


class ChatMessage(BaseModel):
    """A chat message."""
    role: str  # "user", "assistant", or "system"
    content: str


class LocationResult(BaseModel):
    """An extracted location with coordinates."""
    name: str
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    confidence: float = 0.0


class AIService:
    """AI Service using Groq API with Kimi K2."""

    # Default model - Kimi K2 via Groq
    DEFAULT_MODEL = "moonshotai/kimi-k2-instruct-0905"

    # System prompts for different tasks
    SUMMARY_SYSTEM_PROMPT = """Du bist ein Experte für kommunalpolitische Themen in Deutschland.
Deine Aufgabe ist es, Vorlagen, Anträge und Beschlüsse verständlich zusammenzufassen.

Regeln:
- Schreibe in klarem, einfachem Deutsch
- Fasse die wichtigsten Punkte zusammen (max. 3-4 Sätze)
- Erkläre den Kontext, wenn nötig
- Bleibe neutral und objektiv
- Nenne konkrete Zahlen oder Daten, wenn vorhanden"""

    CHAT_SYSTEM_PROMPT = """Du bist ein hilfreicher Assistent für Bürger:innen, die sich über kommunalpolitische Themen informieren möchten.

Kontext: Du hast Zugriff auf Daten aus dem Ratsinformationssystem (RIS) einer Kommune.
Du kannst Fragen zu Sitzungen, Vorlagen, Beschlüssen und Personen beantworten.

Regeln:
- Antworte freundlich und verständlich
- Erkläre Fachbegriffe, wenn nötig
- Wenn du etwas nicht weißt, sage es ehrlich
- Verweise auf konkrete Dokumente, wenn möglich
- Bleibe politisch neutral"""

    LOCATION_SYSTEM_PROMPT = """Du bist ein Experte für das Extrahieren von geografischen Orten aus deutschen kommunalpolitischen Dokumenten.

Deine Aufgabe: Finde alle Orte, Straßen, Plätze oder Gebiete, die im Text erwähnt werden.

Antworte NUR im JSON-Format:
{
  "locations": [
    {"name": "Ortsname", "address": "Vollständige Adresse falls bekannt", "type": "street|place|area|building"}
  ]
}

Wenn keine Orte gefunden werden, antworte: {"locations": []}"""

    def __init__(self):
        """Initialize the AI service."""
        self._client: Groq | None = None

    @property
    def client(self) -> Groq | None:
        """Get Groq client (lazy initialization)."""
        api_key = settings.groq_api_key
        if not api_key:
            return None
        if self._client is None:
            self._client = Groq(api_key=api_key)
        return self._client

    def _call_groq_sync(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.6,
        max_tokens: int = 4096,
    ) -> str:
        """Call the Groq API synchronously."""
        client = self.client
        if not client:
            return "KI-Service nicht verfügbar: Kein API-Key konfiguriert."

        try:
            completion = client.chat.completions.create(
                model=model or self.DEFAULT_MODEL,
                messages=messages,
                temperature=temperature,
                max_completion_tokens=max_tokens,
                top_p=1,
                stream=False,
            )
            return completion.choices[0].message.content or ""
        except Exception as e:
            return f"KI-Fehler: {str(e)}"

    async def _call_groq(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.6,
        max_tokens: int = 4096,
    ) -> str:
        """Call the Groq API (runs sync call in thread)."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self._call_groq_sync(messages, model, temperature, max_tokens)
        )

    async def summarize_paper(
        self,
        paper_name: str,
        paper_text: str | None = None,
        paper_type: str | None = None,
    ) -> str:
        """Generate a summary for a paper/document."""
        content = f"Vorlage: {paper_name}\n"
        if paper_type:
            content += f"Typ: {paper_type}\n"
        if paper_text:
            # Truncate long texts
            max_length = 8000
            if len(paper_text) > max_length:
                paper_text = paper_text[:max_length] + "..."
            content += f"\nInhalt:\n{paper_text}"

        messages = [
            {"role": "system", "content": self.SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Bitte fasse diese Vorlage zusammen:\n\n{content}"},
        ]

        return await self._call_groq(messages, temperature=0.3, max_tokens=500)

    async def summarize_meeting(
        self,
        meeting_name: str,
        agenda_items: list[dict[str, Any]],
        date: str | None = None,
    ) -> str:
        """Generate a summary for a meeting based on its agenda."""
        content = f"Sitzung: {meeting_name}\n"
        if date:
            content += f"Datum: {date}\n"

        content += "\nTagesordnung:\n"
        for item in agenda_items[:20]:  # Limit to 20 items
            number = item.get("number", "")
            name = item.get("name", "Unbekannt")
            content += f"- {number}: {name}\n"

        messages = [
            {"role": "system", "content": self.SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Bitte fasse diese Sitzung zusammen:\n\n{content}"},
        ]

        return await self._call_groq(messages, temperature=0.3, max_tokens=500)

    async def chat(
        self,
        user_message: str,
        context: str | None = None,
        history: list[ChatMessage] | None = None,
    ) -> str:
        """Chat with the AI about municipal data."""
        messages = [
            {"role": "system", "content": self.CHAT_SYSTEM_PROMPT},
        ]

        # Add context if provided
        if context:
            messages.append({
                "role": "system",
                "content": f"Aktueller Kontext:\n{context}",
            })

        # Add chat history
        if history:
            for msg in history[-10:]:  # Limit to last 10 messages
                messages.append({"role": msg.role, "content": msg.content})

        # Add the new user message
        messages.append({"role": "user", "content": user_message})

        return await self._call_groq(messages, temperature=0.6, max_tokens=4096)

    async def extract_locations(
        self,
        text: str,
        city: str = "",
    ) -> list[LocationResult]:
        """Extract geographic locations from text."""
        messages = [
            {"role": "system", "content": self.LOCATION_SYSTEM_PROMPT},
            {"role": "user", "content": f"Extrahiere alle Orte aus diesem Text (Stadt: {city}):\n\n{text[:5000]}"},
        ]

        response = await self._call_groq(messages, temperature=0.1, max_tokens=1000)

        # Parse JSON response
        try:
            # Try to extract JSON from the response
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                data = json.loads(json_match.group())
                locations = []
                for loc in data.get("locations", []):
                    locations.append(LocationResult(
                        name=loc.get("name", ""),
                        address=loc.get("address"),
                    ))
                return locations
        except (json.JSONDecodeError, KeyError):
            pass

        return []

    async def geocode_location(
        self,
        location_name: str,
        city: str = "",
    ) -> LocationResult | None:
        """Geocode a location using Photon (OpenStreetMap-based)."""
        query = f"{location_name}, {city}" if city else location_name

        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://photon.komoot.io/api/",
                params={"q": query, "limit": 1, "lang": "de"},
                timeout=10.0,
            )

            if response.status_code != 200:
                return None

            data = response.json()
            features = data.get("features", [])

            if not features:
                return None

            feature = features[0]
            coords = feature.get("geometry", {}).get("coordinates", [])
            props = feature.get("properties", {})

            if len(coords) >= 2:
                return LocationResult(
                    name=props.get("name", location_name),
                    address=props.get("street"),
                    longitude=coords[0],
                    latitude=coords[1],
                    confidence=1.0,
                )

        return None

    async def extract_and_geocode_locations(
        self,
        text: str,
        city: str = "",
    ) -> list[LocationResult]:
        """Extract locations from text and geocode them."""
        locations = await self.extract_locations(text, city)

        geocoded = []
        for loc in locations:
            result = await self.geocode_location(loc.name, city)
            if result:
                geocoded.append(result)
            else:
                geocoded.append(loc)

        return geocoded

    async def parse_search_query(
        self,
        query: str,
    ) -> dict[str, Any]:
        """Parse a natural language search query into structured filters."""
        system_prompt = """Analysiere die Suchanfrage und extrahiere strukturierte Filter.

Antworte NUR im JSON-Format:
{
  "keywords": ["relevante", "suchbegriffe"],
  "paper_type": "Antrag|Vorlage|Beschluss|null",
  "date_from": "YYYY-MM-DD oder null",
  "date_to": "YYYY-MM-DD oder null",
  "organization": "Name des Gremiums oder null",
  "person": "Name der Person oder null"
}"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Suchanfrage: {query}"},
        ]

        response = await self._call_groq(messages, temperature=0.1, max_tokens=300)

        try:
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

        return {"keywords": query.split()}


# Singleton instance
ai_service = AIService()
