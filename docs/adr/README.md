# Architekturentscheidungen (ADR)

Jede Entscheidung mit Wirkung auf Architektur, Werkzeuge oder Konventionen wird hier im
[MADR-Format](https://adr.github.io/madr/) festgehalten: Kontext, Optionen, Entscheidung, Folgen.
Dateiname `JJJJMMTT-kurztitel.md`. Status: vorgeschlagen, angenommen, abgelöst.

| ADR | Titel | Status |
|-----|-------|--------|
| [20260909-frontend-komponenten-und-build.md](20260909-frontend-komponenten-und-build.md) | Frontend: Komponentenbibliothek, Build-Pipeline, CSP in Stufen | angenommen |
| [20260909-qualitaetsgates-ci.md](20260909-qualitaetsgates-ci.md) | Qualitätsgates in der CI und Ratchet-Prinzip | angenommen |
| [20260909-schema-contract-django-ingestor.md](20260909-schema-contract-django-ingestor.md) | Schema-Contract zwischen Django und Ingestor | angenommen |
| [20260917-kein-scraping-sternberg-regisafe-komuna.md](20260917-kein-scraping-sternberg-regisafe-komuna.md) | Kein Scraping für gehostetes Sternberg RIM, regisafe und komuna; Kooperationspfad | angenommen |
| [20260929-schichtenmodell.md](20260929-schichtenmodell.md) | Datendrehscheibe A1: Vier Schichten mit durchgesetzten Abhängigkeitsregeln | angenommen |
| [20260929-ereignistechnik-postgres.md](20260929-ereignistechnik-postgres.md) | Datendrehscheibe A2: Ereignistechnik in PostgreSQL: Outbox-Journal und Worker, kein Broker | angenommen |
| [20260929-sequenzierer.md](20260929-sequenzierer.md) | Datendrehscheibe A3: Folgenummer nach dem Commit: Sequenzierer mit `pg_snapshot_xmin` | angenommen |
| [20260929-auftraege-und-zeitplaene.md](20260929-auftraege-und-zeitplaene.md) | Datendrehscheibe A4: Aufträge und Zeitpläne: eigenes Django-Tasks-Backend auf PostgreSQL | angenommen |
| [20260929-ereignisvertraege.md](20260929-ereignisvertraege.md) | Datendrehscheibe A5: Verträge für Ereignisse und Befehle: JSON Schema, nur additive Änderungen | angenommen |
| [20260929-befehle-synchron.md](20260929-befehle-synchron.md) | Datendrehscheibe A6: Befehle laufen synchron: In-Process- und HTTP-Client, Idempotenzschlüssel, RFC 9457 | angenommen |
| [20260929-kanonisches-modell.md](20260929-kanonisches-modell.md) | Datendrehscheibe A7: Kanonisches RIS-Modell: OParl-Kern mit Erweiterungen, stabile uuid5-Kennungen, Löschsemantik | angenommen |
| [20260929-fremdschluessel-ris-bestand.md](20260929-fremdschluessel-ris-bestand.md) | Datendrehscheibe A8: Fremdschlüssel auf den RIS-Bestand: nie CASCADE | angenommen |
| [20260929-adapter-rahmen.md](20260929-adapter-rahmen.md) | Datendrehscheibe A9: Adapter-Rahmen: übersetzen ohne Geschäftsregeln, Zustellprotokoll, dünne Webhooks mit HMAC | angenommen |
| [20260929-aenderungsfeed-format.md](20260929-aenderungsfeed-format.md) | Datendrehscheibe A10: Änderungsfeed: Cursor, upsert/delete/redact, Snapshot-Übergabe, 410 | angenommen |
| [20260929-portal-modul.md](20260929-portal-modul.md) | Datendrehscheibe A11: Bürgerportal als eigenes Fachmodul; `insight_core` wird reiner RIS-Bestand | vorgeschlagen |

Die Einträge „Datendrehscheibe A1–A11“ gehören zusammen (Epic #476); die Reihenfolge der Nummern entspricht den Abhängigkeiten.
