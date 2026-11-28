# Installationsprofile: Module schaltbar, Kriterien für eigene Dienste

- Status: vorgeschlagen (Entwurf). Stufe 1 ist im Code: Schalter, Katalog, Prüfung je Profil in der CI.
- Datum: 2026-11-28
- Issue: #1594 (Teil von #495)
- Hängt ab von: [A1 Schichtenmodell](20260929-schichtenmodell.md),
  [A2 Ereignistechnik](20260929-ereignistechnik-postgres.md),
  [A4 Aufträge und Zeitpläne](20260929-auftraege-und-zeitplaene.md),
  [A6 Befehle](20260929-befehle-synchron.md),
  [A8 Fremdschlüssel](20260929-fremdschluessel-ris-bestand.md),
  [A10 Änderungsfeed](20260929-aenderungsfeed-format.md),
  [A11 Portal-Modul](20260929-portal-modul.md),
  [Eine Schnittstelle](20261105-eine-schnittstelle-api-first.md)

## Kontext

Jede Installation enthält heute Session, Work und das Bürgerportal. Eine Verwaltung, die nur den Sitzungsdienst
betreibt, bekommt trotzdem die Adressen, Menüs, Zeitpläne und Abonnements der beiden anderen Module. Das
Schichtenmodell (A1) nennt Installationsprofile wie „nur Session“ als Ziel; geprüft wurde es bisher nirgends.

Dazu kommt die Frage, welche Teile als eigene Dienste laufen sollen. Der Ingestor ist schon ein eigener Prozess,
Texterkennung und Live-Übertragung laufen in eigenen Workern. Für weitere Schnitte fehlt ein Maßstab: Eine
Zerlegung ohne Nutzen kostet Betrieb, Latenz und Konsistenz.

Hindernisse für ein echtes „nur Session“:

- Fremdschlüssel zwischen den Modulen: Anträge in Work verweisen auf Session, Beschluss-Abos des Bürgerportals auf
  Tagesordnungspunkte in Session (A11), Protokollierung auf beide.
- Bekannte Importe über Modulgrenzen, festgehalten als Bestand im Vertrag `module-unabhaengig` (import-linter).
- Das Bürgerportal liegt noch in `insight_core`, zusammen mit dem RIS-Bestand (A11 ist nicht umgesetzt).

## Entscheidung

### 1. Ein Schalter je Modul, Apps bleiben installiert (Stufe 1)

- `MODUL_SESSION`, `MODUL_WORK` und `MODUL_PORTAL` (`an` oder `aus`, Standard `an`) ergeben
  `settings.MANDARI_MODULE`. Compose liest sie aus der `.env`, Helm aus `module.*`.
- Ein abgeschaltetes Modul hat keine Adressen (HTTP und WebSocket), keine Menüeinträge (auch nicht in der
  Seitenleiste des Admins), keine Zeitpläne, keine Abonnements, keine Bereiche im Einstieg der Schnittstelle und
  keine Befehle. Middleware, die in seine Adressen weiterleitet, entfällt.
- Seine Apps bleiben in `INSTALLED_APPS`: Tabellen, Migrationen und Fremdschlüssel bleiben unverändert. Signale
  bleiben verbunden, der Admin zeigt die Modelle weiter, Verwaltungsbefehle lassen sich weiter aufrufen.
- **Katalog an einer Stelle** (`apps/common/module.py`): je Modul der Code, der es voraussetzt (Pakete, Module,
  Klassen, Funktionen, auch Stellen anderer Module wie die öffentlichen Fraktionsprotokolle im Bürgerportal), die
  Anfänge seiner Adressen und die Apps, die ganz zu ihm gehören. Der Katalog kennt die Module nur als
  Zeichenketten; die Plattform importiert weiter keine Fachmodule.
- **Ausblenden an der Quelle:** `mandari/urls.py`, `insight_core/urls.py` und `mandari/routing.py` binden Adressen
  nur eingeschalteter Module ein und importieren deren Ansichten erst dann. Die Ereignistechnik lädt
  `schedules.py` und `subscribers.py` abgeschalteter Apps nicht und registriert keinen Zeitplan und kein
  Abonnement, dessen Code ein abgeschaltetes Modul voraussetzt. Verweise in ein anderes Modul stehen in Vorlagen
  nur unter `{% if "<modul>"|modul_aktiv %}`.
