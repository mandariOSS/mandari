# Öffentliches Beschluss-Tracking „Was wurde aus …?“ (Insight)

Issue #48. Bürgerinnen und Bürger sehen im Insight-Portal, was aus einem Beschluss geworden ist,
und können einzelne Beschlüsse abonnieren. Grundlage ist die Beschlusskontrolle im Session-RIS
(Issue #37, siehe `docs/SESSION_BESCHLUSSKONTROLLE.md`).

## Freigabe durch die Verwaltung (Opt-in)

1. **Mandant:** *Einstellungen → Bürgerportal → „Umsetzungsstand veröffentlichen“*
   (`SessionTenant.implementation_publish`, setzt `insight_publish` voraus). Audit-Eintrag.
2. **Je Beschluss:** Im Beschlussregister erhält das Tracking-Formular zwei zusätzliche Felder:
   *Öffentliche Statusmeldung* (`implementation_public_note`, Freitext ohne Interna) und
   *öffentlich zeigen* (`implementation_public`, Standard an). Der interne Erledigungsvermerk
   (`implementation_note`) wird **nie** veröffentlicht.

Sichtbar sind nur Beschlüsse, die alle Bedingungen erfüllen: Mandant aktiv und beide Schalter an,
Sitzung und TOP öffentlich, Ergebnis „angenommen“, nicht abgesetzt, Beschluss freigegeben
(`decision_tracking.is_publicly_visible`).

## Insight

- Liste `/insight/beschluesse/` (Navigation „Beschlüsse“): Kacheln je Status (Umsetzung steht an /
  in Umsetzung / umgesetzt / zurückgestellt), Filter Gremium, Jahr, „nur offene“, Volltext
  (Betreff, Beschlusstext, Nummer, öffentliche Statusmeldung). Ohne Freigabe der Verwaltung
  erscheint ein Hinweis.
- Detail `/insight/beschluesse/<id>/`: Status-Zeitleiste beschlossen → in Umsetzung → umgesetzt
  (bzw. zurückgestellt), Beschlusstext, öffentliche Statusmeldung mit Stand-Datum, zuständige
  Stelle, geplante Umsetzung (überschritten rot), Links zu Sitzung und Vorlage (Beratungsverlauf)
  im Insight-Portal, Abo-Formular. Die aktive Kommune folgt dem Beschluss (Deep-Links aus E-Mails).

## Abo und Benachrichtigung

- `DecisionSubscription` (E-Mail, Beschluss, Token, Double-Opt-In wie beim Insight-Digest).
  Bestätigung `/insight/beschluesse/abo/bestaetigen/<token>/`, Abmeldung `/insight/beschluesse/abo/abmelden/<token>/`.
- Signal auf `SessionAgendaItem` (`insight_core/signals.py`): ändert sich Umsetzungsstand **oder**
  öffentliche Statusmeldung eines sichtbaren Beschlusses, erhalten bestätigte Abonnent:innen eine
  E-Mail mit neuem Stand, Statusmeldung, Link zur Zeitleiste und Abmeldelink
  (`emails/decisions/status.html`). Nur interne Änderungen (Vermerk, Frist, Stelle) lösen keine Mail aus;
  ausgeblendete oder nicht mehr freigegebene Beschlüsse ebenfalls nicht.
- Absender: `INSIGHT_DIGEST_FROM_EMAIL` bzw. `DEFAULT_FROM_EMAIL`, Basis-URL `SITE_URL`.

## Dateien

- Service `insight_core/services/decision_tracking.py`, Views `insight_core/views/decisions.py`
- Templates `pages/decisions/{list,detail,subscription_status}.html`, `emails/decisions/{confirm,status}.html`
- Session: `views/settings.py` (`ImplementationPublishView`), `views/resolutions.py`, `resolutions/register.html`
- Migrationen `session/0024_beschluss_tracking_veroeffentlichung`, `insight_core/0026_decision_subscription`
- Test `scripts/smoke_insight_decisions.py`
