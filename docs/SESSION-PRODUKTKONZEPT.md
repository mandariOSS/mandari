# Mandari Session - Produktkonzept und Prozessdokumentation

**Version:** 1.0
**Stand:** Januar 2026
**Dokumenttyp:** Produktboard & Prozessspezifikation

---

## Inhaltsverzeichnis

1. [Executive Summary](#1-executive-summary)
2. [Marktanalyse und Konkurrenz](#2-marktanalyse-und-konkurrenz)
3. [OParl-Spezifikation und Nutzung](#3-oparl-spezifikation-und-nutzung)
4. [API-Architektur: OParl vs. Session-API](#4-api-architektur-oparl-vs-session-api)
5. [Kernprozesse im RIS](#5-kernprozesse-im-ris)
6. [Nichtöffentliche Dokumente](#6-nichtöffentliche-dokumente)
7. [Funktionsmodule](#7-funktionsmodule)
8. [Integrationen und Schnittstellen](#8-integrationen-und-schnittstellen)
9. [Compliance und Rechtliche Anforderungen](#9-compliance-und-rechtliche-anforderungen)
10. [Produktboard und Roadmap](#10-produktboard-und-roadmap)

---

## 1. Executive Summary

### Vision

Mandari Session ist ein modernes, offenes Ratsinformationssystem (RIS) für kommunale Verwaltungen. Es vereint die Stärken etablierter Systeme wie ALLRIS, regisafe und Somacos mit einer offenen API-First-Architektur und nahtloser Integration in das Mandari-Ökosystem.

### Differenzierung

| Aspekt | Proprietäre RIS | Mandari Session |
|--------|-----------------|-----------------|
| Lizenz | Closed Source | Open Source (AGPL-3.0) |
| API | Teilweise OParl | OParl + erweiterte Session-API |
| Vendor Lock-in | Hoch | Keiner |
| Work-Integration | Keine | Native Integration |
| KI-Features | Teilweise | Vollständig integriert |

### Kernprinzipien

1. **OParl First**: Maximale Nutzung des OParl-Standards
2. **Erweiterte Session-API**: Für Features jenseits von OParl
3. **Verschlüsselte Kommunikation**: Für nichtöffentliche Inhalte
4. **Prozessorientierung**: Workflows, nicht nur Datenhaltung

---

## 2. Marktanalyse und Konkurrenz

### 2.1 Marktübersicht

In Deutschland existieren ca. 15 verschiedene RIS-Systeme. Die fünf wichtigsten Anbieter:

| Anbieter | Produkt | Kunden | Stärken |
|----------|---------|--------|---------|
| CC e-gov | ALLRIS 4 | 700+ | Workflow, HKR-Integration |
| regisafe | Ratsinformation | 500+ | BSI-Zertifizierung, KI-Assistent |
| SOMACOS | Session/SessionNet | 2.200+ | Mandatos-App, 15+ Jahre Erfahrung |
| STERNBERG | SD.NET | 400+ | Sitzungsgeldabrechnung |
| KOMMUNE-AKTIV | Sitzungsdienst | 300+ | Preis-Leistung für kleine Kommunen |

### 2.2 Detaillierte Konkurrenzanalyse

#### ALLRIS 4 (CC e-gov)

**Quellen:** [CC e-gov Website](https://www.cc-egov.de/allris-4/), [Produktbeschreibung PDF](https://www.k21media.de/_files/mod_itguide/CC_Produkt_ALLRIS.pdf)

**Kernfunktionen:**
- Vollständige Sitzungsverwaltung (Planung → Nachbereitung)
- Vorlagenerstellung mit integriertem Texteditor oder MS Word
- Vordefinierte und Ad-hoc-Workflows
- Automatisierte E-Mail-Benachrichtigungen
- Mitzeichnung und Freigabe von Vorlagen
- Tagesordnungsabstimmung
- Niederschriftserstellung
- Beschlussverfolgung mit Workflows

**Module:**
- Sitzungsbearbeitung
- Sitzungsgeld mit HKR-Schnittstelle
- Workflow-Management
- Vorlagenerstellung
- Antragsverfahren
- Beschlussverfolgung
- Online-Abstimmung (gpaNRW-zertifiziert)
- Zwei-Faktor-Authentifizierung

**Stärken:**
- Marktführer mit 700+ Kommunen
- Direkte HKR-Verknüpfung für Sitzungsgelder
- Flexible Anpassung an unterschiedliche Abläufe
- Native Apps für Android, iOS, Windows

**Schwächen:**
- Proprietäres System, hoher Vendor Lock-in
- Keine native Integration für Fraktionsarbeit
- Komplexe Lizenzstruktur

---

#### regisafe Ratsinformation

**Quellen:** [regisafe Website](https://www.regisafe.de/produkt/ratsinformationssystem/), [Sitzungsassistent](https://www.regisafe.de/sitzungsassistent-mit-ki/)

**Kernfunktionen:**
- Zentrale Verwaltung von Ratssitzungsdaten
- Automatische Veröffentlichung
- Integrierte Protokollerstellung
- Sitzungsgeldabrechnung

**Besondere Features:**
- **KI-Sitzungsassistent**: Automatische Transkription von Audioaufnahmen, strukturierte Zusammenfassungen pro TOP
- **BSI-Zertifizierung**: Geprüfte Informationssicherheit
- **BITV-Zertifizierung**: Barrierefreiheit
- Mobile Apps mit Offline-Funktion

**Dokumentenmanagement:**
- Gesamtdokument zu TOPs oder Sitzung generieren
- Dokumentenfreigabe
- Kollaborative Bearbeitung
- Persönliche Notizen mit Freigabeoptionen

**Stärken:**
- KI-Integration (Protokollassistent)
- Hohe Sicherheitsstandards (BSI-zertifiziert)
- Barrierefreiheit zertifiziert

**Schwächen:**
- Fokus auf öffentliche Darstellung
- Begrenzte Workflow-Flexibilität

---

#### SOMACOS Session/SessionNet/Mandatos

**Quellen:** [SOMACOS Website](https://somacos.de/), [Session Funktionen](https://somacos.de/loesungen/sitzungsmanagement/session/)

**Produktsuite:**
1. **Session** (Kernprodukt): Sitzungsmanagement für Verwaltung
2. **SessionNet**: Web-Portal für Bürger und Mandatsträger
3. **Mandatos**: Native Apps für Mandatsträger

**Session-Kernfunktionen:**
- Kompletter Sitzungsworkflow
- Vorlagenverarbeitung
- Entscheidungskontrolle und Projektverfolgung
- Aufwandsentschädigung
- Integration mit Textverarbeitungsanwendungen
- "Session Today" Dashboard
- Workflow-Assistent

**SessionNet-Features:**
- OParl 1.0 Webservice-Schnittstelle
- Rollenbasierte Zugriffsteuerung (Intranet/Extranet/Internet)
- Volltextsuche

**Mandatos-App:**
- Offline-Zugriff
- Verschlüsselte Speicherung
- Elektronische Annotationen
- Automatische Synchronisation
- Integrierte Online-Abstimmung (gpaNRW-zertifiziert)
- Anwesenheitsmanagement
- Multi-Mandat-Unterstützung

**Besondere Module:**
- **Session Projekte**: Beschlusscontrolling
- **Session Sitzungsgeld**: SEPA-Export, Finanzschnittstellen
- **Druckmanagement**: Digitale Sitzungsmappe

**Stärken:**
- Größter Kundenstamm (2.200+)
- Ausgereifte App-Lösung (Mandatos)
- Umfassende Sicherheitsfeatures

**Schwächen:**
- Komplexe Produktstruktur
- Höhere Einstiegshürde

---

#### STERNBERG SD.NET

**Quellen:** [Sternberg Website](https://www.sitzungsdienst.net/)

**Kernfunktionen:**
- Tagesordnungserstellung
- Vorlagenverwaltung
- Einladungsversand (digital/print)
- Protokollführung
- Beschlusskontrolle
- Fristenmanagement

**Sitzungsgeldabrechnung:**
- Automatische Anwesenheitslisten aus Protokollführung
- Aufwandsentschädigungen, Sitzungsgelder, Fahrtkosten
- Verdienstausfälle und sonstige Kosten
- Integration mit Finanzverfahren

**Workflow-Modul:**
- Freigabestatus: persönlich → Entwurf → Durchlauf → vorläufige Freigabe → Freigabe → Veröffentlichung → Archivierung
- Definition der Beteiligten mit Aktionen (Kenntnisnahme, Stellungnahme, Bearbeitung, Freigabe)

**Stellvertreterregelung:**
- 1., 2. und 3. persönliche oder allgemeine Stellvertreter

**Stärken:**
- Sehr gute Sitzungsgeldabrechnung
- Flexible Stellvertreterregelung
- KI-gestützte Protokollierung

---

#### KOMMUNE-AKTIV

**Quellen:** [Kommune-Aktiv Website](https://www.kommune-aktiv.de/)

**Kernfunktionen:**
- Ratsinformationssystem
- Bürgerinformationssystem
- Beschlussverfolgung als Aufgabenorganisation
- Digitale Akte

**Beschlussverfolgung:**
- Zuständigkeiten klären
- Fristen im Blick behalten
- Umsetzungen dokumentieren

**Stärken:**
- Preis-Leistung für kleine Kommunen
- Einfache Bedienung
- Modularer Aufbau

---

### 2.3 Feature-Matrix

| Feature | ALLRIS | regisafe | SOMACOS | SD.NET | Mandari |
|---------|--------|----------|---------|--------|---------|
| Sitzungsplanung | ✓ | ✓ | ✓ | ✓ | ✓ |
| Tagesordnungserstellung | ✓ | ✓ | ✓ | ✓ | ✓ |
| Vorlagenverwaltung | ✓ | ✓ | ✓ | ✓ | ✓ |
| Workflow-Management | ✓ | ○ | ✓ | ✓ | ✓ |
| Mitzeichnung/Freigabe | ✓ | ○ | ✓ | ✓ | ✓ |
| Protokollerstellung | ✓ | ✓ | ✓ | ✓ | ✓ |
| KI-Protokollassistent | ○ | ✓ | ○ | ✓ | ✓ |
| Audio-Transkription | ○ | ✓ | ○ | ○ | ✓ |
| Beschlussverfolgung | ✓ | ○ | ✓ | ✓ | ✓ |
| Online-Abstimmung | ✓ | ○ | ✓ | ○ | ✓ |
| gpaNRW-Zertifizierung | ✓ | ○ | ✓ | ○ | Geplant |
| Sitzungsgeldabrechnung | ✓ | ✓ | ✓ | ✓ | ✓ |
| HKR-Schnittstelle | ✓ | ○ | ✓ | ✓ | ✓ |
| OParl-Export | ✓ | ✓ | ✓ | ✓ | ✓ |
| Native Mobile Apps | ✓ | ✓ | ✓ | ○ | PWA |
| Offline-Modus | ✓ | ✓ | ✓ | ○ | ✓ |
| Digitale Signatur | ✓ | ○ | ○ | ○ | ✓ |
| Barrierefreiheit BITV | ○ | ✓ | ○ | ○ | ✓ |
| BSI-Zertifizierung | ○ | ✓ | ○ | ○ | Geplant |
| Open Source | ✗ | ✗ | ✗ | ✗ | ✓ |
| Work-Integration | ✗ | ✗ | ✗ | ✗ | ✓ |

**Legende:** ✓ = vorhanden, ○ = teilweise/optional, ✗ = nicht vorhanden

---

## 3. OParl-Spezifikation und Nutzung

### 3.1 OParl-Entitäten (Version 1.1)

**Quelle:** [OParl Spezifikation](https://oparl.org/spezifikation/online-ansicht/)

OParl definiert folgende Objekttypen:

#### System
- `id`, `type`, `oparlVersion`, `body`

#### Body (Kommune)
- `id`, `name`, `shortName`, `website`
- `contactEmail`, `contactPhone`
- `organization`, `person`, `meeting`, `paper` (externe Listen)
- `legislativeTerm`, `location`
- `created`, `modified`

#### Organization (Gremium/Fraktion)
- `id`, `name`, `shortName`, `organizationType`
- `meeting`, `member`, `subOrganizationOf`
- `created`, `modified`

#### Person
- `id`, `name`, `familyName`, `givenName`
- `formOfAddress`, `gender`, `email`, `phone`, `website`
- `membership` (Array)
- `created`, `modified`

#### Membership
- `id`, `person`, `organization`, `role`
- `votingRight` (boolean)
- `startDate`, `endDate`
- `created`, `modified`

#### Meeting (Sitzung)
- `id`, `name`, `meetingType`
- `date`, `startDate`, `endDate`
- `location`, `organizationId`
- `agendaItem` (Array), `auxiliaryFile` (Array)
- `created`, `modified`

#### AgendaItem (Tagesordnungspunkt)
- `id`, `number`, `title`
- **`public` (boolean)** ← Wichtig für nichtöffentliche TOPs
- `consultation` (Array), `result`, `resolution`
- `auxiliaryFile` (Array)
- `created`, `modified`

#### Paper (Vorlage/Vorgang)
- `id`, `name`, `paperType`, `reference`, `date`
- `location`, `organizationId`, `person` (Array)
- `consultation` (Array), `auxiliaryFile` (Array)
- `created`, `modified`

#### Consultation (Beratung)
- `id`, `paper`, `agendaItem`, `organization`, `role`
- `created`, `modified`

#### File (Dokument)
- `id`, `name`, `fileName`, `mimeType`, `size`
- `accessUrl`, `downloadUrl`, `text`
- `created`, `modified`

#### Location
- `id`, `description`, `geometry` (GeoJSON)
- `created`, `modified`

#### LegislativeTerm (Wahlperiode)
- `id`, `name`, `startDate`, `endDate`
- `created`, `modified`

### 3.2 OParl-Nutzung in Mandari

**Prinzip:** OParl vollständig nutzen, bevor wir erweitern.

#### Was OParl bereits bietet:
- Vollständige Entitäten für Ratsinformation
- `public`-Flag für Öffentlichkeit von TOPs
- Referenzen zwischen Entitäten
- Paginierung und inkrementeller Sync
- Zeitstempel für Änderungsverfolgung

#### Was OParl NICHT bietet:
- Workflow-Status (Entwurf, Freigabe, etc.)
- Anwesenheit und Abstimmungsergebnisse (Details)
- Sitzungsgelder und Abrechnungen
- Vertraulichkeitsstufen (nur binary public/non-public)
- Benutzer- und Rechteverwaltung
- Benachrichtigungen
- Fraktionsinterne Anmerkungen

### 3.3 OParl-Export-Strategie

```
┌─────────────────────────────────────────────────────────────┐
│                    Mandari Session                          │
│              (Vollständige Datenhaltung)                    │
└─────────────────────┬───────────────────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        │             │             │
        ▼             ▼             ▼
┌───────────┐  ┌───────────┐  ┌───────────┐
│  OParl    │  │ Session-  │  │  Externe  │
│  Export   │  │   API     │  │  Systeme  │
│ (public)  │  │ (private) │  │  (OParl)  │
└─────┬─────┘  └─────┬─────┘  └───────────┘
      │              │
      ▼              ▼
┌───────────┐  ┌───────────┐
│ Mandari   │  │ Mandari   │
│ Insight   │  │  Work     │
│ (Bürger)  │  │(Fraktion) │
└───────────┘  └───────────┘
```

**OParl-Export** (Standard, öffentlich):
- Alle öffentlichen Daten
- `public=true` TOPs
- Öffentliche Dokumente
- Standardkonform für externe Konsumenten

**Session-API** (Erweitert, authentifiziert):
- Nichtöffentliche TOPs für berechtigte Nutzer
- Workflow-Status und Freigaben
- Abstimmungsergebnisse (detailliert)
- Anwesenheitsdaten
- Sitzungsgelder
- Annotationen und Kommentare

---

## 4. API-Architektur: OParl vs. Session-API

### 4.1 Zwei-Schnittstellen-Modell

#### OParl-Schnittstelle (Port 443, /oparl/v1)

**Zweck:** Offene Daten für alle, standardkonform

```json
GET /oparl/v1/bodies/{id}/meetings
{
  "data": [
    {
      "id": "https://mandari.example.org/oparl/v1/meeting/123",
      "type": "https://schema.oparl.org/1.1/Meeting",
      "name": "42. Sitzung des Stadtrats",
      "start": "2026-02-15T18:00:00+01:00",
      "agendaItem": [
        {
          "id": ".../agendaItem/1",
          "number": "1",
          "name": "Eröffnung und Begrüßung",
          "public": true
        },
        {
          "id": ".../agendaItem/2",
          "number": "2",
          "name": "Nichtöffentlicher Tagesordnungspunkt",
          "public": false
          // Kein weiterer Inhalt für nicht-authentifizierte Anfragen
        }
      ]
    }
  ]
}
```

#### Session-API (Port 443, /api/v1/session)

**Zweck:** Erweiterte Funktionalität für authentifizierte Nutzer

```json
GET /api/v1/session/meetings/{id}
Authorization: Bearer <token>

{
  "id": "123",
  "oparl_id": "https://mandari.example.org/oparl/v1/meeting/123",
  "name": "42. Sitzung des Stadtrats",
  "status": "SCHEDULED",  // Session-spezifisch
  "start": "2026-02-15T18:00:00+01:00",

  "agenda_items": [
    {
      "id": "1",
      "number": "1",
      "name": "Eröffnung und Begrüßung",
      "public": true,
      "confidentiality_level": "PUBLIC"
    },
    {
      "id": "2",
      "number": "2",
      "name": "Personalangelegenheit Bauamt",
      "public": false,
      "confidentiality_level": "NON_PUBLIC",
      "non_public_reason": "Personalangelegenheit gem. § 35 GO",
      // Vollinhalte nur für berechtigte Nutzer
      "papers": [...],
      "consultations": [...]
    }
  ],

  // Session-spezifische Erweiterungen
  "attendance": {
    "expected": 35,
    "confirmed": 28,
    "present": null  // Erst nach Sitzungsbeginn
  },

  "invitations": {
    "sent_at": "2026-02-08T10:00:00+01:00",
    "channels": ["email", "portal"],
    "responses": {
      "accepted": 25,
      "declined": 3,
      "pending": 7
    }
  }
}
```

### 4.2 Session-API Endpunkte

#### Sitzungsverwaltung
```
GET    /api/v1/session/meetings
POST   /api/v1/session/meetings
GET    /api/v1/session/meetings/{id}
PUT    /api/v1/session/meetings/{id}
DELETE /api/v1/session/meetings/{id}

POST   /api/v1/session/meetings/{id}/invite
POST   /api/v1/session/meetings/{id}/cancel
POST   /api/v1/session/meetings/{id}/start
POST   /api/v1/session/meetings/{id}/end

GET    /api/v1/session/meetings/{id}/agenda
PUT    /api/v1/session/meetings/{id}/agenda
POST   /api/v1/session/meetings/{id}/agenda/reorder

GET    /api/v1/session/meetings/{id}/attendance
POST   /api/v1/session/meetings/{id}/attendance
PUT    /api/v1/session/meetings/{id}/attendance/{person_id}

GET    /api/v1/session/meetings/{id}/protocol
POST   /api/v1/session/meetings/{id}/protocol
POST   /api/v1/session/meetings/{id}/protocol/generate
POST   /api/v1/session/meetings/{id}/protocol/approve
```

#### Vorlagenverwaltung
```
GET    /api/v1/session/papers
POST   /api/v1/session/papers
GET    /api/v1/session/papers/{id}
PUT    /api/v1/session/papers/{id}
DELETE /api/v1/session/papers/{id}

GET    /api/v1/session/papers/{id}/workflow
POST   /api/v1/session/papers/{id}/submit
POST   /api/v1/session/papers/{id}/approve
POST   /api/v1/session/papers/{id}/reject
POST   /api/v1/session/papers/{id}/schedule

GET    /api/v1/session/papers/{id}/files
POST   /api/v1/session/papers/{id}/files
DELETE /api/v1/session/papers/{id}/files/{file_id}
```

#### Abstimmungen
```
GET    /api/v1/session/votes
POST   /api/v1/session/meetings/{meeting_id}/agenda/{item_id}/vote
GET    /api/v1/session/votes/{id}
PUT    /api/v1/session/votes/{id}/result

GET    /api/v1/session/votes/{id}/individual  // Namentliche Abstimmung
```

#### Sitzungsgelder
```
GET    /api/v1/session/allowances
POST   /api/v1/session/allowances/calculate/{meeting_id}
GET    /api/v1/session/allowances/person/{person_id}
POST   /api/v1/session/allowances/export/hkr
POST   /api/v1/session/allowances/export/sepa
```

### 4.3 Work-Integration

Mandari Work kann als Quelle entweder:
1. **OParl-Schnittstelle** eines externen RIS nutzen
2. **Session-API** von Mandari Session nutzen

```
┌─────────────────┐      ┌─────────────────┐
│  Externes RIS   │      │ Mandari Session │
│  (ALLRIS etc.)  │      │                 │
└────────┬────────┘      └────────┬────────┘
         │                        │
    OParl-API               Session-API
    (public only)        (public + non-public)
         │                        │
         └──────────┬─────────────┘
                    │
                    ▼
           ┌───────────────┐
           │  Mandari Work │
           │  (Fraktionen) │
           └───────────────┘
```

#### Work-Session-Schnittstelle (verschlüsselt)

Für nichtöffentliche Dokumente:

```python
# Verschlüsselte Anfrage für nichtöffentliche TOPs
GET /api/v1/session/work/meetings/{id}/non-public
Authorization: Bearer <organization_token>
X-Work-Organization: <org_id>

# Response ist E2E-verschlüsselt
{
  "encrypted_payload": "base64...",
  "encryption_key_id": "org-key-123",
  "algorithm": "AES-256-GCM"
}
```

**Sicherheitskonzept:**
1. Organisation in Work registriert öffentlichen Schlüssel
2. Session verschlüsselt nichtöffentliche Inhalte mit Org-Schlüssel
3. Work entschlüsselt lokal
4. Keine Klartexte auf dem Server für Transit

---

## 5. Kernprozesse im RIS

### 5.1 Prozessübersicht

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        SITZUNGSLEBENSZYKLUS                              │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   │
│  │ Planung │ → │ Vorbere-│ → │ Einla-  │ → │ Durch-  │ → │ Nachbe- │   │
│  │         │   │  itung  │   │  dung   │   │ führung │   │ reitung │   │
│  └─────────┘   └─────────┘   └─────────┘   └─────────┘   └─────────┘   │
│       │             │             │             │             │          │
│       ▼             ▼             ▼             ▼             ▼          │
│   Termin        Tages-       Versand        Anwesen-     Protokoll      │
│   festlegen     ordnung      per E-Mail/    heit,        erstellen,     │
│                 erstellen    Portal/App     Abstimmung,  freigeben,     │
│                              Sitzungs-      Beschlüsse   publizieren    │
│                              unterlagen                                  │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Prozess: Sitzungsplanung

#### Eingaben
- Gremium/Organisation
- Wahlperiode/Legislatur
- Wiederkehrende Termine (optional)

#### Schritte

1. **Termin anlegen**
   - Datum, Uhrzeit, voraussichtliche Dauer
   - Ort (Ratssaal, Sitzungsraum, ggf. virtuell)
   - Zuständiges Gremium auswählen

2. **Vorlage verwenden** (optional)
   - Sitzungsvorlage für wiederkehrende Sitzungen
   - Enthält Standard-TOPs (Eröffnung, Genehmigung Protokoll, etc.)
   - Vordefinierte Zeitfenster

3. **Status setzen**
   - `DRAFT` → `PLANNED` → `SCHEDULED`

#### Ausgaben
- Sitzungstermin im System
- Platzhalter für Tagesordnung
- Kalendereinträge für Beteiligte

#### Vereinfachungspotenzial
- **Automatische Terminfindung**: Basierend auf RRULE und Feiertagen
- **Raumverfügbarkeitsprüfung**: Integration mit Raumbuchungssystem
- **Teilnehmerverfügbarkeit**: Kalenderabgleich (optional)

---

### 5.3 Prozess: Vorlagenverwaltung

#### Status-Workflow

```
  ┌─────────┐
  │ ENTWURF │ ← Sachbearbeiter erstellt
  └────┬────┘
       │ Einreichen
       ▼
  ┌─────────┐
  │ DURCHLAUF│ ← Mitzeichnung durch Beteiligte
  └────┬────┘
       │ Alle Mitzeichnungen erfolgt
       ▼
  ┌─────────┐
  │FREIGABE │ ← Amtsleiter/Dezernent gibt frei
  └────┬────┘
       │ Zuordnung zu Sitzung
       ▼
  ┌─────────┐
  │ TERMINIERT│ ← Auf Tagesordnung
  └────┬────┘
       │ Sitzung durchgeführt
       ▼
  ┌─────────┐
  │BESCHLOSSEN│ ← Mit Beschlusstext
  └────┬────┘
       │ Veröffentlichung
       ▼
  ┌─────────────┐
  │VERÖFFENTLICHT│
  └─────────────┘
```

#### Mitzeichnungsprozess

**Quellen:** [ALLRIS Workflow](https://www.cc-egov.de/allris-4/), [Verwaltungsorganisation](https://www.verwaltung-innovativ.de/SharedDocs/Publikationen/Organisation/e_vorgangsbearbeitung.pdf)

```
Sachbearbeiter → Amtsleiter → Kämmerer → Dezernent → Bürgermeister
     (1)            (2)          (3)         (4)          (5)
```

**Konfigurierbare Optionen pro Mitzeichner:**
- **Kenntnisnahme**: Nur informell, keine Freigabe nötig
- **Stellungnahme**: Kommentar erforderlich
- **Bearbeitung**: Kann Dokument ändern
- **Freigabe**: Muss explizit freigeben

**Automatisierung:**
- E-Mail-Benachrichtigung bei neuem Durchlauf
- Erinnerung bei überschrittener Frist
- Eskalation bei Verzögerung
- Automatische Freigabe nach X Tagen (optional)

---

### 5.4 Prozess: Tagesordnungserstellung

#### Eingaben
- Geplante Sitzung
- Freigegebene Vorlagen
- Standard-TOPs aus Vorlage

#### Schritte

1. **Vorlagen zuordnen**
   - Drag & Drop aus Pool freigegebener Vorlagen
   - Automatische Nummerierung
   - Zeitschätzung pro TOP

2. **Reihenfolge festlegen**
   - Öffentliche TOPs zuerst
   - Nichtöffentliche TOPs am Ende
   - Manuelle Anpassung möglich

3. **Öffentlichkeit festlegen**
   - Pro TOP: öffentlich/nichtöffentlich
   - Begründung bei nichtöffentlich (Pflichtfeld)
   - Vertraulichkeitsstufe (PUBLIC, NON_PUBLIC, CONFIDENTIAL)

4. **Abstimmung Tagesordnung**
   - Workflow zur Abstimmung mit Gremienvorstand
   - Änderungswünsche erfassen
   - Finale Freigabe

#### Ausgaben
- Fertige Tagesordnung
- Nummerierte TOPs
- Zeitplan (optional)

---

### 5.5 Prozess: Einladungsversand

#### Rechtliche Grundlagen

**Quelle:** [Kommunalbrevier](https://www.kommunalbrevier.de/kommunalbrevier/ratssitzung/mustergeschaeftsordnung-fuer-gemeinderaete-mgescho/2-Form-und-Frist-der-Einladung/)

> "Die Ratsmitglieder und die Beigeordneten werden schriftlich oder elektronisch unter Mitteilung der Tagesordnung, des Ortes und der Zeit der Sitzung eingeladen."

#### Versandkanäle

1. **E-Mail** (Standard)
   - Tagesordnung im Text
   - Sitzungsunterlagen als Anhang oder Link
   - Lesebestätigung anfordern

2. **Portal/App** (SessionNet, Mandatos-Modell)
   - Push-Benachrichtigung
   - Sitzungsmappe digital verfügbar
   - Teilnahmebestätigung

3. **Postalisch** (Fallback)
   - Für Mitglieder ohne digitalen Zugang
   - Satzungsgemäße Frist beachten

#### Sitzungsmappe

**Quellen:** [SOMACOS Druckauftrag](https://somacos.de/loesungen/sonstige/druckauftrag/)

- **PDF-Gesamtdokument**: Alle Unterlagen in einem PDF
- **ZIP-Paket**: Einzelne Dokumente pro TOP
- **Personalisiert**: Wasserzeichen mit Name
- **Kommentierbar**: Annotationen möglich

#### Teilnahmebestätigung

```json
{
  "responses": [
    {"person_id": "1", "status": "ACCEPTED", "responded_at": "..."},
    {"person_id": "2", "status": "DECLINED", "reason": "Urlaub", "substitute_id": "5"},
    {"person_id": "3", "status": "PENDING"}
  ]
}
```

---

### 5.6 Prozess: Sitzungsdurchführung

#### Anwesenheitserfassung

**Quellen:** [STERNBERG Sitzungsmanagement](https://www.sitzungsdienst.net/sitzungsmanagement/)

**Erfassungsmethoden:**
1. **Manuell**: Protokollführer trägt ein
2. **Check-in**: QR-Code scannen
3. **App-basiert**: Mandatos, regisafe App
4. **Automatisch**: Bei Online-Sitzung aus Teilnehmerliste

**Datenfelder:**
- Status: ANWESEND, ENTSCHULDIGT, UNENTSCHULDIGT, ZU_SPÄT, VORZEITIG_GEGANGEN
- Ankunftszeit, Abgangszeit
- Stellvertreter (falls aktiv)

**Stellvertreterregelung:**
- 1., 2., 3. persönlicher Stellvertreter
- Allgemeiner Stellvertreter (Fraktion)
- Automatischer Vorschlag basierend auf Wahldaten

#### Abstimmungen

**Quellen:** [SOMACOS Online-Abstimmung](https://somacos.de/loesungen/sonstige/onlineabstimmung/), [Kommunalbrevier](https://www.kommunalbrevier.de/kommunalbrevier/ratssitzung/mustergeschaeftsordnung-fuer-gemeinderaete-mgescho/23-Beschlussfassung/)

**Abstimmungsarten:**
1. **Offen**: Handzeichen, Zählung
2. **Namentlich**: Einzelstimmen protokolliert
3. **Geheim**: Keine Zuordnung zu Personen

**Rechtlicher Hinweis (NRW):**
> "Geheime Abstimmung hat Vorrang vor namentlicher Abstimmung" (§ 50 Abs. 1 S. 6 GO NRW)

**Digitale Abstimmung (gpaNRW-zertifiziert):**
- Timer während Abstimmung
- Echtzeitanzeige der Stimmenanzahl
- Automatische Auswertung
- Protokollierung der Einzelstimmen (bei namentlich)

**Ergebnisse:**
- ANGENOMMEN, ABGELEHNT, UNENTSCHIEDEN, VERWIESEN
- Ja-Stimmen, Nein-Stimmen, Enthaltungen, Nicht teilgenommen

---

### 5.7 Prozess: Protokollerstellung

#### Protokollarten

1. **Verlaufsprotokoll**: Detaillierter Verlauf der Diskussion
2. **Ergebnisprotokoll**: Nur Beschlüsse und Ergebnisse
3. **Wortprotokoll**: Transkription (selten)

#### Workflow

```
  ┌─────────────┐
  │  SITZUNG    │ ← Notizen, Audio (optional)
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │ ROHPROTOKOLL│ ← Automatisch aus Metadaten + KI
  └──────┬──────┘
         │ Bearbeitung durch Protokollführer
         ▼
  ┌─────────────┐
  │   ENTWURF   │ ← Zur Prüfung
  └──────┬──────┘
         │ Freigabe durch Vorsitzenden
         ▼
  ┌─────────────┐
  │  VORLÄUFIG  │ ← Versand an Mitglieder
  └──────┬──────┘
         │ Genehmigung in nächster Sitzung
         ▼
  ┌─────────────┐
  │  GENEHMIGT  │ ← Mit Unterschriften
  └──────┬──────┘
         │ Veröffentlichung (öffentlicher Teil)
         ▼
  ┌─────────────┐
  │VERÖFFENTLICHT│
  └─────────────┘
```

#### KI-Protokollassistent

**Quellen:** [regisafe Sitzungsassistent](https://www.regisafe.de/sitzungsassistent-mit-ki/)

**Funktionen:**
1. **Audio-Transkription**: Whisper-basiert, Deutsch optimiert
2. **Sprechererkennung**: Zuordnung zu Ratsmitgliedern
3. **Strukturierte Zusammenfassung**: Pro TOP
4. **Beschlusserkennung**: Automatische Extraktion

**Workflow:**
```
Audio-Upload → Transkription → Sprechererkennung →
TOP-Zuordnung → Zusammenfassung → Beschlüsse extrahieren →
Rohprotokoll generieren
```

#### Digitale Signatur

**Quellen:** [Bundesdruckerei](https://www.bundesdruckerei.de/en/innovation-hub/different-digital-signature-levels), [Kommune21](https://www.kommune21.de/k21-meldungen/nadeloehr-der-transformation/)

**Signaturarten:**
1. **Einfache elektronische Signatur**: E-Mail-Bestätigung
2. **Fortgeschrittene elektronische Signatur**: Zertifikatsbasiert
3. **Qualifizierte elektronische Signatur (QES)**: Rechtlich gleichwertig mit handschriftlicher Unterschrift

**Ab 2027:** EUDI-Wallet ermöglicht QES direkt aus dem Personalausweis

---

### 5.8 Prozess: Beschlussverfolgung

**Quellen:** [SOMACOS Beschlusskontrolle](https://somacos.de/loesungen/sonstige/beschlusskontrolle/), [ALLRIS](https://www.cc-egov.de/allris-4/)

#### Workflow

```
Beschluss gefasst
       │
       ▼
┌─────────────────┐
│   OFFEN         │ ← Beschluss dokumentiert
└────────┬────────┘
         │ Zuständigkeit klären
         ▼
┌─────────────────┐
│   ZUGEWIESEN    │ ← Verantwortlicher Sachbearbeiter
└────────┬────────┘
         │ Bearbeitung
         ▼
┌─────────────────┐
│ IN BEARBEITUNG  │ ← Fortschritt dokumentieren
└────────┬────────┘
         │ Umsetzung abgeschlossen
         ▼
┌─────────────────┐
│   ERLEDIGT      │ ← Bericht erstellt
└────────┬────────┘
         │ Bericht an Gremium
         ▼
┌─────────────────┐
│ ABGESCHLOSSEN   │ ← Gremium nimmt Kenntnis
└─────────────────┘
```

#### Features
- **Fristenmanagement**: Wiedervorlage, Eskalation
- **Statusberichte**: Automatische Zusammenfassung für Gremium
- **Kennzahlen**: Erledigungsquote, durchschnittliche Bearbeitungsdauer

---

### 5.9 Prozess: Sitzungsgeldabrechnung

**Quellen:** [ALLRIS Sitzungsgeld](https://www.cc-egov.de/allris-4/), [SOMACOS Aufwandsentschädigung](https://somacos.de/loesungen/sonstige/aufwandsentschaedigung/), [STERNBERG](https://www.sitzungsdienst.net/produkte/sitzungsgeldabrechnung/)

#### Abrechnungsarten

1. **Sitzungsgeld**: Pro Sitzung, gestaffelt nach Gremium
2. **Aufwandsentschädigung**: Monatliche Pauschalen
3. **Fahrtkosten**: Kilometerpauschale, ÖPNV-Erstattung
4. **Verdienstausfall**: Nachweis erforderlich
5. **Sonstige Kosten**: Kinderbetreuung, Reisekosten

#### Workflow

```
Sitzung beendet → Anwesenheit bestätigt →
Automatische Berechnung → Manuelle Prüfung →
Freigabe → Export an HKR/Finanzwesen →
Auszahlung
```

#### Automatisierung
- Anwesenheitsdaten automatisch aus Protokoll
- Entfernungsberechnung aus Adressdaten
- Satzungsparameter einmalig konfiguriert
- Steuerpflichtige Beträge markiert

#### Export-Schnittstellen
- **HKR-Schnittstelle**: Direkte Buchung im Finanzverfahren
- **SEPA-Export**: Überweisungsdaten
- **Mitteilungsverordnung**: Berichte an Finanzbehörden

---

## 6. Nichtöffentliche Dokumente

### 6.1 Rechtliche Grundlagen

**Quellen:** [dejure.org](https://dejure.org/gesetze/GemO/35.html), [Kommunalbrevier](https://www.kommunalbrevier.de/kommunalbrevier/ratssitzung/einladung-oeffentlichkeit-tagesordnung/ii-oeffentlichkeit/2-oeffentliche-oder-nicht-oeffentliche-sitzung/)

#### Gemeindeordnung (Beispiel NRW § 48)

> "Die Sitzungen des Rates sind öffentlich. Nichtöffentlich darf nur verhandelt werden, wenn es das öffentliche Wohl oder berechtigte Interessen Einzelner erfordern."

#### Typische Gründe für Nichtöffentlichkeit:
- **Personalangelegenheiten**: Einstellungen, Beförderungen, Abmahnungen
- **Grundstücksgeschäfte**: Kaufverhandlungen, Pachtverträge
- **Vergabeverfahren**: Vor Zuschlagserteilung
- **Rechtsstreitigkeiten**: Vergleichsverhandlungen
- **Steuergeheimnisse**: Einzelsteuerfälle

#### Verschwiegenheitspflicht

> "Die Gemeinderäte sind zur Verschwiegenheit über alle in nichtöffentlicher Sitzung behandelten Angelegenheiten so lange verpflichtet, bis sie der Bürgermeister von der Schweigepflicht entbindet."

#### Informationsfreiheitsgesetz (IFG)

**Wichtig:** Das IFG gilt NICHT für nichtöffentliche Sitzungsprotokolle. Die Gemeindeordnung ist lex specialis.

### 6.2 Vertraulichkeitsstufen in Mandari

| Stufe | Code | Insight | Work | Session | OParl public |
|-------|------|---------|------|---------|--------------|
| Öffentlich | PUBLIC | ✓ | ✓ | ✓ | true |
| Nichtöffentlich | NON_PUBLIC | ✗ | ✓* | ✓ | false |
| Vertraulich | CONFIDENTIAL | ✗ | ✗ | ✓ | false |
| Eingeschränkt | RESTRICTED | ✗ | ✗ | Rolle | false |

*Work: Nur für Mandatsträger mit gültigem Mandat im betreffenden Gremium

### 6.3 Verschlüsselungskonzept für Work-Integration

#### Problem
Nichtöffentliche Dokumente müssen zwischen Session und Work übertragen werden, ohne dass sie auf Transportwegen oder bei Kompromittierung des Servers im Klartext vorliegen.

#### Lösung: End-to-End-Verschlüsselung

```
┌─────────────────┐
│ Mandari Session │
│  (Server)       │
└────────┬────────┘
         │
         │ 1. Verschlüsselung mit Org-Public-Key
         │    (AES-256-GCM, Schlüssel mit RSA-OAEP)
         ▼
┌─────────────────┐
│   Encrypted     │
│   Payload       │
│   (Transit)     │
└────────┬────────┘
         │
         │ 2. Transport (HTTPS)
         ▼
┌─────────────────┐
│  Mandari Work   │
│  (Client)       │
└────────┬────────┘
         │
         │ 3. Entschlüsselung mit Org-Private-Key
         │    (nur im Client/Browser)
         ▼
┌─────────────────┐
│   Klartext      │
│   (nur lokal)   │
└─────────────────┘
```

#### Schlüsselverwaltung

1. **Organisationsschlüssel**: Jede Organisation in Work hat ein RSA-Schlüsselpaar
2. **Private Key**: Nur im Browser des Org-Admins, nie auf Server
3. **Public Key**: Bei Session registriert
4. **Rotation**: Jährlich, mit Re-Verschlüsselung

#### API-Endpunkt

```python
# Session-Server
@router.get("/work/meetings/{id}/non-public")
async def get_non_public_content(
    meeting_id: uuid.UUID,
    org_id: uuid.UUID = Header(..., alias="X-Work-Organization"),
    token: str = Depends(verify_work_token),
):
    # Prüfe Berechtigung
    org = await get_organization(org_id)
    if not await has_mandate_access(org, meeting_id):
        raise HTTPException(403, "Kein Zugriff auf nichtöffentliche Inhalte")

    # Hole Public Key der Organisation
    org_public_key = await get_org_public_key(org_id)

    # Hole nichtöffentliche Inhalte
    content = await get_non_public_meeting_content(meeting_id)

    # Verschlüssele mit Org-Public-Key
    encrypted = encrypt_for_organization(content, org_public_key)

    return {
        "encrypted_payload": base64.b64encode(encrypted.ciphertext),
        "encrypted_key": base64.b64encode(encrypted.encrypted_key),
        "iv": base64.b64encode(encrypted.iv),
        "algorithm": "AES-256-GCM",
        "key_encryption": "RSA-OAEP-SHA256"
    }
```

---

## 7. Funktionsmodule

### 7.1 Modul: Stammdatenverwaltung

#### Personen
- Name, Titel, Anrede
- Kontaktdaten (E-Mail, Telefon, Adresse)
- Foto (optional)
- Bankverbindung (für Sitzungsgelder)
- Mandatszeiträume
- Fraktionszugehörigkeit

#### Gremien/Organisationen
- Name, Kurzname, Typ (Rat, Ausschuss, Fraktion, AG)
- Mitglieder mit Rollen
- Stellvertreterregelungen
- Übergeordnetes Gremium
- Wahlperioden

#### Mitgliedschaften
- Person ↔ Organisation
- Rolle (Vorsitz, Stellv. Vorsitz, Mitglied, Beratendes Mitglied)
- Stimmrecht
- Zeitraum (von/bis)

### 7.2 Modul: Dokumentenmanagement

#### Dokumententypen
- Vorlagen, Beschlussvorlagen, Anträge
- Protokolle, Niederschriften
- Anlagen, Gutachten, Stellungnahmen
- Pläne, Karten, Bilder

#### Versionierung
- Automatische Versionsnummern
- Änderungshistorie
- Vergleich zwischen Versionen

#### Formate
- Upload: PDF, DOCX, XLSX, Bilder
- Konvertierung zu PDF/A für Archivierung
- OCR für eingescannte Dokumente
- Volltextextraktion

### 7.3 Modul: Vorlageneditor

#### Optionen
1. **Integrierter Editor**: Web-basiert, WYSIWYG
2. **MS Word Integration**: Checkout/Checkin
3. **Template-System**: Vorlagen mit Platzhaltern

#### Features
- Formatvorlagen (Überschriften, Listen, Tabellen)
- Platzhalter für Metadaten (Datum, Aktenzeichen, Gremium)
- Automatische Nummerierung
- Inhaltsverzeichnis generieren

### 7.4 Modul: Kalender und Terminplanung

#### Ansichten
- Monatsansicht, Wochenansicht, Listenansicht
- Filter nach Gremium, Person, Raum

#### Features
- Wiederkehrende Termine (RRULE)
- Feiertags-Integration
- Raumverwaltung
- Kollisionswarnung

#### Export
- iCal-Feed (ICS)
- Sync mit Outlook, Google Calendar
- Widget für Website

### 7.5 Modul: Benachrichtigungen

#### Kanäle
- E-Mail
- Push (App/PWA)
- In-App-Nachrichten
- SMS (optional)

#### Trigger
- Neue Sitzung geplant
- Einladung versandt
- Vorlage zur Mitzeichnung
- Frist läuft ab
- Abstimmung gestartet
- Protokoll verfügbar

#### Konfiguration
- Pro Benutzer individuell
- Standardprofile pro Rolle

### 7.6 Modul: Suche und Recherche

#### Suchfunktionen
- Volltextsuche (Meilisearch)
- Filter nach Typ, Gremium, Zeitraum, Status
- Facetten-Navigation

#### Indexierung
- Metadaten aller Entitäten
- Dokumenteninhalte (OCR)
- Protokolltexte
- Beschlüsse

---

## 8. Integrationen und Schnittstellen

### 8.1 HKR-Schnittstelle (Haushalts-, Kassen-, Rechnungswesen)

**Quellen:** [ALLRIS HKR](https://www.cc-egov.de/allris-4/), [ITEBO SAP](https://www.itebo.de/anwendungen/erp-systeme/sap/)

#### Zweck
- Automatische Buchung von Sitzungsgeldern
- Übergabe an Finanzverfahren

#### Formate
- XML nach HKR-Standard
- CSV für Import
- API für direkte Integration

#### Unterstützte Systeme
- H&H proDoppik
- SAP IS-PS / S/4HANA
- OK.FIS / OK.FINN (dataport)
- ab-data Finanzsoftware
- Infoma newsystem

### 8.2 SAP-Integration (Zukunft)

**Quellen:** [bpc AG](https://www.bpc.ag/dienstleistungen/sap-s4hana-erp/sap-s4hana-kommunen/), [Nagarro Suite4Public](https://www.nagarro.com/de/services/erp/sap/german-public-sector)

#### Szenarien
1. **SAP Kommunalmaster HR**: Personaldaten, Sitzungsgelder
2. **SAP FI**: Buchung von Aufwandsentschädigungen
3. **SAP MM**: Beschaffungsvorgänge aus Beschlüssen

#### Technischer Ansatz
- REST-API oder RFC-Schnittstelle
- IDoc-Austausch
- Middleware (PI/PO, CPI)

### 8.3 DMS-Integration

#### Zweck
- Ablage von Dokumenten im zentralen DMS
- Aktenzeichen-Verknüpfung
- Langzeitarchivierung

#### Unterstützte Systeme
- enaio (OPTIMAL SYSTEMS)
- d.velop
- Fabasoft
- regisafe DMS

### 8.4 Authentifizierung

#### Unterstützte Verfahren
- Lokale Benutzerkonten
- LDAP/Active Directory
- SAML 2.0 (für SSO)
- OpenID Connect
- BundID (eID)

#### Zwei-Faktor-Authentifizierung
- TOTP (Google Authenticator etc.)
- SMS-TAN
- Hardware-Token (FIDO2)

### 8.5 Kalendersysteme

#### Integration mit
- Microsoft Exchange/Outlook
- Google Workspace
- Nextcloud Calendar
- CalDAV-Server

#### Funktionen
- Bidirektionale Synchronisation
- Terminerinnerungen
- Raumbuchung

---

## 9. Compliance und Rechtliche Anforderungen

### 9.1 Barrierefreiheit (BITV 2.0)

**Quellen:** [Bundesfachstelle Barrierefreiheit](https://www.bundesfachstelle-barrierefreiheit.de/DE/Fachwissen/Informationstechnik/EU-Webseitenrichtlinie/BGG-und-BITV-2-0/Die-neue-BITV-2-0/die-neue-bitv-2-0_node.html)

#### Anforderungen
- WCAG 2.1 Level AA
- EN 301 549 (europäischer Standard)
- Tastaturnavigation
- Screenreader-Kompatibilität
- Kontrastverhältnisse
- Alternative Texte für Bilder

#### Maßnahmen
- Semantisches HTML
- ARIA-Labels
- Skip-Links
- Fokus-Management
- Erklärung zur Barrierefreiheit

### 9.2 Datenschutz (DSGVO)

**Quellen:** [Datenschutz-Bayern](https://www.datenschutz-bayern.de/datenschutzreform2018/AP_Loeschung_Archivierung.pdf)

#### Verarbeitungsverzeichnis
- Zweck der Verarbeitung
- Kategorien betroffener Personen
- Empfänger
- Speicherfristen

#### Löschkonzept
- Automatische Löschung nach Fristablauf
- Protokollierung der Löschung
- Ausnahme: Archivierung im öffentlichen Interesse

#### Betroffenenrechte
- Auskunft (Art. 15 DSGVO)
- Berichtigung (Art. 16 DSGVO)
- Löschung (Art. 17 DSGVO) - eingeschränkt bei Archivpflicht

### 9.3 Archivierung

**Quellen:** [iTernity Whitepaper](https://iternity.com/files/assets/03_Downloads-PDFs/DE/Whitepapers/iTernity_Whitepaper_Rechtliche-Anforderungen-Archivierung-DE.pdf)

#### Anforderungen
- Revisionssichere Speicherung
- Unveränderlichkeit (WORM)
- Langzeitlesbarkeit (PDF/A)
- Metadaten-Erhaltung

#### Aufbewahrungsfristen
- Sitzungsprotokolle: 30 Jahre (teils unbegrenzt)
- Vorlagen: 10 Jahre nach Abschluss
- Personalakten: 5 Jahre nach Ausscheiden
- Finanzbelege: 10 Jahre

#### Anbietungspflicht
- Bei Fristablauf: Angebot an Kommunalarchiv
- Archiv entscheidet über dauerhafte Aufbewahrung

### 9.4 IT-Sicherheit

#### Maßnahmen
- Verschlüsselung in Transit (TLS 1.3)
- Verschlüsselung at Rest (AES-256)
- Regelmäßige Sicherheitsaudits
- Penetrationstests
- BSI-Grundschutz (optional: Zertifizierung)

#### Logging und Audit
- Alle Zugriffe protokolliert
- Änderungshistorie
- Exportfunktion für Prüfungen

---

## 10. Produktboard und Roadmap

### 10.1 MVP (Minimum Viable Product)

#### Must-Have Features

| Feature | Priorität | Abhängigkeiten |
|---------|-----------|----------------|
| Sitzungsplanung | P0 | - |
| Tagesordnungserstellung | P0 | Sitzungsplanung |
| Vorlagenverwaltung (CRUD) | P0 | - |
| OParl-Export | P0 | Alle Stammdaten |
| Benutzer- und Rollenverwaltung | P0 | - |
| Einfacher Workflow (Entwurf → Freigabe) | P0 | Vorlagen |
| Protokollerstellung (manuell) | P0 | Sitzungen |
| Öffentlich/Nichtöffentlich-Flag | P0 | TOPs |

### 10.2 Phase 2: Kernworkflows

| Feature | Priorität | Abhängigkeiten |
|---------|-----------|----------------|
| Erweiterte Workflows (Mitzeichnung) | P1 | MVP |
| Einladungsversand (E-Mail) | P1 | Sitzungen |
| Anwesenheitserfassung | P1 | Sitzungen |
| Session-API für Work | P1 | MVP, Work-Modul |
| Verschlüsselte Übertragung | P1 | Session-API |
| Beschlussverfolgung | P1 | Protokolle |
| Sitzungsgeldberechnung | P1 | Anwesenheit |

### 10.3 Phase 3: Erweiterte Features

| Feature | Priorität | Abhängigkeiten |
|---------|-----------|----------------|
| Online-Abstimmung | P2 | Sitzungen |
| KI-Protokollassistent | P2 | Protokolle |
| Audio-Transkription | P2 | KI-Integration |
| HKR-Export | P2 | Sitzungsgelder |
| Mobile App / PWA | P2 | Session-API |
| Digitale Signatur | P2 | Protokolle |
| Dokumentengenerierung | P2 | Vorlagen |

### 10.4 Phase 4: Enterprise

| Feature | Priorität | Abhängigkeiten |
|---------|-----------|----------------|
| gpaNRW-Zertifizierung | P3 | Online-Abstimmung |
| BSI-Zertifizierung | P3 | IT-Sicherheit |
| SAP-Integration | P3 | HKR |
| Multi-Mandanten-Fähigkeit | P3 | Alle |
| White-Label | P3 | Alle |
| API-Marketplace | P3 | Session-API |

### 10.5 Backlog (Ideen)

- Live-Streaming von Sitzungen
- Automatische Untertitel
- Gebärdensprach-Avatar
- Bürgeranfragen-Management
- Petitionsworkflow
- Haushaltsdiskussion (Bürgerbeteiligung)
- Statistik-Dashboard
- Benchmark mit anderen Kommunen

---

## Anhang A: Glossar

| Begriff | Erklärung |
|---------|-----------|
| **AgendaItem** | Tagesordnungspunkt (TOP) |
| **Body** | Kommune/Körperschaft im OParl-Schema |
| **Consultation** | Beratung einer Vorlage in einem Gremium |
| **gpaNRW** | Gemeindeprüfungsanstalt NRW |
| **HKR** | Haushalts-, Kassen- und Rechnungswesen |
| **Meeting** | Sitzung eines Gremiums |
| **Membership** | Mitgliedschaft einer Person in einem Gremium |
| **OParl** | Offener Standard für parlamentarische Informationssysteme |
| **Organization** | Gremium, Fraktion oder Arbeitsgruppe |
| **Paper** | Vorlage/Vorgang |
| **Person** | Ratsmitglied oder sonstige beteiligte Person |
| **QES** | Qualifizierte elektronische Signatur |
| **RIS** | Ratsinformationssystem |
| **TOP** | Tagesordnungspunkt |

---

## Anhang B: Quellen

### Marktanalyse
- [ALLRIS 4](https://www.cc-egov.de/allris-4/)
- [regisafe Ratsinformation](https://www.regisafe.de/produkt/ratsinformationssystem/)
- [SOMACOS](https://somacos.de/)
- [STERNBERG SD.NET](https://www.sitzungsdienst.net/)
- [KOMMUNE-AKTIV](https://www.kommune-aktiv.de/)

### Standards und Spezifikationen
- [OParl Spezifikation](https://oparl.org/spezifikation/online-ansicht/)
- [OParl GitHub](https://github.com/OParl/spec)

### Rechtliche Grundlagen
- [Gemeindeordnung NRW](https://dejure.org/gesetze/GemO/35.html)
- [Kommunalbrevier](https://www.kommunalbrevier.de/)
- [BITV 2.0](https://www.bundesfachstelle-barrierefreiheit.de/)

### Technische Dokumentation
- [HKR-Verfahren](https://kaykansok.de/offener-bereich/loesungen/kommunen/systeme/hkr/)
- [SAP für Kommunen](https://www.bpc.ag/dienstleistungen/sap-s4hana-erp/sap-s4hana-kommunen/)

---

*Dokument erstellt: Januar 2026*
*Letzte Aktualisierung: Januar 2026*
*Autor: Mandari Development Team*
