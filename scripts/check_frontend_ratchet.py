# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Frontend-Ratchet: Kennzahlen der Template-Hygiene dürfen nur sinken.

Gezählt werden in mandari/templates:
- Inline-<script>-Blöcke (ohne src, ohne JSON-Typ)
- Inline-<style>-Blöcke
- Templates mit mehr als MAX_LINES Zeilen
- on*="…"-Event-Handler im Markup
- style="…"-Attribute

Die Baseline liegt in scripts/frontend_ratchet_baseline.json. Steigt ein Wert über
die Baseline, schlägt der Lauf fehl. Sinkt ein Wert, wird die Baseline mit
`--update` nachgezogen (im selben PR committen). Templates in der Allowlist
(Layouts, E-Mails, PDF, PWA) werden bei Inline-Skripten/-Styles nicht gezählt,
weil dort Inline-Code fachlich begründet ist.

Aufruf:  python scripts/check_frontend_ratchet.py [--update] [--verbose]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TEMPLATES = Path("mandari/templates")
BASELINE = Path("scripts/frontend_ratchet_baseline.json")
MAX_LINES = 300
ALLOW_INLINE = (
    re.compile(r"^base[^/]*\.html$"),
    re.compile(r"^(work|session)/base_[^/]*\.html$"),
    re.compile(r"^emails/"),
    re.compile(r"/pdf/"),
    re.compile(r"^pwa/"),
    re.compile(r"^work/notifications/email/"),
)
SCRIPT_RE = re.compile(
    r"<script\b(?![^>]*\bsrc=)(?![^>]*type=[\"'](?:application/(?:ld\+)?json|text/template)[\"'])[^>]*>", re.I
)
STYLE_RE = re.compile(r"<style\b[^>]*>", re.I)
ONHANDLER_RE = re.compile(r"\son[a-z]+=\"", re.I)
STYLE_ATTR_RE = re.compile(r"\sstyle=\"", re.I)
CLASS_RE = re.compile(r"\sclass=\"([^\"{}]+)\"")
DUP_MIN_TOKENS = 4
DUP_MIN_COUNT = 10


def allowed(rel: str) -> bool:
    return any(p.search(rel) for p in ALLOW_INLINE)


def measure(verbose: bool = False) -> dict[str, int]:
    counts = {
        "inline_scripts": 0,
        "inline_styles": 0,
        "templates_over_300": 0,
        "on_handlers": 0,
        "style_attrs": 0,
        "duplicate_class_chains": 0,
    }
    big: list[tuple[int, str]] = []
    chains: dict[str, int] = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        rel = path.relative_to(TEMPLATES).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.count("\n") + 1
        if lines > MAX_LINES:
            counts["templates_over_300"] += 1
            big.append((lines, rel))
        counts["on_handlers"] += len(ONHANDLER_RE.findall(text))
        counts["style_attrs"] += len(STYLE_ATTR_RE.findall(text))
        for chain in CLASS_RE.findall(text):
            key = " ".join(sorted(chain.split()))
            if len(chain.split()) >= DUP_MIN_TOKENS:
                chains[key] = chains.get(key, 0) + 1
        if not allowed(rel):
            counts["inline_scripts"] += len(SCRIPT_RE.findall(text))
            counts["inline_styles"] += len(STYLE_RE.findall(text))
    counts["duplicate_class_chains"] = sum(1 for n in chains.values() if n >= DUP_MIN_COUNT)
    if verbose:
        for lines, rel in sorted(big, reverse=True):
            print(f"  {lines:5d}  {rel}")
        for key, n in sorted(chains.items(), key=lambda kv: -kv[1])[:15]:
            if n >= DUP_MIN_COUNT:
                print(f"  {n:4d}x  {key[:100]}")
    return counts


def main(argv: list[str]) -> int:
    update = "--update" in argv
    verbose = "--verbose" in argv
    current = measure(verbose)
    if not BASELINE.exists() or update:
        BASELINE.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("Baseline geschrieben:", json.dumps(current))
        return 0
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    failed = False
    for key, value in current.items():
        limit = baseline.get(key, value)
        marker = "OK " if value <= limit else "ZU HOCH"
        if value > limit:
            failed = True
        print(f"{marker:8s} {key:20s} {value:5d}  (Baseline {limit})")
    if failed:
        print(
            "\nFrontend-Ratchet verletzt: Inline-Code oder Template-Größe hat zugenommen. "
            "Bitte auslagern (frontend/…, Komponenten) statt Baseline erhöhen."
        )
        return 1
    lowered = {k: v for k, v in current.items() if v < baseline.get(k, v)}
    if lowered:
        print("\nHinweis: Werte gesunken, Baseline mit --update nachziehen:", json.dumps(lowered))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
