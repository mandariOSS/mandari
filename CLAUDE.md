# Mandari 2.0 - Claude-Kontext

@AGENTS.md

Der gemeinsame Projektkontext für alle KI-Assistenten steht in [AGENTS.md](AGENTS.md) und wird oben
eingebunden. Hier steht nur, was zusätzlich für Claude gilt.

---

## Git-Workflow

**Entwickelt wird auf Branches von `dev`; Änderungen kommen per Pull Request nach `dev`.** `main` ist der
Produktionsstand und wird nur nachgezogen. Ablauf und Prüfungen:
[CONTRIBUTING.md](CONTRIBUTING.md#commits-und-pull-requests).

```bash
# Standard-Workflow
git fetch origin
git switch -c fix/mein-thema origin/dev
# ... Änderungen, Tests ...
git add <files>
git commit -m "fix(bereich): Beschreibung"
git push -u origin fix/mein-thema
gh pr create --base dev
```

---

## .private/ - Interne Planungsdokumente

Das `.private/` Verzeichnis (gitignoriert, nur lokal) enthält interne Planungsdokumente:

| Datei | Inhalt |
|-------|--------|
| `MASTER_FEATURE_LIST.md` | Vollständige Feature-Liste & Roadmap |
| `ARCHITECTURE_OPTIMIZATION_PLAN.md` | Architektur-Optimierungen |
| `CI_CD_AND_INSTALL_PLAN.md` | CI/CD & Deployment-Konfiguration |
| `DJANGO_PERFORMANCE_OPTIMIZATION_PLAN.md` | Performance-Optimierungen |
| `PLAN_TEXT_EXTRACTION_SEO_SEARCH.md` | Text-Extraktion, SEO, Suche |
| `SPDX_AND_COPYRIGHT_PLAN.md` | Lizenz & Copyright Headers |
| `INSIGHT_FEATURE_STATUS.md` | **Feature-Status Insight Portal** (Pflichtdoku) |

**Wichtig**: Bei neuen Features zuerst `.private/MASTER_FEATURE_LIST.md` prüfen!

### Pflicht: Feature-Status aktualisieren

Nach **jeder abgeschlossenen Aufgabe**, die ein Insight-Feature betrifft, MUSS `.private/INSIGHT_FEATURE_STATUS.md` aktualisiert werden:
- Status-Emoji anpassen (:white_check_mark: / :construction: / :clipboard:)
- Fortschritts-Prozent aktualisieren
- Neue Unter-Komponenten eintragen falls hinzugefügt
- Datum "Zuletzt aktualisiert" setzen