- **Gemeinsam genutzt:** Was im Code oder unter den Adressen eines Moduls liegt, aber auch ohne es gebraucht wird,
  steht in `gemeinsam` des Katalogs (heute Dokumentvorschau und Kartenkacheln unter `/insight/`, die die Recherche
  in Work nutzt). Die Liste soll nur kürzer werden: Jeder Eintrag ist eine Stelle, die in die Drehscheibe gehört.

### 2. Prüfung je Profil in der CI

Der Job „Installationsprofile“ startet vier Profile (alle, nur Session, nur Work, nur Bürgerportal) gegen
PostgreSQL: `manage.py check`, `migrate` und `manage.py installationsprofil_pruefen`. Ein Befund ist

- eine Adresse (HTTP oder WebSocket), deren Ansicht oder Pfad ein abgeschaltetes Modul voraussetzt (zugleich
  Systemprüfung `module.E002`),
- ein registrierter Zeitplan oder ein registriertes Abonnement eines abgeschalteten Moduls,
- eine Probeseite mit Fehler, die Probeadresse eines abgeschalteten Moduls ohne 404, ein Verweis auf eine
  Adresse eines abgeschalteten Moduls, ein Bereich eines abgeschalteten Moduls im Einstieg `/api/`,
- ein geladener Einstieg (`urls`, `views`, `routing`, `consumers`, `schedules`, `subscribers`, `commands`,
  `api`) einer App, die ganz zu einem abgeschalteten Modul gehört.

Zugriffe auf Modelle eines anderen Moduls prüft weiter import-linter (`module-unabhaengig`); dessen Bestand ist
die Arbeitsliste für Stufe 2.

### 3. Stufe 2: abgeschaltete Module ohne Tabellen

Ein abgeschaltetes Modul verlässt `INSTALLED_APPS` erst, wenn kein anderes Modul mehr Fremdschlüssel auf seine
Tabellen hat und der Bestand im Vertrag `module-unabhaengig` für dieses Modul leer ist. Für das Bürgerportal setzt
das A11 voraus (eigene App `apps/portal`). Bis dahin prüft die CI beim Bürgerportal Adressen, Zeitpläne,
Abonnements und Probeseiten, nicht die geladenen Einstiege.

### 4. Kriterien für einen eigenen Dienst

Ein Teil von mandari wird ein eigener Dienst (eigener Container mit eigenem Prozess, eigener Rolle und eigenem
Rückfall), wenn **alle** Bedingungen erfüllt sind:

| Nr. | Kriterium | Woran man es misst |
|---|---|---|
| D1 | **Eigener Lastverlauf:** Die Last kommt zu anderen Zeiten oder in anderer Größe als die der Webanfragen, und getrennte Prozessrollen im selben Image reichen nicht. | Metriken je Rolle (`docs/MONITORING.md`): CPU, Speicher, Warteschlangen; Antwortzeiten des Webprozesses während der Spitzen des Teils |
| D2 | **Eigene Ausfallgrenze:** Fällt der Teil aus, laufen die übrigen weiter, und er läuft mit einem älteren Stand der übrigen weiter. | Ausfallprobe in Staging: Teil anhalten, Kernseiten und Worker prüfen; Wiederanlauf ab Cursor |
| D3 | **Eigener Datenbestand:** Der Teil schreibt nur eigene Tabellen oder Speicher; fremde Daten liest er nur über den Änderungsfeed (A10) oder die offene Schnittstelle. Keine Fremdschlüssel in fremde Tabellen (A8). | Kennzahl K1 (Funktionen, die fremde Daten schreiben) für den Teil gleich 0; keine Fremdschlüssel über die Grenze |
| D4 | **Kommunikation nur über Feed und Befehle:** Ereignisse und Feed hinein, Befehle mit Idempotenzschlüssel hinaus (A6, HTTP-Client). Gemeinsamer Code nur aus `shared/` und den Verträgen (`hub/contracts`). | import-linter-Vertrag für den Teil ohne Ausnahmen; Befehle des Teils im Register |
| D5 | **Belegter Nutzen:** Eine Kennzahl verbessert sich nachweisbar: Antwortzeit des Webprozesses bei Spitzen, Wiederanlaufzeit, Betrieb beim Kunden (etwa Texterkennung auf eigener Hardware), Kosten. | Messung vor und nach dem Schnitt, festgehalten im Issue |
| D6 | **Betrieb tragbar:** eigener Container je Rolle, Gesundheitsprüfung, Metriken, dokumentierter Rückfall auf den gemeinsamen Prozess. | `DEPLOYMENT.md`, `docs/MONITORING.md`, Helm-Chart |

