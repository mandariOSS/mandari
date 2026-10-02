# Leitfaden: Umstieg aus SessionNet zu mandari Session

Stand: 10/2026 · Issue #762, Bezug #42

Für Betreiber und Verwaltungen, die von SessionNet (oder einem anderen Ratsinformationssystem ohne
freigeschaltetes OParl-Modul) zu mandari Session wechseln. Der Umstieg hat drei Wege: den öffentlichen
Bestand über den SessionNet-Adapter, die Stammdaten über Listen der Verwaltung und den nichtöffentlichen
Altbestand als Archivpaket. Eine vollständige Migration des Nichtöffentlichen ist nicht vorgesehen.

## 1. Bestandsaufnahme mit der Verwaltung

- Version und Betrieb von SessionNet (selbst betrieben oder im Rechenzentrum), Zusatzmodule (etwa OParl,
  Bürgerinfo, Apps für Mandatsträger), welche Listen und Exporte Session liefern kann
- Körperschaften in der Instanz: Viele Samtgemeinden führen sich und ihre Mitgliedsgemeinden in einer
  Instanz (Auswahl „Mandant“ in der Bürgerinfo); jede Körperschaft wird ein eigener Body
- Aufbewahrung und Archivierung der Niederschriften, zuständiges Archiv
- laufende Vorgänge: Vorlagen in Bearbeitung, offene Beschlusskontrollen, geplante Sitzungen
- Stichtag für den Umstieg (Abschnitt 5)

**Vor jeder Übernahme personenbezogener Daten** steht der Vertrag zur Auftragsverarbeitung. Bankdaten und
nichtöffentliche Inhalte nur über einen gesicherten Kanal entgegennehmen, nie per E-Mail. Zugriff auf
Systeme der Verwaltung nur in ihrem Auftrag.

## 2. Öffentlicher Bestand

Gremien, Mitglieder, Sitzungen, Tagesordnungen und öffentliche Vorlagen holt der SessionNet-Adapter aus der
Bürgerinfo in den RIS-Bestand; sie erscheinen im Bürgerportal. Anbindung, Schlüssel `bodies` für Instanzen
mit mehreren Körperschaften und die Regeln (User-Agent, robots.txt, Drosselung, keine Umgehung) stehen in
[SCRAPER_SOURCES.md](SCRAPER_SOURCES.md). Eine Quelle erst nach Freigabe anbinden und bis zur Freigabe im
Bürgerportal ausblenden.

Der öffentliche Altbestand bleibt zunächst im RIS-Bestand; ein lesender Altbestand in Session folgt später.

## 3. Stammdaten

Wahlperioden, Gremien, Fraktionen, Ämter, Personen und Besetzungen übernimmt
`session_stammdaten_import` ([SESSION_STAMMDATEN_IMPORT.md](SESSION_STAMMDATEN_IMPORT.md)):

1. Vorlagen erzeugen, bei angebundenem Adapter vorbefüllt mit dem öffentlichen Bestand je Körperschaft
   (`--vorlagen … --aus-ris …`).
2. Die Verwaltung ergänzt und korrigiert die Listen (Kontakt- und Bankdaten, Beginn der Besetzungen,
   Stellvertretungen, nicht öffentlich geführte Mitglieder) oder liefert eigene Listen aus Session.
3. Prüflauf (`--dry-run`) mit Gegenprobe gegen die öffentlichen Mitgliederlisten; Fehler zeilengenau an die
   Verwaltung zurückgeben, bis der Prüflauf ohne Fehler endet.
4. Import mit Bericht als JSON für das Prüfprotokoll; ein zweiter Lauf mit denselben Dateien ändert nichts.
5. Importdateien danach sicher löschen oder in der Akte ablegen.

## 4. Nichtöffentlicher Bestand

Nicht migrieren, sondern je Sitzung als Archivpaket (PDF der nichtöffentlichen Niederschrift und der
nichtöffentlichen Vorlagen) bei der Verwaltung bzw. dem zuständigen Archiv ablegen. In Session nur, was
laufende Vorgänge brauchen, vor allem offene Beschlusskontrollen.

## 5. Stichtag

Der Wechsel erfolgt an einer Sitzungsrunde:

- Vorlagen in Bearbeitung werden im Altsystem abgeschlossen oder in Session neu angelegt.
- Offene Beschlusskontrollen werden in Session übernommen.
- Sitzungen ab dem Stichtag lädt Session; das Altsystem bleibt bis zur Abnahme lesend erreichbar.

## 6. Prüfprotokoll

Vorlage für die Abnahme durch die Verwaltung (je Körperschaft ausfüllen):

```text
Prüfprotokoll Umstieg zu mandari Session
Verwaltung / Körperschaft: ____________________   Stichtag: __.__.____
Auftragsverarbeitung unterzeichnet am: __.__.____   Übergabeweg der Dateien: ____________________

1. Zählabgleich (aus dem Importbericht, Abschnitt „zaehlabgleich“)
   Objektart      Altsystem   Datei   neu   geändert   unverändert   fehlerhaft   Bestand nachher
   Wahlperioden   ________    _____   ___   ________   ___________   __________   _______________
   Gremien        ________    _____   ___   ________   ___________   __________   _______________
   Fraktionen     ________    _____   ___   ________   ___________   __________   _______________
   Ämter          ________    _____   ___   ________   ___________   __________   _______________
   Personen       ________    _____   ___   ________   ___________   __________   _______________
   Besetzungen    ________    _____   ___   ________   ___________   __________   _______________
   Öffentlicher Bestand (RIS): Gremien ____ Sitzungen ____ Vorlagen ____ (Zählung im RIS-Bestand)

2. Gegenprobe mit den öffentlichen Mitgliederlisten
   Abweichungen geklärt: [ ] ja  [ ] nein – Bemerkung: ______________________________

3. Stichproben (mindestens je Objektart drei Datensätze im Altsystem und in Session vergleichen)
   Datensatz                      geprüft von   Ergebnis
   ______________________________ ___________   [ ] stimmt  [ ] Abweichung: __________
   ______________________________ ___________   [ ] stimmt  [ ] Abweichung: __________
   ______________________________ ___________   [ ] stimmt  [ ] Abweichung: __________

4. Nichtöffentlicher Bestand
   Archivpakete übergeben an: ____________________ am __.__.____
   Offene Beschlusskontrollen übernommen: ____ von ____

5. Löschung
   Importdateien und Berichte gelöscht bzw. zur Akte genommen am: __.__.____

Abnahme durch die Verwaltung
Name, Funktion: ____________________   Datum: __.__.____   Unterschrift: ____________________
```
