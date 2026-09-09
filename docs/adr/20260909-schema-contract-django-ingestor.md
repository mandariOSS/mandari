# Schema-Contract zwischen Django und Ingestor

- Status: angenommen
- Datum: 2026-09-09
- Issue: #161

## Kontext

Die OParl-Tabellen (`oparl_*`) werden zweimal beschrieben: in Django (`insight_core/models.py`, Quelle
der Migrationen und damit des tatsächlichen Datenbankschemas) und im Ingestor
(`ingestor/src/storage/models.py`, SQLAlchemy, Quelle der INSERT/UPSERT-Statements). Beide Seiten
wurden bisher von Hand synchron gehalten. Eine Django-Migration konnte den Ingestor still brechen,
und umgekehrt konnte der Ingestor Werte schreiben, die das Django-Schema nicht zulässt.

Der erste automatische Abgleich hat genau solche Abweichungen gefunden: vier NOT-NULL-Spalten ohne
Datenbank-Default, die nur Django kannte (Ingestor-INSERTs in `oparl_files` und `oparl_persons` wären
auf einer aus Migrationen aufgebauten Datenbank gescheitert), fünf URL-Spalten, die Django als
`varchar(200)` anlegte, während der Ingestor `Text` schrieb, sowie Nullability- und Längenunterschiede.

## Optionen

1. **Gemeinsame Tabellendefinition in `shared/`** (eine Quelle, aus der Django-Modelle und
   SQLAlchemy-Tabellen erzeugt werden). Sauber, aber ein großer Umbau beider Seiten; Django-Felder
   tragen viel Anwendungslogik (Choices, Validierung, Upload-Pfade), die sich nicht generieren lässt.
2. **CI-Diff der beiden Definitionen.** Beide Seiten werden auf ein gemeinsames Spaltenformat
   normalisiert (Typfamilie, Nullability, Länge, Fremdschlüssel, DB-Default) und verglichen; die CI
   schlägt bei Abweichungen fehl. Kein Umbau, sofort wirksam, erweiterbar.
3. **Introspektion der laufenden Datenbank** in beiden Projekten (SQLAlchemy `automap`, Django
   `inspectdb`). Löst das Problem nur zur Laufzeit und macht Typen und Konstanten unsichtbar.

## Entscheidung

Option 2. `insight_core/schema_contract.py` normalisiert beide Seiten und vergleicht; das Skript
`scripts/check_schema_contract.py` ist Bestandteil des CI-Jobs „Test“, die Tests in
`insight_core/tests/test_schema_contract.py` halten den Contract fest und beweisen, dass absichtliche
Änderungen erkannt werden. Regeln:

- Spalte im Ingestor, aber nicht in Django → Fehler (existiert nicht in der DB).
- NOT-NULL-Spalte ohne DB-Default nur in Django → Fehler (Ingestor-INSERTs scheitern). Django-Felder,
  die der Ingestor nicht kennt, bekommen deshalb `db_default`.
- Django NOT NULL, Ingestor nullable → Fehler. Django nullable, Ingestor NOT NULL → Warnung.
- Ingestor `Text` gegen Django `varchar(n)` → Fehler; Ingestor-Länge größer als Django-Länge → Fehler.
- Unterschiedliche Typfamilien → Fehler, außer `string`/`text` und `integer`/`bigint` (Warnung).

## Vorgehen bei Schemaänderungen

1. Feld in Django ändern, Migration erzeugen. Pflichtfelder, die der Ingestor nicht setzt, bekommen
   `db_default`.
2. Dieselbe Spalte im Ingestor nachziehen (`ingestor/src/storage/models.py`), sofern der Ingestor sie
   schreibt oder liest. Enrichment-Spalten, die nur Django-Worker füllen, stehen zusätzlich in
   `ENRICHMENT_FIELDS` (`ingestor/src/storage/database.py`), damit Upserts sie nicht überschreiben.
3. `python scripts/check_schema_contract.py` lokal ausführen; die CI wiederholt das.

## Folgen

- Schema-Drift zwischen Django und Ingestor wird im Pull Request sichtbar, nicht im Betrieb.
- Die Produktionsdatenbank weicht historisch von den Migrationen ab (eigene Lineage, siehe
  Betriebsdokumentation); der Contract prüft die Definitionen, nicht die laufende Datenbank. Ein
  Abgleich der Prod-DB gegen die Migrationen bleibt eine eigene Betriebsaufgabe.
- Option 1 bleibt möglich, wenn beide Seiten später aus einer Quelle generiert werden sollen; die
  Normalisierung aus diesem ADR ist dann die Testbasis.
