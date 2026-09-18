# Präsentationsumgebung „Demo-Drehbuch“

Stand: 09/2026

Für eine Produktvorstellung von rund 20 Minuten nimmt eine einzige Drucksache ihren Weg durch
alle Teile von mandari: Die Fraktion schreibt den Antrag (Work), der Sitzungsdienst macht daraus
eine Drucksache und berät sie (Session), die Öffentlichkeit sieht das Ergebnis im Bürgerportal
(Insight) und über die OParl-Schnittstelle. Das Management-Command `setup_demo_praesentation`
setzt die nötigen Daten auf die bestehende [Demo-Umgebung](DEMO_ENVIRONMENT.md) auf.

Alle Daten sind synthetisch und als Demo gekennzeichnet.

## Aufruf

```bash
python manage.py setup_demo_praesentation                  # Profil nrw (Standard)
python manage.py setup_demo_praesentation --profil hamburg
python manage.py setup_demo_praesentation --reset          # Präsentation entfernen
```

Fehlt der Demo-Mandant, legt der Befehl zuerst die Basisdemo an (`setup_demo_environment`) und
gibt deren Zugangsdaten aus. Ist sie schon da, bleibt sie unverändert – ihre Passwörter
rotieren nicht.

Am Ende stehen in der Ausgabe die Zugangsdaten neu angelegter Nutzer, die Einstiegspunkte
(Bürgerportal-Kommune, Bürgerportal-Sitzung, Bürgerportal-Beschluss, OParl-System, Work-Antrag,
Session-Dashboard A und B) und ein Spickzettel mit dem Klickpfad.

## Profile

| Profil | Wahlperiode | Nummernkreis (Preset) | Beispielnummern |
|---|---|---|---|
| `nrw` (Standard) | „Wahlperiode 2025–2030“, Nr. 1, ab 01.11.2025 | Verwaltung und Politik getrennt (`nrw_verwaltung_politik`) | Vorlage `0001/2026`, umgewandelter Antrag `AN/0001/2026`, Bezeichnung „Vorlagen-Nr.“ |
| `hamburg` | „22. Wahlperiode“, Nr. 22, ab 09.06.2024 | Bezirksversammlung (`hamburg_bezirk`) | `22-0001` für alle Drucksachenarten, Bezeichnung „Drucksache“ |

Beide Mandanten bekommen dasselbe Profil, zählen aber jeder für sich (siehe
[Nummernkreise](SESSION_NUMMERNKREISE.md)). Ein Profilwechsel ändert Wahlperiode und
Nummernkreis; bereits vergebene Nummern bleiben. Wer durchgängig neue Nummern im anderen
Schema zeigen will, räumt vorher mit `--reset` auf.

## Was angelegt wird

### Mandant A – „Stadtverwaltung Musterstadt (Demo)“

Der bestehende Demo-Mandant bekommt Wahlperiode und Nummernkreis des Profils und veröffentlicht
im Bürgerportal (`insight_publish`) samt Umsetzungsstand seiner Beschlüsse
(`implementation_publish`). Dazu die Drehbuch-Daten (feste Titel sind der natürliche Schlüssel,
Vorlagen entstehen ohne Nummer – die vergibt der Nummernkreis):

- **Kommende Sitzung** „Hauptausschuss (Demo-Drehbuch)“ in 7 Tagen, Einladung fristgerecht
  versandt (Ladungsfrist des Gremiums plus ein Tag). Öffentlicher Teil: Eröffnung, „Anmietung von
  Räumen für das Jugendzentrum“, „Sanierung Spielplatz Stadtpark“; nichtöffentlicher Teil:
  „Grundstücksangelegenheit Flurstück 12/3“ mit nichtöffentlicher Vorlage.
- **Beratungsfolge** der Jugendzentrum-Vorlage: Vorberatung im Ausschuss für Bauen und Verkehr
  (vergangene Sitzung, Empfehlung angenommen) → Entscheidung im Hauptausschuss (kommende
  Sitzung, mit dem TOP verknüpft).
