# Stammdaten aus Bestandssystemen übernehmen (Session)

Stand: 10/2026 · Issue #762 (Teil L11a), Bezug #42

Wer von einem anderen Ratsinformationssystem zu mandari Session wechselt, bringt Wahlperioden, Gremien,
Fraktionen, Ämter, Personen und Besetzungen mit. Der Befehl `session_stammdaten_import` übernimmt sie aus
Listen der Verwaltung: je Objektart eine CSV- oder XLSX-Datei. Er prüft zuerst alles, schreibt nur ohne
Fehler und dann ganz oder gar nicht. Ein zweiter Lauf mit denselben Dateien ändert nichts.

Den öffentlichen Bestand (Sitzungen, Vorlagen, öffentliche Mitgliederlisten) holt bei SessionNet ohne OParl
der SessionNet-Adapter in den RIS-Bestand ([SCRAPER_SOURCES.md](SCRAPER_SOURCES.md)); er kann die Vorlagen
vorbefüllen und dient als Gegenprobe. Den Gesamtablauf beschreibt der Leitfaden
[SESSION_UMSTIEG_SESSIONNET.md](SESSION_UMSTIEG_SESSIONNET.md).

## Ablauf

```bash
# 1. Leere Vorlagen für die Verwaltung erzeugen …
python manage.py session_stammdaten_import --vorlagen /daten/vorlagen

# … oder vorbefüllt mit dem öffentlichen Bestand einer Körperschaft im RIS-Bestand
python manage.py session_stammdaten_import --vorlagen /daten/vorlagen \
    --aus-ris "https://ratsinfo.example.de/bi/" --stichtag 2026-10-01

# 2. Prüflauf: liest und prüft alles, schreibt nichts; Bericht zusätzlich als Datei
python manage.py session_stammdaten_import --tenant musterstadt /daten/import --dry-run \
    --bericht /daten/import/pruefbericht.txt

# 3. Optional: Gegenprobe mit den öffentlichen Mitgliederlisten (Body im RIS-Bestand)
python manage.py session_stammdaten_import --tenant musterstadt /daten/import --dry-run \
    --gegenprobe "https://ratsinfo.example.de/bi/" --stichtag 2026-10-01

# 4. Import (nur ohne Fehler); Bericht als JSON für die Akte
python manage.py session_stammdaten_import --tenant musterstadt /daten/import --bericht /daten/bericht.json
```

Fehlt eine Datei, wird diese Objektart übersprungen. Besetzungen brauchen die Personendatei. Vorlagen
überschreiben nie vorhandene Importdateien; dafür ein leeres Verzeichnis angeben. Prüfen und Schreiben laufen
beim Import in einer Transaktion; gleichzeitige Läufe für denselben Mandanten warten aufeinander.

## Vorlagen aus dem öffentlichen Bestand

`--aus-ris` nennt einen Body im RIS-Bestand wie bei der Gegenprobe (unten). Die Vorlagen enthalten dann:

| Datei | Inhalt |
|---|---|
| `wahlperioden` | alle Wahlperioden des Bodys (bei SessionNet aus der Auswahl in der Gremienliste) |
| `gremien`, `fraktionen` | alle Gremien; Namen mit „Fraktion“ oder „Gruppe“ in `fraktionen`. Die Art ist aus dem Namen geschätzt (Rat, Ausschuss, Beirat, Kommission; Verwaltungs-, Samtgemeinde-, Kreis- und Regionsausschuss als Hauptausschuss) und bleibt leer, wenn der Name nichts hergibt |
| `personen` | wer am Stichtag eine Besetzung hat; Anrede, Titel, Vor- und Nachname aus dem Anzeigenamen getrennt („Nachname, Vorname“ und Namenszusätze wie „von der“ werden erkannt), `kennung` = OParl-Kennung der Person. Kontakt- und Bankdaten bleiben leer |
| `besetzungen` | die am Stichtag laufenden Besetzungen mit Funktion und Stimmrecht; ohne Beginn die am Stichtag laufende Wahlperiode. Bei Stellvertretungen, Hinzugewählten und sachkundigen Bürgern bleibt `stimmrecht` leer, wenn die Quelle nur ihren Standard nennt (SessionNet: „ja“, solange die Überschrift nicht „ohne Stimmrecht“ sagt); dann gilt die Regel unter „Dateien und Spalten“ |

Bekannte Funktionen werden vereinheitlicht („Ratsherr“ → Mitglied, „Stellvertretende Mitglieder“ →
stellvertretendes Mitglied, „Hinzugewählte Mitglieder“ → hinzugewähltes Mitglied), unbekannte wie
„Bürgermeister“ bleiben stehen; der Prüflauf meldet sie, und die Verwaltung entscheidet (Vorsitz oder
Mitglied). Zellen, die mit `=`, `+`, `-` oder `@` beginnen, bekommen ein Hochkomma, damit Excel sie nicht als
Formel ausführt; der Import entfernt es wieder.