D1 bis D4 sagen, ob ein Schnitt möglich ist, D5, ob er sich lohnt, D6, ob er betreibbar ist. Fehlt D5, bleibt der
Teil im Monolithen, auch wenn D1 bis D4 erfüllt sind. Getrennte Prozessrollen (Worker je Warteschlange, eigener
Server für die Texterkennung) sind der erste Schritt und oft genug.

### 5. Kandidaten

Zu prüfen und zu bewerten (Issue #1594, je Kandidat eine Zeile mit D1 bis D6):

- Ingestor: schon eigener Prozess und eigenes Image; schreibt den RIS-Bestand direkt (Schema-Vertrag), nicht über
  Befehle (D3, D4 offen).
- Suchindex: eigenes Ziel (Elasticsearch), gefüllt über das Abonnement `suchindex` (D3, D4 nahe).
- Dokumentkette mit Texterkennung (#919): eigene Warteschlangen und Worker, eigener Server möglich (D1, D6 nahe).
- Bürgerportal (#491): erst A11, dann Bezug der Daten nur über die offene Schnittstelle (D3, D4 offen).
- Externe Sitzungsdienste (Live-Übertragung, externer Protokolldienst): laufen außerhalb und sprechen über
  Schnittstelle und Befehle.

## Alternativen

- **Module aus `INSTALLED_APPS` nehmen.** Bricht Migrationen und Fremdschlüssel anderer Module; erst Stufe 2.
- **Adressen registriert lassen und per Middleware mit 404 beantworten.** Menüs, Zeitpläne und Abonnements
  liefen weiter, Verweise fielen erst beim Klick auf. Verworfen: Was es nicht gibt, soll nicht registriert sein.
- **Ein Profilname statt drei Schalter** (`INSTALLATIONSPROFIL=session`). Weniger flexibel (Session mit
  Bürgerportal), und jeder neue Mix bräuchte einen Namen. Verworfen; die CI prüft die vier Grundprofile.
- **Sofort Dienste schneiden.** Ohne Kriterien und Messung entscheidet der Geschmack. Verworfen.

## Folgen

**Positiv**

- Installationen nur mit Session, nur mit Work oder nur mit Bürgerportal sind möglich und in der CI geprüft.
- Neue Kopplungen fallen auf: Ein Verweis, eine Adresse oder ein Zeitplan in ein abgeschaltetes Modul macht den
  Job rot.
- Für eigene Dienste gibt es einen Maßstab statt Einzelfallentscheidungen.

**Negativ**

- Stufe 1 lässt Tabellen, Signale, Admin und Verwaltungsbefehle abgeschalteter Module stehen. Seiten eines
  eingeschalteten Moduls, die Tabellen eines abgeschalteten lesen (Bestand in `module-unabhaengig`, etwa
  „Ladungen der Verwaltung“ in Work), zeigen dann nichts.
- Der Katalog muss gepflegt werden: Neue Stellen, die ein anderes Modul voraussetzen, gehören in dessen `code`.
- Der Job „Installationsprofile“ braucht je Profil einen Läufer mit PostgreSQL (vier Läufer je Änderung am Code).

## Prüfung (Fitnessfunktion)

- CI-Job „Installationsprofile“ (`manage.py installationsprofil_pruefen`) für alle, nur Session, nur Work, nur
  Bürgerportal.
- Systemprüfung `module.E001` (unbekanntes Modul) und `module.E002` (Adresse eines abgeschalteten Moduls).
- `mandari/apps/common/tests/test_module.py`: Schalter, Katalog, Filter der Register, Vorlagenfilter.
- import-linter `module-unabhaengig`: Bestand darf nur kürzer werden.

## Bezug

- `apps/common/module.py`, `mandari/routing.py`, `apps/common/management/commands/installationsprofil_pruefen.py`
- `DEPLOYMENT.md`, „Installationsprofile: Module abschalten“
- `docs/DATENDREHSCHEIBE.md`, Abschnitt 5 (Architektur-Kennzahlen)
