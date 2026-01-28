# ADR 001: FastAPI statt Django

## Status

Akzeptiert

## Kontext

Das vorherige Mandari-System basierte auf Django mit Django REST Framework. Bei der Neuimplementierung standen zwei Optionen zur Wahl:

1. **Django + DRF** - Bewährtes Full-Stack-Framework
2. **FastAPI** - Modernes async API-Framework

## Entscheidung

Wir verwenden **FastAPI** für das neue Backend.

## Begründung

### Vorteile von FastAPI

1. **Native async/await Support**
   - Besser für I/O-intensive Operationen (OParl-Sync, DB-Queries)
   - Höhere Durchsatzrate bei vielen gleichzeitigen Anfragen

2. **Automatische OpenAPI-Dokumentation**
   - Swagger UI und ReDoc out-of-the-box
   - Automatisch generierte Client-SDKs möglich

3. **Pydantic-Integration**
   - Strikte Typisierung
   - Automatische Validierung
   - Bessere IDE-Unterstützung

4. **Leichtgewichtiger**
   - Weniger Overhead als Django
   - Schnellerer Startup
   - Einfacher zu verstehen

5. **Moderne Python-Features**
   - Type Hints überall
   - Dependency Injection
   - Async Middleware

### Nachteile

1. **Kein Admin-Interface** - Muss selbst gebaut oder extern gelöst werden
2. **Weniger Batteries-included** - Mehr manuelle Konfiguration nötig
3. **Jüngeres Ökosystem** - Weniger fertige Plugins

### Warum nicht Django?

Das alte System hatte folgende Probleme:

- **Monolithische Struktur** - Schwer zu entkoppeln
- **Sync-only by default** - Async war nachträglich und kompliziert
- **Overhead** - Viele ungenuzte Features (Admin, Forms, etc.)
- **Template-lastig** - Für API-first nicht optimal

## Konsequenzen

### Positiv

- Einfacherer, fokussierterer Code
- Bessere Performance bei API-Calls
- Moderne Entwicklungserfahrung
- Klare API-Dokumentation

### Negativ

- Admin-Panel muss separat entwickelt werden
- Team muss FastAPI lernen
- Einige Django-Pakete nicht verfügbar

## Alternativen betrachtet

- **Litestar** - Zu neu, kleineres Ökosystem
- **Starlette** - Zu low-level für unsere Zwecke
- **Flask** - Kein nativer async Support
