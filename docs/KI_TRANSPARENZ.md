# KI-Transparenz nach EU AI Act Art. 50

Stand: Issue #938 (10/2026). Die Transparenzpflichten aus Art. 50 gelten seit dem 02.08.2026. Für Systeme, die vorher
in Verkehr gebracht wurden, gilt die Kennzeichnungspflicht nach Abs. 2 ab dem 02.12.2026. Die rechtliche Bewertung
ist eine Einschätzung aus der Entwicklung. Die Bestätigung durch die Kanzlei steht noch aus.

## Was Art. 50 verlangt

- **Abs. 1 (Anbieter):** Wer mit einer KI interagiert, erfährt das, sofern es nicht offensichtlich ist.
- **Abs. 2 (Anbieter):** Systeme, die synthetischen Text erzeugen, kennzeichnen ihre Ausgaben maschinenlesbar als
  KI-erzeugt. Ausgenommen sind unterstützende Funktionen für die übliche Bearbeitung, die den Inhalt nicht wesentlich
  verändern.
- **Abs. 4 (Betreiber):** Wer KI-Text veröffentlicht, um die Öffentlichkeit über Angelegenheiten von öffentlichem
  Interesse zu informieren, legt offen, dass er KI-erzeugt ist. Das entfällt, wenn ein Mensch den Text geprüft hat
  und jemand redaktionell verantwortlich ist.
- Freie und quelloffene KI-Systeme sind von der Verordnung weitgehend ausgenommen, **Art. 50 gilt für sie aber**
  (Art. 2 Abs. 12). Die Pflichten treffen deshalb auch den Selbstbetrieb von mandari.

## Rollen

mandari baut die KI-Funktionen selbst. Dazu nutzt mandari ein Sprachmodell eines Anbieters aus der EU über eine
OpenAI-kompatible Schnittstelle (`apps.common.ki_anbieter`). mandari ist damit **Anbieter** der KI-Funktionen
nach Abs. 1 und 2. Das gilt auch für die quelloffene Software, die Dritte selbst betreiben.

**Betreiber** nach Abs. 4 ist, wer die Plattform einsetzt und KI-Text veröffentlicht. Auf mandari.de ist das für
Insight mandari selbst. Bei Work ist es die Fraktion bzw. Organisation. Betreibt eine Kommune oder Partei mandari
selbst, ist sie Betreiberin. Die Kennzeichnung ist in der Software eingebaut, damit Betreiber sie nicht selbst
nachrüsten müssen.

## Bestandsaufnahme

