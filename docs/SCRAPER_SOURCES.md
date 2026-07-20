# Nicht-OParl-Quellen: Scraper & Bridges

Mandari kann neben echten OParl-APIs auch Ratsinformationssysteme ohne
OParl-Schnittstelle anbinden. Zwei Wege:

1. **Scraper-Adapter im Ingestor** (`ingestor/src/scrapers/`) — z. B.
   **SessionNet** (Somacos). Der Adapter erzeugt synthetisches
   OParl-1.1-JSON und speist es in die unveränderte Pipeline ein
   (OParlProcessor → Upserts → OCR-Queue → Elasticsearch → Tombstones).
2. **Externer OParl-Proxy** — z. B. **ALLRIS** via
   [Aeroid/oparl-bridge](https://github.com/Aeroid/oparl-bridge) (MIT).
   Der Proxy läuft als eigener Container; Mandari konsumiert seine
   OParl-Ausgabe als ganz normale OParl-Quelle. Der Ingestor-Kern bleibt
   unberührt.

Die Art einer Quelle steht in `OParlSource.sync_config["source_type"]`
(JSONB, additiv — bewusst keine eigene Spalte, um Schema-Drift zwischen
Django und Ingestor zu vermeiden):

| `source_type` | Bedeutung |
|---|---|
| `oparl` (Default, Schlüssel fehlt) | echte OParl-API |
| `bridge:allris` | oparl-bridge-Proxy vor ALLRIS (normaler OParl-Sync, nur Provenienz-Label) |
| `scraper:sessionnet` | SessionNet-HTML-Scraper im Ingestor |

Im Django-Admin (OParl-Quellen) sind Art, Parse-Quote und robots.txt-Status
je Quelle sichtbar; der Filter "Quellen-Art" trennt OParl/Bridge/Scraper.

---

## 1. SessionNet-Kommune anbinden (Schritt für Schritt)

SessionNet-Bürgerinfo-Frontends sind mandantenfähige Standard-Templates.
Der Adapter unterstützt die klassische `*.asp`-Variante und die neuere
`*.php`-Variante (identisches Markup, verifiziert an
buergerinfo.luedenscheid.de und rat.eschweiler.de/bi).

### Schritt 1: Basis-URL ermitteln

Die Basis-URL ist das Verzeichnis, in dem die SessionNet-Seiten liegen —
erkennbar an Seiten wie `si0040.asp` (Sitzungskalender), `vo0050.asp`
(Vorlage), `gr0040.asp` (Gremien). Beispiele:

- `https://buergerinfo.luedenscheid.de/` (Kürzel direkt im Root, `.asp`)
- `https://rat.eschweiler.de/bi/` (Unterverzeichnis `bi/`, `.php`)
- `https://sessionnet.owl-it.de/unna/bi/` (Hoster mit Mandanten-Pfad)

Kurz prüfen: `<basis-url>si0040.asp` (oder `.php`) muss den
Sitzungskalender liefern und der Seitentitel „SessionNet | …" sein.

### Schritt 2: robots.txt prüfen

Der Ingestor prüft robots.txt automatisch vor jedem Crawl und überspringt
die Quelle bei einem Disallow (Status wird im Admin angezeigt). Vorab
manuell prüfen schadet nicht: `https://<host>/robots.txt`.

### Schritt 3: Quelle im Django-Admin anlegen

Admin → Insight → OParl-Quellen → Hinzufügen:

- **Name**: z. B. „Stadt Eschweiler (SessionNet)"
- **URL**: die Basis-URL (dient als eindeutiger Quellen-Schlüssel), z. B.
  `https://rat.eschweiler.de/bi/`
- **Sync config**:

```json
{
  "source_type": "scraper:sessionnet",
  "scraper": {
    "base_url": "https://rat.eschweiler.de/bi/",
    "body_name": "Stadt Eschweiler",
    "variant": "php",
    "rate_limit_seconds": 2.0,
    "calendar_window_days": [-60, 210],
    "full_window_days": [-365, 210]
  }
}
```

Alle `scraper`-Schlüssel außer `base_url` sind optional:

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `body_name` | "Unbekannte Kommune" | Anzeigename des Bodies |
| `variant` | Auto-Detect | `"asp"` oder `"php"` |
| `rate_limit_seconds` | `2.0` | Mindestabstand zwischen Requests je Host |
| `max_concurrent` | `1` | immer 1 (Serialisierung, RIS-Server schonen) |
| `calendar_window_days` | `[-60, 210]` | Kalenderfenster inkrementeller Läufe |
| `full_window_days` | `[-365, 210]` | Fenster des Full-Crawls (Historie) |
| `max_detail_pages` | unbegrenzt | Obergrenze Detailseiten je Lauf (Onboarding/Pilot) |
| `members_on_full_only` | `true` | Gremien-Mitglieder nur im Full-Crawl crawlen |

### Schritt 4: Probe-Crawl mit Limit

Beim Onboarding zunächst mit strengem Limit fahren
(`"max_detail_pages": 20`), Ergebnis im Admin prüfen (Scraper-Status:
Parse-Quote, gespeicherte Entitäten), Stichproben gegen die Live-Seiten
vergleichen. Danach das Limit entfernen und einen Full-Sync auslösen
(Admin-Aktion „Vollständiger Sync").

### Was der Adapter extrahiert

| SessionNet-Seite | OParl-Entität | external_id (kanonische URL) |
|---|---|---|
| Basis-URL | Body | `https://<host>/<prefix>/` |
| `si0057?__ksinr=` | Meeting inkl. TOPs (Ö/NÖ, Beschlüsse) | `…/si0057.asp?__ksinr=N` |
| `si0050?__ksinr=` | Sitzungsdokumente (Einladung/Niederschrift) | `…/getfile.asp?id=N&type=do` |
| `vo0050?__kvonr=` | Paper (Betreff, Nummer, Art) + Anlagen-PDFs | `…/vo0050.asp?__kvonr=N` |
| `gr0040` | Organizations (Gremien) | `…/kp0040.asp?__kgrnr=N` |
| `kp0040?__kgrnr=` | Persons + Memberships (inkl. Stimmrecht) | `…/pe0051.asp?__kpenr=N` |
| TOP ↔ Vorlage | Consultation | `…/vo0050.asp?__kvonr=N#consultation/<ksinr>` |

PDF-Anlagen laufen automatisch durch die bestehende OCR-Queue
(`text_extraction_status='pending'` → pypdf → Tesseract → Mistral) und
werden in Elasticsearch indexiert.

### Änderungserkennung & Löschungen

- **Listen-Diffing**: Je Kalendermonat wird ein Hash der Sitzungsliste in
  `sync_config["scraper_state"]["list_snapshots"]` gehalten; unveränderte
  Monate werden im inkrementellen Lauf übersprungen.
- **Content-Hash je Entität**: `mandari:contentHash` im `raw_json`;
  Upsert nur bei Differenz. `modified` ist die Crawl-Zeit des letzten
  echten Updates — unsere eigene OParl-Ausgabe bleibt damit inkrementell
  konsumierbar.
- **Verschwinden ≠ Löschen**: Objekte, die in **3 aufeinanderfolgenden
  Full-Crawls** (konfigurierbar: `SCRAPER_TOMBSTONE_FULL_CRAWLS`) nicht
  mehr gesehen wurden, werden per `mark_entity_deleted` tombstoned
  (`deleted=true`, ES-Dokument entfernt) — nie physisch gelöscht.
  Taucht ein Objekt wieder auf, hebt der Upsert-Pfad den Tombstone
  automatisch auf. Erfasst werden derzeit Sitzungen im Crawl-Fenster und
  Gremien (konservativ, um Historie außerhalb des Fensters nie fälschlich
  zu tombstonen).

### Monitoring

- Prometheus: `mandari_ingestor_scraper_pages_fetched_total`,
  `mandari_ingestor_scraper_parse_failures_total{page_type=…}`,
  `mandari_ingestor_scraper_parse_quota` je Quelle.
- Fällt die Parse-Quote eines Laufs unter 80 %
  (`SCRAPER_PARSE_QUOTA_WARN`), wird gewarnt und der Lauf als fehlerhaft
  im SyncLog vermerkt — typisches Symptom eines Frontend-Redesigns der
  Instanz (Parser-Bruch).
- Golden-File-Tests (`ingestor/tests/test_sessionnet_parser.py`) mit
  eingefrorenen HTML-Fixtures zweier realer Instanzen sichern die Parser
  in CI ab.

---

## 2. ALLRIS via oparl-bridge (externer Container)

**ALLRIS 4/net** (CC e-gov) rendert sein Frontend per Apache-Wicket/Ajax —
klassisches HTML-Scraping reicht dort nicht.
[Aeroid/oparl-bridge](https://github.com/Aeroid/oparl-bridge) (MIT,
FastAPI + Playwright + SQLite) scrapt eine ALLRIS-Instanz (Wicket-UI plus
die XML-API der ALLRIS-Windows-App, `/app01`) und publiziert die Daten als
OParl-1.1-API. Mandari konsumiert diese API als **ganz normale
OParl-Quelle** — null Änderungen am Ingestor-Kern.

**Gepinnte Version: `v0.2.0`**
(Commit `930d0a6a9005c79758dfaaa6ea52b044158e17a0`, 2026-06-14).
Kein Fork im Mandari-Repo — die Bridge ist eine externe Abhängigkeit;
Updates bewusst durch Neu-Pinnen übernehmen.

Bewertungs-Kurzfassung (Code-Review 2026-07-20):

- Funktionsfähig und aktiv gepflegt; eigene Tests + OParl-Validierungs-
  Skript (`scripts/validate_oparl.py`); saubere Struktur
  (scraper/normalizer/db/api getrennt).
- Ein Deployment je ALLRIS-Instanz (keine Mandantenfähigkeit).
- OParl-Ausgabe: Listen ohne Pagination (`links: {}`) und ohne
  `modified_since`-Filter — für unseren Ingestor unkritisch: er folgt
  `links.next` nur wenn vorhanden und vergleicht Objekte ohnehin
  client-seitig (`batch_check_entities_exist`); überzählige
  Query-Parameter ignoriert die Bridge.
- Initial-Sync historischer Daten dauert je nach Kommune 10–40 Minuten
  (Playwright); PDFs werden on-demand geproxiet, nicht gespeichert.
- Kein offizielles Docker-Image → eigenes Dockerfile nötig (Beispiel
  unten). Playwright/Chromium bleiben damit aus dem Ingestor-Container
  heraus.

### Deployment (Beispiel)

Beispieldateien: [`docs/examples/oparl-bridge.Dockerfile`](examples/oparl-bridge.Dockerfile)
und [`docs/examples/docker-compose.oparl-bridge.yml`](examples/docker-compose.oparl-bridge.yml).

```bash
# Image einmalig aus der gepinnten Upstream-Version bauen
docker build -f docs/examples/oparl-bridge.Dockerfile \
  -t mandari/oparl-bridge:v0.2.0 \
  https://github.com/Aeroid/oparl-bridge.git#v0.2.0
```

```yaml
# Auszug: ein Bridge-Container je ALLRIS-Kommune
services:
  oparl-bridge-musterstadt:
    image: mandari/oparl-bridge:v0.2.0
    environment:
      OPARL_ALLRIS_BASE_URL: "https://ratsinfo.musterstadt.de/allris"
      OPARL_API_BASE_URL: "https://oparl-musterstadt.intern.mandari.de"
      OPARL_BODY_NAME: "Stadt Musterstadt"
      OPARL_SCRAPER_DELAY_MS: "2000"
    volumes:
      - oparl_bridge_musterstadt:/data
    restart: unless-stopped
```

### Anbindung in Mandari

1. Bridge-Container starten, initialen Sync abwarten
   (`oparl-bridge-sync`-CLI bzw. UI der Bridge), OParl-Ausgabe prüfen:
   `curl https://<bridge-host>/` (System-Objekt) und `/bodies`.
2. Quelle im Django-Admin anlegen:
   - **URL**: System-Endpoint der Bridge (z. B. `https://<bridge-host>/`)
   - **Sync config**: `{"source_type": "bridge:allris"}`
3. Sync auslösen — die Quelle verhält sich wie jede OParl-API. Da die
   Bridge kein `modified_since` filtert, greift automatisch der
   client-seitige Abgleich des Ingestors (vorhandene Mechanik, Issue #22).

---

## 3. Politeness-Defaults (alle Scraper-Quellen)

- **User-Agent**: `mandari-ingestor (+https://mandari.de/crawler)`
  (Env `SCRAPER_USER_AGENT`). Die Crawler-Infoseite
  `https://mandari.de/crawler` gehört zur Marketing-Website und erklärt,
  wer wir sind, warum wir crawlen und wie man uns erreicht/drosselt.
- **Rate-Limit**: max. 1 Request / 2 s je Host (konfigurierbar je Quelle),
  `max_concurrent=1` — RIS-Server kleiner Kommunen sind schwachbrüstig.
- **robots.txt**: wird respektiert (24-h-Cache je Host). Disallow →
  Quelle wird nicht gecrawlt und im Admin markiert. Nicht abrufbare oder
  ungültige robots.txt gilt als „erlaubt" (RFC 9309).
- **Keine Umgehung** von Logins, CAPTCHAs oder Session-Schranken — nur
  öffentliche Bürgerinfo-Bereiche.
- **Full-Crawls** sind selten (Scheduler-Nachtfenster) und gestaffelt;
  beim Onboarding einer Kommune wächst der OCR-Backlog sprunghaft →
  Kommunen einzeln aufschalten.

## 4. Opt-out- / Takedown-Prozess

Kommunen (oder Betroffene) können sich jederzeit über die auf der
Crawler-Infoseite genannten Kontakte melden. Ablauf:

1. **Drosseln/Pausieren**: Quelle im Admin deaktivieren (`is_active`)
   oder `rate_limit_seconds` erhöhen — wirkt ab dem nächsten Zyklus.
   Alternativ setzt die Kommune ein robots.txt-Disallow für unseren
   User-Agent (`mandari-ingestor`); der Ingestor respektiert das
   automatisch.
2. **Inhalte entfernen (Takedown)**: Objekte werden zunächst tombstoned
   (`deleted=true` — aus Portalen und Suche verschwunden, Daten bleiben
   für Prüfzwecke). Endgültiges physisches Löschen ausschließlich über
   das bestehende Django-Kommando:

   ```bash
   python manage.py purge_deleted --dry-run   # Vorschau
   python manage.py purge_deleted --yes       # endgültig löschen
   ```

3. **Abwägung**: Opt-out-Wünsche werden geprüft, nicht blind ausgeführt
   (Transparenzinteresse an amtlichen Informationen); mindestens
   Drosselung/Nachtfenster wird immer angeboten. Reaktionszeit: 5 Werktage.
