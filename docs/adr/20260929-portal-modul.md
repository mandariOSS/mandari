# Bürgerportal als eigenes Fachmodul; `insight_core` wird reiner RIS-Bestand

- Status: angenommen
- Datum: 2026-09-29
- Issue: #476
- Paket: Datendrehscheibe, A11
- Hängt ab von: [A1 Schichtenmodell](20260929-schichtenmodell.md),
  [A7 Kanonisches Modell](20260929-kanonisches-modell.md)

## Kontext

Die App `insight_core` enthält zwei Dinge, die nach dem Schichtenmodell in verschiedene Schichten
gehören:

- den **RIS-Bestand** aller Kommunen (Modelle `OParl*`, Tabellen `oparl_*`), den auch Work, die App
  und die Open-Data-Plattform brauchen (Drehscheibe);
- das **Bürgerportal** als Produkt: Seiten, Templates, Abos und Merkliste, Beschluss-Abos,
  Ratsfragen, Kontaktanfragen, Chat-Oberfläche, Ortszuordnung und Kartenkacheln (Fachmodul).

Dazu kommen Teile von `insight_ai`, soweit es um die Oberfläche von Chat und Zusammenfassung geht.
Solange beides in einer App liegt, kann `import-linter` Portal und RIS-Bestand nicht
unterscheiden. An den RIS-Tabellen hängen 34 Fremdschlüssel aus anderen Apps und 18 Migrationen;
alle Modelle haben feste Tabellennamen (`db_table`).

## Entscheidung

- Eine neue App `apps/portal` (Label `portal`) übernimmt das Bürgerportal: Views, URLs, Templates,
  Middleware und Context-Processors des Portals sowie die Modelle für Abos, Merkliste,
  Benachrichtigungsprotokoll, Beschluss-Abos, Ratsfragen, Kontaktanfragen und Chat-Nutzung. Ob die
  Geo-Modelle (Ortszuordnung, Straßen, Adressen, Kartenkacheln) zum Portal oder zur Drehscheibe
  gehören, wird bei der Umsetzung je Modell entschieden: Nutzt nur das Portal sie, gehören sie zum
  Portal.
- **State-Move statt Datenumzug:** Die Modelle wechseln per `SeparateDatabaseAndState` die App; die
  Tabellen bleiben mit ihrem `db_table` unverändert, es werden keine Daten kopiert. ContentTypes
  und Berechtigungen werden per Datenmigration auf das neue Label umgehängt.
- `insight_core` behält Label und Tabellen und enthält danach **nur noch den RIS-Bestand**. Eine
  Umbenennung findet nicht statt.
- Die URLs des Portals bleiben unverändert.
- Das Portal ist ein Fachmodul im Sinne von [A1](20260929-schichtenmodell.md): Es kennt Session
  und Work nicht, liest RIS-Daten nur über die Fassade der Drehscheibe
  ([A7](20260929-kanonisches-modell.md)) und nur die öffentliche Sicht über einen zentralen Filter.
- Abo-Treffer, Zusammenfassungen und Beschluss-Abos werden ein Abonnement des Portals auf
  öffentliche `ris.*`-Ereignisse ([A2](20260929-ereignistechnik-postgres.md)); Mails laufen als
  Aufträge, nicht in der Anfrage.
- Beschluss-Abos verweisen heute per Fremdschlüssel auf Tagesordnungspunkte in Session. Sie
  verweisen künftig auf die kanonische Kennung des Tagesordnungspunkts im RIS-Bestand; Beschluss
  und Umsetzung liest das Portal aus dem kanonischen Modell statt aus Session-Tabellen.
- Der Name `portal` vermeidet die Verwechslung mit den historischen `insight_*`-Paketen.

## Alternativen

- **`insight_core` umbenennen oder die RIS-Modelle in eine neue App verschieben.** Betrifft 34
  Fremdschlüssel, Migrationsabhängigkeiten, ContentTypes und den Schema-Vertrag des Ingestors;
  teuer und riskant ohne fachlichen Gewinn. Verworfen.
- **Neue Tabellen mit Datenkopie.** Ausfallzeit oder Doppelhaltung, schwieriger Rückfall.
  Verworfen.
- **Portal in `insight_core` lassen und nur per Konvention trennen.** Die Schichten wären nicht
  prüfbar. Verworfen.
- **Portal sofort als eigener Dienst mit eigener Datenbank.** Verfrüht; bleibt Ausbaustufe, wenn
  ein Anlass besteht (Last trotz getrennter Prozessrollen, geforderte Sicherheitszonen, getrennter
  Betrieb). Die hier beschriebene Trennung ist dafür Voraussetzung.

## Folgen

**Positiv**

- Klare Schichten: Das Portal wird ein schlankes Fachmodul, der RIS-Bestand steht allen Modulen
  gleichberechtigt zur Verfügung.
- Getrennte Prozessrollen für öffentliche Seiten und Arbeitsbereiche sowie Installationsprofile
  ohne Portal werden einfacher.
- Keine Datenmigration, keine Ausfallzeit, Tabellen unverändert.

**Negativ**

- `insight_core` und einige Tabellennamen tragen weiter historische Namen.
- State-Move-Migrationen sind heikel (Abhängigkeiten, ContentTypes, Berechtigungen, generische
  Relationen, Admin-Protokoll); der Rückfall auf das vorherige Image muss getestet werden.
- Viele Dateien werden verschoben; parallele Arbeit am Portal kollidiert in dieser Zeit.

## Prüfung (Fitnessfunktion)

- Test: `insight_core` enthält nur Modelle des RIS-Bestands (Positivliste).
- `makemigrations --check` meldet keine offenen Änderungen; die State-Move-Migrationen erzeugen
  kein DDL auf den verschobenen Tabellen.
- Test: Die Liste der `db_table`-Namen ist vor und nach dem Umzug gleich.
- Test: Alle bisherigen Portal-URLs lösen auf dieselben Pfade auf.
- import-linter: `apps.portal` ist unabhängig von `apps.session` und `apps.work`; `OParl*`-Modelle
  werden im Portal nur über die Fassade gelesen.
- Test gegen nichtöffentliche Inhalte in Seiten, Suche, Sitemap und Feed des Portals.
- Rückfallprobe: vorheriges Image gegen die migrierte Datenbank, Smoke-Test.

## Bezug

- [A1 Schichtenmodell](20260929-schichtenmodell.md), [A2 Ereignistechnik](20260929-ereignistechnik-postgres.md),
  [A7 Kanonisches Modell](20260929-kanonisches-modell.md)
- `docs/INSIGHT_QUESTIONS.md`, `docs/INSIGHT_DECISION_TRACKING.md`
- Django, `SeparateDatabaseAndState`:
  https://docs.djangoproject.com/en/stable/ref/migration-operations/#separatedatabaseandstate
