# Neues Design in Work ausrollen

Das neue Erscheinungsbild von Work (Rahmen wie Insight, Startseite, Sitzungsansicht, Vorbereitung und Editor; Epic
#850) steht hinter einem **Schalter je Organisation**. Das Feld steht für neu angelegte Organisationen auf „aus“. In
einer Installation legt der Betreiber fest, welche Organisationen es nutzen (Soll-Stand). Diese Anleitung beschreibt,
wie ein Update diesen Stand bestätigt, wie neue Organisationen dazukommen und wie der Rückweg geht.
Der Rückweg ist jederzeit derselbe Schalter.

| Was | Wo |
|---|---|
| Schalter | Feld `Organization.work_new_design` (#852), für neue Organisationen aus; Darstellung: `apps/work/rahmen.py` |
| Soll-Stand | vom Betreiber festgelegt; Abgleich mit `work_neues_design status --soll an` |
| Schalten und Stand | Verwaltungsbefehl `work_neues_design` (`apps/work/management/commands/work_neues_design.py`) |
| Prüfen | `scripts/pruefe_work_design.py` (Playwright): alle Bereiche, fünf Rollen, fünf Breiten, Schalter an und aus; zählt Seiten ohne neue Gestaltung |
| Stand je Seite | `data-gestaltung` am Hauptbereich im neuen Rahmen: `neu` setzt die Seite selbst (Block `gestaltung`), sonst `bisher` |
| Demo-Inhalte | `setup_demo_environment` ruft `setup_demo_work` auf (nächtlicher Neuaufbau der Demo) |

## Soll-Stand je Instanz

| Instanz | Soll | Anmerkung |
|---|---|---|
| Eigener Betrieb | vom Betreiber festgelegt, etwa alle Organisationen an | eine neu angelegte Organisation steht zunächst auf „aus“ und wird nach „Organisation einschalten“ eingeschaltet |
| Öffentliche Demo | Demo-Organisation an | setzt `setup_demo_environment` bei jedem Neuaufbau |
| Staging, Selbstbetrieb, lokal | Stand der Datenbank, aus der die Instanz aufgebaut ist | ohne eigenen Beschluss gilt das Feld: neue Organisationen aus, die Demo-Organisation an |

Der Soll-Stand ist eine Entscheidung des Betreibers. Bleibt eine Organisation bewusst beim bisherigen Rahmen,
wird das festgehalten und beim Abgleich mit `--org <slug> --soll aus` geprüft.

**Ein Abgleich schaltet nie um.** `work_neues_design status --soll an` nennt nur die Abweichungen und endet dann mit
einem Fehler. Wer daraufhin pauschal umschaltet, dreht womöglich den beschlossenen Stand zurück; vor allem
`aus --alle` ist ausschließlich der Notfall-Rückweg (Abschnitt „Rückweg“).

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
python manage.py work_neues_design status --soll an              # Abgleich mit dem Soll-Stand, schaltet nichts um
python manage.py work_neues_design an  --org <slug> --probelauf  # zeigt nur, was sich ändern würde
python manage.py work_neues_design an  --org <slug>              # einschalten (mehrere: --org a --org b)
python manage.py work_neues_design aus --org <slug>              # Rückweg für eine Organisation
python manage.py work_neues_design aus --alle                    # Rückweg für alle Organisationen
```

Eingeschaltet wird bewusst nur je Organisation; `an --alle` gibt es nicht. Unbekannte Kurznamen brechen ab, ohne
etwas zu ändern. `status --soll an|aus` (auch mit `--org`) nennt jede Organisation, die abweicht, und endet dann mit
Exit-Code 1; stimmt alles, meldet er „Soll-Stand bestätigt (an)“ bzw. „(aus)“.

## Prüfskript

`scripts/pruefe_work_design.py` meldet sich je Rolle an (Vorsitz, Mitglied, Sachkundige, nicht vereidigt, Gast),
erkundet alle Work-Seiten über die Links, die diese Rolle sieht (nur Seitenaufrufe, keine Aktionen, Downloads oder
Schnittstellen; je Adressmuster höchstens zwei Seiten), und misst jede Seite in 390, 1280, 1440, 1920 und 2560 px.

**Wo es läuft:** lokal, in der CI, gegen die öffentliche Demo und gegen Staging – **nicht gegen Produktion**.
Produktion wird nach einem Deploy von Hand angesehen. Lokal folgt das Skript jedem Link außer denen der Sperrliste
(`AUSGESCHLOSSEN`: Aktionen, Teilstücke, Dateien, Schnittstellen), damit neue Seiten auffallen. Gegen entfernte
Instanzen ruft es zusätzlich nur Seiten der Erlaubnisliste `ERLAUBT_ENTFERNT` auf, auch mit `--nur`. Eine künftige
Adresse, deren Aufruf etwas ändert, wird dort also nie aufgerufen, selbst wenn sie auf der Sperrliste fehlt. Andere
sichtbare Links nennt der Bericht als „nicht geprüft“. Eine neue Seite kommt auf die Erlaubnisliste, wenn ihr Aufruf
nichts ändert. Der Test `apps/common/tests/test_pruefe_work_design.py` prüft, dass jeder Eintrag eine Seite von Work
mit GET ist und nicht auf der Sperrliste steht.

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
| Sichtbarer Link nicht auf der Erlaubnisliste (nur gegen entfernte Instanzen; nicht aufgerufen), je Adressmuster | Hinweis in der Zusammenfassung |

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

# Staging mit eigenen Konten je Rolle (nur Seiten der Erlaubnisliste):
MANDARI_PRUEF_VORSITZ_EMAIL=… MANDARI_PRUEF_VORSITZ_PASSWORT=… \
    python scripts/pruefe_work_design.py --basis https://<staging> --org <slug> --rollen vorsitz
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
laden statt Fenster verbreitern; genauer, dauert länger), `--sichtbar` (Browser sichtbar), `--nur-erlaubte` (lokal
wie gegen eine entfernte Instanz nur Seiten der Erlaubnisliste).

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

## Ablauf bei einem Update

Ein Update ändert den Schalter nicht: Es bringt neue Seiten und Bausteine, der Stand der Organisationen bleibt, wie
er ist. Darum gilt bei jedem Update: bestätigen statt umschalten.

1. Vorher den Stand sichern: `work_neues_design status --json > work-design-vorher.json`.
2. Nach dem Update `work_neues_design status --soll an` (bzw. mit `--org <slug> --soll aus` für bewusste
   Ausnahmen). Meldet der Abgleich eine Abweichung, **nichts pauschal umschalten**, auf keinen Fall `aus --alle`,
   sondern mit dem gesicherten Stand vergleichen. Meist ist es eine neu angelegte Organisation (Feld „aus“).
3. Das Prüfskript gegen eine Demo- oder Testinstanz laufen lassen, nie gegen Produktion.

### Organisation einschalten

In Absprache mit der Organisation (Ankündigung im Produkt, #857):

- `work_neues_design an --org <slug> --probelauf`, dann ohne `--probelauf`
- `work_neues_design status --org <slug> --soll an` bestätigt den Stand
- Sichtprüfung in Work durch die Administration der Organisation, Fehlerprotokoll der ersten Stunde ohne neue 5xx

## Rückweg

Jeder Rückweg weicht vom beschlossenen Stand ab und geschieht nur nach Entscheidung des Betreibers. Danach
festhalten, welche Organisationen aus sind, damit der nächste Abgleich sie nicht als Fehler meldet
und niemand sie unbeabsichtigt wieder einschaltet.

| Fall | Vorgehen |
|---|---|
| Eine Organisation hat Probleme mit dem neuen Design | `work_neues_design aus --org <slug>` – wirkt beim nächsten Seitenaufruf, Daten bleiben unberührt; danach `status --org <slug> --soll aus` |
| Das neue Design stört überall | Stand sichern (`status --json > work-design-vor-rueckweg.json`), dann `work_neues_design aus --alle`. Zurück zum beschlossenen Stand mit `an --org <slug>` je Organisation aus der Sicherung (`an --alle` gibt es bewusst nicht) |
| Das Image muss zurück | `deploy.sh rollback <tag>`; das Feld und der Stand der Organisationen bleiben (additive Migration, ältere Abbilder ignorieren es) |
| Ein Befund nur in einer Breite oder Rolle | Schalter an lassen, wenn die Arbeit nicht behindert ist; Issue mit Bild aus der Demo |

Weil der Schalter keine Daten verändert, braucht der Rückweg keine Datensicherung und keine Migration.