Die Verwaltung prüft Namen, Arten und Funktionen, trägt Beginn, Kontakt- und Bankdaten nach und schickt die
Dateien über einen gesicherten Kanal zurück. Öffentliche Listen sind oft unvollständig (etwa ohne
Stellvertretungen oder sachkundige Einwohner); maßgeblich sind die Angaben der Verwaltung.

## Dateien und Spalten

Dateinamen: `wahlperioden`, `gremien`, `fraktionen`, `aemter`, `personen`, `besetzungen`, jeweils `.csv`
oder `.xlsx` (erstes Tabellenblatt). CSV: Semikolon, Komma oder Tabulator; UTF-8 oder Windows-1252 (Excel).
Die erste Zeile nennt die Spalten; Groß-/Kleinschreibung, Umlaute und Leerzeichen sind egal
(„E-Mail“ = `email`, „Von“ = `beginn`). Unbekannte Spalten meldet der Bericht als Hinweis. Datum als
`TT.MM.JJJJ` oder `JJJJ-MM-TT`, in XLSX auch als Datumszelle. Ja/Nein als `ja`/`nein` (auch `x`, `1`, `0`).
Pflichtspalten sind **fett**.

| Datei | Spalten |
|---|---|
| `wahlperioden` | **name**, nummer, beginn, ende |
| `gremien` | **name**, kurzname, **art** (Rat, Ausschuss, Beirat, Kommission, Sonstiges; auch Kreistag, Ortsrat, Verwaltungsausschuss …), ausschussart (Hauptausschuss – auch Verwaltungs-, Kreis-, Regions- oder Samtgemeindeausschuss –, Finanzausschuss, Rechnungsprüfungsausschuss, anderer), uebergeordnet, ladungsfrist_tage, sollstaerke, beginn, ende, aktiv |
| `fraktionen` | **name**, kurzname, beginn, ende, aktiv |
| `aemter` | **name**, kurzname, uebergeordnet, aktiv |
| `personen` | **kennung**, anrede, titel, **vorname**, **nachname**, email, telefon, adresse, kontoinhaber, iban, bic, zustellweg (E-Mail, Portal, Brief), mandat_beginn, mandat_ende, aktiv |
| `besetzungen` | **person** (Kennung aus `personen`), **gremium** (Name eines Gremiums, einer Fraktion oder eines Amts), funktion (Mitglied, stellvertretendes Mitglied, Vorsitz, stellv. Vorsitz, hinzugewählt, sachkundige/r Bürger/in, beratend bzw. Grundmandat, Gast), stimmrecht, beginn, ende, wahlperiode, vertretung_fuer (Kennung) |

- `kennung` verknüpft nur die Dateien untereinander (z. B. die Personennummer des Altsystems) und wird nicht
  gespeichert.
- Ohne Angabe in `stimmrecht` gilt für neue Besetzungen die Tabelle unten; eine Angabe in der Spalte geht
  immer vor. Sonst zählten Personen ohne Stimme bei der Beschlussfähigkeit mit. Stellvertretende Mitglieder
  mit `vertretung_fuer` rücken für die vertretene Person nach. Maßgeblich ist das Landesprofil des Mandanten.
- Ohne `wahlperiode` bekommt eine neue Besetzung die Wahlperiode, in die ihr Beginn fällt.
- Gremien und Fraktionen dürfen in den Besetzungen auch vorkommen, wenn sie schon im Mandanten angelegt sind.

| Funktion | Stimmrecht ohne Angabe |
|---|---|
| beratend, Grundmandat (§ 71 Abs. 4 NKomVG), Gast | nein |
| hinzugewählt | nein (§ 71 Abs. 7 NKomVG), Sammelhinweis im Bericht. In Ausschüssen nach § 73 NKomVG regelt das jeweilige Gesetz das Stimmrecht; dort `ja` angeben |
| sachkundige/r Bürger/in | Landesprofil Niedersachsen: nein wie hinzugewählt; Landesprofil Nordrhein-Westfalen: ja (§ 58 Abs. 3 GO NRW); sonst ja, mit Sammelhinweis, das Stimmrecht nach Landesrecht zu prüfen |
| stellvertretendes Mitglied ohne `vertretung_fuer` | nein, Hinweis im Bericht |
| alle anderen | ja |

## Abgleich mit dem Bestand

Der Import erkennt vorhandene Datensätze an fachlichen Schlüsseln; eine eigene Kennungsspalte gibt es nicht:

| Objekt | Schlüssel |
|---|---|
| Wahlperiode, Gremium, Fraktion, Amt | Name (Groß-/Kleinschreibung und Leerraum egal); gibt es den Namen im Mandanten mehrfach, ist das ein Fehler |
| Person | Vor- und Nachname; gibt es den Namen im Mandanten mehrfach, zusätzlich die E-Mail |
| Besetzung | Gremium, Person und Beginn |

