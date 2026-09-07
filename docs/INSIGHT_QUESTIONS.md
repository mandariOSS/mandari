# Ratsfragen (Insight) und Personenfotos — Betrieb

## Ratsfragen (Abgeordnetenwatch-Stil)

Bürger:innen stellen Mandatsträger:innen öffentliche Fragen; Fragen und Antworten sind
für alle sichtbar. Eigener Reiter **Ratsfragen** im Insight-Portal (`/insight/fragen/`).

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
| `INSIGHT_MODERATION_EMAILS` | Kommagetrennte Empfänger der Moderations-Hinweise; leer → alle aktiven Superuser mit E-Mail |
| `SITE_URL` | Basis für alle Links in E-Mails |

**Cron** (täglich, erinnert Ratsmitglieder 14 Tage nach Veröffentlichung, danach alle 14 Tage):

```cron
30 7 * * * docker exec mandari python manage.py send_question_reminders >> /var/log/mandari-question-reminders.log 2>&1
```

## Personenfotos

Fotos werden aus dem Ratsinformationssystem geladen und **lokal zwischengespeichert**
(`OParlPerson.photo`, max. 400 px JPEG). Vorteile: keine kaputten Hotlinks bei RIS-Umzügen oder
Bot-Schutz, keine 404-Requests im Browser, klarer Initialen-Fallback bei „kein Foto“.

**Konfiguration je Kommune** (Admin → Kommunen → „Personenfotos“): URL-Template mit `{id}` und
Regex mit einer Capture-Group auf die `external_id`. Für bekannte RIS greifen Presets automatisch
(`insight_core/services/person_photos.py`, z. B. SessionNet Stadt Münster:
`https://www.stadt-muenster.de/sessionnet/sessionnetbi/im/pe{id}.jpg` + `/people/(\d+)$`).

Manuell im Admin hochgeladene Fotos (`photo_status = manual`) werden nie überschrieben.

**Cron** (wöchentlich; prüft nur ungeprüfte oder >30 Tage alte Einträge):

```cron
0 3 * * 1 docker exec mandari python manage.py fetch_person_photos >> /var/log/mandari-person-photos.log 2>&1
```

Einmalig alles neu laden: `python manage.py fetch_person_photos --body muenster --force`