| Funktion | Code | Wer sieht den Text | Menschliche Prüfung | Rolle mandari | Kennzeichnung |
|----------|------|--------------------|---------------------|---------------|---------------|
| Zusammenfassung eines Vorgangs (Insight) | `insight_ai/services/summarizer.py`, `insight_core/views/papers.py`, `templates/partials/paper_summary.html` | **öffentlich:** Vorgangsseite, Description und JSON-LD der Seite, OParl-API, MCP-Werkzeug `vorgang` | keine; entsteht auf Abruf durch Besucher und wird gespeichert | Anbieter (Abs. 2) und auf mandari.de Betreiber (Abs. 4) | sichtbar: „Mit KI aus den Dokumenten erstellt. Die Zusammenfassung kann Fehler enthalten.“ und „KI-Zusammenfassung:“ in Description/JSON-LD; maschinenlesbar: `data-ai-generated="true"`, `mandari:summaryAiGenerated`, `zusammenfassung_ki_erzeugt` |
| Dieselbe Zusammenfassung in Work | `templates/work/ris/neu/vorgang.html`, `templates/work/ris/paper_detail.html` | intern: Mitglieder der Organisation | keine | Anbieter (Abs. 2) | sichtbar wie oben (seit #938 auch im bisherigen Rahmen); `data-ai-generated="true"` |
| KI-Assistent (Insight) | `insight_core/views/chat.py`, `insight_ai/services/chat_service.py`, `templates/partials/chat_nachrichten.html` | nur die fragende Person; Antworten werden nicht gespeichert | keine (Antwort an eine Person, keine Veröffentlichung) | Anbieter (Abs. 1 und 2) | sichtbar: Seitentitel „KI-Assistent“, Einwilligung mit Anbieter und Ort, Satz „Antworten erstellt eine KI aus öffentlichen Ratsdokumenten. Sie können Fehler enthalten.“; maschinenlesbar: `data-ai-generated="true"` an jeder Antwort, Feld `ai_generated: true` in `POST /insight/chat/api/message/` |
| Fragen an die Ratsdaten (Work) | `apps/work/ris/fragen.py`, `templates/work/ris/partials/frage_antwort.html` | intern: die fragende Person | keine (nur für die eigene Recherche) | Anbieter (Abs. 1 und 2) | sichtbar: „KI-Antwort aus den öffentlichen Ratsdaten – maßgeblich sind die Quellen.“; `data-ai-generated="true"` |
| Schreibhilfe und Co-Editor (Work) | `apps/work/motions/services.py`, `apps/work/motions/views/actions.py`, `templates/work/motions/partials/_editor_ai_panel.html` | intern: die schreibende Person im KI-Bereich des Editors | ja: Vorschläge werden erst mit „Übernehmen“ Teil des Dokuments, das die Fraktion verantwortet | Anbieter (Abs. 1 und 2); die Fraktion ist Betreiberin, Abs. 4 entfällt durch Prüfung und redaktionelle Verantwortung | sichtbar: KI-Bereich mit Hinweis „Ich bin Ihr KI-Assistent“; maschinenlesbar: Feld `ai_generated: true` in `POST /work/<org>/documents/ai/`, `data-ai-generated` an jeder KI-Nachricht |
| KI-Verortung (Insight) | `insight_core/services/georeferencing.py` | öffentlich als Ort auf der Karte | keine | kein synthetischer Text: Die KI zieht Ortsangaben aus dem vorhandenen Text, die Herkunft steht als `source="ai"` („KI-Extraktion“) an der Verortung | nicht nach Art. 50 Abs. 2 erforderlich |
| KI-Protokollentwurf (Aufzeichnung) | `apps/minutes/models.py` (`ProtocolDraft`) | noch keine Ausgabe: nur Datenmodell, keine Oberfläche, keine API | vorgesehen: Ein Entwurf wird erst durch Übernahme der Protokollführung Teil der Niederschrift (`accepted_at`) | Anbieter (Abs. 2) | folgt mit der Oberfläche; Entwurfsansicht und API brauchen dann dasselbe Merkmal |
| Texterkennung (OCR) | `hub/ris/erkennung.py`, `shared/mandari_dokumente` | öffentlich als Dokumenttext | – | kein synthetischer Text: OCR gibt vorhandenen Text wieder | nicht erforderlich |

Session (Verwaltungs-RIS) erzeugt keinen Text mit KI.

## Maschinenlesbare Merkmale

- **HTML:** `data-ai-generated="true"` steht am Element, das genau den KI-Text enthält, nicht an Überschrift,
  Hinweis oder Quellenliste. Ein eigenes Microdata- oder Schema.org-Merkmal für KI-Text gibt es derzeit nicht. Legt
  ein Verhaltenskodex nach Art. 50 Abs. 7 ein Format fest, ergänzen wir es hier.
- **Description und JSON-LD der Vorgangsseite:** Suchergebnisse und Vorschaukarten zeigen den Text ohne die Seite.
  Die Kennzeichnung steht deshalb im Text selbst: „KI-Zusammenfassung: …“ (`insight_core.seo.KI_ZUSAMMENFASSUNG_PRAEFIX`).
- **OParl-API:** `mandari:summaryAiGenerated: true` neben `mandari:summary` (Abbildung `hub/ris/mapping/bestand.py`,
  Version 3). `mandari:summary` bleibt unverändert ein Text.
- **JSON-Schnittstellen:** `ai_generated: true` in den Antworten des KI-Assistenten und der Schreibhilfe. Das MCP-
  bzw. Chat-Werkzeug `vorgang` liefert `zusammenfassung_ki_erzeugt: true`, wenn eine Zusammenfassung vorhanden ist.

Alle Merkmale ergänzen die bestehenden Antworten; kein Feld ist entfallen oder hat seinen Typ geändert.

## Menschliche Prüfung als Arbeitsschritt

- **Insight-Zusammenfassungen:** Eine Freigabe vor der Anzeige würde Redaktion für tausende Vorgänge je Kommune
  verlangen. Die Zusammenfassungen bleiben deshalb ungeprüft und gekennzeichnet. Eine Freigabe wäre eine neue
  Funktion und ist nicht Teil von #938.
- **Schreibhilfe:** Die Übernahme durch die schreibende Person ist die Prüfung. Das Dokument verantwortet danach
  die Fraktion.
- **KI-Protokoll:** Die Übernahme durch die Protokollführung ist im Datenmodell vorgesehen und bleibt Pflicht,
  wenn die Funktion eine Oberfläche bekommt.

## Tests

`insight_core/tests/test_ki_kennzeichnung.py`, `insight_core/tests/test_seo_titel.py` (Description, JSON-LD),
`insight_core/tests/test_ki_buergerportal.py` (KI-Assistent), `hub/ris/tests/test_mapping_bestand.py` (OParl),
`insight_ai/tests/test_chat_werkzeuge.py` (Werkzeug `vorgang`), `apps/work/ris/tests/test_fragen.py`,
`apps/work/ris/tests/test_neues_design.py` (Vorgangsseite in Work), `apps/work/motions/tests/test_ki_europa.py`
(Schreibhilfe).
