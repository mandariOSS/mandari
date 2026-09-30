# Beitragsvereinbarung

**Stand:** in Vorbereitung. Die Vereinbarung wird derzeit finalisiert; ihr Text steht hier, sobald er
freigegeben ist.

Beiträge von außerhalb des Projektteams nehmen wir erst an, nachdem eine Beitragsvereinbarung
(Contributor License Agreement) geschlossen wurde. Beiträge bleiben in jedem Fall unter der AGPL frei
verfügbar.

Bis die Vereinbarung vorliegt: bitte vor einem Pull Request ein Issue öffnen, damit wir das Vorgehen
abstimmen können. Fehlermeldungen, Fragen und Vorschläge sind davon unabhängig jederzeit willkommen.

## Fassungen

| Fassung | Für wen | Text | Gültig ab |
|---------|---------|------|-----------|
| folgt | Einzelpersonen | folgt | – |
| folgt | Organisationen, deren Beschäftigte beitragen | folgt | – |

## Ablauf

Das Verfahren für die Zustimmung wird zusammen mit dem Text veröffentlicht und dann hier beschrieben,
getrennt für Einzelpersonen und für Organisationen.

Für die Prüfung im Pull Request gilt schon jetzt: Sie arbeitet je GitHub-Konto. Konten, für die eine
Organisation die Vereinbarung geschlossen hat, werden deshalb einzeln eingetragen.

## Prüfung im Pull Request

In jedem Pull Request gegen `dev` oder `main` läuft der Check **„Beitragsvereinbarung“**
(`.github/workflows/cla.yml`, Logik in `scripts/cla_pruefung.py`).

Geprüft werden

- das Konto, das den Pull Request geöffnet hat, und
- alle Konten, denen GitHub die Commits zuordnet: Autorin oder Autor und Mitautoren aus
  `Co-authored-by`.

Freigestellt sind die Projektkonten und die Bots, die in `.github/cla.json` stehen (zum Beispiel
Dependabot). Jedes andere Konto braucht einen Eintrag in der Signaturliste zu einer gültigen Fassung.

Fehlt ein Eintrag, schlägt der Check fehl, und ein Kommentar im Pull Request nennt die betroffenen
Konten und die nächsten Schritte. Der Kommentar wird bei jedem Lauf aktualisiert, nicht wiederholt.

**Commits ohne zugeordnetes Konto.** GitHub ordnet einen Commit über die E-Mail-Adresse der
Autorenangabe einem Konto zu. Ist die Adresse in keinem Konto hinterlegt, lässt sich nicht
feststellen, für wen die Vereinbarung gelten muss; der Check schlägt dann fehl. Abhilfe: die Adresse
im eigenen GitHub-Konto hinterlegen (Settings → Emails). Die Commits müssen dafür nicht neu
geschrieben werden. Bei Pull Requests von Projektkonten und Bots zählen solche Commits als deren
Beitrag.

## Welche Daten die Prüfung braucht

Die Signaturliste enthält je Eintrag

- den GitHub-Kontonamen,
- die numerische Kontokennung (sie bleibt gleich, wenn ein Konto umbenannt wird; ein neu
  registriertes Konto mit einem frei gewordenen Namen erbt die Zustimmung deshalb nicht),
- das Datum der Zustimmung und
- die Fassung der Vereinbarung.

Sie enthält keine E-Mail-Adressen, keine Klarnamen und keine Anschriften.

Die Liste steht **nicht in diesem Repository** und ist nicht öffentlich. Eine Datei im öffentlichen
Repository bliebe über die Versionsgeschichte und jede Kopie dauerhaft abrufbar, auch nachdem ein
Eintrag gelöscht wurde; eine nicht öffentliche Liste lässt sich berichtigen und löschen. Der
Workflow erhält die Liste als Secret. Im öffentlichen Protokoll eines Laufs steht je Konto nur der
Status (Projektkonto, Bot, Zustimmung liegt vor, Zustimmung fehlt), nie Datum oder Fassung.

Dass für ein Konto eine Zustimmung vorliegt, lässt sich damit nur mittelbar erkennen: daran, dass
sein Pull Request die Prüfung besteht.

Welche Angaben für die Vereinbarung selbst nötig sind und wie sie verarbeitet werden, wird zusammen
mit dem Text veröffentlicht.

## Sicherheit des Workflows

