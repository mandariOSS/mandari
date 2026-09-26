# Mandari 2.0 - Claude-Kontext

@AGENTS.md

Der gemeinsame Projektkontext für alle KI-Assistenten steht in [AGENTS.md](AGENTS.md) und wird oben
eingebunden. Hier steht nur, was zusätzlich für Claude gilt.

---

## Git-Workflow

**Entwicklung erfolgt ausschließlich auf dem `dev` Branch.**

```bash
# Standard-Workflow
git checkout dev
git pull origin dev
# ... Änderungen ...
git add <files>
git commit -m "feat/fix/chore: Beschreibung"
git push origin dev
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
