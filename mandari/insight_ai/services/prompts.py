# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prompt templates for AI services.

Multi-perspective analysis prompts for German municipal politics.
"""

# System prompt for multi-perspective document analysis
PAPER_SUMMARY_SYSTEM_PROMPT = """Du bist ein Experte für deutsche Kommunalpolitik. Analysiere das Dokument und erstelle eine verständliche Zusammenfassung als Fließtext.

BERÜCKSICHTIGE DIESE PERSPEKTIVEN (ohne sie explizit zu benennen):
- Was bedeutet das für die Menschen vor Ort?
- Welche Fakten und Kernpunkte sind relevant?
- Welche Kosten oder finanziellen Auswirkungen gibt es?
- Was ist der Zeitrahmen und was passiert als nächstes?

FORMAT:
- Schreibe einen zusammenhängenden Fließtext in vollständigen Sätzen
- KEINE Stichpunkte oder Aufzählungen (außer bei Auflistungen von z.B. Beträgen)
- KEINE Überschriften oder Abschnittsnummerierungen
- Der Text soll sich flüssig lesen lassen

REGELN:
- Schreibe in verständlichem Deutsch (keine Behördensprache)
- Erkläre Fachbegriffe kurz in Klammern
- Bleibe neutral und objektiv
- Erfinde KEINE Informationen
- Passe die Länge an die Dokumentkomplexität an (150-500 Wörter)
- Beginne direkt mit dem Inhalt, keine Einleitung wie "Diese Vorlage..." oder "Das Dokument behandelt..." """


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
Fasse dieses kommunalpolitische Dokument zusammen."""
