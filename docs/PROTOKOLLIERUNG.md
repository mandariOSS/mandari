# Protokollierung: Systemprotokolle und fachliches Audit-Log

Bezug: BSI IT-Grundschutz OPS.1.1.5 (Protokollierung, Anforderungen A6, A8, A10) und
CON.10.A17 (Überlauf überwachen); Issues #221, #263, #366 (Teil von #217).

Diese Seite beschreibt, was mandari protokolliert, wozu, wie lange, wer die Protokolle einsehen
darf und wie sie gesichert und gegen nachträgliche Veränderung geschützt sind. Sie richtet sich
an Betreiber, Verwaltungen, Revision, Datenschutzbeauftragte und Personalvertretungen.
Verbindlich sind stets die Regelungen der jeweiligen Kommune; die genannten Fristen und
Voreinstellungen lassen sich anpassen.

Leitgedanke: **so lange wie nötig, nicht wie möglich.** Protokolle enthalten IP-Adressen und
Kennungen; jede Frist ist zugleich eine Löschfrist. Nicht protokolliert werden Inhalte von
Dokumenten, Passwörter, Tokens oder Mailtexte.

Aufbau: Die Übersicht nennt alle Protokollarten. Die Abschnitte 1 bis 11 betreffen das
fachliche Audit-Log im Sitzungsdienst (Session), Abschnitt 12 die Systemprotokolle der
Installation, Abschnitt 13 den Abgleich mit dem BSI-Baustein.

## Übersicht: Protokollarten und Aufbewahrung

| Protokoll | Inhalt | Aufbewahrung | Wo | Begründung |
|---|---|---|---|---|
| **Anwendungs- und Container-Logs** | Strukturierte JSON-Zeilen der Anwendung (Anfrage-Kennung, keine personenbezogenen Inhalte), Worker, Ingestor, Postgres, Elasticsearch, Redis, Website | **90 Tage**, höchstens 8 GB | systemd-Journal des Hosts (`journalctl CONTAINER_NAME=…`, Abschnitt 12.1) | Sicherheitsvorfälle werden oft erst Wochen später erkannt; 90 Tage decken die Frage „seit wann?“ ab, ohne Bewegungsprofile über Monate zu bilden |
| **Zugriffslog des Reverse Proxy** | Caddy-Zugriffslog (IP, Pfad, Status, Zeit) | **14 Tage** | Datei im Caddy-Datenvolume (`/data/access.log`), zeitlich rollierend | IP-Adressen; nötig für Störungsanalyse und Missbrauchserkennung (Art. 6 Abs. 1 lit. f DSGVO), danach ohne Zweck |
| **Fachliches Audit-Log** (Session) | Wer hat wann was geändert oder Nichtöffentliches angesehen; nicht änder- oder löschbar | **je Mandant** (`audit_years`, Einstellungen → Datenschutz), Empfehlung 5–10 Jahre | Datenbank, in jeder Sicherung | Nachweis für Gremienarbeit; Frist folgt Archiv- und Aufbewahrungsvorschriften der Kommune (Abschnitt 10, [Löschkonzept](DATENSCHUTZ.md)) |
| **Archivpakete des Protokolls** | Vor der fristgerechten Löschung archivierte Einträge (JSON, CSV, Kettenanker, `SHA256SUMS`) | **je Mandant** ab Ablage (`archive_years`, Einstellungen → Datenschutz), Sicherheitsprotokoll `SECURITY_AUDIT_ARCHIVE_RETENTION_DAYS`; Standard jeweils 0 = keine Löschung | Archivspeicher (`AUDIT_ARCHIVE_ROOT` bzw. `AUDIT_ARCHIVE_STORAGE`) | Nachweis über die Frist des Protokolls hinaus; vor der Löschung dem Kommunalarchiv anbieten (Abschnitt 10) |
| **Sicherheitsprotokoll** (plattformweit) | Anmeldungen von Konten ohne Session-Mandant, Fehlversuche mit unbekannter Kennung (nur als HMAC), von der Drosselung abgewiesene Versuche, Eingriffe in den Betrieb | `SECURITY_AUDIT_RETENTION_DAYS`, Standard 365 Tage | Datenbank, eigene Hash-Kette | nur für den Plattformbetrieb (Abschnitte 4 und 10) |
| **Anmeldeversuche für die Drosselung** (`LoginAttempt`) | E-Mail-Adresse, IP-Adresse, Browserkennung und Ergebnis je Anmelde- und Code-Versuch | **30 Tage** (`LOGIN_ATTEMPT_RETENTION_DAYS`, 0 = keine Löschung), täglich gelöscht (`befehl:purge_login_attempts`) | Datenbank | Datenschutz: E-Mail- und IP-Adressen nur so lange wie nötig. Die Drosselung zählt nur 15 Minuten; Anmeldungen und Fehlversuche stehen zusätzlich im Sicherheits- bzw. Mandantenprotokoll mit eigener Frist (Abschnitt 4) |
| **Logdateien der Cron-Jobs** | Ausgaben der Host-Cronjobs (Backup-Watchdog, Neustart ungesunder Container, Überlaufwarnung; `/var/log/mandari-*.log`); Erinnerungen, Fotos und Löschläufe laufen als Zeitpläne im Worker und schreiben in dessen Container-Log | **12 Wochen**, komprimiert | Host, `logrotate` | Nur Zahlen und Statusmeldungen; für die Nachvollziehbarkeit geplanter Läufe |
| **Backup-Statusdateien** | Erfolg, Dauer, Prüfergebnisse je Lauf | letzter Stand je Lauf | `status/` des Backup-Stacks | Grundlage der Watchdog-Mail ([BACKUP.md](BACKUP.md)) |

