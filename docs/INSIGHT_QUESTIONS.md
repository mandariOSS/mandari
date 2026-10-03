# Ratsfragen (Insight) und Personenfotos — Betrieb

## Ratsfragen (Abgeordnetenwatch-Stil)

Bürger:innen stellen Mandatsträger:innen öffentliche Fragen; Fragen und Antworten sind
für alle sichtbar. Eigener Reiter **Ratsfragen** im Insight-Portal (`/insight/fragen/`).

**Pausiert (Standard).** Bis zum Neuaufbau sind die Ratsfragen über `INSIGHT_QUESTIONS_ENABLED=false`
eingefroren (Issue #734):

- Portal, Detailseiten und der Reiter „Fragen“ der Personen zeigen die bisherigen Fragen und Antworten mit
  einem Hinweis. Antwortquoten und Antwortzeiten je Person und Fraktion sind ausgeblendet; offene Fragen
  tragen „Ohne Antwort“ statt „Offen seit … Tagen“.
- Stellen, Bestätigen, Antworten und die Bestätigungsseite antworten mit 404.
- Keine Mails (Bestätigung, Moderation, Ratsmitglied, Fragesteller:in), keine Erinnerungen;
  `send_question_reminders` endet mit Hinweis. Die Admin-Aktionen zum Freischalten und Erinnern ändern
  nichts; Ablehnen bleibt möglich.

Mit `INSIGHT_QUESTIONS_ENABLED=true` gilt der folgende Ablauf.

**Ablauf**

1. Frage stellen (`/insight/fragen/stellen/` → Person wählen → Formular mit Themenbereich)
2. E-Mail-Verifizierung durch die Fragesteller:in
3. Moderation im Django-Admin (`/admin/insight_core/publicquestion/`) — Moderator:innen
   erhalten bei jeder verifizierten Frage und jeder eingereichten Antwort eine Hinweis-Mail
4. Freischaltung: Ratsmitglied erhält Antwort-Link (Token, kein Login), Fragesteller:in den
   öffentlichen Link
5. Antwort → Moderation → Veröffentlichung; Fragesteller:in wird informiert

**Wer darf gefragt werden?** `question_service.is_mandate_holder()`: aktive Mitgliedschaft in
einem Hauptorgan (Rat, Stadtrat, Kreistag, Regionalrat, …; Verwaltungs-/Protokollrollen
ausgenommen) **oder** in einer Fraktion. Damit funktioniert die Erkennung unabhängig von den
RIS-spezifischen Rollenbezeichnungen.

**Konfiguration**

| Variable | Bedeutung |
|----------|-----------|
| `INSIGHT_QUESTIONS_ENABLED` | `true` schaltet die Ratsfragen ein; Standard `false` (pausiert, siehe oben) |
| `INSIGHT_MODERATION_EMAILS` | Kommagetrennte Empfänger der Moderations-Hinweise; leer → alle aktiven Superuser mit E-Mail |
| `SITE_URL` | Basis für alle Links in E-Mails |

**Zeitplan im Worker** `befehl:send_question_reminders` (täglich 07:30, erinnert Ratsmitglieder
14 Tage nach Veröffentlichung, danach alle 14 Tage; `DEPLOYMENT.md`, „Geplante Aufgaben“).

## Personenfotos

Fotos werden aus dem Ratsinformationssystem geladen und **lokal zwischengespeichert**
(`OParlPerson.photo`, max. 400 px JPEG). Vorteile: keine kaputten Hotlinks bei RIS-Umzügen oder
Bot-Schutz, keine 404-Requests im Browser, klarer Initialen-Fallback bei „kein Foto“.

**Konfiguration je Kommune** (Admin → Kommunen → „Personenfotos“): URL-Template mit `{id}` und
Regex mit einer Capture-Group auf die `external_id`. Für bekannte RIS greifen Presets automatisch
(`insight_core/services/person_photos.py`, z. B. SessionNet Stadt Münster:
`https://www.stadt-muenster.de/sessionnet/sessionnetbi/im/pe{id}.jpg` + `/people/(\d+)$`).

Manuell im Admin hochgeladene Fotos (`photo_status = manual`) werden nie überschrieben.

**Zeitplan im Worker** `befehl:fetch_person_photos` (montags 03:00; prüft nur ungeprüfte oder
>30 Tage alte Einträge).

Einmalig alles neu laden: `python manage.py fetch_person_photos --body muenster --force`
