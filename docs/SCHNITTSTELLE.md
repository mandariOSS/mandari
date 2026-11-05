# Schnittstelle: Einstieg, Beschreibung, gemeinsame Regeln

Grundlage: ADR [Eine Schnittstelle: API-first auf OParl, kommParl und OpenAPI](adr/20261105-eine-schnittstelle-api-first.md)
(Epic #1203), umgesetzt in #1205. Die Bereiche selbst beschreiben [OPARL_API.md](OPARL_API.md),
[SESSION_OPARL_API.md](SESSION_OPARL_API.md) und [API_V1_SESSION.md](API_V1_SESSION.md).

## Adressen

| Adresse | Inhalt |
|---|---|
| `GET /api/` | Einstieg: Verweis auf das OParl-System der Installation, `kommparl:conformsTo`, Beschreibung, Dokumentation, Bereiche |
| `GET /api/openapi.json` | eine OpenAPI-3.1-Beschreibung aller Bereiche |
| `GET /api/docs/` | Swagger UI mit den lokalen Dateien von django-ninja (kein CDN) |

Alle übrigen Adressen unter `/api/` (`stats/`, `contact/`, `v1/session/`, `public/v1/`, `provisioning/`,
`provisioning/v2/`) bleiben unverändert; `hub/api/tests/test_schnittstelle.py` prüft das.

### Der Einstieg ist kein zweites OParl-System

In OParl ist die Adresse eines Objekts seine Kennung. Ein `System` unter `/api/` hätte entweder eine fremde
Kennung (zwei Adressen für ein Objekt, zwei Fassungen mit verschiedenem `ETag`) oder eine zweite Kennung. Beides
verwirrt Anwendungen, die Objekte an ihrer `id` erkennen. Der Einstieg ist deshalb ein eigenes Dokument und
verweist mit `system` auf das System des Aggregators (`/oparl/v1/system`).

`kommparl:conformsTo` nennt nur Kennungen, deren Anforderungen die Installation vollständig erfüllt (kommParl,
Kapitel 5): heute `https://schema.kommparl.de/0.1/`. Änderungsfeed und Snapshot gibt es schon, die Körperschaften
nennen sie aber als `mandari:changes` und `mandari:snapshot`. Die Profile verlangen `kommparl:changes` und
`kommparl:snapshot`. Ihre Kennungen kommen hinzu, sobald die Ausgaben beide Eigenschaften nennen (ADR, Regel 2).

## Bereiche anmelden

Die Drehscheibe kennt die Fachmodule nicht (A1). Jeder Bereich meldet sich deshalb beim Start an:

```python
from hub.api.openapi import Area, register_area

register_area(Area(key="session-v1", title="Session-API v1", description="…",
                   entry="/api/v1/session/{tenant_slug}/", openapi=beschreibung))
```

`openapi` liefert die Beschreibung des Bereichs (`paths`, `components`) und wird erst beim Abruf gebaut.
Heute angemeldet: der OParl-Aggregator (`hub/api/openapi/aggregator.py`), OParl je Mandant und die Session-API v1
(`apps/session/api/schnittstelle.py`). Die OParl-Pfade stammen aus den Routen der Ausgaben, die Schemas der
OParl-Typen aus `hub/api/oparl_types.py`, derselben Tabelle wie die Typprüfung des OParl-Validators.

## Gemeinsame Regeln als Code

Fachfreie Bausteine liegen in der Plattform, `apps/common/api/`:

| Baustein | Modul | genutzt von |
|---|---|---|
| Fehler nach RFC 9457 (`Problem`, `ProblemError`, `problem_response`, Handler für django-ninja) | `problems.py`, `ninja.py` | Session-API v1, Befehle über HTTP, Änderungsfeed, Datenkatalog |
| `ETag` und `If-None-Match` | `conditional.py` | OParl-Ausgaben, Einstieg |
| `Deprecation` (RFC 9745) und `Sunset` (RFC 8594) | `deprecation.py` | alte Session-API |
| `Idempotency-Key` lesen und prüfen | `idempotency.py` | Befehle (`hub.commands`), Session-API v1 |
| Seiten wie in OParl (`data`, `pagination`, `links`, `Link`-Header) | `pages.py` | OParl-Ausgaben, Beschreibung |

## Eingecheckte Beschreibung und CI

Die Beschreibung liegt als [`docs/schnittstelle/openapi.json`](schnittstelle/openapi.json) im Repository. Wer
einen Endpunkt ändert, schreibt sie neu und committet sie mit:

```bash
cd mandari && python manage.py schnittstelle_beschreibung        # neu schreiben
cd mandari && python manage.py schnittstelle_beschreibung --pruefen
```

- **Aktualität:** `hub/api/tests/test_schnittstelle.py` vergleicht die Datei mit der erzeugten Beschreibung.
- **Brechende Änderungen:** Der CI-Job „Schnittstelle“ vergleicht die Datei mit dem Stand der Basis (oasdiff,
  Stufe `ERR`) und prüft sie gegen OpenAPI 3.1. Eine brechende Änderung braucht eine neue Hauptversion im Pfad
  und eine Abkündigung nach [RELEASE_POLITIK.md](RELEASE_POLITIK.md), Abschnitt 5.
- **Befehlsparität:** Jeder Befehl im Register (`hub/contracts/schemas`) braucht einen Endpunkt, der ihn in
  `x-mandari-command` nennt. Ausnahmen stehen in `mandari/hub/api/openapi/befehle_ohne_endpunkt.json`; die Liste
  darf nur kürzer werden (`scripts/check_schnittstelle.py --basis <stand>`).