- **Vergangene Sitzung** „Hauptausschuss (Demo-Drehbuch, vergangen)“ vor 21 Tagen: Anwesenheit
  (fünf anwesend, eine Person entschuldigt), namentliche Abstimmung mit Ja, Nein und Enthaltung,
  genehmigtes Protokoll mit öffentlichem und verschlüsseltem nichtöffentlichem Teil,
  Beschlussnummern. Ein Beschluss ist „In Umsetzung“ mit Frist in 14 Tagen und öffentlicher
  Statusmeldung, ein zweiter hat seine Frist überschritten (Überfällig-Filter, Erinnerung).
  Für die Abstimmung erhält der Hauptausschuss drei zusätzliche Sitze aus der Basisdemo.
- **Sitzungsgeld**: Sätze des Hauptausschusses je Funktion und offene Positionen für die
  Anwesenden, erzeugt vom Verwaltungsnutzer. Genehmigen darf nur jemand anderes (Vier-Augen-
  Prinzip); das Recht `manage_allowances` hat unter den Standardrollen nur der Administrator.
- **Verbindung Work ↔ Session**: Einreichungs-Token „Demo-Drehbuch: Einreichung der
  Musterfraktion“ (`can_submit_applications`), mit der Musterfraktion verbunden. Eine
  bestehende, nutzbare Verbindung wird weiterverwendet.

### Work – Musterfraktion (Demo)

Antrag „Antrag: Tempo 30 vor der Grundschule am Musterweg (Demo)“ im Entwurf, mit
Beschlussvorschlag, Begründung und finanziellen Auswirkungen als Überschriften-Abschnitte –
das Formular „Bei Verwaltung einreichen“ ist damit vollständig vorbelegt.

### Mandant B – „Bezirksamt Musterstadt-Süd (Demo)“

Slug `bezirksamt-musterstadt-sued-demo`, Standardrollen, gleiches Profil wie A mit eigenem
Zähler, zwei Gremien (Hauptausschuss, Regionalausschuss), zwei Vorlagen und eine kommende
Sitzung. Daran lässt sich zeigen, dass jeder Bezirk seine Drucksachen selbst zählt.

### Leitstelle

`demo-leitstelle@demo.mandari.de` mit Rolle Administrator in A und B – in der Seitenleiste
erscheint „Mandant wechseln“. Das Passwort wird beim Anlegen erzeugt und nur dann ausgegeben;
ein neues setzt `python manage.py changepassword demo-leitstelle@demo.mandari.de`. Die Domain
`demo.mandari.de` ist über `TWO_FACTOR_EXEMPT_EMAIL_DOMAINS` von der Zwei-Faktor-Pflicht
ausgenommen (Standardwert); fehlt sie dort, weist der Befehl darauf hin.

### Spiegel ins Bürgerportal

Nach dem Anlegen spiegelt der Befehl Mandant A synchron im eigenen Prozess ins Bürgerportal
(`insight_sync/session_mirror.py`, Voll-Abgleich). Er ruft dafür die Session-OParl-API über den
Django-Test-Client ab – ohne HTTP, mit dem Host aus `SITE_URL`, damit die Kennungen dieselben
sind wie beim Abruf durch den Ingestor. Die gespiegelte Kommune ist nicht gelistet
(`is_listed=False`): Sie erscheint in keiner Kommunenauswahl, ist aber über den ausgegebenen
Link `/insight/kommune/<uuid>/` erreichbar.

## Drehbuch

