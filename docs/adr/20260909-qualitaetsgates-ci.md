# Qualitätsgates in der CI und Ratchet-Prinzip

- Status: angenommen
- Datum: 2026-09-09

## Kontext

Die CI konnte in mehreren Schritten nicht fehlschlagen: `check --deploy` lief mit `|| true`, der
Test-Schritt mit `|| echo`, der Editor-Build im Dockerfile mit `|| true`. Es gab keine pytest-Tests im
Django-Projekt, die 79 Ingestor-Tests liefen nicht in der CI, mypy war konfiguriert, wurde aber nie
ausgeführt. Sicherheits-Settings kamen ausschließlich vom Reverse Proxy. Ein Audit im September 2026
hat das dokumentiert.

## Entscheidung

1. **Gates dürfen rot werden.** Alle `|| true`- und `|| echo`-Konstrukte in CI und Dockerfile werden
   entfernt. `manage.py check --deploy --fail-level WARNING` ist blockierend; stillgelegte Checks stehen
   mit Begründung in `SILENCED_SYSTEM_CHECKS`.
2. **Ratchet statt Big Bang.** Für Kennzahlen, die nicht sofort auf null gebracht werden können
   (Inline-Skripte, Inline-Styles, Templates über 300 Zeilen, Event-Handler-Attribute, Typisierungs-
   Allowlist), wird eine Baseline im Repo geführt. Die CI schlägt fehl, wenn ein Wert steigt; sinkt er,
   wird die Baseline im selben PR nachgezogen. Skript: `scripts/check_frontend_ratchet.py`.
3. **Sicherheit in Django.** Transport- und Cookie-Sicherheit, Referrer-Policy und CSP werden in
   `settings.py` konfiguriert; der Proxy bleibt zusätzliche Verteidigungslinie, nicht die einzige.
4. **Tests wachsen aus den Smoke-Skripten.** Neue Tests entstehen unter `apps/<app>/tests/` mit
   pytest-django; Smoke-Skripte werden bei Berührung migriert. Ingestor-Tests laufen als eigener Job.
5. **Supply Chain.** Dependabot für pip, uv, npm, GitHub Actions und Docker; Lockfile für das
   Django-Projekt und CycloneDX-SBOM je Release folgen als Issues.

## Folgen

- Die erste CI nach dieser Entscheidung wird strenger; Fehlschläge sind gewollt und werden behoben,
  nicht stillgelegt.
- Baseline-Dateien sind Teil des Repos und werden in Reviews mitgelesen.
- Der Weg zu Enforce-CSP, mypy-strict und Coverage-Schwellen ist messbar und in Issues nachverfolgbar.
