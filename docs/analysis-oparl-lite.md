# Analyse: OParl-Lite vs. Mandari Ingestor

**Datum:** 2026-01-28
**Autor:** Claude (auf Anfrage)
**Quellen:** [OParl-Lite Repository](https://codeberg.org/machdenstaat/oparl-lite)

---

## Executive Summary

OParl-Lite ist ein TypeScript-basiertes Microservices-System mit GraphQL-API und Event-Driven Architecture. Nach eingehender Analyse komme ich zu dem Schluss: **Eine Einbindung oder Migration zu OParl-Lite ist für Mandari nicht empfehlenswert**, aber einige Architekturideen sind durchaus inspirierend.

| Kriterium | Empfehlung |
|-----------|------------|
| Code-Übernahme | **Nein** |
| Architektur-Inspiration | **Teilweise** |
| Direkte Integration | **Nein** |
| Komplette Migration | **Nein** |

---

## Architektur-Vergleich

### OParl-Lite

```
┌─────────────────────────────────────────────────────────────┐
│                    OParl-Lite (TypeScript)                  │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────┐   │
│  │  GraphQL    │  │   Meeting    │  │    Webhook      │   │
│  │   Server    │  │  Aggregator  │  │   Forwarder     │   │
│  │  (Port 4000)│  │              │  │                 │   │
│  └──────┬──────┘  └──────┬───────┘  └────────┬────────┘   │
│         │                │                    │            │
│         └────────────────┼────────────────────┘            │
│                          │                                 │
│                    ┌─────┴─────┐                          │
│                    │   Redis   │  (Pub/Sub)               │
│                    └─────┬─────┘                          │
│                          │                                 │
│                    ┌─────┴─────┐                          │
│                    │ PostgreSQL│  (Prisma)                │
│                    └───────────┘                          │
│                                                             │
│  Features:                                                  │
│  - GraphQL Subscriptions (WebSocket)                       │
│  - RSS Poller für Change Detection                         │
│  - Circuit Breakers                                         │
│  - HMAC Webhook Signing                                     │
│  - Prometheus Metrics                                       │
└─────────────────────────────────────────────────────────────┘
```

### Mandari Ingestor

```
┌─────────────────────────────────────────────────────────────┐
│                  Mandari Ingestor (Python)                  │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                    CLI (Typer)                       │   │
│  │   sync | daemon | add-source | status | init-sources│   │
│  └───────────────────────┬─────────────────────────────┘   │
│                          │                                 │
│  ┌───────────────────────┴─────────────────────────────┐   │
│  │              SyncOrchestrator                        │   │
│  │   - Full/Incremental Sync                           │   │
│  │   - Parallel Body Processing                        │   │
│  │   - Progress Tracking (Rich)                        │   │
│  └───────────────────────┬─────────────────────────────┘   │
│                          │                                 │
│  ┌───────────┐  ┌────────┴────────┐  ┌─────────────────┐   │
│  │  OParl    │  │   Processor     │  │    Database     │   │
│  │  Client   │  │                 │  │    Storage      │   │
│  │  (httpx)  │  │  (Transform)    │  │  (SQLAlchemy)   │   │
│  └───────────┘  └─────────────────┘  └─────────────────┘   │
│                                                             │
│  Features:                                                  │
│  - Async HTTP mit Connection Pooling                       │
│  - ETag/If-Modified-Since Caching                          │
│  - Exponential Backoff Retry                               │
│  - Scheduler Daemon                                         │
│  - Multi-Source Support                                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Detaillierter Vergleich

### Technologie-Stack

| Aspekt | OParl-Lite | Mandari Ingestor |
|--------|------------|------------------|
| **Sprache** | TypeScript (Node.js) | Python 3.12 |
| **HTTP Client** | Axios/Fetch | httpx (async) |
| **ORM** | Prisma | SQLAlchemy (async) |
| **Datenbank** | PostgreSQL | PostgreSQL |
| **API** | GraphQL (Pothos) | Direkt in Django |
| **Messaging** | Redis Pub/Sub | Kein Messaging |
| **Caching** | Redis | HTTP-Level (ETag) |
| **Architektur** | Microservices (3) | Monolith |

### Sync-Strategie

| Aspekt | OParl-Lite | Mandari Ingestor |
|--------|------------|------------------|
| **Change Detection** | RSS Feed Polling | DB-Vergleich + HTTP Caching |
| **Update-Intervall** | ~15 Min (RSS) | Konfigurierbar (Default 15 Min) |
| **Inkrementell** | Ja (via RSS) | Ja (via modified_since) |
| **Full Sync** | Ja | Ja |
| **Parallelisierung** | Begrenzt | Stark (asyncio.gather) |

### Stärken & Schwächen

#### OParl-Lite

**Stärken:**
- Real-time Updates via GraphQL Subscriptions
- Event-Driven Architecture ermöglicht lose Kopplung
- Webhook-System für externe Consumer
- Production-grade Monitoring (Prometheus)
- Circuit Breakers für Resilienz
- Gut dokumentiert

**Schwächen:**
- Hohe Komplexität (3 Microservices)
- RSS-basierte Change Detection ist veraltet und fehleranfällig
- Nur für Bonn optimiert
- Node.js Ecosystem (npm-Dependency-Hell)
- Keine Multi-Source-Unterstützung dokumentiert
- GraphQL-Overhead für einfache Datenabfragen

#### Mandari Ingestor

**Stärken:**
- Einfache, verständliche Architektur
- Python-Ecosystem (stabil, typisiert)
- Multi-Source-Support von Anfang an
- HTTP-Level Caching (ETag/If-Modified-Since) ist effizienter
- Starke Parallelisierung (asyncio)
- Direkter DB-Vergleich statt RSS (zuverlässiger)
- CLI mit Progress-Tracking
- Integration mit Django (Mandari Backend)

**Schwächen:**
- Keine Real-time Updates (Poll-basiert)
- Kein Webhook-System für externe Consumer
- Kein Prometheus-Export
- Keine Circuit Breaker

---

## Konkrete Verbesserungsmöglichkeiten

Basierend auf OParl-Lite könnten folgende Features **in Mandari nachgerüstet** werden:

### 1. Event-Emission nach Sync (Empfohlen)

```python
# Nach jedem Sync: Event an Redis senden
import redis

async def emit_sync_event(entity_type: str, entity_id: str, action: str):
    """Emit sync event for real-time subscribers."""
    r = redis.from_url(settings.redis_url)
    await r.publish("mandari:sync", json.dumps({
        "type": entity_type,
        "id": entity_id,
        "action": action,  # created, updated, deleted
        "timestamp": datetime.utcnow().isoformat()
    }))
```

**Aufwand:** Gering (1-2 Stunden)
**Nutzen:** Hoch - Django kann auf Sync-Events reagieren

### 2. Prometheus Metrics (Empfohlen)

```python
from prometheus_client import Counter, Histogram, Gauge

sync_duration = Histogram('mandari_sync_duration_seconds', 'Sync duration')
entities_synced = Counter('mandari_entities_synced_total', 'Entities synced', ['type'])
sync_errors = Counter('mandari_sync_errors_total', 'Sync errors', ['source'])
```

**Aufwand:** Gering (2-3 Stunden)
**Nutzen:** Mittel - Besseres Monitoring

### 3. Circuit Breaker für fehlerhafte Sources (Optional)

```python
from circuitbreaker import circuit

@circuit(failure_threshold=5, recovery_timeout=300)
async def fetch_with_circuit_breaker(url: str):
    """Fetch with circuit breaker pattern."""
    return await self._do_fetch(url)
```

**Aufwand:** Gering (1-2 Stunden)
**Nutzen:** Mittel - Verhindert Überlastung bei API-Problemen

### 4. Webhook-System (Optional)

Ein Webhook-System könnte externe Consumer (andere Apps, Services) benachrichtigen:

```python
# Einfache Webhook-Implementierung
async def notify_webhooks(event: dict):
    webhooks = await get_registered_webhooks()
    for webhook in webhooks:
        try:
            await httpx.post(webhook.url, json=event, headers={
                "X-Mandari-Signature": sign_payload(event, webhook.secret)
            })
        except Exception as e:
            logger.warning(f"Webhook failed: {e}")
```

**Aufwand:** Mittel (4-6 Stunden)
**Nutzen:** Gering für Mandari (Django ist direkt angebunden)

---

## Nicht Empfohlene Übernahmen

### 1. GraphQL API

**Warum nicht:**
- Mandari nutzt Django Templates + HTMX (kein SPA)
- GraphQL-Overhead für Server-Side-Rendering unnötig
- REST/Django ORM ist für den Use Case effizienter
- Zusätzliche Komplexität ohne Mehrwert

### 2. Microservices-Architektur

**Warum nicht:**
- Mandari ist ein Monolith mit klarer Struktur
- Microservices erhöhen Ops-Komplexität massiv
- Für 2 VMs bei Hetzner überdimensioniert
- Inter-Service-Kommunikation ist Fehlerquelle

### 3. RSS-basierte Change Detection

**Warum nicht:**
- Nicht alle OParl-Server bieten RSS
- HTTP ETag/If-Modified-Since ist standardkonformer
- DB-Vergleich ist zuverlässiger
- RSS-Feeds können verzögert sein

### 4. TypeScript/Node.js Migration

**Warum nicht:**
- Python-Ecosystem ist reifer für Data Processing
- Django ist Kern von Mandari
- Zwei Sprachen erhöhen Wartungsaufwand
- asyncio ist performant genug

---

## Fazit

### Gesamtbewertung

| Aspekt | Bewertung |
|--------|-----------|
| **Code-Qualität OParl-Lite** | Gut (Production-grade) |
| **Architektur-Fit für Mandari** | Schlecht (zu komplex) |
| **Technologie-Fit** | Schlecht (TypeScript vs Python) |
| **Ideen-Inspiration** | Mittel (Events, Metrics) |

### Empfehlung

**Keine Migration oder Integration mit OParl-Lite.**

Der Mandari Ingestor ist für den spezifischen Use Case (Django-Backend, Multi-Source, Server-Side-Rendering) besser geeignet. Die Architektur ist einfacher, performanter und wartbarer.

**Stattdessen empfohlen:**

1. **Event-Emission nach Sync** via Redis implementieren (einfach)
2. **Prometheus Metrics** hinzufügen für Monitoring
3. **Circuit Breaker** für robustere Fehlerbehandlung
4. Bestehende Stärken (HTTP-Caching, Parallelisierung) beibehalten

### Risiken einer OParl-Lite-Integration

1. **Technologie-Mismatch:** Python ↔ TypeScript erfordert IPC/REST
2. **Doppelte Datenhaltung:** Prisma + Django ORM = Sync-Probleme
3. **Ops-Komplexität:** 3 zusätzliche Services zu deployen
4. **Debugging:** Distributed Tracing erforderlich
5. **Wartung:** Zwei Codebases in zwei Sprachen

---

## Quellen

- [OParl-Lite Repository](https://codeberg.org/machdenstaat/oparl-lite)
- [OParl Standard](https://oparl.org/spezifikation/online-ansicht/)
- Mandari Ingestor Codebase (`apps/ingestor/`)
