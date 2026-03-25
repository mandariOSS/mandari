# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Chat service with RAG context from Elasticsearch.

Handles:
- RAG context building from Elasticsearch search results
- Token budget management
- Chat completion via NebiusProvider
"""

import logging
from typing import Any

from insight_ai.providers.base import ChatMessage
from insight_ai.providers.nebius import NebiusProvider
from insight_ai.services.prompts import CHAT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Token budget constants
MAX_TOTAL_TOKENS = 32000
SYSTEM_PROMPT_TOKENS = 500
MAX_RAG_TOKENS = 20000
MAX_HISTORY_TOKENS = 8000
MAX_RESPONSE_TOKENS = 3000
SAFETY_BUFFER = 500

# Approximate chars per token for German text
CHARS_PER_TOKEN = 3


def _estimate_tokens(text: str) -> int:
    """Rough token estimate for German text."""
    return len(text) // CHARS_PER_TOKEN


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to approximately max_tokens."""
    max_chars = max_tokens * CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[...]"


def build_rag_context(query: str, body_id: str | None) -> tuple[str, list[dict]]:
    """
    Search relevant documents via Elasticsearch and build RAG context.

    Args:
        query: User's question
        body_id: UUID of the active municipality (or None)

    Returns:
        Tuple of (context_text, sources_list)
    """
    try:
        from insight_core.services.search_service import get_search_service

        search_service = get_search_service()
        result = search_service.search_all(
            query=query,
            body_id=body_id,
            page=1,
            page_size=5,
        )
    except Exception as e:
        logger.warning(f"Elasticsearch RAG search failed: {e}")
        return "", []

    hits = result.get("results", [])
    if not hits:
        return "", []

    context_parts = []
    sources = []

    for hit in hits:
        hit_type = hit.get("type", hit.get("_index", "").rstrip("s"))
        title = hit.get("name") or hit.get("file_name") or "Unbekannt"
        hit_id = hit.get("id", "")

        # Build URL based on type
        url = ""
        if hit_type == "paper":
            url = f"/insight/vorgaenge/{hit_id}/"
        elif hit_type == "meeting":
            url = f"/insight/termine/{hit_id}/"
        elif hit_type == "organization":
            url = f"/insight/gremien/{hit_id}/"
        elif hit_type == "person":
            url = f"/insight/personen/{hit_id}/"
        elif hit_type == "file":
            paper_id = hit.get("paper_id")
            if paper_id:
                url = f"/insight/vorgaenge/{paper_id}/"

        # Build context snippet
        snippet_parts = [f"### {title}"]
        if hit.get("reference"):
            snippet_parts.append(f"Aktenzeichen: {hit['reference']}")
        if hit.get("paper_type"):
            snippet_parts.append(f"Typ: {hit['paper_type']}")
        if hit.get("date") or hit.get("start"):
            snippet_parts.append(f"Datum: {hit.get('date') or hit.get('start', '')}")

        # Add text content (from file or cropped content)
        text_content = hit.get("text_content") or hit.get("text_preview") or ""
        if text_content:
            # Limit per-document text to ~4000 tokens
            text_content = _truncate_to_tokens(text_content, 4000)
            snippet_parts.append(f"\n{text_content}")

        context_parts.append("\n".join(snippet_parts))

        if url:
            sources.append(
                {
                    "title": title[:100],
                    "url": url,
                    "type": hit_type,
                }
            )

    # Join context and truncate to budget
    context_text = "\n\n---\n\n".join(context_parts)
    context_text = _truncate_to_tokens(context_text, MAX_RAG_TOKENS)

    return context_text, sources


def _build_history_messages(history: list[dict], max_tokens: int) -> list[ChatMessage]:
    """
    Convert chat history to ChatMessage list, truncating oldest first.

    Args:
        history: List of {role, content} dicts
        max_tokens: Maximum token budget for history

    Returns:
        List of ChatMessage objects within budget
    """
    if not history:
        return []

    # Keep only last 6 messages (3 turns)
    recent = history[-6:]

    # Estimate total tokens
    total = sum(_estimate_tokens(m.get("content", "")) for m in recent)

    # Remove oldest messages if over budget
    while total > max_tokens and len(recent) > 0:
        removed = recent.pop(0)
        total -= _estimate_tokens(removed.get("content", ""))

    return [
        ChatMessage(role=m["role"], content=m["content"])
        for m in recent
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]


def process_chat_message(
    message: str,
    history: list[dict],
    body_id: str | None,
) -> dict[str, Any]:
    """
    Process a chat message with RAG context and AI completion.

    Args:
        message: User's message
        history: Chat history [{role, content}, ...]
        body_id: Active municipality UUID (str or None)

    Returns:
        {
            "response": "markdown text",
            "sources": [{"title": ..., "url": ..., "type": ...}],
            "tokens_used": int,
        }

    Raises:
        ValueError: If the AI provider is not configured
    """
    provider = NebiusProvider()
    if not provider.is_available():
        raise ValueError("KI-Assistent ist nicht konfiguriert. Bitte setzen Sie den NEBIUS_API_KEY.")

    # 1. Build RAG context from Elasticsearch
    rag_context, sources = build_rag_context(message, body_id)

    # 2. Build system prompt with RAG context
    if rag_context:
        system_content = f"{CHAT_SYSTEM_PROMPT}\n\n## RELEVANTE DOKUMENTE\n\n{rag_context}"
    else:
        system_content = (
            f"{CHAT_SYSTEM_PROMPT}\n\n"
            "Hinweis: Es wurden keine relevanten Dokumente gefunden. "
            "Antworte basierend auf allgemeinem Wissen über deutsche Kommunalpolitik."
        )

    # 3. Build message list
    messages = [ChatMessage(role="system", content=system_content)]

    # 4. Add history (within token budget)
    history_messages = _build_history_messages(history, MAX_HISTORY_TOKENS)
    messages.extend(history_messages)

    # 5. Add current user message
    messages.append(ChatMessage(role="user", content=message))

    # 6. Call AI provider
    response = provider.chat_completion(
        messages=messages,
        max_tokens=MAX_RESPONSE_TOKENS,
        temperature=0.3,
    )

    return {
        "response": response.content,
        "sources": sources,
        "tokens_used": response.total_tokens,
    }