## 1. Audit-Log: Zweck und Grundsätze

Das Protokoll macht nachvollziehbar, wer wann welche Inhalte angesehen, geändert oder
heruntergeladen hat – insbesondere nichtöffentliche. Es dient

- der **Rechenschaftspflicht** der Verwaltung (Art. 5 Abs. 2 DSGVO) und dem Nachweis
  ordnungsgemäßer Verfahren gegenüber Prüfinstanzen,
- der **Sicherheit der Verarbeitung** (Art. 32 DSGVO): Unberechtigte Zugriffe und Manipulationen
  sollen erkennbar und aufklärbar sein,
- der **Aufklärung von Verstößen** gegen die Verschwiegenheit bei nichtöffentlichen Beratungen.

Dabei gelten vier Grundsätze:

1. **Datensparsamkeit:** Protokolliert werden Person, Zeitpunkt, Vorgang und Objekt-Referenz –
   nie Inhalte. Verschlüsselte Felder erscheinen nur mit dem Vermerk, *dass* sie geändert wurden.
2. **Zweckbindung:** Das Protokoll dient ausschließlich den oben genannten Zwecken. Eine Nutzung
   zur Leistungs- oder Verhaltenskontrolle der Beschäftigten ist nicht vorgesehen.
3. **Funktionstrennung:** Wer das System verwaltet, kontrolliert nicht zugleich das Protokoll.
   Einsicht und Export sind eigene Rechte (Abschnitt 6).
4. **Unveränderbarkeit:** Einträge lassen sich in der Anwendung weder ändern noch einzeln löschen;
   eine Hash-Kette macht Veränderungen an der Anwendung vorbei erkennbar (Abschnitt 7).

## 2. Was protokolliert wird

| Bereich | Einträge | Aktion im Protokoll |
|---|---|---|
| Änderungen an Kernobjekten | Sitzungen, TOPs, Vorlagen, Anträge, Niederschriften, Personen, Gremien, Mitgliedschaften, Anwesenheiten, Beratungsfolge, Wahlperioden, Anlagen, Nutzer | Erstellt, Geändert, Gelöscht, Freigegeben, Veröffentlicht, Abgesetzt, Ersetzt, Einladung versandt |
| Stimmabgaben | Erfassung der Einzelstimmen je TOP mit alter und neuer Stimme je Person; Rückläufe bei Umlaufbeschlüssen; Feststellung des Ergebnisses | Stimmabgabe erfasst, Abstimmungsergebnis festgestellt |
| Mitzeichnungen | Entscheidung je Station (mitgezeichnet oder zurückgewiesen, mit Kommentar) | Mitzeichnung entschieden |
| Entschädigungen | Jeder Posten eines Sitzungsgelds und jeder Monatspauschale einzeln: festgesetzt, genehmigt, ausgezahlt, storniert | Entschädigung festgesetzt/genehmigt/ausgezahlt/storniert |
| Rollen und Rechte | Zuweisen und Entziehen von Rollen; Anlegen und Ändern einer Rolle mit erteilten und entzogenen Rechten; Rollen aus einer angenommenen Einladung mit der einladenden Person als Vergebende | Rollen geändert, Rechte geändert |
| Einladungen und Zugänge | Einladen (Adresse, Rollen, Gültigkeit), erneut Senden, Zurückziehen, Annahme; Einreichungs-Zugänge anlegen (Name, Rechte, Ablauf – nie das Token) und zurückziehen | Erstellt, Geändert, Gelöscht |
| Lesezugriffe | Ansicht nichtöffentlicher Sitzungen, TOPs, Vorlagen (auch früherer Fassungen) und Niederschriften; Abruf von Dokumenten mit nichtöffentlichem Inhalt; nichtöffentliche Sitzungen und Vorlagen in Antworten der Session-API | Angesehen, Heruntergeladen |
| Downloads | Jeder Download einer Anlage, einer früheren Fassung und einer Sitzungsmappe; nichtöffentliche sind gekennzeichnet | Heruntergeladen |
| Anmeldungen | Anmeldung, Abmeldung, fehlgeschlagene Anmeldung (falsches Passwort, falscher zweiter Faktor) | Anmeldung, Abmeldung, Anmeldung fehlgeschlagen |
| Zugriffe auf das Protokoll | Einsicht (mit Filter), Export (mit Prüfsumme), Kettenprüfung (mit Ergebnis), Archivierung | Protokoll eingesehen/exportiert/geprüft/archiviert |
| Datenschutz | Änderung der Fristen und Einstellungen, jeder Löschlauf, jede Betroffenenauskunft | Geändert, Gelöscht, Heruntergeladen |

