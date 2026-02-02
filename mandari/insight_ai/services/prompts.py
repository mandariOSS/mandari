# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prompt templates for AI services.

Multi-perspective analysis prompts for German municipal politics.
"""

# System prompt for multi-perspective document analysis
PAPER_SUMMARY_SYSTEM_PROMPT = """Du bist ein Experte für deutsche Kommunalpolitik. Analysiere das Dokument aus verschiedenen Perspektiven und erstelle eine umfassende, verständliche Zusammenfassung.

PERSPEKTIVEN:
1. **Bürger:innen-Perspektive**: Was bedeutet das für die Menschen vor Ort?
2. **Politische Perspektive**: Welche Positionen und Interessen sind erkennbar?
3. **Sachliche Perspektive**: Was sind die Fakten und Kernpunkte?
4. **Finanzielle Perspektive**: Welche Kosten oder Einsparungen entstehen?
5. **Zeitliche Perspektive**: Was ist der Zeitrahmen, was passiert als nächstes?

STRUKTUR DEINER ANTWORT:
1. **Kernaussage** (2-3 Sätze): Worum geht es im Wesentlichen?
2. **Detailanalyse** (5-7 Stichpunkte): Die wichtigsten Aspekte aus allen Perspektiven
3. **Relevanz für Bürger:innen** (2-3 Sätze): Was sollten Einwohner wissen?

REGELN:
- Schreibe in verständlichem Deutsch (keine Behördensprache)
- Erkläre Fachbegriffe kurz in Klammern
- Bleibe neutral und objektiv
- Erfinde KEINE Informationen
- Länge: 200-400 Wörter"""


def build_paper_summary_user_prompt(
    paper_name: str,
    paper_type: str | None,
    reference: str | None,
    date: str | None,
    text_content: str,
) -> str:
    """
    Build the user prompt for paper summarization.

    Args:
        paper_name: Name of the paper/document
        paper_type: Type of paper (e.g., "Antrag", "Vorlage")
        reference: Reference number (Aktenzeichen)
        date: Date of the paper
        text_content: Combined text content from all files

    Returns:
        Formatted user prompt string
    """
    # Build metadata section
    metadata_lines = [f"## Dokument: {paper_name}"]

    if paper_type:
        metadata_lines.append(f"**Typ:** {paper_type}")
    if reference:
        metadata_lines.append(f"**Aktenzeichen:** {reference}")
    if date:
        metadata_lines.append(f"**Datum:** {date}")

    metadata = "\n".join(metadata_lines)

    return f"""{metadata}

## DOKUMENTINHALT:
{text_content}

---
Analysiere dieses kommunalpolitische Dokument aus den verschiedenen Perspektiven."""
