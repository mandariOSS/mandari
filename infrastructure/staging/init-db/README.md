# Database Initialization

Platzieren Sie hier SQL-Dateien, die beim ersten Start der PostgreSQL-Datenbank ausgeführt werden sollen.

## Daten aus SQLite exportieren

```bash
# Im mandari Verzeichnis
python manage.py dumpdata --natural-foreign --natural-primary -e contenttypes -e auth.Permission --indent 2 > ../infrastructure/staging/init-db/initial_data.json
```

## Daten in Staging importieren

```bash
docker-compose -f docker-compose.staging.yml exec django python manage.py loaddata /docker-entrypoint-initdb.d/initial_data.json
```
