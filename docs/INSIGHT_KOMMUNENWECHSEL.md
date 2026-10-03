# Kommunenwechsel im Bürgerportal

In Deutschland gibt es rund 11.000 Gemeinden, dazu Gemeindeverbände wie Samtgemeinden, Verbandsgemeinden und
Ämter. Das Bürgerportal zeigt deshalb nie eine Liste aller Kommunen. Wer die Kommune wechselt (Seitenleiste,
Kopfzeile am Handy oder Auswahlseite `/insight/`), hat vier Wege:

1. **Suchfeld „Kommune oder Postleitzahl“.** Während der Eingabe kommen höchstens acht Vorschläge vom Server,
   unscharf (Tippfehler) und umlauttolerant („Ubungsheim“ findet „Übungsheim“). Gefunden wird über den Namen, einen
   Ortsteil oder eine Postleitzahl. Kreis und Land stehen in der zweiten Zeile, damit gleichnamige Orte
   unterscheidbar sind; ein zweites Wort filtert danach („Beispielstadt Hessen“).
2. **„In meiner Nähe“.** Nur auf Klick fragt der Browser den Standort. An den Server geht ausschließlich eine
   Zelle von 0,1 Grad (etwa 11 km), die genaue Entfernung rechnet der Browser. Gespeichert wird nichts.
3. **„Zuletzt besucht“.** Bis zu fünf Kommunen merkt sich der Browser selbst (`localStorage`, kein Cookie).
4. **Stöbern** Land → Kreis → (Gemeindeverband →) Kommune. Kreisfreie Städte sind auf der Ebene des Landes direkt
   wählbar; Kreise mit mehr als 40 Einträgen gliedern sich zuerst nach Gemeindeverbänden.

Wählbar sind nur Kommunen mit Daten, also mit einer gelisteten Körperschaft (`OParlBody.is_listed`) desselben
Regionalschlüssels oder AGS. Alle übrigen erscheinen mit dem Hinweis „noch nicht verfügbar“. Jede Kommune behält
ihre direkte Adresse `/insight/k/<slug>/`.

Ohne JavaScript leistet die Seite `/insight/kommunen/` dasselbe mit Formular und Links. Am Handy öffnet sich der
Wechsel als Blatt über den ganzen Bildschirm mit dem Fokus im Suchfeld; Pfeiltasten wandern zwischen Suchfeld und
Vorschlägen, Escape führt zurück bzw. schließt den Dialog.

## Schnittstellen

Alle drei Antworten hängen nicht von der Sitzung ab und dürfen öffentlich zwischengespeichert werden.

| Adresse | Zweck |
|---|---|
| `GET /insight/kommunen/vorschlaege/?q=…` | höchstens acht Vorschläge (`name`, `ort`, `hinweis`, `verfuegbar`, `url`) |
| `GET /insight/kommunen/naehe/?zelle=51.9,7.6` | Kandidaten rund um die Zelle mit Koordinaten, dazu die nächste Kommune mit Daten |
| `GET /insight/kommunen/stoebern/?land=05&kreis=05999&verband=…` | eine Stufe des Stöberns |

Code: `insight_core/services/kommunenverzeichnis.py` (Suche, Nähe, Stöbern), `insight_core/views/kommunen.py`,
`frontend/alpine/kommunen-wahl.ts`, `templates/components/kommunen_wahl.html`.

## Verzeichnis füllen

Das Verzeichnis (`Municipality`, Suchbegriffe in `MunicipalityTerm`) wird per CSV-Datei befüllt. In PostgreSQL
legt die Migration die Erweiterung `pg_trgm` und einen Trigramm-Index auf die Suchbegriffe an; die Suche antwortet
damit auch bei Zehntausenden Begriffen in wenigen Millisekunden.

```bash
python manage.py kommunenverzeichnis_importieren --datei kommunen.csv            # anlegen bzw. aktualisieren
python manage.py kommunenverzeichnis_importieren --datei kommunen.csv --ersetzen # Einträge außerhalb der Datei entfernen
python manage.py kommunenverzeichnis_importieren --aus-koerperschaften           # gelistete Kommunen ergänzen
```

Format: UTF-8, Semikolon, Kopfzeile, je Gemeinde, Gemeindeverband oder kreisfreie Stadt eine Zeile.

```
schluessel;name;art;kreis;breite;laenge;plz;ortsteile
059990000000;Beispielstadt;Kreisfreie Stadt;;51.9625;7.6256;48143|48145;Nordviertel|Heidekamp
039995401000;Samtgemeinde Heideland;Samtgemeinde;Landkreis Musterkreis;52.62;10.24;;
039995401014;Moorbach;Gemeinde;Landkreis Musterkreis;52.61;10.24;29331;Unterdorf
```

| Spalte | Inhalt |
|---|---|
| `schluessel` | Regionalschlüssel (12 Stellen) oder, wenn nicht bekannt, Amtlicher Gemeindeschlüssel (8 Stellen); Land und Kreis ergeben sich daraus |
| `name` | Name ohne Zusatz „Stadt“ bzw. „Gemeinde“ |
| `art` | z. B. Stadt, Gemeinde, Kreisfreie Stadt, Samtgemeinde; Samtgemeinde, Verbandsgemeinde, Amt und Verwaltungsgemeinschaft bilden im Stöbern eine eigene Stufe |
| `kreis` | Name des Kreises, bei kreisfreien Städten leer |
| `breite`, `laenge` | Mittelpunkt in Dezimalgrad (für „In meiner Nähe“), optional |
| `plz`, `ortsteile` | mit `|` getrennt, optional |

Der Import ist idempotent und läuft in einer Transaktion; fehlerhafte Zeilen werden mit Zeilennummer gemeldet und
übersprungen. Ohne Import findet der Wechsel weiterhin alle gelisteten Kommunen über ihren Namen; „noch nicht
verfügbar“, Ortsteile, Postleitzahlen und das Stöbern brauchen das Verzeichnis.

**Quellen.** Schlüssel, Namen, Kreise und Mittelpunkte enthält das Gemeindeverzeichnis des Statistischen
Bundesamts (GV-ISys, Datenlizenz Deutschland – Namensnennung 2.0). Postleitzahlen und Ortsteile lassen sich aus
OpenStreetMap ableiten (ODbL, Namensnennung „© OpenStreetMap-Mitwirkende“). Die Umwandlung in das Format oben
ist ein einmaliger Schritt beim Betrieb; Lizenzhinweise gehören in die Erklärung des Bürgerportals.
