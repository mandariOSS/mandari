# Vier Schichten mit durchgesetzten Abhängigkeitsregeln

- Status: angenommen
- Datum: 2026-09-29
- Issue: #476
- Paket: Datendrehscheibe, A1
- Hängt ab von: –

## Kontext

mandari ist ein Django-Monolith mit drei Produkten (Session, Work, Bürgerportal) und einem
eigenständigen Ingestor. Grenzen zwischen diesen Teilen gibt es bisher nur als Konvention. In der
Praxis schreiben Fachmodule direkt in Tabellen anderer Module, Signale verketten Session mit Work
und dem Bürgerportal, den Suchindex beschreiben drei Wege, die Texterkennung existiert zweimal.
Jede neue Schnittstelle (App, Open-Data-Plattform, Partnersysteme, Änderungsfeed) müsste heute an
mehrere Produkte einzeln angebunden werden.

Das Paket `insight` vermischt zwei Dinge: das Produkt Bürgerportal und den RIS-Bestand aller
Kommunen samt Synchronisation, Suche und OParl-Schnittstelle. Den RIS-Bestand brauchen aber auch
Work, die App und die Open-Data-Plattform.

Die Ursachen liegen innen: Es fehlen Eigentümerschaft je Datenbestand und Verträge zwischen den
Modulen. Eine Zerlegung in Dienste würde diese Probleme über Netzgrenzen verteilen, nicht lösen.

## Entscheidung

mandari bleibt **ein modularer Monolith** (ein Image, mehrere Prozessrollen) und wird in vier
Schichten geschnitten:

| Schicht | Inhalt | Pakete (Ziel) |
|---|---|---|
| 1 Zugänge | Menschen über Oberflächen, Maschinen über die offene Schnittstelle, Partnersysteme über Adapter | keine eigene Software |
| 2 Fachmodule | Session, Work, Bürgerportal, künftig Data; besitzen ihre Fachdaten | `apps/session`, `apps/work`, `apps/portal`, `apps/minutes` |
| 3 Datendrehscheibe | gemeinsames RIS-Modell und RIS-Bestand, Verträge, Sichten, Adapter, offene Schnittstelle | `hub/`, `insight_core` (nur RIS-Bestand), `ingestor/` |
| 4 Plattform | Identität, Mandanten, Rechte, Audit, Verschlüsselung, Mail, Dateien, Ereignistechnik, Aufträge, Dokument- und KI-Dienste, Betrieb | `apps/events`, `apps/common`, `apps/accounts`, `apps/tenants` |

**Regeln:**

1. Abhängigkeiten zeigen nur nach unten.
2. Jeder Datenbestand hat genau einen Eigentümer. Nur er schreibt.
3. Fachmodule kennen sich nicht. Sie tauschen sich über Ereignisse
   ([Ereignistechnik](20260929-ereignistechnik-postgres.md)) und Befehle
   ([Befehle](20260929-befehle-synchron.md)) aus.
4. Nach außen führt nur die Drehscheibe. Ausnahmen: Menschen über die Oberfläche ihres Moduls,
   Anmeldung und SSO über die Plattform.
5. Die Plattform kennt keine Fachbegriffe. Importiert ein Plattformdienst eine Sitzung oder
   Vorlage, ist der Schnitt falsch.

**Faustregel für neuen Code:** Kommt er ohne Fachbegriffe aus, gehört er in die Plattform. Braucht
er die Begriffe mehrerer Module oder eines Partners, gehört er in die Drehscheibe. Sonst bleibt er
im Fachmodul.

**Durchsetzung** mit `import-linter` in `mandari/pyproject.toml`, drei Verträge:

- `layers`: `apps.session | apps.work | apps.portal | apps.minutes` über `hub | insight_core` über
  `apps.events | apps.common | apps.accounts | apps.tenants`
- `independence`: `apps.session`, `apps.work`, `apps.portal`
- `forbidden`: Die Plattform importiert weder Fachmodule noch die Drehscheibe.

Bestehende Verstöße stehen in `ignore_imports`. Die Liste darf nur kürzer werden (Ratchet wie in
[20260909-qualitaetsgates-ci](20260909-qualitaetsgates-ci.md)); Ziel ist eine leere Liste.
Importe innerhalb von Funktionen zählen mit.

