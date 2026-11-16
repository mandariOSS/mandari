# Mandari: Ergänzungen für Claude

@AGENTS.md

Der gemeinsame Projektkontext für alle KI-Assistenten steht in [AGENTS.md](AGENTS.md) und wird oben
eingebunden, samt den verbindlichen Arbeitsregeln. Hier steht nur, was zusätzlich für Claude gilt.

---

## Git-Workflow

**Entwickelt wird auf Branches von `dev`; Änderungen kommen per Pull Request nach `dev`.** `main` ist der
Produktionsstand; aus ihm erscheint alle zwei Wochen eine Version. Ablauf, Prioritäten und Release-Zug:
[docs/WEITERENTWICKLUNG.md](docs/WEITERENTWICKLUNG.md); Prüfungen: [CONTRIBUTING.md](CONTRIBUTING.md#commits-und-pull-requests).

```bash
# Standard-Workflow
git fetch origin
git switch -c fix/mein-thema origin/dev
# ... Änderungen, Tests, CHANGELOG-Zeile unter [Unreleased] ...
git add <files>
git commit -m "fix(bereich): Beschreibung"
git push -u origin fix/mein-thema
gh pr create --base dev
```

**Korrektur für eine ältere Version** (Sicherheit ab „mittel“ oder kritischer Fehler): erst wie oben nach
`dev`, nach dem Merge zurückportieren, nie direkt auf `release/v*`:

```bash
python scripts/rueckportieren.py <PR-Nummer> --grund sicherheit-hoch   # öffnet je Pflegelinie einen PR
```

---

## Interne Unterlagen

Wer mit dem privaten Repository arbeitet (Arbeitskopie `.private/`, gitignoriert), findet die internen
Arbeitsregeln in `.private/projektsteuerung/ARBEITSREGELN.md`. Konzepte dort sind Hintergrund, keine
Aufgabenquelle; Aufgaben stehen nur in GitHub.
