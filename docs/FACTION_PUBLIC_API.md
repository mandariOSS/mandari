# Öffentliche Fraktions-API v1

Read-only-JSON-API, mit der Fraktionen die **öffentlichen** Termine und
Tagesordnungen ihrer Fraktionssitzungen automatisch auf der eigenen
Webseite anzeigen können (Issue #71).

## Grundprinzipien

- **Opt-in je Organisation**: Die API ist standardmäßig **deaktiviert** und
  wird je Organisation bewusst eingeschaltet
  (Work-Portal → Organisation → Reiter **„API"**).
- **Opakes Token statt Organisations-Slug**: Der Zugriff läuft über ein
  zufälliges Token in der URL. Organisationen sind dadurch nicht
  enumerierbar. Das Token kann jederzeit in den Einstellungen erneuert
  werden — bisherige URLs werden dann sofort ungültig.
- **Strikt öffentliche Inhalte**: Ausgeliefert werden ausschließlich
  öffentliche, angenommene Tagesordnungspunkte (Nummer, Titel) sowie
  Sitzungs-Metadaten (Datum, Ort, Status). **Niemals** enthalten:
  nicht-öffentliche TOPs (auch nicht als Platzhalter), Protokollinhalte,
  Beschlüsse, Teilnehmerdaten, Video-Links oder Sitzungs-Entwürfe.
- **Read-only**: nur `GET` (plus `OPTIONS` für CORS-Preflight).
- **CORS**: Standardmäßig `Access-Control-Allow-Origin: *`. Optional kann
  die Organisation die erlaubten Origins auf konkrete Webseiten
  einschränken — dann wird nur ein gelisteter Origin gespiegelt.
- **Caching**: `Cache-Control: public, max-age=<konfigurierbar>`
  (Standard 300 s, Schema: 1 h). Bitte clientseitig nicht häufiger als
  nötig abrufen.
- **Konfigurierbar je Organisation** (Reiter „API"): Zeitfenster für
  vergangene (Standard 90 Tage) und kommende Sitzungen (Standard 365 Tage),
  Inhaltsumfang (Sitzungsort und Tagesordnung einzeln abschaltbar),
  CORS-Origins, Cache-Dauer. Der Reiter zeigt zudem eine
  Nutzungsstatistik (Abrufe gesamt, letzter Abruf — keine IPs) und ein
  fertiges Einbindungs-Snippet zum Kopieren.
- **Versionierung**: Alle Pfade liegen stabil unter `/api/public/v1/…`.
  Inkompatible Änderungen erscheinen nur unter einer neuen Version
  (`/api/public/v2/…`).

> **Hinweis Subdomain:** Die API ist bewusst pfadbasiert gebaut
> (`https://mandari.de/api/public/v1/…`). Ein späteres Routing über die
> Subdomain `api.mandari.de` ist ein reines Caddy-/Proxy-Thema und ändert
> die Pfade unterhalb von `/api/public/v1/` nicht.

## Endpunkte

Basis: `https://mandari.de/api/public/v1`

| Methode | Pfad | Beschreibung |
|---------|------|--------------|
| GET | `/openapi.json` | OpenAPI-3.0-Schema (ohne Token abrufbar) |
| GET | `/fraktionen/<token>/` | Zugangs-Info + Endpunkt-Übersicht |
| GET | `/fraktionen/<token>/sitzungen/` | Terminliste: kommende + vergangene Sitzungen (Zeitraum der Vergangenheit je Organisation konfigurierbar, Standard 90 Tage) |
| GET | `/fraktionen/<token>/sitzungen/<id>/` | Sitzungsdetail mit öffentlicher Tagesordnung |

Unbekannte, deaktivierte oder inaktive Zugänge liefern einheitlich
**404** (`{"error": "not_found"}`) — die API verrät nicht, ob ein Token
existiert.

## Beispiel

```bash
curl https://mandari.de/api/public/v1/fraktionen/<token>/sitzungen/
```

```json
{
  "api_version": "1.0",
  "organization": {"name": "Fraktion Beispiel"},
  "count": 2,
  "meetings": [
    {
      "id": "6f0c…",
      "title": "Fraktionssitzung März",
      "number": 12,
      "start": "2026-03-02T18:00:00+01:00",
      "end": null,
      "location": "Fraktionsbüro",
      "is_virtual": false,
      "status": "invited",
      "cancelled": false
    }
  ]
}
```

Sitzungsdetail (`…/sitzungen/<id>/`) ergänzt das Feld `agenda`:

```json
{
  "agenda": [
    {"number": "1", "title": "Tagesordnung festlegen und letztes Protokoll genehmigen"},
    {"number": "2", "title": "Spielplatz Musterstraße"}
  ]
}
```

## Einbindung auf der Webseite (Beispiel)

```html
<ul id="sitzungen"></ul>
<script>
fetch("https://mandari.de/api/public/v1/fraktionen/<token>/sitzungen/")
  .then((r) => r.json())
  .then((data) => {
    const list = document.getElementById("sitzungen");
    for (const m of data.meetings) {
      const li = document.createElement("li");
      li.textContent = `${new Date(m.start).toLocaleString("de-DE")} — ${m.title}`;
      list.appendChild(li);
    }
  });
</script>
```

## Sicherheit & Datenschutz

- Das Token gewährt ausschließlich Lesezugriff auf ohnehin öffentliche
  Inhalte — trotzdem: URL wie ein Passwort behandeln und bei Bedarf über
  „API-Token erneuern" austauschen.
- Aktivierung, Änderungen und Token-Erneuerung werden in der
  Änderungshistorie der Fraktionssitzungen auditiert.
- Die Filterung auf öffentliche Inhalte wird serverseitig erzwungen und
  per Smoke-Test (`scripts/smoke_faction_meetings.py`, Phase R) mit
  Response-Scans abgesichert.
