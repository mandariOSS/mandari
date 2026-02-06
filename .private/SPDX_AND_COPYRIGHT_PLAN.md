# SPDX-Header und Copyright-Handling Plan

**Erstellt**: 2026-02-06
**Status**: Planung
**Copyright Holder**: Sven Konopka and contributors

---

## 1. Übersicht

### Ziel
- SPDX-Header in allen Source-Dateien
- Klare Copyright-Regeln für Contributors
- DCO (Developer Certificate of Origin) für Beiträge

### SPDX-Identifier
```
SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
SPDX-License-Identifier: AGPL-3.0-or-later
```

---

## 2. SPDX-Header nach Dateityp

### Python (.py)
```python
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Module docstring here."""
```

### Django Templates (.html)
```html
{# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors #}
{# SPDX-License-Identifier: AGPL-3.0-or-later #}
{% extends "base.html" %}
```

### JavaScript (.js)
```javascript
// SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
// SPDX-License-Identifier: AGPL-3.0-or-later
```

### CSS / Tailwind (.css)
```css
/* SPDX-FileCopyrightText: 2025 Sven Konopka and contributors */
/* SPDX-License-Identifier: AGPL-3.0-or-later */
```

### Shell Scripts (.sh)
```bash
#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
```

### YAML (.yml, .yaml)
```yaml
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
```

### TOML (.toml)
```toml
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
```

### Dockerfile
```dockerfile
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
```

### Markdown (.md) - Optional
```markdown
<!-- SPDX-FileCopyrightText: 2025 Sven Konopka and contributors -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
```
*Hinweis: Bei Markdown ist SPDX optional, da Docs meist nicht als "Code" gelten*

---

## 3. Betroffene Dateien

### Zu bearbeiten (geschätzt)

| Verzeichnis | Dateityp | Anzahl (ca.) |
|-------------|----------|--------------|
| `mandari/apps/**/*.py` | Python | ~150 |
| `mandari/insight_*/**/*.py` | Python | ~30 |
| `mandari/templates/**/*.html` | Django Templates | ~80 |
| `mandari/static/**/*.js` | JavaScript | ~10 |
| `mandari/static/**/*.css` | CSS | ~5 |
| `ingestor/src/**/*.py` | Python | ~20 |
| `*.sh` | Shell | ~5 |
| `docker-compose*.yml` | YAML | ~3 |
| `Dockerfile*` | Docker | ~2 |
| `.github/workflows/*.yml` | YAML | ~2 |

**Gesamt: ~300+ Dateien**

### Ausnahmen (KEIN SPDX nötig)
- `__init__.py` (leere Dateien)
- `migrations/*.py` (auto-generiert)
- `node_modules/` (third-party)
- `.venv/` (virtual environment)
- `*.pyc`, `__pycache__/` (compiled)
- `.env*` (Konfiguration)
- `*.json` (Daten, kein SPDX-Kommentar möglich)

---

## 4. Automatisierungsskript

### add-spdx-headers.py
```python
#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Add SPDX headers to all source files."""

import os
from pathlib import Path

SPDX_HEADER = {
    ".py": """\
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

""",
    ".html": """\
{# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors #}
{# SPDX-License-Identifier: AGPL-3.0-or-later #}
""",
    ".js": """\
// SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
// SPDX-License-Identifier: AGPL-3.0-or-later

""",
    ".css": """\
/* SPDX-FileCopyrightText: 2025 Sven Konopka and contributors */
/* SPDX-License-Identifier: AGPL-3.0-or-later */

""",
    ".sh": """\
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

""",
    ".yml": """\
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

""",
    ".yaml": """\
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

""",
}

EXCLUDE_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__",
    ".git", "migrations", "dist", "build"
}

EXCLUDE_FILES = {"__init__.py"}


def should_skip(path: Path) -> bool:
    """Check if file should be skipped."""
    # Skip excluded directories
    for part in path.parts:
        if part in EXCLUDE_DIRS:
            return True

    # Skip excluded files
    if path.name in EXCLUDE_FILES:
        return True

    return False


def has_spdx_header(content: str) -> bool:
    """Check if file already has SPDX header."""
    return "SPDX-License-Identifier" in content[:500]


def add_header(path: Path, header: str) -> bool:
    """Add SPDX header to file."""
    content = path.read_text(encoding="utf-8")

    if has_spdx_header(content):
        return False  # Already has header

    # Handle shebang
    if content.startswith("#!"):
        lines = content.split("\n", 1)
        new_content = lines[0] + "\n" + header + lines[1]
    else:
        new_content = header + content

    path.write_text(new_content, encoding="utf-8")
    return True


def main():
    """Process all files."""
    base_path = Path(".")
    modified = 0
    skipped = 0

    for ext, header in SPDX_HEADER.items():
        for path in base_path.rglob(f"*{ext}"):
            if should_skip(path):
                continue

            if add_header(path, header):
                print(f"Added header: {path}")
                modified += 1
            else:
                skipped += 1

    print(f"\nDone: {modified} files modified, {skipped} already had headers")


if __name__ == "__main__":
    main()
```

---

## 5. DCO (Developer Certificate of Origin)

### Was ist DCO?
Der DCO ist eine leichtgewichtige Alternative zu CLAs. Contributors bestätigen per Sign-off, dass sie das Recht haben, den Code beizutragen.

### DCO Text (hinzufügen zu CONTRIBUTING.md)