Vorhandene Datensätze werden aktualisiert, wenn die Datei abweichende Werte nennt. **Leere Zellen ändern
nichts.** Der Import löscht nichts; was im Altsystem fehlt, bleibt in Session.

Gibt es genau eine Person gleichen Namens, nennt die Datei aber eine andere E-Mail-Adresse als der Bestand,
meldet der Bericht „möglicherweise eine andere Person“ und ob sich dabei Bankdaten ändern; die Werte selbst
zeigt er nicht. Ist es eine andere Person, sie vorher von Hand mit ihrer E-Mail-Adresse anlegen; dann ordnet
der Import über die E-Mail richtig zu.

## Prüfungen

Fehler stehen mit Datei und Zeile im Bericht; mit Fehlern schreibt auch der Import nichts:

- Pflichtfelder, Datumsangaben, Zahlen, Ja/Nein und Auswahlwerte
- Kennungen und Namen doppelt in einer Datei; dieselbe Person in zwei Zeilen
- Ende vor Beginn; Wahlperioden, die sich überschneiden; Besetzungen derselben Person im selben Gremium,
  die sich überschneiden (auch mit vorhandenen Besetzungen)
- Verweise auf unbekannte oder fehlerhafte Personen, Gremien, Wahlperioden und übergeordnete Gremien
- Namen von Wahlperioden, Gremien, Fraktionen und Ämtern, die es im Mandanten mehrfach gibt (auch als
  übergeordnetes Gremium und als Wahlperiode, in die der Beginn einer Besetzung fällt)
- übergeordnete Gremien oder Ämter, die einen Ring bilden (A unter B, B unter A), aus Dateien und Bestand
  zusammen
- E-Mail-Adresse, IBAN (Prüfziffer) und BIC
- ein Name, den es im Mandanten schon mit anderer Art gibt (z. B. als Fraktion statt als Gremium)

Hinweise (z. B. Beginn außerhalb der genannten Wahlperiode, angenommenes Stimmrecht, andere E-Mail bei
gleichem Namen, Abweichung in der Gegenprobe) halten den Import nicht auf.

## Bericht und Zählabgleich

Der Bericht nennt je Objektart die Zeilen der Datei und wie sie verteilt sind: neu, geändert, unverändert,
fehlerhaft – die Summe muss die Zeilenzahl ergeben. Dazu den Bestand im Mandanten vorher und nachher, die
geänderten Felder je Datensatz und das Ergebnis der Gegenprobe. Mit `--bericht datei.json` entsteht eine
maschinenlesbare Fassung für das Prüfprotokoll der Übernahme.

Jede Anlage und Änderung steht außerdem im Prüfprotokoll des Mandanten (Protokollierung).

## Gegenprobe mit den öffentlichen Mitgliederlisten

`--gegenprobe` nennt einen Body im RIS-Bestand (OParl-Kennung, bei SessionNet die Basis-URL bzw. mit
`?__cpanr=N`, oder die UUID). Für jedes Gremium der Besetzungsdatei vergleicht der Import die am Stichtag
laufenden Besetzungen mit den öffentlichen Mitgliedern gleichnamiger Gremien: übereinstimmend, nur öffentlich,
nur im Import. Anrede, akademische Titel und die Schreibweise „Nachname, Vorname“ stören den Vergleich nicht.
Abweichungen sind Hinweise: Öffentliche Listen zeigen oft nicht alle Mitglieder (etwa ohne Stellvertretungen).

## Datenschutz

- Personenbezogene Daten nur mit Vertrag zur Auftragsverarbeitung übernehmen.
- Bankdaten und Kontaktdaten nur über einen gesicherten Kanal entgegennehmen, nie per E-Mail.
- Telefon, Adresse und Bankdaten speichert Session verschlüsselt; sie erscheinen nie im Bericht, in der
  Ausgabe oder in Fehlermeldungen. Der Bericht enthält Namen.
- Importdateien und Berichte nach Abschluss sicher löschen bzw. in der Akte ablegen.

## Grenzen

- Die Zuordnung zu Körperschaften (Samtgemeinde und Mitgliedsgemeinden in einem Mandanten) folgt mit #756
  (Spalte `koerperschaft`). Bis dahin erkennt der Import Gremien am Namen im ganzen Mandanten: Gleichnamige
  Gremien zweier Körperschaften (etwa zweimal „Verwaltungsausschuss“) meldet er als Fehler, statt einen
  beliebigen Datensatz zu nehmen; übernehmen lassen sie sich erst mit der Spalte.
- Sitzungen, Vorlagen und Niederschriften übernimmt der Befehl nicht (#762, Soll-Teil).
- Zeilen je Datei höchstens 20.000, Dateigröße höchstens 10 MB; XLSX entpackt höchstens 50 MB, Spalten bis
  XFD wie in Excel und höchstens 2 Millionen Zellen.
