# Beschlusskontrolle (Session) und Sichtbarkeit für Mandatsträger (Work)

Issue #37. Nach der Beschlussfassung dokumentiert die Verwaltung im Session-RIS je Beschluss
(`SessionAgendaItem` mit `vote_result`) den Umsetzungsstand: zuständige Stelle, Erledigungsfrist,
Status (offen / in Umsetzung / erledigt / zurückgestellt) und Erledigungsvermerk.

## Verwaltung (Session)

- Beschlussregister: `<tenant>/resolutions/` mit Ampel (offen, in Umsetzung, überfällig, erledigt),
  Filtern nach Gremium, Jahr, Ergebnis, Umsetzungsstand, CSV-Export
- Pflege je Beschluss: `<tenant>/agenda/<top>/tracking/` (Recht `edit_meetings`), Audit-Eintrag
- Wiedervorlage: Erinnerung bei nahender oder überschrittener Frist (siehe `docs/SESSION_REMINDERS.md`)

## Mandatsträger (Work)

- Seite *Ratsinformation → Beschlüsse* (`/work/<org>/ris/decisions/`, Recht `ris.view`)
- Datenquelle: alle aktiven Session-Mandanten, deren `oparl_body` mit den Kommunen der
  Organisation verknüpft ist (`Organization.get_all_bodies()`)
- **Nur öffentliche Inhalte**: TOP und Sitzung müssen `is_public` sein, abgesetzte TOPs und TOPs
  ohne Ergebnis fehlen. Nicht-öffentliche Beschlüsse erscheinen weder in der Liste noch in der Suche.
- Ampel-Kacheln (offen / in Umsetzung / überfällig / erledigt), Filter nach Gremium, Jahr,
  Umsetzungsstand, „nur überfällig“ und Volltext (Betreff, Beschlusstext, Nummer, zuständiges Amt)
- Je Beschluss: Nummer, Ergebnis, Betreff, Beschlusstext, Gremium und Sitzungsdatum, Umsetzungsstand
  mit Erledigungsvermerk und Stand-Datum, zuständige Stelle, Frist (überfällig rot), Link zur
  Sitzung im RIS (wenn die Sitzung per OParl verknüpft ist)
- Ohne Session-Mandant zur Kommune zeigt die Seite einen Hinweis statt Daten.

Test: `scripts/smoke_work_decisions.py`.
