# Session-OParl-API (je Mandant)

Jeder aktive Session-Mandant (Kommune im Verwaltungs-RIS) stellt unter

```
https://<host>/session/<slug>/api/oparl/
```

einen eigenen, **OParl-1.1-konformen System-Endpoint** bereit (Issue #35).
Die API folgt dem Muster des mandari-Aggregators (`docs/OPARL_API.md`,
Issue #17): rein lesend, anonym, JSON, CORS offen, Rate-Limit
(`OPARL_API_RATE_LIMIT`, Standard 120 Anfragen/Minute je IP).

- **Spezifikation**: https://oparl.org/spezifikation/
- **Implementierung**: `mandari/apps/session/api/oparl.py`,
  Sichtbarkeit/Tombstones in `mandari/apps/session/oparl_publication.py`

## Sicherheitsgarantie: NUR öffentliche Daten

Die API liefert ausschließlich Daten, die im Session-RIS als öffentlich
markiert sind:

| Objekttyp | Sichtbarkeitsregel |
|-----------|--------------------|
| Meeting | `is_public=True` |
| AgendaItem | `is_public=True` **und** Sitzung öffentlich (NÖ-Teil niemals; `resolutionText` nur der öffentliche Beschlusstext) |
| Paper | `is_public=True` |
| File | `is_public=True` **und** übergeordnetes Objekt (Vorlage/Sitzung/TOP) öffentlich |
| Consultation | Vorlage öffentlich; Referenzen auf NÖ-Sitzungen/-TOPs werden ausgelassen |
| Person | ohne geschützte Daten — verschlüsselte Felder (Telefon, Adresse, Bankdaten) werden nie gelesen |
| Organization, Membership, LegislativeTerm | vollständig (keine Ö/NÖ-Unterteilung) |

Nicht-öffentliche Objekte existieren nach außen nicht: Ihre
Objekt-Endpunkte liefern 404 (sofern sie nie veröffentlicht waren).
Beweis-Suite: `python scripts/smoke_session_oparl.py` (Ö/NÖ-Beweis über
die gesamte API-Oberfläche) sowie `scripts/smoke_session_matrix.py`.

## Endpunkte

| Endpunkt | Inhalt |
|----------|--------|
| `GET …/api/oparl/` | System-Objekt (Einstiegspunkt) |
| `GET …/api/oparl/bodies/` | Externe Liste mit der einen Kommune |
| `GET …/api/oparl/body/` | Body-Objekt (inkl. eingebetteter `legislativeTerm`) |
| `GET …/api/oparl/organizations/` | Gremien (paginiert, filterbar) |
| `GET …/api/oparl/people/` | Personen (paginiert, Memberships eingebettet) |
| `GET …/api/oparl/meetings/` | Sitzungen (nur Ö; TOPs des Ö-Teils eingebettet) |
| `GET …/api/oparl/papers/` | Vorlagen (nur Ö; `mainFile`/`auxiliaryFile`/`consultation` eingebettet) |
| `GET …/api/oparl/memberships/`, `…/agendaitems/`, `…/consultations/`, `…/files/`, `…/legislativeterms/` | weitere externe Listen (OParl 1.1 Body-Listen) |
| `GET …/api/oparl/<typ>/<uuid>/` | Objekt-Endpunkte aller Typen |
| `GET …/api/oparl/file/<uuid>/download/` | Anonymer Datei-Abruf (nur öffentlich sichtbare Anlagen; `?download=1` für Attachment) |

Objekttypen für `<typ>`: `organization`, `person`, `membership`, `meeting`,
`agendaitem`, `paper`, `consultation`, `file`, `legislativeterm`.

`Paper.mainFile` ist die älteste öffentliche Anlage der Vorlage, alle
weiteren erscheinen unter `auxiliaryFile`.

## Beratungsfolge (Consultation)

Die Beratungsfolge aus Issue #34 (`SessionConsultation`) wird spec-konform
als `Consultation` ausgeliefert: `paper`, `organization`, `meeting`/
`agendaItem` (sobald terminiert und öffentlich), `role`
(Vorberatung/Anhörung/Entscheidung/Kenntnisnahme) und `authoritative`
für die entscheidende Station.

## Pagination und Zeitfilter

Wie beim Aggregator: OParl-Listen-Envelope (`data`/`pagination`/`links`)
mit echten `links.next`-URLs und HTTP-`Link`-Headern; Seitengröße über
`OPARL_API_PAGE_SIZE` (Standard 100). Sortierung nach `modified`
aufsteigend — stabil für inkrementelle Clients.

Alle Listen unterstützen `created_since`, `created_until`,
`modified_since`, `modified_until`. **Zeitstempel MÜSSEN eine explizite
Zeitzone enthalten** (`+00:00`, `Z`, …); naive Zeitstempel werden mit
HTTP 400 abgelehnt (`+` in URLs als `%2B` kodieren).

## Gelöschte/entöffentlichte Objekte (Tombstones)

OParl 1.1 §2.8, Muster wie beim Aggregator: Objekte, die einmal
öffentlich ausgeliefert wurden und danach **gelöscht** oder auf
**nicht öffentlich** gestellt werden, hinterlassen einen Tombstone
(`SessionOParlTombstone`):

- Objekt-Endpunkte liefern weiterhin **HTTP 200** mit dem gekürzten
  Objekt (`id`, `type`, `created`, `modified`, `deleted: true`);
  `modified` ist der Löschzeitpunkt. Inhalte entfallen vollständig.
- Listen **ohne** Filter enthalten keine Tombstones.
- Listen **mit `modified_since`** enthalten passende Tombstones —
  inkrementelle Clients bekommen Löschungen zuverlässig mit.
- Wird ein Objekt wieder veröffentlicht (NÖ → Ö), verschwindet der
  Tombstone und das Vollobjekt ist wieder abrufbar.
- Ö→NÖ-Wechsel kaskadieren: Eine entöffentlichte Sitzung hinterlässt
  auch Tombstones für ihre öffentlichen TOPs und Anlagen; eine
  entöffentlichte Vorlage für ihre Anlagen und Beratungsstationen.

## Konsumenten

Ein generischer OParl-Client kann einen Session-Mandanten vollständig und
inkrementell spiegeln — insbesondere der mandari-Ingestor bzw. der lokale
Sync-Befehl für den Insight-Durchstich (Issue #36, nächster Abschnitt).

# Session-Kommunen im Bürgerportal (Insight-Durchstich)

Der eigentliche USP: Eine Session-Kommune erscheint **automatisch** im
offenen, kommunenübergreifenden Insight-Bürgerportal (Issue #36) — der
Ingestor konsumiert die Session-OParl-API als ganz normale OParl-Quelle.

## Veröffentlichungs-Schalter

Der Mandant entscheidet, ab wann seine öffentlichen Daten ins Portal
fließen: **Session → Einstellungen → Bürgerportal** (Berechtigung
`manage_settings`; Feld `SessionTenant.insight_publish`, Umschalten wird
im Audit-Log protokolliert).

Beim Aktivieren wird die OParl-API des Mandanten automatisch als
`OParlSource` registriert (Signal-Hook, `sync_config.session_tenant` =
Mandanten-Slug); beim Deaktivieren wird die Quelle inaktiv gesetzt —
es findet kein weiterer Sync statt. Bereits gespiegelte Daten bleiben
erhalten, bis die Kommune eine Löschung beauftragt
(`manage.py purge_deleted`, siehe `docs/OPARL_API.md`).

## Quelle per CLI registrieren

```bash
# Registrieren (setzt insight_publish und legt die OParlSource an)
python manage.py session_insight_source --tenant musterstadt

# Alle veröffentlichten Mandanten (nach)registrieren, z. B. nach Umzug
python manage.py session_insight_source --all

# Deaktivieren
python manage.py session_insight_source --tenant musterstadt --deactivate

# Abweichende Basis-URL (z. B. lokale Instanz)
python manage.py session_insight_source --tenant musterstadt --base-url http://localhost:8000
```

## Sync-Wege

1. **Produktion: Ingestor-Daemon** (`ingestor/`): Die registrierte Quelle
   ist eine normale OParl-Quelle und wird vom Daemon im regulären Zyklus
   mitsynchronisiert (inkl. `modified_since` und Tombstones). Es ist
   keine weitere Konfiguration nötig — hier wird bewusst **kein**
   automatischer Prod-Sync eingerichtet; der Daemon-Zyklus übernimmt.
2. **Lokal/Einzel-Sync: `sync_session_insight`** — synchroner,
   leichtgewichtiger Spiegel ohne Daemon-Abhängigkeiten (urllib,
   funktioniert auch mit SQLite):

   ```bash
   # inkrementell (modified_since = letzter Sync, inkl. Tombstones)
   python manage.py sync_session_insight --tenant musterstadt

   # Voll-Sync bzw. gezielt nach Quell-URL
   python manage.py sync_session_insight --source-url http://localhost:8000/session/musterstadt/api/oparl/ --full

   # alle registrierten Session-Quellen
   python manage.py sync_session_insight --all
   ```

   Implementierung: `mandari/insight_sync/session_mirror.py`.

Nach dem Sync ist die Kommune im Bürgerportal sichtbar
(`/insight/vorgaenge/…`, `/insight/termine/…`, Suche/Karte je nach
aktivierten Diensten); Änderungen erscheinen mit dem nächsten Zyklus,
Ö→NÖ-Wechsel und Löschungen werden über Tombstones nachgezogen und im
Portal ausgeblendet.

## Anleitung: Musterstadt-Mandant an Insight anbinden

1. In Session als Admin des Mandanten anmelden:
   `/session/musterstadt/settings/` → Karte **Bürgerportal** →
   „Im Bürgerportal veröffentlichen“. (Alternativ:
   `python manage.py session_insight_source --tenant musterstadt`.)
2. Prüfen: Im Django-Admin unter *OParl-Quellen* existiert jetzt
   `Sitzungsdienst Stadt Musterstadt` mit der URL
   `<SITE_URL>/session/musterstadt/api/oparl/` (aktiv).
3. Sync anstoßen (falls nicht auf den Daemon-Zyklus gewartet werden soll):
   `python manage.py sync_session_insight --tenant musterstadt --full`
4. Ergebnis: Die öffentlichen Gremien, Personen, Sitzungen (nur Ö-Teile),
   Vorlagen, Anlagen und Beratungsfolgen der Musterstadt erscheinen im
   Bürgerportal unter `/insight/`.

Ende-zu-Ende-Beweis (inkl. NÖ-Ausschluss über den kompletten
Insight-Datenbestand): `python scripts/smoke_insight_durchstich.py`.

## Betrieb

| Umgebungsvariable | Standard | Bedeutung |
|-------------------|----------|-----------|
| `OPARL_API_PAGE_SIZE` | `100` | Objekte pro Listen-Seite |
| `OPARL_API_RATE_LIMIT` | `120` | Anfragen/Minute je IP (`0` = deaktiviert) |

Die IDs der API werden aus dem Request-Host gebaut (`build_absolute_uri`)
— die API funktioniert damit unter jedem konfigurierten Host
(`ALLOWED_HOSTS`) ohne weitere Konfiguration.

Smoke-Tests: `python scripts/smoke_session_oparl.py` (Spec-Struktur,
Pagination, Filter, Tombstones, Ö/NÖ-Beweis) und
`python scripts/smoke_session_matrix.py` (Tenant-Isolation, Ö/NÖ-Matrix).
