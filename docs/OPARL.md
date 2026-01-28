# OParl-Dokumentation

## Was ist OParl?

OParl ist ein standardisiertes Datenaustauschformat für Ratsinformationen in Deutschland. Es ermöglicht den einheitlichen Zugriff auf kommunalpolitische Daten verschiedener Städte und Gemeinden.

**Offizielle Spezifikation:** https://oparl.org/spezifikation/

## OParl-Entitäten

### System

Der Einstiegspunkt einer OParl-API.

```json
{
  "id": "https://example.oparl.org/oparl/v1",
  "type": "https://schema.oparl.org/1.1/System",
  "name": "Ratsinformationssystem Example",
  "body": "https://example.oparl.org/oparl/v1/body"
}
```

### Body

Eine Körperschaft/Kommune.

```json
{
  "id": "https://example.oparl.org/oparl/v1/body/1",
  "type": "https://schema.oparl.org/1.1/Body",
  "name": "Stadt Example",
  "shortName": "Example",
  "website": "https://www.example.de",
  "meeting": "https://example.oparl.org/oparl/v1/body/1/meeting",
  "paper": "https://example.oparl.org/oparl/v1/body/1/paper",
  "person": "https://example.oparl.org/oparl/v1/body/1/person",
  "organization": "https://example.oparl.org/oparl/v1/body/1/organization"
}
```

### Meeting

Eine Sitzung (Rat, Ausschuss, etc.).

```json
{
  "id": "https://example.oparl.org/oparl/v1/meeting/123",
  "type": "https://schema.oparl.org/1.1/Meeting",
  "name": "47. Sitzung des Stadtrates",
  "start": "2024-01-15T18:00:00+01:00",
  "end": "2024-01-15T21:00:00+01:00",
  "location": {
    "description": "Ratssaal im Rathaus"
  },
  "agendaItem": "https://example.oparl.org/oparl/v1/meeting/123/agendaItem"
}
```

### Paper

Eine Vorlage/ein Vorgang.

```json
{
  "id": "https://example.oparl.org/oparl/v1/paper/456",
  "type": "https://schema.oparl.org/1.1/Paper",
  "name": "Antrag: Ausbau des Radwegenetzes",
  "reference": "BV/2024/0123",
  "paperType": "Beschlussvorlage",
  "date": "2024-01-10",
  "mainFile": {
    "id": "https://example.oparl.org/oparl/v1/file/789",
    "accessUrl": "https://example.oparl.org/files/BV-2024-0123.pdf"
  }
}
```

### Person

Ein Ratsmitglied.

```json
{
  "id": "https://example.oparl.org/oparl/v1/person/42",
  "type": "https://schema.oparl.org/1.1/Person",
  "name": "Max Mustermann",
  "familyName": "Mustermann",
  "givenName": "Max",
  "title": "Dr.",
  "email": "max.mustermann@example.de"
}
```

### Organization

Ein Gremium oder eine Fraktion.

```json
{
  "id": "https://example.oparl.org/oparl/v1/organization/5",
  "type": "https://schema.oparl.org/1.1/Organization",
  "name": "Ausschuss für Verkehr und Mobilität",
  "shortName": "Verkehrsausschuss",
  "organizationType": "Ausschuss"
}
```

### AgendaItem

Ein Tagesordnungspunkt.

```json
{
  "id": "https://example.oparl.org/oparl/v1/agendaItem/1001",
  "type": "https://schema.oparl.org/1.1/AgendaItem",
  "number": "TOP 5",
  "name": "Ausbau des Radwegenetzes",
  "public": true,
  "result": "einstimmig angenommen",
  "consultation": {
    "paper": "https://example.oparl.org/oparl/v1/paper/456"
  }
}
```

### File

Ein Dokument (PDF, etc.).

```json
{
  "id": "https://example.oparl.org/oparl/v1/file/789",
  "type": "https://schema.oparl.org/1.1/File",
  "name": "Beschlussvorlage BV/2024/0123",
  "fileName": "BV-2024-0123.pdf",
  "mimeType": "application/pdf",
  "size": 245760,
  "accessUrl": "https://example.oparl.org/files/BV-2024-0123.pdf"
}
```

## Mapping zu Mandari

| OParl | Mandari | Tabelle |
|-------|---------|---------|
| System | OParlSource | `oparl_sources` |
| Body | OParlBody | `oparl_bodies` |
| Meeting | OParlMeeting | `oparl_meetings` |
| Paper | OParlPaper | `oparl_papers` |
| Person | OParlPerson | `oparl_persons` |
| Organization | OParlOrganization | `oparl_organizations` |
| AgendaItem | OParlAgendaItem | `oparl_agenda_items` |
| File | OParlFile | `oparl_files` |

## Sync-Strategien

### Vollständiger Sync

Lädt alle Daten von der OParl-Quelle herunter.

```bash
uv run python -m src.main sync --source <url> --full
```

### Inkrementeller Sync

Lädt nur geänderte Objekte (basierend auf `modified`-Timestamp).

```bash
uv run python -m src.main sync --source <url>
```

### ETag-Caching

Der Ingestor nutzt ETags und If-Modified-Since Header, um Bandbreite zu sparen.

## Bekannte OParl-Endpunkte

Eine Liste öffentlicher OParl-APIs:

- https://dev.oparl.org/api/bodies
- Weitere auf https://oparl.org/implementierungen/

## Datenqualität

OParl-Daten können in Qualität variieren. Häufige Probleme:

- Fehlende Pflichtfelder
- Inkonsistente Zeitformate
- Nicht erreichbare Dokument-URLs
- Unvollständige Beziehungen

Der Mandari-Ingestor versucht, mit diesen Problemen umzugehen und protokolliert Validierungsfehler.
