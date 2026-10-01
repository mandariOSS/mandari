# Lasttests mit Locust

Lastszenarien zu Issue #228. Mengengerüste, gemessene Zahlen und
Größenempfehlungen stehen in [`docs/LASTTESTS.md`](../docs/LASTTESTS.md);
hier stehen nur die Befehle.

| Datei | Zweck |
|---|---|
| `locustfile.py` | Szenarien (Portal, Sitzungsdienst, Fraktion, OParl, Live-Abstimmung, Sitzungsgeldlauf) |
| `budgets.json` | je Mengengerüst: Laufparameter, PostgreSQL-Einstellungen, Budgets für p95 und Fehlerquote |
| `auswerten.py` | Bericht aus den Locust-Ergebnissen, Prüfung der Budgets (Status 1 bei Verletzung) |
| `ressourcen.py` | CPU und Speicher der Anwendung, Datenbank, Suche und des Lastgebers mitschreiben (Linux) |
| `Caddyfile` | vorgeschalteter Webserver wie im Betrieb (TLS, Kompression, mehrere Anwendungsprozesse) |

**Nur gegen eigene Test- oder Entwicklungsinstanzen.** Niemals gegen eine
Produktionsinstanz oder fremde Systeme: Der Generator legt Konten mit bekanntem
Passwort an, die Szenarien schreiben Stimmen und Sitzungsgeld-Positionen.

## In der CI (empfohlen)

Der Workflow [`Lasttest`](../.github/workflows/lasttest.yml) baut die Umgebung
auf einem GitHub-Läufer auf (Daphne hinter Caddy, PostgreSQL mit den
Einstellungen der Größenklasse, Redis, Elasticsearch), erzeugt die Daten, lässt
Locust laufen und prüft die Budgets. Er läuft

- wöchentlich mit dem Profil `klein` (Budget-Gate),
- von Hand: *Actions → Lasttest → Run workflow*, Profil `klein`, `mittel` oder
  `gross` (der Lauf „Großstadt“), Nutzerzahl, Laufzeit und Zahl der
  Anwendungsprozesse optional; mit **Stufen** (z. B. `25,50,75,100,150,200`)
  eine Kapazitätsmessung statt fester Nutzerzahl,
- in Pull Requests, die `loadtest/`, den Workflow oder den Datengenerator ändern.

Ergebnis: Bericht in der Zusammenfassung des Laufs, Rohdaten (CSV, HTML-Bericht
von Locust, Ressourcen, Protokolle der Anwendung) als Artefakt `lasttest-<profil>`
bzw. `lasttest-<profil>-kapazitaet`. Der Läufer hat 4 vCPU für alles zusammen; das
Profil `gross` mit 400 Nutzern überlastet ihn (Einordnung in `docs/LASTTESTS.md`).

## Kapazitätsmessung (Stufenlast)

`LOADTEST_STUFEN=25,50,100` (und optional `LOADTEST_STUFENDAUER`, Vorgabe 120 s)
ersetzt die feste Nutzerzahl durch Stufen; `-u`/`-r` sind dann wirkungslos. Der
Bericht nennt je Stufe Durchsatz, p95, Fehlerquote und CPU (ohne die ersten 20 s
nach jedem Wechsel) und die höchste Stufe mit p95 bis 1 s und höchstens 1 %
Fehlern. Budgets gelten dabei nicht — die Messung überschreitet die Grenze absichtlich.

```bash
LOADTEST_PROFILE=gross LOADTEST_STUFEN=25,50,75,100 locust -f loadtest/locustfile.py --headless        --host http://127.0.0.1:8000 --csv loadtest/results/gross-kapazitaet
python loadtest/auswerten.py bericht --profil gross --ergebnisse loadtest/results/gross-kapazitaet
```

## Lokal gegen den Entwicklungsserver

Alle Befehle aus `mandari/` (die Anwendung) bzw. dem Repo-Root (Locust). Der
Entwicklungsserver ist Daphne (ASGI), genau wie im Betrieb — nur mit einem Prozess.

```bash
cd mandari
npm ci --no-audit --no-fund && npm run build          # Vite-Manifest für die Seiten

export DEBUG=true
export DATABASE_URL=sqlite:///$PWD/../.lasttest.sqlite3   # oder eine PostgreSQL-URL
export ENCRYPTION_MASTER_KEY=$(python -c 'import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())')
export ELASTICSEARCH_AUTO_INDEX=False
export ELASTICSEARCH_URL=                              # leer: Suche fällt sofort auf die Datenbank zurück
export MANDARI_SYNC_WATCHDOG=0
export REDIS_URL=                                     # leer: Cache im Prozess, Channel-Layer im Speicher
export ALLOWED_HOSTS=localhost,127.0.0.1
export OPARL_API_RATE_LIMIT=0                         # alle simulierten Abnehmer kommen von einer Adresse
export DJANGO_LOG_LEVEL=WARNING

python manage.py migrate --noinput
python manage.py generate_load_data --profile klein   # letzte Zeile: LOADTEST_BODY_ID=<uuid>
python manage.py runserver 127.0.0.1:8000 --noreload
```

Hinweis zu `ELASTICSEARCH_URL`: Zeigt die URL auf einen Server, der nicht
antwortet, wartet der Client seine Wiederholungen ab (bis über eine Minute je
Suche). Leer lassen oder Elasticsearch tatsächlich starten.

In einem zweiten Terminal (Repo-Root):

