# Protokollierungskonzept – mandari Session RIS

Stand: September 2026 · Bezug: Issue #221 (Teil von #217)

Dieses Konzept beschreibt, was mandari im Verwaltungs-RIS (Session) protokolliert, wozu, wie
lange, wer das Protokoll einsehen darf und wie es gegen nachträgliche Veränderung geschützt
ist. Es richtet sich an Verwaltungen, Revision, Datenschutzbeauftragte und Personalvertretungen.
Verbindlich sind stets die Regelungen der jeweiligen Kommune; die hier genannten Fristen und
Voreinstellungen lassen sich je Mandant anpassen.

## 1. Zweck und Grundsätze

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
| Rollen und Rechte | Zuweisen und Entziehen von Rollen; Anlegen und Ändern einer Rolle mit erteilten und entzogenen Rechten | Rollen geändert, Rechte geändert |
| Lesezugriffe | Ansicht nichtöffentlicher Sitzungen, TOPs, Vorlagen (auch früherer Fassungen) und Niederschriften; Abruf von Dokumenten mit nichtöffentlichem Inhalt | Angesehen, Heruntergeladen |
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

## 5. Rechtsgrundlage (allgemeiner Hinweis)

Verantwortlich für die Verarbeitung ist die jeweilige Kommune; mandari verarbeitet im Auftrag
(siehe [AVV-Muster](DSGVO_AVV_MUSTER.md)). Die Protokollierung stützt sich in der Regel auf die
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
Fraktionssitzungen im Work-Portal) und das Sicherheitsprotokoll.

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
  ohne Web-Zugriff). mandari löscht Archivpakete nicht selbst: Die Kommune bietet sie ihrem Archiv
  an oder legt eine eigene Frist fest.
- **Oberfläche:** Ein Lauf aus der Oberfläche löscht höchstens 50 000 Einträge; der Rest folgt
  beim nächsten Lauf. Der Befehl kennt keine Obergrenze.
- **Sicherheitsprotokoll:** `SECURITY_AUDIT_RETENTION_DAYS` (Standard 365 Tage); der Befehl
  `purge_security_audit_log` archiviert und löscht nach demselben Verfahren.

## 11. Einführung und Betrieb

1. **Update einspielen.** Die Migrationen ergänzen die Felder der Kette ohne Umschreiben der
   Bestandsdaten; die Indizes entstehen auf PostgreSQL ohne Sperre der Schreibzugriffe.
2. **Altbestand verketten:** einmal `python manage.py audit_chain_backfill`. Bis dahin bleiben
   Einträge des betroffenen Mandanten unverkettet; der Befehl hängt den Bestand in Zeitreihenfolge
   an und schaltet die Kette scharf. Er ist wiederholbar.
3. **Regelmäßige Läufe:** `verify_audit_chain` täglich, `purge_security_audit_log` täglich,
   `session_privacy_purge` monatlich (siehe DEPLOYMENT.md, „Geplante Aufgaben“).
4. **Rollen vergeben:** Revision und Datenschutz den zuständigen Personen zuweisen und prüfen,
   ob die Administrator-Rolle die Protokoll-Ansicht behalten soll.

## 12. Zugehörige Dokumente

- [Löschkonzept](DSGVO_LOESCHKONZEPT.md)
- [Technische und organisatorische Maßnahmen](DSGVO_TOM.md)
- [AVV-Muster](DSGVO_AVV_MUSTER.md)