**Übergangspakete:** `insight_search` und `oparl_api` gehen in der Drehscheibe auf
(`hub/projections`, `hub/api`), `insight_sync` im Ingestor. `insight_ai` teilt sich auf das Portal
(Oberfläche) und die Plattform (ein KI-Client) auf. Der Ingestor ist ein eigenes Programm; seine
Grenze zur Datenbank sichert der Schema-Vertrag
([20260909-schema-contract-django-ingestor](20260909-schema-contract-django-ingestor.md)).

## Alternativen

- **Microservices je Produkt oder Fähigkeit.** Audit-Hash-Kette und Vier-Augen-Prinzip hängen an
  Datenbanktransaktionen; getrennte Dienste bräuchten verteilte Abläufe, einen Broker, ein Schema
  je Dienst und mehr Container in jeder kommunalen Installation, dazu Grundschutz-Modellierung je
  Dienst. Verworfen. Die Schichten halten eine spätere Abtrennung einzelner Teile offen, wenn ein
  konkreter Anlass besteht (Last, Sicherheitszonen, getrennter Betrieb).
- **Drei Schichten, Drehscheibe als Teil der Plattform.** Dann müsste die Plattform Sitzungen und
  Vorlagen kennen. Vergleichbare Systeme (GitLab, OpenProject) und Referenzmodelle (DIN SPEC 91357,
  modulare Monolithen nach Grzybek) trennen die fachfreie Mechanik von der fachlichen Integration.
  Verworfen.
- **Regeln nur als Konvention und im Review.** Grenzen erodieren erfahrungsgemäß, gerade bei
  hohem Änderungstempo. Verworfen.
- **Tach statt import-linter.** Gleichwertig für unsere Zwecke. import-linter deckt die drei
  benötigten Vertragsarten ab und passt in die vorhandene Werkzeugkette; ein späterer Wechsel
  ändert die Architektur nicht.
- **Eigene Pakete oder Repositories je Modul.** Harte Grenzen, aber Versionierung zwischen Paketen
  und getrennte Releases ohne Nutzen für ein kleines Team und für On-Prem-Betreiber, die eine
  Version installieren. Verworfen.

## Folgen

**Positiv**

- Grenzen sind maschinell prüfbar; neue Verstöße scheitern in der CI.
- Neue Clients und Partner binden sich an die Drehscheibe statt an einzelne Produkte. Der Aufwand
  wächst linear mit der Zahl der Anschlüsse, nicht mit dem Produkt aus Anschlüssen und Modulen.
- Das Bürgerportal wird ein schlankes Fachmodul, der RIS-Bestand steht allen Modulen zur Verfügung
  ([Portal-Modul](20260929-portal-modul.md)).
- Installationsprofile wie „nur Session“ werden möglich.

**Negativ**

- Die Ausnahmeliste ist zu Beginn lang; ihr Abbau braucht Disziplin über viele Releases.
- Modulübergreifende Folgen erscheinen verzögert statt in derselben Anfrage. Oberflächen zeigen
  sie als „wird übernommen“.
- Fassaden und Befehle sind zusätzliche Indirektion gegenüber direkten ORM-Zugriffen.
- Das Label `insight_core` bleibt bestehen, obwohl es nur noch den RIS-Bestand enthält; eine
  Umbenennung würde zu viele Abhängigkeiten berühren.

## Prüfung (Fitnessfunktion)

- CI-Schritt `lint-imports` mit den drei Verträgen, blockierend.
- Ratchet-Skript zählt die Einträge in `ignore_imports`: Anstieg bricht den Build, ein Rückgang
  wird im selben Pull Request als neue Baseline festgeschrieben.
- Kennzahlen je Release: Zahl der Ausnahmen, Zugriffe auf RIS-Modelle außerhalb der Drehscheibe
  ([Kanonisches Modell](20260929-kanonisches-modell.md)), Schreibzugriffe auf Daten fremder Module.
- Definition of Done: Kein Pull Request verschlechtert den import-linter-Stand.

## Bezug

- ADR-Paket Datendrehscheibe: Grundlage für A2 bis A11
- [20260909-qualitaetsgates-ci](20260909-qualitaetsgates-ci.md),
  [20260909-schema-contract-django-ingestor](20260909-schema-contract-django-ingestor.md)
- `docs/ENGINEERING_STANDARDS.md`, Abschnitt 1
- import-linter: https://import-linter.readthedocs.io/
- GitLab EventStore: https://docs.gitlab.com/development/eventstore/
- K. Grzybek, Modular Monolith with DDD: https://github.com/kgrzybek/modular-monolith-with-ddd
