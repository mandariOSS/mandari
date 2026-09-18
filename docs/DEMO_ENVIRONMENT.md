# Demo- und Musterumgebung

Das Management-Command `setup_demo_environment` erstellt eine vollständige,
klar als Demo erkennbare Musterumgebung über alle drei Portale hinweg.
Alle Daten sind synthetisch und frei erfunden.

```bash
python manage.py setup_demo_environment          # anlegen/aktualisieren
python manage.py setup_demo_environment --reset  # restlos entfernen
```

Für Produktvorstellungen setzt `setup_demo_praesentation` ein durchgehendes Drehbuch auf diese
Umgebung auf – eine Drucksache von der Fraktion bis ins Bürgerportal, dazu ein zweiter Mandant
und eine Leitstelle (siehe [DEMO_PRAESENTATION.md](DEMO_PRAESENTATION.md)).

## Was wird angelegt?

### 1. Insight (öffentliches Portal)

Fiktive Kommune **„Musterstadt (Demo)"** (`OParlBody`, Slug `musterstadt-demo`):

- 3 Gremien (Rat, Hauptausschuss, Ausschuss für Bauen und Verkehr) und
  2 Fraktionen als `OParlOrganization`
- 8 fiktive Personen mit Mitgliedschaften (Rat, Ausschüsse, Fraktionen)
- 1 Wahlperiode, 6 Sitzungen (vergangen und kommend) mit Tagesordnungspunkten
- 12 Vorlagen verschiedener Typen (Beschlussvorlage, Antrag, Anfrage,
  Mitteilungsvorlage) mit Beratungen (`OParlConsultation`)
- 2 kleine, selbst generierte PDF-Dateien mit gesetztem `text_content`
  (keine OCR nötig), abgelegt unter `MEDIA_ROOT/demo/`

Die Kommune ist **nicht gelistet** (`is_listed=False`): Sie erscheint weder in der
Kommunenauswahl noch in Übersichten, Sitemaps oder der OParl-Aggregations-API, ist aber per
direkter URL erreichbar (`/insight/kommune/<uuid>/`). Überall steht „(Demo)" im Namen.

### 2. Work (Fraktions-Arbeitsbereich)

Organisation **„Musterfraktion (Demo)"** (Slug `musterfraktion-demo`),
verknüpft mit der Musterstadt und der Parteigruppe „Musterpartei (Demo)":

- Standard-Rollen über die `setup_roles`-Mechanik
- 3 Demo-Nutzer: `demo-vorsitz@demo.mandari.de` (Fraktionsvorsitz),
  `demo-mitglied@demo.mandari.de` (Fraktionsmitglied),
  `demo-gast@demo.mandari.de` (Gast mit Ordner-Freigabe „Lesen")
- 2 Anträge (einer im Entwurf, einer eingereicht), 1 Sitzungsvorbereitung
  mit Positionen und Notizen zur kommenden Ratssitzung, 3 Aufgaben,
  1 Fraktionssitzung

### 3. Session (Verwaltungs-RIS)

Mandant **„Stadtverwaltung Musterstadt (Demo)"**
(Slug `stadtverwaltung-musterstadt-demo`):

- Standard-Rollen und 4 Verwaltungsnutzer, je einer pro Rolle:
  `demo-verwaltung@demo.mandari.de` (Administrator),
  `demo-sachbearbeitung@demo.mandari.de` (Sachbearbeiter),
  `demo-protokoll@demo.mandari.de` (Protokollant),
  `demo-lesezugriff@demo.mandari.de` (Lesezugriff)
- 3 Gremien und 8 Personen passend zur Kommune — Kontaktdaten werden
  über die Accessoren AES-256-GCM-verschlüsselt gespeichert; dazu das Amt
  „Kämmerei (Demo)" mit einer Mitzeichnungsregel für Vorlagen mit finanziellen Auswirkungen
- 3 Sitzungen mit Tagesordnung, 4 Vorlagen (eine mit vertraulichem Inhalt),
  2 Anträge der Musterfraktion
- Anwesenheit und genehmigtes Protokoll der vergangenen Hauptausschuss-Sitzung

Der Mandant veröffentlicht nicht im Bürgerportal (`insight_publish` aus); das schaltet erst
die Präsentationsumgebung ein.

## Zugangsdaten

Die Passwörter der Demo-Nutzer werden bei **jedem Lauf neu generiert** und
ausschließlich auf stdout ausgegeben — sie werden nirgendwo gespeichert.
Ein erneuter Lauf rotiert die Passwörter (praktisch, wenn sie verloren gehen).

Die Domain `demo.mandari.de` ist über `TWO_FACTOR_EXEMPT_EMAIL_DOMAINS` (Standardwert) von der
Zwei-Faktor-Pflicht ausgenommen – die Zugänge werden gemeinsam genutzt und enthalten keine
echten Daten.

## Idempotenz und Aufräumen

- Alle Objekte hängen an festen Demo-Kennungen: Slugs (`*-demo`) bzw.
  external_ids mit dem Präfix `https://demo.mandari.invalid/oparl/...`
  (kollidiert nie mit echten OParl-Quellen). Wiederholte Läufe
  aktualisieren per `update_or_create` statt zu duplizieren.
- Die Demo-`OParlSource` ist **inaktiv**, damit der Sync-Daemon sie nie abruft.
- `--reset` löscht ausschließlich die über diese Kennungen identifizierten
  Objekte (Session-Mandant, Work-Organisation, Parteigruppe, Demo-Nutzer,
  OParl-Quelle inkl. Kommune, generierte PDF-Dateien) – und zuvor eine aufgesetzte
  Präsentationsumgebung (`setup_demo_praesentation --reset`).

## Verifikation

Selbst-enthaltener Smoke-Test (frische SQLite-Instanz):

```bash
python scripts/smoke_demo_environment.py
```

Prüft: doppelter Lauf ohne Duplikate, Demo-Logins, Rendering der
Insight-Seiten der Musterstadt, Work-Dashboard inkl. Gast-Zugriff über die
Ordner-Freigabe, Session-Dashboard sowie vollständiges Aufräumen per `--reset`.