| Schritt | Wer | Klickpfad | Zeigt |
|---|---|---|---|
| 1 | Fraktionsvorsitz (`demo-vorsitz@…`) | Work-Antrag öffnen → Symbol „Bei Verwaltung einreichen“ → Zielgremium wählen, Bestätigung anhaken → „Jetzt einreichen“ | Eingangsnummer `A/<Jahr>/…` im Editor-Kopf, Status „Eingereicht“ |
| 2 | Verwaltung (`demo-verwaltung@…`) | Session A → Anträge → Tempo-30-Antrag → „In Vorlage umwandeln“ → Gremium prüfen → „In Vorlage umwandeln“ | Vorlage mit Nummer aus dem Kreis (`22-…` bzw. `AN/…/<Jahr>`) |
| 3 | Verwaltung | Sitzungen → „Hauptausschuss (Demo-Drehbuch)“ → Beratungsfolge am Jugendzentrum-TOP → Stift → „Öffentlicher Tagesordnungspunkt“ abwählen → Speichern | TOP wandert in den nichtöffentlichen Teil; im zweiten Fenster (abgemeldet, Bürgerportal-Sitzung) ist er nach dem Neuladen verschwunden |
| 4 | – | OParl-System-URL im Browser; `…/api/oparl/agendaitem/<id>/` | JSON; der TOP ist nur noch als gelöschtes Objekt (`deleted: true`) abrufbar |
| 5 | Verwaltung | Beschlüsse → „Überfällig“; vergangene Sitzung → Niederschrift | Frist überschritten, Umsetzungsstand; Protokoll mit Ö- und NÖ-Teil |
| 6 | Bürgerportal | Link „Bürgerportal-Beschluss“ | „Was wurde aus …?“ mit öffentlicher Statusmeldung, ohne internen Vermerk |
| 7 | Verwaltung, dann Leitstelle | Sitzungsgeld (Link mit Zeitraum der vergangenen Sitzung) → „2. Genehmigen (Vier-Augen)“ | Die Verwaltung wird abgewiesen, die Leitstelle genehmigt fünf Positionen |
| 8 | Leitstelle | Seitenleiste „Mandant wechseln“ → „Bezirksamt Musterstadt-Süd (Demo)“ | Eigener Mandant, eigene Nummernfolge ab `…0001` |

Hinweise für die Vorführung:

- Jede Rolle in einem eigenen Browserfenster bzw. Profil anmelden; das Bürgerportal in einem
  privaten Fenster ohne Anmeldung öffnen.
- Die Rücknahme im Bürgerportal (Schritt 3) wirkt sofort, ebenso die neue Nummerierung der
  übrigen TOPs. Die in Schritt 2 umgewandelte Vorlage steht sofort in der OParl-Schnittstelle
  (Schritt 4); im Bürgerportal erscheint sie mit dem nächsten Abgleich des Spiegels (Ingestor
  alle 10 Minuten bzw. erneuter Befehlslauf).
- Der Umsetzungsstand im Bürgerportal hängt an der mit dem Mandanten verknüpften Kommune der
  Basisdemo („Musterstadt (Demo)“), nicht an der gespiegelten. Der ausgegebene Beschluss-Link
  führt direkt dorthin.

## Proben und Aufräumen

- **Probe zurücksetzen:** Befehl erneut ausführen. Er nimmt die Einreichung samt umgewandelter
  Vorlage zurück (Antrag wieder im Entwurf), stellt den TOP wieder öffentlich, setzt das
  Sitzungsgeld auf „offen“ und gleicht den Spiegel neu ab. Vergebene Nummern bleiben vergeben,
  die nächste Umwandlung bekommt also eine höhere Nummer.
- **Aufräumen:** `--reset` entfernt Mandant B, den Leitstellen-Nutzer, die Drehbuch-Daten in A
  und Work, Verbindung und Token sowie die gespiegelte Quelle samt Kommune. Mandant A kehrt zum
  Standard-Nummernkreis ohne Veröffentlichung zurück. Die Basisdemo bleibt.
- `setup_demo_environment --reset` räumt eine aufgesetzte Präsentation automatisch mit auf.

## Tests

- `apps/common/tests/test_setup_demo_praesentation.py`: Idempotenz, Nummern je Mandant, Leitstelle,
  Verbindung und Einreichung, Drehbuch-Daten, Vier-Augen-Prinzip, Spiegel inkl. sofortiger
  Rücknahme, Proben-Rücksetzung und `--reset`.
- `tests_e2e/test_demo_drehbuch.py`: spielt das Drehbuch im Browser durch (Profil `hamburg`):
  `MANDARI_E2E=1 pytest tests_e2e/test_demo_drehbuch.py --browser-channel chromium` nach
  `npm run build`.
