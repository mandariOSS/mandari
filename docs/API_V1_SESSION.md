# Session-API v1 (django-ninja) und Migrationsleitfaden

Stand: 2026-09-09 (Issue #163). Die Session-API v1 ist der Pilot für die API-Basis aus
`docs/ENGINEERING_STANDARDS.md` §6: generiertes OpenAPI-Schema, Pydantic-Schemas, Fehler nach
RFC 9457, versionierte Pfade.

## Endpunkte

| Methode | Pfad | Zugriff |
|---------|------|---------|
| GET | `/api/v1/session/{tenant_slug}/` | Einstiegspunkt mit allen Links |
| GET | `/api/v1/session/{tenant_slug}/meetings/` | öffentlich; NÖ-Sitzungen mit Recht `view_non_public_meetings` oder Token-Flag `can_read_meetings` |
| GET | `/api/v1/session/{tenant_slug}/papers/` | öffentlich; NÖ-Vorlagen samt Texten mit `view_non_public_papers` oder `can_read_papers` |
| GET | `/api/v1/session/{tenant_slug}/applications/` | nur mit Recht `view_applications` |
| POST | `/api/v1/session/{tenant_slug}/applications/submit/` | API-Token mit `can_submit_applications` |
| GET | `/api/v1/session/{tenant_slug}/applications/{id}/feedback/` | nur das einreichende API-Token (bzw. ein Token derselben in Work verbundenen Organisation) |
| GET | `/api/v1/session/openapi.json` | OpenAPI-3-Dokument |
| GET | `/api/v1/session/docs` | Swagger UI (lokale Assets, kein CDN) |

Listen unterstützen `limit` (1–200, Standard 100) und `offset`; `meta.total` ist die Gesamtzahl.

### Rückmeldestand eines Antrags (Issue #316)

`GET …/applications/{id}/feedback/` liefert Eingangsnummer und Status, die Vorlage (`converted`,
`public`, `reference` nur bei veröffentlichter Vorlage), die Stationen der Beratungsfolge und den
Beschluss. Es gelten die Ö/NÖ-Regeln der OParl-Schnittstelle: Eine nicht-öffentliche Station trägt
nur `order`, `public: false` und `label: "nicht-öffentlich beraten"`; der Beschluss stammt nur aus
einer öffentlichen, entscheidenden Station. Bearbeitungsnotizen und andere Interna der Verwaltung
werden nie ausgeliefert. Anträge anderer Tokens bzw. Organisationen antworten mit 404. Die Antwort
auf `…/applications/submit/` enthält die URL unter `feedback`.
Öffentliche OParl-1.1-Daten liefert unverändert `/session/<slug>/api/oparl/` (`docs/SESSION_OPARL_API.md`).

## Authentifizierung

- **API-Token**: `Authorization: Bearer <token>` (64 Zeichen, angelegt unter Einstellungen → API-Tokens).
  Token gehören zu genau einem Mandanten; Rechte über die Flags `can_read_meetings`, `can_read_papers`,
  `can_submit_applications`; optional IP-Beschränkung und Ratenlimit je Minute (429 mit `Retry-After`).
- **Sitzung**: angemeldete Nutzer des Session-RIS mit ihren Rollenrechten (nur GET).
- Ohne beides: anonym, nur öffentliche Daten.

## Fehlerformat (RFC 9457)

Alle Fehler sind `application/problem+json`:

```json
{
  "type": "https://docs.mandari.de/api/probleme/keine-berechtigung",
  "title": "Keine Berechtigung",
  "status": 403,
  "detail": "Dieses Token darf keine Anträge einreichen.",
  "instance": "/api/v1/session/musterstadt/applications/submit/",
  "request_id": "3f2b…"
}
```

Validierungsfehler (422) tragen zusätzlich `errors` mit `loc`, `msg`, `type` je Feld. `request_id`
entspricht dem `X-Request-ID`-Header und findet sich in den Server-Logs.

## Ablösung der alten Pfade

`/session/<slug>/api/session/meetings/`, `…/papers/`, `…/applications/`, `…/applications/submit/`
bleiben bis **31.03.2027** erreichbar und antworten mit `Deprecation: true`, `Sunset` und
`Link: <neuer Pfad>; rel="successor-version"`. Der Einstiegspunkt `/session/<slug>/api/` verweist
unter `v1` auf die neue API. Unterschiede beim Umstieg:

| Alt | Neu |
|-----|-----|
| Fehler als `{"error": "…"}` mit 400/401/403 | `application/problem+json`; fehlende/ungültige Felder → 422 mit `errors` |
| feste 100 Einträge | `limit`/`offset`, `meta.total` |
| Token nur zum Einreichen | Token liest NÖ-Daten gemäß Flags |
| `application_type`-Aliase `proposal`, `urgent_motion` | weiterhin akzeptiert (auf `motion`/`urgent` abgebildet) |

## Neue Endpunkte anlegen (Leitfaden)

1. Schema in `apps/session/api/v1/schemas.py` (Ein- und Ausgabe, `Field(description=…)`).
2. Endpunkt in `apps/session/api/v1/endpoints.py` auf dem `router`; Mandant über `get_tenant()`,
   Aufrufer über `resolve_principal()` und `principal.require(<recht>, <detail>)`.
3. Fehler ausschließlich über `Problem(status, detail, kind=…)`; nie `JsonResponse` von Hand.
4. Test in `apps/session/tests/test_api_v1.py` (Sichtbarkeit, Auth-Wege, Fehlerformat).
5. Alte `JsonResponse`-Views, die ersetzt werden, bekommen `successor_url_name` (Deprecation-Header)
   und einen Eintrag in der Tabelle oben.

Weitere APIs (Work-Portal, Open Data) folgen demselben Muster unter `/api/v1/<bereich>/`.