Jeder Eintrag enthält: Zeitpunkt, handelnde Person (bei Vertretungen zusätzlich „in Vertretung
für …“), Aktion, Objekt (Art, Kennung, kurze Beschreibung), Änderungsangaben, IP-Adresse und
Browserkennung sowie die Angaben der Hash-Kette (laufende Nummer, Hash des Vorgängers, eigener
Hash).

## 3. Lesezugriffe auf nichtöffentliche Inhalte

- **Erfasst** werden Ansichten, die nichtöffentliche Inhalte zeigen: eine nichtöffentliche
  Sitzung oder der nichtöffentliche Teil einer Tagesordnung, ein nichtöffentlicher TOP, eine
  nichtöffentliche Vorlage und ihre Fassungen, der nichtöffentliche Teil einer Niederschrift
  sowie erzeugte Dokumente mit nichtöffentlichem Inhalt (Tagesordnung, interne Niederschrift,
  Beschlussauszüge).
- **Session-API:** Listen der Session-API (Sitzungen, Vorlagen) liefern Nichtöffentliches nur
  angemeldeten Personen mit dem Recht dazu, nie API-Token. Je Abruf entsteht **ein** Lesezugriff, etwa
  „Vorlagen (Session-API v1)“, mit Anzahl, Referenzen und Kennungen der nichtöffentlichen Objekte der
  Antwort; so kostet auch eine Seite mit 200 Vorlagen nur einen Eintrag. Derselbe Abruf mit denselben
  Objekten wird wie unten zusammengefasst, mit eigenem Merker getrennt von der Oberfläche. Der Filter
  „Objekt“ der Protokollansicht findet diese Einträge nicht; die Kennungen stehen im Export (Issue #366).
- **Nur die Referenz:** Statt eines Betreffs steht eine neutrale Beschreibung im Protokoll,
  etwa „Vorlage V/2026/0042“ oder „TOP N1, Sitzung vom 12.03.2026“. Inhalte werden nie
  gespeichert.
- **Zusammengefasst:** Ruft dieselbe Person dasselbe Objekt innerhalb von zehn Minuten erneut auf,
  steht der erste Eintrag für den ganzen Zeitraum. So bleibt das Protokoll übersichtlich.
- **Je Mandant abschaltbar:** *Einstellungen → Datenschutz → Protokollierung von Lesezugriffen*.
  Standard ist „an“. Unabhängig davon werden Downloads von Anlagen, Anmeldungen und Zugriffe auf
  das Protokoll selbst immer protokolliert; jede Änderung des Schalters steht im Protokoll.
- **Hinweis zur Mitbestimmung:** Lesezugriffsprotokolle können technisch geeignet sein, Verhalten
  von Beschäftigten zu überwachen. Wir empfehlen, die Personalvertretung vor der Einführung zu
  beteiligen und Zweck, Zugriff und Auswertung in einer Dienstvereinbarung festzuhalten.

## 4. Anmeldungen

Anmeldung, Abmeldung und fehlgeschlagene Anmeldung werden für alle Portale erfasst, denn alle
nutzen dieselbe Anmeldung.

- **Session-Nutzer:** Das Ereignis steht im Protokoll jedes Mandanten, dem das Konto aktiv
  zugeordnet ist. Bei einem Fehlversuch ist nicht belegt, wer es war; das Protokoll nennt deshalb
  nur das betroffene Konto, keine handelnde Person.
- **Alle anderen Konten** (Fraktionen, Bürgerportal, Plattformbetrieb) und Fehlversuche mit
  unbekannter Kennung stehen im **mandantenübergreifenden Sicherheitsprotokoll**. Es ist nur für
  den Plattformbetrieb einsehbar und bildet eine eigene Hash-Kette.
- **Kein Passwort, keine Kennung im Klartext:** Das Passwort wird nie gelesen oder gespeichert.
  Eine eingegebene Kennung ohne passendes Konto wird nur als schlüsselgebundener Hash
  (HMAC-SHA-256 mit einem geheimen Schlüssel der Installation) festgehalten. Ein einfacher
  Hash ließe sich mit Listen üblicher Adressen zurückrechnen; der geheime Schlüssel verhindert
  das. Gleiche Kennungen bleiben dennoch erkennbar, etwa bei wiederholten Angriffen auf eine
  Adresse. Hat jemand versehentlich sein Passwort in das Feld für die E-Mail-Adresse getippt,
  ist es so ebenfalls nicht lesbar.
- Fehler beim Protokollieren verhindern keine Anmeldung; sie werden im Betriebslog vermerkt.
- **Von der Drosselung abgewiesene Versuche** (Ereignis „Anmeldung durch Drosselung abgewiesen“,
  `login_throttled`, Issue #366) stehen immer im Sicherheitsprotokoll, auch bei Session-Nutzern. Erfasst
  werden Passwortschritt, Code- und Schlüsselschritt sowie die Einrichtung des zweiten Faktors bei der
  Anmeldung. **Zusammengefasst je Adresse und Zeitfenster:** Der erste abgewiesene Versuch einer Adresse
  in einem festen 15-Minuten-Fenster schreibt einen Eintrag, alle weiteren in diesem Fenster nicht. IPv6
  zählt wie die Drosselung je /64-Netz. Der Eintrag nennt Schritt, gegriffene Schwelle (Adresse, Netz,
  Konto) und Fensterbeginn, dazu das Konto nur als Referenz (zweiter Schritt) bzw. die eingegebene Kennung
  nur als HMAC (Passwortschritt). Die Zahl der weiteren Abweisungen im Fenster steht nicht im Eintrag,
  denn Einträge sind unveränderbar. Die Zusammenfassung hält der Cache fest; fällt er aus, prüft die
  Datenbank je Adresse bzw. je /64-Netz. Eine Abweisung zählt nicht als weiterer Fehlversuch.
- **Eine Form der Kennung:** Die eingegebene Adresse wird einmal normalisiert (ohne Leerraum am Rand,
  klein) und so für die Zählung der Drosselung, den Anmeldeversuch (`LoginAttempt`) und den HMAC verwendet.
- **Anmeldeversuche für die Drosselung** (`LoginAttempt`: E-Mail-Adresse, IP-Adresse, Browserkennung,
  Ergebnis) braucht die Anwendung nur für die Zählung der letzten 15 Minuten. Der Zeitplan
  `befehl:purge_login_attempts` löscht sie täglich nach `LOGIN_ATTEMPT_RETENTION_DAYS` (Vorgabe
  30 Tage, 0 = keine Löschung, Issue #366). Die Einträge im
  Sicherheits- und Mandantenprotokoll folgen ihren eigenen Fristen (Abschnitt 10).

## 5. Rechtsgrundlage (allgemeiner Hinweis)

Verantwortlich für die Verarbeitung ist die jeweilige Kommune; mandari verarbeitet im Auftrag
(siehe [AVV-Muster](DATENSCHUTZ.md), Abschnitt 1). Die Protokollierung stützt sich in der Regel auf die
Pflicht zur Sicherheit der Verarbeitung und zur Rechenschaft (Art. 5 Abs. 2, Art. 24 und
Art. 32 DSGVO) in Verbindung mit Art. 6 Abs. 1 lit. c oder e DSGVO, ergänzt durch das
Landesdatenschutzrecht, das Kommunalrecht (Verschwiegenheit bei nichtöffentlichen Beratungen)
und das Beschäftigtendatenschutzrecht. Die konkrete Rechtsgrundlage, Fristen und etwaige
Dienstvereinbarungen legt die Kommune fest. Dieses Konzept ersetzt keine Rechtsberatung.

## 6. Zugriff auf das Protokoll

| Recht | Wirkung |
|---|---|
| Audit-Log anzeigen | Protokoll des eigenen Mandanten einsehen und filtern |
| Audit-Log exportieren und prüfen | Zeitraum exportieren, Hash-Kette prüfen, Kettenstatus einsehen |

- Beide sind **Kontrollrechte** und gehören **nicht** zur Administrator-Vollmacht. Auch eine
  Rolle mit „Administrator“ erhält sie nur, wenn sie in der Rechte-Matrix angehakt sind.
- **Standardrollen:** Neue Mandanten erhalten die Rollen „Revision“ und „Datenschutz“ mit beiden
  Kontrollrechten und ohne Zugriff auf Sitzungs- oder Vorlageninhalte. Bestehende Mandanten
  erhalten diese Rollen beim Update ebenfalls; ihre Administrator-Rollen behalten die
  Protokoll-Ansicht als angehakten, jetzt abwählbaren Haken.
- Wer ein Kontrollrecht hat, muss einen **zweiten Faktor** verwenden.
- Jede Rollen- und Rechteänderung steht selbst im Protokoll – auch, wenn sich jemand eine
  Kontrollrolle selbst zuweist.
- **Mandantentrennung:** Jeder Mandant sieht und exportiert ausschließlich sein eigenes Protokoll.
- Jede Einsicht (mit den gesetzten Filtern), jeder Export und jede Prüfung wird protokolliert.

## 7. Manipulationsschutz: Hash-Kette

Jeder Mandant hat eine eigene Kette, ebenso jede Fraktion (Änderungshistorie der
Fraktionssitzungen im Work-Portal) und das Sicherheitsprotokoll. In der Änderungshistorie der Fraktion
stehen seit Issue #366 auch Rollen und Rechte: zugewiesene und entzogene Rollen einer Mitgliedschaft
(auch aus Einladung, Selbstregistrierung und genehmigtem Änderungsantrag), zusätzliche und verweigerte
Einzelrechte, Anlegen, Ändern, Zurücksetzen und Löschen einer Rolle mit erteilten und entzogenen Rechten,
Deaktivieren, Reaktivieren, Freischalten und Entfernen einer Mitgliedschaft sowie die Vereidigung. Offene
Registrierungsanfragen erscheinen erst mit der Freischaltung namentlich; eine abgelehnte Anfrage steht nur
neutral als „Registrierungsanfrage“ mit ihrer internen Kennung im Protokoll, denn das Konto wird danach
gelöscht.

**Grenze:** Erfasst werden die Wege des Work-Portals (Mitgliederverwaltung, Rollen, Einladungen,
Selbstregistrierung, Änderungsanträge). Rollen- und Rechteänderungen über die Django-Administration
(Mitgliedschaften und Rollen), über Verwaltungsbefehle (`setup_roles`, `fix_permissions --fix`) und die
Standardrollen beim Anlegen einer Organisation (`create_default_roles`) schreiben keinen Eintrag in die
Änderungshistorie der Fraktion. Diese Wege sind dem Plattformbetrieb vorbehalten.

- Jeder Eintrag erhält eine **laufende Nummer** (`seq`), den **Hash seines Vorgängers**
  (`prev_hash`; der erste Eintrag verweist auf 64 Nullen) und seinen **eigenen Hash**
  (`entry_hash`).
- Der eigene Hash ist SHA-256 über die kanonische JSON-Darstellung aller Felder des Eintrags
  einschließlich Nummer und Vorgänger-Hash: sortierte Schlüssel, keine Leerzeichen, UTF-8.
  Er deckt Zeitpunkt, handelnde und vertretene Person (als unveränderliche Kennung), IP-Adresse,
  Browserkennung, Aktion, Objekt und Änderungsangaben ab.
- Wird ein Eintrag verändert, passt sein Hash nicht mehr. Wird einer gelöscht, entsteht eine
  Lücke in der Nummernfolge und die Verkettung reißt. Wird am Ende gelöscht, passt die Kette
  nicht mehr zum gespeicherten Kettenkopf. Nachträglich ohne Nummer eingefügte Einträge fallen
  als „außerhalb der Kette“ auf.
- **Nebenläufigkeit:** Neue Einträge sperren den Kettenkopf ihres Mandanten bis zum Ende ihrer
  Transaktion; zusätzlich verhindert ein eindeutiger Index doppelte Nummern.
- **Grenze:** Wer Schreibzugriff auf die Datenbank hat, könnte die gesamte Kette neu berechnen.
  Dagegen hilft ein Abgleich mit außerhalb aufbewahrten Kettenankern: Jeder Export und jedes
  Archivpaket enthält Nummer und Hash des Kettenkopfs, ebenso die Ausgabe des Prüfbefehls. Wir
  empfehlen, diese Angaben regelmäßig außerhalb von mandari festzuhalten, etwa in der Akte der
  Revision.

### Nachrechnen außerhalb von mandari

Ein Eintrag aus dem JSON-Export lässt sich unabhängig von mandari prüfen:

1. Die Felder `entry_hash` und `anzeige` entfernen.
2. Den Rest als JSON mit sortierten Schlüsseln, ohne Leerzeichen und ohne Escaping von Umlauten
   serialisieren (`json.dumps(eintrag, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`).
3. SHA-256 über die UTF-8-Bytes bilden – das Ergebnis muss `entry_hash` entsprechen.
4. `prev_hash` jedes Eintrags muss dem `entry_hash` des Eintrags mit der vorherigen Nummer
   entsprechen.

## 8. Prüfung

- **In der Oberfläche:** *Audit-Log → Kette jetzt prüfen* (Recht „exportieren und prüfen“). Das
  Ergebnis erscheint sofort und wird mit Anzahl, Befunden und Dauer protokolliert; die Ansicht
  zeigt die letzte Prüfung. Sehr große Protokolle prüft der Betrieb über den Befehl.
- **Befehl:** `python manage.py verify_audit_chain` prüft alle Ketten, mit `--tenant <kürzel>`
  einen Mandanten, mit `--chain session|faction|security` eine Art. Bei einem Befund endet der
  Befehl mit Exit-Code 1; mit `--strict` gelten auch Hinweise (z. B. noch nicht verketteter
  Altbestand) als Befund. Wir empfehlen einen täglichen Lauf mit Benachrichtigung.

## 9. Export für Prüfinstanzen

*Audit-Log → Zeitraum exportieren* (Recht „exportieren und prüfen“):

- **CSV** (Semikolon, UTF-8): alle gehashten Felder, dazu Anzeigefelder (E-Mail der handelnden
  und der vertretenen Person, Bezeichnung der Aktion). Zellen, die mit `=`, `+`, `-`, `@`,
  Tabulator oder Wagenrücklauf beginnen, erhalten ein vorangestelltes Hochkomma, damit
  Tabellenkalkulationen sie nicht als Formel ausführen.
- **JSON** mit Umschlag: Mandant, Zeitraum, Zeitpunkt, erstellende Person, Kettenkopf und -anker,
  die Einträge, deren Anzahl und die Prüfsumme über die Einträge (`pruefsumme`).
- **ZIP**: CSV, JSON, `kette.json` und die Begleitdatei `SHA256SUMS`.
- **Prüfsumme je Export:** SHA-256 der ausgelieferten Datei im Antwortkopf `X-Checksum-SHA256`
  und im Protokolleintrag des Exports – so lässt sich später belegen, dass eine Datei
  unverändert ist.
- Die Oberfläche exportiert höchstens `AUDIT_EXPORT_MAX_ROWS` Einträge (Standard 100 000).
  Größere Zeiträume exportiert der Betrieb mit
  `python manage.py export_audit_log --tenant <kürzel> --from JJJJ-MM-TT --to JJJJ-MM-TT --format zip --output <verzeichnis>`;
  auch dieser Export wird protokolliert.

## 10. Aufbewahrung, Archiv und Löschung

- **Frist je Mandant:** *Einstellungen → Datenschutz → Audit-Log-Einträge* in Jahren
  (0 = keine Löschung). Empfehlung: 5 bis 10 Jahre, maßgeblich sind die örtlichen Vorschriften.
- **Archivpaket vor der Löschung:** Der Löschlauf (`session_privacy_purge` oder *Löschlauf
  ausführen*) prüft die abgelaufenen Einträge, legt sie als ZIP-Paket ab (JSON, CSV,
  `kette.json` mit den Ankern vor und nach dem gelöschten Stück, `SHA256SUMS`) und löscht sie
  erst danach. Die Archivierung selbst ist ein Protokolleintrag mit Paketname und Prüfsumme.
- **Nur intakte Anfangsstücke:** Gelöscht wird stets ein lückenloses Anfangsstück der Kette.
  Findet der Lauf dort eine Veränderung, archiviert und löscht er nichts – die Spur bleibt
  erhalten und erscheint als Hinweis im Ergebnis des Laufs.
- **Kette bleibt prüfbar:** Nach der Löschung merkt sich der Kettenkopf den Hash des letzten
  gelöschten Eintrags als Anker; die Prüfung beginnt dort.
- **Speicherort:** Einstellung `AUDIT_ARCHIVE_STORAGE` (ein konfigurierter Speicher der
  Installation) oder `AUDIT_ARCHIVE_ROOT` (Verzeichnis, Standard unterhalb des Medienverzeichnisses
  ohne Web-Zugriff).
- **Frist für Archivpakete** (Issue #366): *Einstellungen → Datenschutz → Archivpakete des
  Audit-Logs* in Jahren ab Ablage des Pakets (Standard 0 = keine Löschung). Der Löschlauf löscht
  abgelaufene Pakete des Mandanten aus dem Archivspeicher und nennt sie im Protokolleintrag des
  Laufs. Maßgeblich ist der Zeitstempel (UTC) am Anfang des Paketnamens, nicht die Änderungszeit
  des Speichers; andere Dateien im Ordner bleiben unberührt. Vorher bietet die Kommune die Pakete
  ihrem Archiv an. Die Kette bleibt ab dem Anker prüfbar, nur das Stück vor dem Anker lässt sich
  danach nicht mehr vorlegen.
- **Oberfläche:** Ein Lauf aus der Oberfläche löscht höchstens 50 000 Einträge; der Rest folgt
  beim nächsten Lauf. Der Befehl kennt keine Obergrenze.
- **Sicherheitsprotokoll:** `SECURITY_AUDIT_RETENTION_DAYS` (Standard 365 Tage); der Befehl
  `purge_security_audit_log` archiviert und löscht nach demselben Verfahren. Seine Archivpakete
  löscht er nach `SECURITY_AUDIT_ARCHIVE_RETENTION_DAYS` ab Ablage (Standard 0 = keine Löschung,
  `--archive-days`); jede solche Löschung steht als „Eingriff in den Betrieb“
  (`archivpakete_geloescht`, mit Paketnamen) im Sicherheitsprotokoll.
- **Schutz der Archivpakete:** Die Pakete liegen unverschlüsselt im Archivspeicher; Speicherort
  ohne Web-Zugriff und Dateirechte (0600) schützen sie.

## 11. Einführung und Betrieb

1. **Update einspielen.** Die Migrationen ergänzen die Felder der Kette ohne Umschreiben der
   Bestandsdaten; die Indizes entstehen auf PostgreSQL ohne Sperre der Schreibzugriffe.
2. **Altbestand verketten:** einmal `python manage.py audit_chain_backfill`. Bis dahin bleiben
   Einträge des betroffenen Mandanten unverkettet; der Befehl hängt den Bestand in Zeitreihenfolge
   an und schaltet die Kette scharf. Er ist wiederholbar.
3. **Regelmäßige Läufe:** `verify_audit_chain` täglich, `purge_login_attempts` und
   `purge_security_audit_log` täglich, `session_privacy_purge` monatlich (Zeitpläne im Worker, siehe
   DEPLOYMENT.md, „Geplante Aufgaben“).
4. **Rollen vergeben:** Revision und Datenschutz den zuständigen Personen zuweisen und prüfen,
   ob die Administrator-Rolle die Protokoll-Ansicht behalten soll.

## 12. Systemprotokolle der Installation

### 12.1 Ablage: journald statt Docker-JSON-Dateien

Docker schreibt Container-Ausgaben standardmäßig in JSON-Dateien im Container-Verzeichnis,
begrenzt nach Größe (`max-size`, `max-file`). Zwei Schwächen: Die Dateien verschwinden, sobald
der Container beim Deploy neu erstellt wird, und je nach Last ist die Grenze nach wenigen Tagen
erreicht.

Deshalb laufen die Container-Logs auf Produktionshosts über den Treiber `journald`
(`deploy/logging/docker-compose.journald.yml`). Das Journal liegt auf dem Host, hält nach
**Zeit** vor (`deploy/logging/journald-mandari.conf`: 90 Tage, höchstens 8 GB, mindestens 5 GB
der Platte bleiben frei, komprimiert, persistent) und übersteht jede Neuerstellung eines
Containers. Die Container heißen nach `COMPOSE_PROJECT_NAME` (Vorgabe `mandari`): Anwendung
`mandari`, sonst `mandari-<dienst>`.

```bash
journalctl CONTAINER_NAME=mandari -S -1h                 # letzte Stunde der Anwendung
journalctl CONTAINER_NAME=mandari-ingestor -p warning    # Warnungen und Fehler des Ingestors
journalctl CONTAINER_NAME=mandari -o cat | grep '"trace":"abc123"'   # eine Anfrage verfolgen
journalctl --disk-usage                                  # Belegung
docker logs --since 10m mandari                          # funktioniert weiterhin
```

`docker logs` und `docker compose logs` arbeiten mit journald unverändert. Der Treiber ist nur
auf Linux-Hosts mit systemd verfügbar; für lokale Entwicklung bleibt `json-file` aus der
Basis-Compose.

### 12.2 Sicherung

`/var/log/journal` wird als `/source/journal` in die tägliche restic-Sicherung eingehängt
(`BACKUP_SRC_JOURNAL`, [BACKUP.md](BACKUP.md)); das Caddy-Datenvolume mit dem Zugriffslog ist
ebenfalls Teil der Sicherung. Damit liegen die Protokolle verschlüsselt an zwei Standorten und
überstehen einen Serververlust. Die Aufbewahrung in der Sicherung (30 Tage) ist kürzer als die
Vorhaltezeit auf dem Host; die Sicherung dient dem Wiederanlauf, nicht der Archivierung.

### 12.3 Zugriffsschutz (A10)

- **Systemprotokolle:** Das Journal ist nur für `root` und die Gruppe `systemd-journal`
  lesbar. Eine nachträgliche unbemerkte Veränderung setzt Root-Rechte auf dem Host voraus; wer
  sie hat, kontrolliert ohnehin alles. Entscheidung: **keine kryptografische Versiegelung der
  Systemprotokolle.** Der Schutz besteht aus Betriebssystemrechten plus der täglichen,
  verschlüsselten Offsite-Kopie, gegen die sich ein manipulierter Stand vergleichen lässt. Wer
  mehr braucht, kann journald-Versiegelung (Forward Secure Sealing, `journalctl --setup-keys`,
  `Seal=yes`) aktivieren; das Verifikationsgeheimnis gehört dann außerhalb des Hosts verwahrt.
- **Fachliches Audit-Log:** unveränderlich in der Anwendung, gesichert durch die Hash-Kette mit
  Prüfung und Export (Abschnitte 7 bis 9).
- **Zugriffslog:** liegt im Caddy-Volume (root), keine Weitergabe an Dritte.

### 12.4 Überwachung des Überlaufs (CON.10.A17)

`deploy/logging/journal-alert.sh` läuft stündlich per Cron und schickt eine Mail (über den
mandari-Container), sobald das Journal 90 % seiner Obergrenze (`SystemMaxUse`) belegt. Ab der
Obergrenze kürzt journald nach Größe statt nach Zeit, die zugesagten 90 Tage gelten dann nicht
mehr. Die Mail nennt Belegung, Obergrenze und den ältesten Eintrag; höchstens eine Mail je sechs
Stunden. Die Root-Platte überwacht daneben der Disk-Alarm des Betriebsmonitors
([MONITORING.md](MONITORING.md)).

### 12.5 Einrichtung

Die Befehle stehen in [DEPLOYMENT.md, „Protokolle“](../DEPLOYMENT.md#-protokolle). Das
mitgelieferte `Caddyfile` setzt `roll_keep_for 336h` (14 Tage) bereits; nur ein selbst
gepflegtes Caddyfile braucht die Angabe in jedem `log { output file … }`-Block, danach
`docker exec mandari-caddy caddy reload --config /etc/caddy/Caddyfile`. Bei einer von Hand
gepflegten Compose (andere Dienstnamen) den journald-Override entsprechend anpassen.

## 13. Abgleich mit BSI OPS.1.1.5

| Anforderung | Stand |
|---|---|
| A6 zentrale Protokollierung, ausreichend dimensioniert | journald auf dem Host, 90 Tage / höchstens 8 GB, Zugriffslog 14 Tage |
| A8 Archivierung nach Fristen | Fristen je Protokollart festgelegt und begründet (Übersicht), Archivpakete des Audit-Logs (Abschnitt 10) |
| A10 Zugriffsschutz | Betriebssystemrechte, Offsite-Kopie, Versiegelung optional; Audit-Log mit Hash-Kette und Kontrollrechten (Abschnitte 6 und 7) |
| CON.10.A17 Überlauf | stündliche Warnung ab 90 % der Obergrenze |
| Sicherung | Journal und Caddy-Volume in der täglichen restic-Sicherung |

Datenschutzunterlagen (AVV-Muster, TOM, Löschkonzept): [DATENSCHUTZ.md](DATENSCHUTZ.md).