```bash
python -m venv .locust && .locust/bin/pip install -r loadtest/requirements.txt
export LOADTEST_PROFILE=klein
.locust/bin/locust -f loadtest/locustfile.py --headless --host http://127.0.0.1:8000 \
       -u 20 -r 5 -t 3m --csv loadtest/results/klein --html loadtest/results/klein.html
python loadtest/auswerten.py bericht --profil klein --ergebnisse loadtest/results/klein
```

`-u` ist die Zahl gleichzeitiger Nutzer aus dem Mengengerüst (klein 20, mittel 100,
groß 400), `-r` die Anlaufrate je Sekunde, `-t` die Laufzeit; die Werte je Profil
stehen in `budgets.json`. Ohne `--headless` öffnet Locust eine Web-Oberfläche auf
http://localhost:8089.

Voraussetzungen auf dem Zielsystem:

- Daten aus `manage.py generate_load_data` (Profil `klein`, `mittel` oder `gross`).
  Das Kommando läuft nur mit `DEBUG=true` oder mit `--ich-weiss-was-ich-tue` und
  nur gegen eine leere Datenbank ohne echten Bestand.
- Die Konten des Generators melden sich per Passwort an, der Sitzungsgeldlauf mit
  dem Verwaltungskonto (Administrator). Die 2FA-Pflicht muss dafür aus sein
  (`DEBUG=true` oder `TWO_FACTOR_ENFORCEMENT=false`).
- Die Ratenbegrenzung der OParl-Schnittstelle muss aus sein (`OPARL_API_RATE_LIMIT=0`),
  sonst antwortet sie dem Szenario „OParl“ mit 429.
- Über HTTPS mit eigener Zertifizierungsstelle (z. B. Caddy mit `tls internal`):
  `LOADTEST_TLS_PRUEFEN=0`.

## Ergebnisse

Locust schreibt nach `loadtest/results/` (gitignored):

| Datei | Inhalt |
|---|---|
| `<name>_stats.csv` | je Endpunkt: Anfragen, Fehler, Median, p95, p99, Durchsatz |
| `<name>_szenarien.json` | je Szenario: p50, p95, p99 aus allen Einzelwerten; Antworten bedingter Anfragen (304) |
| `<name>_failures.csv` | Fehler mit Ursache |
| `<name>_stats_history.csv` | Verlauf über die Laufzeit |
| `<name>.html` | Bericht von Locust mit Diagrammen |
| `<name>_ressourcen.csv` | CPU und Speicher je Komponente (nur mit `ressourcen.py`, in der CI) |

`auswerten.py bericht` fasst das als Markdown zusammen: Kennzahlen gesamt (Durchsatz,
Fehlerquote, p95), p95 je Szenario und Endpunkt, ETag-Trefferquote, Ressourcen und
das Ergebnis der Budgetprüfung. Mit `--budgets` und dem Profil aus `budgets.json`
endet es mit Status 1, wenn ein Budget verletzt ist.

## Szenarien

Siehe Modul-Dokumentation in `locustfile.py`. Die Gewichte (Portal 5, Sitzungsdienst 3,
Fraktion 2, OParl 1) bilden eine Ratssitzung mit Publikum nach: viele Lesezugriffe,
wenige Schreibvorgänge. Live-Abstimmung (die Protokollführung der laufenden
Ratssitzung) und Sitzungsgeldlauf sind je genau ein Nutzer, unabhängig von der
Nutzerzahl.

Nicht abgebildet: die Synchronisation durch den Ingestor (kein Nutzerzugriff über
HTTP; Messungen in `docs/LASTTESTS.md` verweisen auf die Betriebsdaten).

## Budgets ändern

Budgets dürfen nur sinken. Nennt der Bericht unter „Hinweise“ ein Budget mit großer
Reserve, kann es im selben Pull Request gesenkt werden. Ein neues Szenario oder ein
neuer Endpunkt bekommt sein Budget aus einem Lauf in der CI (etwa doppelter p95,
auf 100 ms gerundet). Ein Test (`mandari/apps/common/tests/test_lasttest_auswertung.py`)
prüft, dass `budgets.json` nur Szenarien und Endpunkte nennt, die `locustfile.py` misst.

## Lauf „Großstadt“ auf eigener Hardware

Der Workflow mit Profil `gross` ist reproduzierbar, teilt sich aber einen Läufer
mit vier Kernen zwischen Anwendung, Datenbank, Suche und Lastgeber. Für einen
Nachweis auf Zielhardware:

1. Zielsystem wie in `DEPLOYMENT.md` aufsetzen (Docker Compose, PostgreSQL, Redis),
   Ressourcen laut Größenklasse „groß“ in `docs/LASTTESTS.md`.
2. Im Anwendungs-Container: `python manage.py generate_load_data --profile gross --ich-weiss-was-ich-tue`
   (leere Datenbank, kein Produktionsbestand; der Lauf dauert einige Minuten).
3. Lastgeber auf einem anderen Rechner im selben Netz:
   `LOADTEST_PROFILE=gross locust -f loadtest/locustfile.py --headless --host https://<host> -u 400 -r 20 -t 15m --csv loadtest/results/gross --html loadtest/results/gross.html`
4. Während des Laufs `docker stats` und `pg_stat_activity` mitschreiben (Verbindungsbudget, siehe `DEPLOYMENT.md`).
5. `python loadtest/auswerten.py bericht --profil gross --ergebnisse loadtest/results/gross --umgebung "<Hardware>"`
   und das Ergebnis in `docs/LASTTESTS.md` eintragen.