```markdown
## Developer Certificate of Origin (DCO)

Mit deinem Beitrag zu diesem Projekt stimmst du dem [Developer Certificate of Origin (DCO)](https://developercertificate.org/) zu:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.

Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

### Signoff

Jeder Commit muss mit deinem Namen und E-Mail signiert sein:

```bash
git commit -s -m "feat: Mein neues Feature"
```

Dies fügt automatisch hinzu:
```
Signed-off-by: Dein Name <deine@email.de>
```
```

---

## 6. Änderungen an CONTRIBUTING.md

### Neue Abschnitte hinzufügen

```markdown
## Lizenz und Copyright

### Lizenz

Mandari ist lizenziert unter [AGPL-3.0-or-later](LICENSE). Mit deinem Beitrag stimmst du zu, dass dein Code unter dieser Lizenz veröffentlicht wird.

### Copyright

- **Bestehender Code**: Copyright 2025 Sven Konopka and contributors
- **Dein Beitrag**: Du behältst das Copyright an deinem Code, erteilst aber eine Lizenz unter AGPL-3.0-or-later

### SPDX-Header

Alle neuen Dateien müssen einen SPDX-Header enthalten:

**Python:**
```python
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
```

**Wenn du eine bestehende Datei signifikant änderst**, kannst du dich zum Copyright hinzufügen:
```python
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-FileCopyrightText: 2026 Dein Name <deine@email.de>
# SPDX-License-Identifier: AGPL-3.0-or-later
```

### Developer Certificate of Origin (DCO)

[... DCO-Text wie oben ...]
```

---

## 7. GitHub Actions: DCO-Check

### .github/workflows/dco-check.yml
```yaml
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

name: DCO Check

on:
  pull_request:
    branches: [main]

jobs:
  dco:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Check DCO Sign-off
        uses: dcoapp/app@v1
        with:
          token: ${{ secrets.GITHUB_TOKEN }}
```

---

## 8. GitHub Actions: SPDX-Check (Optional)

### .github/workflows/spdx-check.yml
```yaml
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

name: SPDX License Check

on:
  pull_request:
    branches: [main]

jobs:
  spdx:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install reuse tool
        run: pip install reuse

      - name: Check SPDX compliance
        run: reuse lint
```

*Hinweis: Erfordert `.reuse/dep5` Datei für Ausnahmen*

---

## 9. Implementierungsplan

### Phase 1: Vorbereitung (Tag 1)
| Task | Beschreibung |
|------|--------------|
| 1.1 | LICENSE-Datei erstellen (AGPL-3.0-or-later Volltext) |
| 1.2 | CONTRIBUTING.md um Copyright-Abschnitt erweitern |
| 1.3 | DCO-Abschnitt hinzufügen |

### Phase 2: SPDX-Header (Tag 2-3)
| Task | Beschreibung |
|------|--------------|
| 2.1 | Automatisierungsskript erstellen |
| 2.2 | Skript in Testumgebung ausführen |
| 2.3 | Manuell prüfen und Edge Cases fixen |
| 2.4 | Alle Änderungen committen |

### Phase 3: CI/CD (Tag 4)
| Task | Beschreibung |
|------|--------------|
| 3.1 | DCO-Check Workflow hinzufügen |
| 3.2 | Optional: SPDX-Check Workflow |
| 3.3 | Testen mit Test-PR |

### Phase 4: Dokumentation (Tag 5)
| Task | Beschreibung |
|------|--------------|
| 4.1 | README aktualisieren |
| 4.2 | CONTRIBUTING.md finalisieren |
| 4.3 | Docs aktualisieren |

---

## 10. Wann Contributors Copyright hinzufügen

### Richtlinie

| Situation | Aktion |
|-----------|--------|
| Kleine Fixes (Typos, Bugfixes < 10 Zeilen) | Kein separates Copyright nötig |
| Signifikante Änderungen an bestehender Datei | Optional: SPDX-FileCopyrightText hinzufügen |
| Neue Datei erstellen | SPDX-Header mit eigenem Namen ODER "and contributors" |
| Komplettes neues Feature/Modul | Eigenes Copyright in SPDX-Header |

### Beispiel: Contributor fügt neues Modul hinzu

```python
# SPDX-FileCopyrightText: 2025 Sven Konopka and contributors
# SPDX-FileCopyrightText: 2026 Max Mustermann <max@example.de>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Neues Modul von Max Mustermann."""
```

---

## 11. Erfolgskriterien

- [ ] LICENSE-Datei existiert mit AGPL-3.0-or-later Volltext
- [ ] Alle ~300 Dateien haben SPDX-Header
- [ ] CONTRIBUTING.md enthält Copyright- und DCO-Abschnitt
- [ ] DCO-Check läuft bei PRs
- [ ] README zeigt korrekten Copyright-Hinweis
- [ ] Neue PRs werden auf Sign-off geprüft

---

## Anhang: AGPL-3.0-or-later LICENSE Datei

Die LICENSE-Datei muss den vollständigen Text der GNU Affero General Public License Version 3 enthalten, verfügbar unter:
https://www.gnu.org/licenses/agpl-3.0.txt

Am Anfang der Datei kann ein Projekt-spezifischer Header stehen:

```
Mandari - Open Source Platform for Municipal Political Transparency
Copyright (C) 2025 Sven Konopka and contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

---

[Vollständiger AGPL-3.0 Text hier einfügen]
```
