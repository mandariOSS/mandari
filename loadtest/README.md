# Lasttests mit Locust

Lastszenarien zu Issue #228. Mengengerüste, gemessene Zahlen und
Größenempfehlungen stehen in [`docs/LASTTESTS.md`](../docs/LASTTESTS.md);
hier stehen nur die Befehle.

## Voraussetzungen

- Eine laufende mandari-Instanz mit den Daten aus `manage.py generate_load_data`
  (Profil `klein`, `mittel` oder `gross`). Das Kommando läuft nur mit `DEBUG=true`
  oder mit `--ich-weiss-was-ich-tue` — niemals gegen eine Produktionsdatenbank.
- Locust auf dem Lastgeber: `pip install -r loadtest/requirements.txt`
  (bewusst nicht in `mandari/requirements.txt`).
- Die Konten des Generators melden sich per Passwort an. Die 2FA-Pflicht muss dafür
  aus sein (`DEBUG=true` oder `TWO_FACTOR_ENFORCEMENT=false`).

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
export DJANGO_LOG_LEVEL=WARNING

python manage.py migrate --noinput
python manage.py generate_load_data --profile klein   # gibt LOADTEST_BODY_ID aus
python manage.py runserver 127.0.0.1:8000 --noreload
```

Hinweis zu `ELASTICSEARCH_URL`: Zeigt die URL auf einen Server, der nicht
antwortet, wartet der Client seine Wiederholungen ab (bis über eine Minute je
Suche). Leer lassen oder Elasticsearch tatsächlich starten.

In einem zweiten Terminal (Repo-Root):

```bash
pip install -r loadtest/requirements.txt
export LOADTEST_PROFILE=klein
export LOADTEST_BODY_ID=<UUID aus der Ausgabe des Generators>   # optional bei genau einer Kommune
locust -f loadtest/locustfile.py --headless --host http://127.0.0.1:8000 \
       -u 20 -r 5 -t 3m --csv loadtest/results/klein --html loadtest/results/klein.html
```

`-u` ist die Zahl gleichzeitiger Nutzer aus dem Mengengerüst (klein 20, mittel 100,
groß 400), `-r` die Anlaufrate je Sekunde, `-t` die Laufzeit. Ohne `--headless`
öffnet Locust eine Web-Oberfläche auf http://localhost:8089.

## Ergebnisse

Locust schreibt nach `loadtest/results/` (gitignored):

| Datei | Inhalt |
|---|---|
| `<name>_stats.csv` | je Endpunkt: Anfragen, Fehler, Median, p95, p99, Durchsatz |
| `<name>_failures.csv` | Fehler mit Ursache |
| `<name>_stats_history.csv` | Verlauf über die Laufzeit |
| `<name>.html` | Bericht mit Diagrammen |

Kennzahlen, die in `docs/LASTTESTS.md` gehören: p95 je Szenario (Spalte `95%`),
Durchsatz (`Requests/s`), Fehlerquote (`Failure Count` / `Request Count`) — mit
Hardware, Profil, Nutzerzahl und Laufzeit.

## Szenarien

Siehe Modul-Dokumentation in `locustfile.py`. Die Gewichte (Portal 5, Sitzungsdienst 3,
Fraktion 2, Live-Abstimmung 1) bilden eine Ratssitzung mit Publikum nach: viele
Lesezugriffe, wenige Schreibvorgänge.

Was fehlt: Sitzungsgeldlauf und Synchronisation (Ingestor) — beides läuft nicht
über HTTP-Anfragen von Nutzern und ist in Issue #228 als Folgeschritt genannt.

## Lauf „Großstadt“ auf repräsentativer Hardware

1. Zielsystem wie in `DEPLOYMENT.md` aufsetzen (Docker Compose, PostgreSQL, Redis),
   Ressourcen laut Größenklasse „groß“ in `docs/LASTTESTS.md`.
2. Im Anwendungs-Container: `python manage.py generate_load_data --profile gross --ich-weiss-was-ich-tue`
   (leere Datenbank, kein Produktionsbestand; der Lauf dauert einige Minuten).
3. Lastgeber auf einem anderen Rechner im selben Netz:
   `LOADTEST_PROFILE=gross locust -f loadtest/locustfile.py --headless --host https://<host> -u 400 -r 20 -t 15m --csv loadtest/results/gross --html loadtest/results/gross.html`
4. Während des Laufs `docker stats` und `pg_stat_activity` mitschreiben (Verbindungsbudget, siehe `DEPLOYMENT.md`).
5. p95, Durchsatz, Fehlerquote und Ressourcenverbrauch in `docs/LASTTESTS.md` eintragen.
