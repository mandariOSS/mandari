# Neues Design in Work ausrollen

Das neue Erscheinungsbild von Work (Rahmen wie Insight, Startseite, Sitzungsansicht, Vorbereitung und Editor; Epic
#850) steht hinter einem **Schalter je Organisation**. Standard ist aus; die Demo-Organisation ist an. So geht das
neue Design nach dem Deploy erst in der Demo in Betrieb, wird dort geprüft und abgenommen und danach Organisation
für Organisation eingeschaltet. Der Rückweg ist jederzeit derselbe Schalter.

| Was | Wo |
|---|---|
| Schalter | Feld `Organization.work_new_design` (#852), Standard aus; Darstellung: `apps/work/rahmen.py` |
| Schalten und Stand | Verwaltungsbefehl `work_neues_design` (`apps/work/management/commands/work_neues_design.py`) |
| Prüfen | `scripts/pruefe_work_design.py` (Playwright): alle Bereiche, fünf Rollen, fünf Breiten, Schalter an und aus; zählt Seiten ohne neue Gestaltung |
| Stand je Seite | `data-gestaltung` am Hauptbereich im neuen Rahmen: `neu` setzt die Seite selbst (Block `gestaltung`), sonst `bisher` |
| Demo-Inhalte | `setup_demo_environment` ruft `setup_demo_work` auf (nächtlicher Neuaufbau der Demo) |

## Was der Schalter ändert – und was nicht

- **Ändert:** nur die Darstellung von Work für die Mitglieder dieser Organisation (Rahmen, Navigation, neue
  Ansichten). Alle bisherigen Adressen bleiben gültig, das Ratsinformationssystem bleibt in Work.
- **Ändert nicht:** Inhalte, Einstellungen, Mitglieder, Rechte. Der Befehl schreibt ausschließlich diese eine
  Spalte der Organisation (ohne `save()`, ohne Signale, ohne neues `updated_at`). Der Test
  `apps/work/tests/test_neues_design_schalter.py` vergleicht dazu den **gesamten** Datenbestand vor und nach dem
  Ein- und Ausschalten.
- **Rechte:** Das neue Design zeigt nichts, was die Rolle vorher nicht sehen durfte. Das Prüfskript meldet jeden
  Link, den eine Rolle sieht, der aber auf 403/404 führt; die Tests der Bereiche prüfen die Rechte je Rolle.

## Verwaltungsbefehl

Im App-Container (`docker exec <app-container> python manage.py …`):

```bash
python manage.py work_neues_design status                        # Stand aller Organisationen
python manage.py work_neues_design status --org <slug> --json    # maschinenlesbar
python manage.py work_neues_design an  --org <slug> --probelauf  # zeigt nur, was sich ändern würde
python manage.py work_neues_design an  --org <slug>              # einschalten (mehrere: --org a --org b)
python manage.py work_neues_design aus --org <slug>              # Rückweg für eine Organisation
python manage.py work_neues_design aus --alle                    # Rückweg für alle Organisationen
```

Eingeschaltet wird bewusst nur je Organisation; `an --alle` gibt es nicht. Unbekannte Kurznamen brechen ab, ohne
etwas zu ändern.

## Prüfskript

`scripts/pruefe_work_design.py` meldet sich je Rolle an (Vorsitz, Mitglied, Sachkundige, nicht vereidigt, Gast),
erkundet alle Work-Seiten über die Links, die diese Rolle sieht (nur Seitenaufrufe, keine Aktionen, Downloads oder
Schnittstellen; je Adressmuster höchstens zwei Seiten), und misst jede Seite in 390, 1280, 1440, 1920 und 2560 px:

| Befund | Schwere |
|---|---|
| Anmeldung gescheitert oder Anmeldeseite nicht nutzbar (etwa Host nicht zugelassen); die Rolle wird übersprungen | Fehler |
| Serverfehler (Status ≥ 500, Fehlerseite) | Fehler |
| Toter Link (sichtbarer Link endet mit 403/404) | Fehler |
| Konsole: JavaScript-Ausnahme, Fehler in der Konsole (Alpine, CSP), eigene Ressource mit Status ≥ 400 | Fehler |
| Fremde Ressource lädt nicht (etwa Kartenkacheln bei gestörter Namensauflösung) | Hinweis |
| Überlauf: Seite breiter als das Fenster | Fehler |
| Leerfläche (Regel #841, für Work 1.280 bis 2.560 px): rechts mehr als ein Viertel frei; Text, Bedienelemente und Flächen mit Rahmen, Hintergrund oder Schatten zählen als Inhalt | Fehler im neuen Design (Schalter an bzw. Seite mit `data-rahmen="neu"`), sonst Hinweis |
| Design passt nicht zum Schalter: `data-rahmen="neu"` bei ausgeschaltetem Schalter | Fehler |
| Seite ohne neuen Rahmen bei eingeschaltetem Schalter (eigene Vorlage ohne `base_work.html`) | Hinweis |
| Seite im neuen Rahmen ohne neue Gestaltung (`data-gestaltung` nicht `neu`), gezählt je Adressmuster über alle Rollen | Hinweis in der Zusammenfassung, mit `--gestaltung-pflicht` Fehler |

Verbindungsversuche der Zusammenarbeit im Editor (WebSocket) zählen nicht. Exit-Code 1 bei mindestens einem Fehler.

Zugänge kommen nur aus der Umgebung: `MANDARI_PRUEF_PASSWORT` (alle Demo-Konten), je Rolle überschreibbar mit
`MANDARI_PRUEF_<ROLLE>_EMAIL` und `MANDARI_PRUEF_<ROLLE>_PASSWORT`. Rollen ohne Zugang werden übersprungen.

```bash
pip install playwright && playwright install chromium

# Lokal (Demo mit DEMO_INSTANCE=true und DEMO_PASSWORD aufgebaut, runserver auf 8000), Schalter an und aus:
MANDARI_PRUEF_PASSWORT=… python scripts/pruefe_work_design.py --basis http://localhost:8000 --schalter beide

# Öffentliche Demo (Schalter wie eingestellt), Bericht als JSON und Bildschirmfotos:
MANDARI_PRUEF_PASSWORT=… python scripts/pruefe_work_design.py --basis https://demo.mandari.de \
    --bericht demo.json --bilder demo-bilder/

# Andere Instanz mit eigenen Konten je Rolle:
MANDARI_PRUEF_VORSITZ_EMAIL=… MANDARI_PRUEF_VORSITZ_PASSWORT=… \
    python scripts/pruefe_work_design.py --basis https://<instanz> --org <slug> --rollen vorsitz
```

Gegen eine entfernte Instanz schaltet das Skript **nie von sich aus** um. `--schalter an|aus|beide` geht dort nur
mit `--schalt-befehl '<befehl mit {aktion} und {org}>'`; das Skript liest vorher den Stand und stellt ihn am Ende
wieder her. Die Datenbank der öffentlichen Demo ändert nur ihr nächtlicher Neuaufbau, nie das Prüfskript.

### Seiten ohne neue Gestaltung zählen

Der neue Rahmen trägt jede Seite, aber nicht jede Seite ist schon inhaltlich umgestellt (Bestandsaufnahme in #951).
Darum kennzeichnet der Hauptbereich im neuen Rahmen den Stand: `<main data-gestaltung="bisher">`, solange die Seite
ihr bisheriges Markup zeigt. Eine Seite, die im neuen Erscheinungsbild steht (gemeinsame Bausteine, keine Kästen in
Kästen, Breiten 390 und 1.280 bis 2.560 px geprüft), setzt in ihrer Vorlage

```django
{% block gestaltung %}neu{% endblock %}
```

Heute sind das Start (`work/dashboard/start.html`) und die Sitzungsvorbereitung (`work/meetings/vorbereitung/seite.html`).
Das Prüfskript nennt am Ende die Zahl der Adressmuster ohne neue Gestaltung und listet sie auf, je Rolle steht
„x von y im neuen Rahmen ohne neue Gestaltung“. Ziel von #951 ist 0; dann läuft das Skript mit
`--gestaltung-pflicht` ohne Fehler. Im bisherigen Rahmen gibt es die Kennzeichnung nicht (Schalter aus zählt nicht).

Weitere Schalter: `--rollen`, `--breiten`, `--max-seiten` (Standard 80 je Rolle), `--neu-laden` (je Breite neu
laden statt Fenster verbreitern; genauer, dauert länger), `--sichtbar` (Browser sichtbar).

Im CI läuft derselbe Ablauf verkleinert als E2E-Test (`tests_e2e/test_work_design_pruefung.py`).

## Demo-Inhalte

`setup_demo_environment` legt die Musterfraktion an und ruft am Ende `setup_demo_work` auf. Damit zeigt die Demo
nach jedem nächtlichen Neuaufbau:

- **Konten** (alle `@demo.mandari.de`): `demo-vorsitz` (Fraktionsvorsitz, vereidigt), `demo-mitglied`
  (Fraktionsmitglied, vereidigt), `demo-sachkundig` (Sachkundige Bürgerin, vereidigt), `demo-unvereidigt`
  (Fraktionsmitglied, noch nicht vereidigt), `demo-gast` (Gast mit Ordnerfreigabe)
- **Standard-Tagesordnung:** Beschlüsse, Politische Arbeit, Termine, Presse und Social Media, Sonstiges und ein
  nicht-öffentlicher Punkt
- **Vergangene Fraktionssitzung:** Anwesenheit vor Ort und online, Beschluss, Notiz und Aufgabe im Protokoll; das
  Protokoll wartet auf die Genehmigung
- **Kommende Fraktionssitzung** mit Ort und Videolink: Genehmigungs-TOP, Standard-Tagesordnung, Unterpunkte mit
  verknüpftem Antrag und verknüpfter Vorlage, zwei nicht-öffentliche TOPs (nur für Vereidigte) und ein
  TOP-Vorschlag der Sachkundigen
- **Sitzungsreihe** wöchentlich montags 18 Uhr (Zu- und Absagen und automatische Einladung aus) mit Ferienpause
  und Feiertagen als Ausnahme; die Termine erzeugt dieselbe Funktion wie im Betrieb
- **Beratungsverlauf:** Die Vorlage zum Feuerwehrbedarfsplan steht im Hauptausschuss und im Rat; die Position aus
  dem Hauptausschuss („Mit Änderungsantrag“, endgültig) erscheint in der Vorbereitung der Ratssitzung. Dazu
  vergangene Positionen mit Ergebnis
- **Antrag mit Kommentaren** an Textstellen (offen mit Antwort, erledigt) und einem allgemeinen Kommentar, dazu zwei
  **Änderungsanträge** (zum eigenen Antrag und zu einer Vorlage des RIS)
- **Ankündigung** „Was ist neu in Work“ im Hinweisband auf Start und in der Glocke (#857) für alle Mitglieder außer
  dem Gast
- **Neues Design an** für die Demo-Organisation (setzt `setup_demo_environment` bei jedem Aufbau)

## Ablauf

### 1. Vor dem Deploy

- [ ] Alle PRs des Updates gemergt, CI auf `dev` grün (einschließlich E2E)
- [ ] Deploy-Vorschlag mit der Projektleitung abgestimmt: Inhalt, Migrationen (für den Schalter nur das additive
      Feld aus #852, `db_default` aus), Datenänderungen (keine), Risiko, Rückweg. **Deploy erst nach ausdrücklicher
      Freigabe.**
- [ ] Lokal: Prüfskript mit `--schalter beide` gegen die lokale Demo ohne Fehler

### 2. Deploy

- [ ] Deploy wie in `DEPLOYMENT.md` (`deploy.sh plan`, dann `apply`); die Prüfung nach dem Deploy ist grün
- [ ] `work_neues_design status`: In Produktion sind alle Organisationen **aus** (die Demo läuft als eigene
      Instanz). Ist eine an, ohne dass es beschlossen war: sofort `aus --org <slug>`
- [ ] Fehlerprotokoll der ersten Stunde ohne neue 5xx

### 3. Demo prüfen

Die öffentliche Demo baut nachts mit dem Image der Produktion neu auf; danach ist das neue Design dort an.
Soll es früher gehen, baut der Betrieb die Demo von Hand neu auf (wie nachts) – die Demo-Datenbank wird nicht von
Hand geändert.

- [ ] Prüfskript gegen die Demo: 0 Fehler, Hinweise gesichtet
- [ ] Rundgang mit den Konten Vorsitz und nicht vereidigt: Start (Hinweisband mit der Ankündigung), Sitzungen
      (Für mich, Alle Gremien), Fraktionssitzungen und eine Fraktionssitzung (NÖ-TOPs nur für Vereidigte,
      TOP-Vorschlag), Vorbereitung der Ratssitzung (Beratungsverlauf), Antrag mit Kommentaren, Recherche mit Karte
      und Suche, Handy (390 px)
- [ ] Zahl der Seiten ohne neue Gestaltung aus dem Bericht des Prüfskripts notieren (Stand für #951)
- [ ] Abnahme des Rahmens durch die Projektleitung an der Demo (Bilder nur aus der Demo)

### 4. Organisation einschalten

Erst nach der Abnahme und in Absprache mit der Organisation (Ankündigung im Produkt, #857):

- [ ] `work_neues_design an --org <slug> --probelauf`, dann ohne `--probelauf`
- [ ] `work_neues_design status --org <slug>` zeigt „an“
- [ ] Sichtprüfung in Work durch die Projektleitung bzw. die Administration der Organisation (Produktion wird nur
      lesend geprüft; das Prüfskript läuft gegen Produktion nur mit Konten, die dafür vorgesehen sind)
- [ ] Fehlerprotokoll der ersten Stunde ohne neue 5xx, keine Rückmeldung „Seite fehlt“

### 5. Rückweg

| Fall | Vorgehen |
|---|---|
| Eine Organisation hat Probleme mit dem neuen Design | `work_neues_design aus --org <slug>` – wirkt beim nächsten Seitenaufruf, Daten bleiben unberührt |
| Das neue Design stört überall | `work_neues_design aus --alle` |
| Das Image muss zurück | `deploy.sh rollback <tag>`; das Feld bleibt stehen (additive Migration, ältere Abbilder ignorieren es) |
| Ein Befund nur in einer Breite oder Rolle | Schalter an lassen, wenn die Arbeit nicht behindert ist; Issue mit Bild aus der Demo |

Weil der Schalter keine Daten verändert, braucht der Rückweg keine Datensicherung und keine Migration.