Der Workflow läuft mit dem Ereignis `pull_request_target`, weil er auch bei Pull Requests aus Forks
kommentieren und die Signaturliste lesen muss. Dafür gelten feste Regeln:

- Ausgecheckt wird nur der Zielzweig, nie der Stand des Pull Requests. Skript und Konfiguration
  stammen also immer aus diesem Repository; ein Pull Request kann sich nicht selbst freistellen.
- Über den Pull Request liest das Skript ausschließlich Metadaten per API.
- Das Token darf lesen und im Pull Request kommentieren, sonst nichts.
- Es wird keine fremde Action verwendet.

`mandari/apps/common/tests/test_cla_pruefung.py` hält diese Eigenschaften und die Prüfregeln fest.

## Pflege durch das Projektteam

### Konfiguration

`.github/cla.json`:

| Schlüssel | Bedeutung |
|-----------|-----------|
| `gueltige_versionen` | Fassungen, zu denen eine Zustimmung gilt. Leer heißt: Es ist noch keine Fassung freigegeben, kein Eintrag gilt. |
| `projektkonten` | Konten des Projektteams; sie brauchen keinen Eintrag. |
| `bots` | Freigestellte Bots, mit der Endung `[bot]`. Andere Bots sind nicht freigestellt. |

Änderungen wirken, sobald sie im Zielzweig des Pull Requests stehen.

### Signaturliste

Format:

```json
{
  "signaturen": [
    {"konto": "beispiel-konto", "id": 1234567, "datum": "2026-01-31", "version": "1.0"}
  ]
}
```

Die maßgebliche Liste führt das Projektteam nicht öffentlich. Nach jeder Änderung:

```bash
gh api users/beispiel-konto --jq .id                          # Kontokennung nachschlagen
python scripts/cla_pruefung.py --liste-pruefen signaturen.json  # Format prüfen
gh secret set CLA_SIGNATUREN --repo mandariOSS/mandari < signaturen.json
```

Danach im betroffenen Pull Request den fehlgeschlagenen Lauf neu starten (Actions →
„Beitragsvereinbarung“ → „Re-run jobs“). Der neue Lauf liest Liste und Konfiguration frisch; ein
weiterer Push ist nicht nötig. Ein Secret fasst höchstens 48 KB, das reicht für mehrere hundert
Einträge.

Eine unlesbare Liste gilt als technischer Fehler: Der Check schlägt fehl, ohne jemandem eine fehlende
Zustimmung vorzuhalten. Pull Requests, die die Liste nicht brauchen (Projektkonten, Bots), laufen
mit einer Warnung durch.

### Lokal prüfen

```bash
GITHUB_TOKEN=$(gh auth token) python scripts/cla_pruefung.py \
  --repo mandariOSS/mandari --pr 123 --kein-kommentar
```

### Nach der Freigabe des Textes

1. Texte in dieses Verzeichnis legen (je Fassung eine Datei für Einzelpersonen und eine für
   Organisationen) und die Tabelle „Fassungen“ ausfüllen.
2. Den Abschnitt „Ablauf“ mit dem Verfahren für die Zustimmung füllen.
3. Die Versionsnummer in `.github/cla.json` unter `gueltige_versionen` eintragen. Der Kommentar im
   Pull Request wechselt damit von „wird derzeit finalisiert“ auf den Verweis hierher.
4. In `CONTRIBUTING.md` den Hinweis „wird derzeit finalisiert“ durch den Verweis auf den Text ersetzen.
5. Den Check „Beitragsvereinbarung“ im Branch-Schutz von `dev` und `main` als Pflicht-Check setzen.
   Erst dann blockiert er das Mergen; vorher zeigt er nur an.

### Grenzen

- Der Check stellt fest, ob für die beteiligten Konten ein Eintrag vorliegt. Er erfasst keine
  Zustimmungen.
- Die Autorenangabe eines Commits legt fest, wer den Commit erstellt. Verlässlich angemeldet ist nur
  das Konto, das den Pull Request öffnet; deshalb braucht dieses Konto immer selbst eine Freistellung
  oder einen Eintrag.
- Wer aus dem Projektteam Commits Dritter in einen eigenen Pull Request übernimmt, klärt die
  Vereinbarung vorher selbst. Ist die Adresse eines solchen Commits keinem Konto zugeordnet, fällt
  er dem Check nicht auf.
