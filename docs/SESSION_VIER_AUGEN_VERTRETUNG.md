# Vier-Augen-Prinzip und Vertretungen (Session)

Stand: 09/2026 · Issue #222 (Teil von #217; Mitzeichnungsstationen: #151)

Freigaben und Genehmigungen lassen sich organisatorisch trennen: Wer einen Vorgang erstellt oder
zuletzt inhaltlich bearbeitet hat, gibt ihn nicht selbst frei. Für Abwesenheiten tragen
Administratoren Vertretungen ein, die nur im Zeitraum wirken und nie mehr erlauben als die
vertretene Person darf.

## Vier-Augen-Prinzip

Einstellungen → **Vier-Augen-Prinzip & Vertretungen** (Recht `manage_settings`), je Mandant und
Vorgangsart:

| Vorgangsart | „Selbst“ (darf nicht freigeben) | Standard |
|---|---|---|
| Vorlagenfreigabe | Ersteller:in und letzte:r inhaltliche:r Bearbeiter:in (Betreff, Art, Sachverhalt, Beschlussvorschlag, vertraulicher Teil, finanzielle Auswirkungen, Anlagen) | aus; wahlweise „nur bei finanziellen Auswirkungen“ oder „immer“ |
| Genehmigung der Niederschrift | Ersteller:in und letzte:r inhaltliche:r Bearbeiter:in (allgemeiner Teil, Unterschriften, Protokoll der TOPs, Beschlussergebnisse) | aus |
| Sitzungsgeld und Pauschalen | wer den Abrechnungs- bzw. Monatslauf erzeugt hat | **an** |
| Übergabe von Beschlussauszügen | Ersteller:in und letzte:r inhaltliche:r Bearbeiter:in der Niederschrift der Sitzung | aus |

Die Migration `session.0029_vier_augen_vertretung` setzt diese Standardwerte auch für bestehende
Mandanten; damit ändert sich nichts an bisherigen Abläufen (Sitzungsgeld galt schon im
Vier-Augen-Prinzip).

**Warum Ersteller:in und letzte:r Bearbeiter:in:** Wer erstellt, trägt die fachliche
Verantwortung; wer zuletzt inhaltlich geändert hat, hat genau den Stand geschaffen, der zur
Freigabe steht. Frühere Zwischenbearbeitungen sperren bewusst nicht dauerhaft – ihr Ergebnis
hat danach eine andere Person weiterbearbeitet, und sonst schlösse schon eine
Tippfehler-Korrektur die Freigabe durch diese Person für immer aus. Wer in der Prüfung selbst
ändert, gibt danach nicht mehr frei; der vorgesehene Weg ist die Zurückweisung mit Anmerkung.

Umsetzung:

- Prüfung auf Service-Ebene: `apps/session/services/four_eyes_service.py` (`evaluate`,
  `authorize`). Die Oberfläche zeigt statt des Freigabeknopfs einen Hinweis, der Server lehnt
  aber auch direkte Anfragen ab.
- Letzte inhaltliche Bearbeitung: `SessionPaper.content_edited_by`,
  `SessionProtocol.content_edited_by`, gesetzt über Signal-Hooks (`track_*`, angebunden in
  `apps/session/signals.py`) aus dem Altzustand des Audit-Logs. Speichern ohne inhaltliche
  Änderung zählt nicht. Bestehende Vorlagen haben das Feld zunächst leer; es füllt sich mit der
  nächsten Bearbeitung.
- Sitzungsgeld: `allowance_service.approve_allowances(..., four_eyes=...)` bzw.
  `approve_monthly_allowances`, Schalter aus `four_eyes_service.required`.

## Vertretungen

Benutzer → **Vertretungen** (Recht `manage_users`): vertretene Person, Vertretung (anderer
aktiver Nutzer desselben Mandanten), Zeitraum (erster bis einschließlich letzter Tag, höchstens
ein Jahr) und Umfang:

| Umfang | Wirkung |
|---|---|
| Freigaben und Genehmigungen | Vorlagen freigeben/zurückweisen, Niederschriften genehmigen/zurückweisen/veröffentlichen – soweit die vertretene Person es selbst darf (`approve_papers`, `approve_protocols`) |
| Arbeitsvorrat | Offene Mitzeichnungen der Ämter der vertretenen Person erscheinen unter „Mitzeichnungen“ und lassen sich in Vertretung erledigen |
| Benachrichtigungen | Freigabe-Aufforderungen und die Zurückweisung eigener Vorlagen gehen in Kopie an die Vertretung |

Regeln gegen Rechteausweitung (Tests: `apps/session/tests/test_vier_augen_vertretung.py`):

- Wirksam nur im Zeitraum; aufgehobene Vertretungen wirken sofort nicht mehr und bleiben als
  Nachweis in der Übersicht.
- Nur im eigenen Mandanten – auch ein unmittelbar in der Datenbank angelegter Datensatz mit
  fremder Person bleibt wirkungslos.
- Nie mehr als die vertretene Person: gerechnet aus deren eigenen Rollen
  (`permissions.role_permissions`). Sichtrechte werden nicht übertragen – eine nichtöffentliche
  Vorlage gibt in Vertretung nur frei, wer sie selbst sehen darf und wessen vertretene Person
  sie sehen darf. Bankdaten (`manage_allowances`) und Sitzungsbearbeitung (`edit_meetings`)
  sind nicht übertragbar.
- Keine Kettenvertretung: Rechte aus einer Vertretung werden nicht weitergereicht. Beim
  Eintragen weist die Oberfläche darauf hin, wenn die Vertretung im Zeitraum selbst abwesend ist.
- Das Vier-Augen-Prinzip gilt für Handelnde **und** vertretene Person.
- Wer ein Recht selbst hat, handelt im eigenen Namen.

Nachweis: `SessionAuditLog.on_behalf_of` („in Vertretung für …“ in der Audit-Log-Ansicht) für
jede Handlung aus einer Vertretung, dazu `approved_on_behalf_of` an Vorlage und Niederschrift.
Die Vertretung selbst (Eintragen, Aufheben) steht ebenfalls im Audit-Log.

Umsetzung: `apps/session/services/delegation_service.py`, Rechteprüfung in
`SessionPermissionChecker` (eine Abfrage je Anfrage, nur für Nutzer ohne die Freigaberechte),
Oberfläche `apps/session/views/approvals.py`.

## Abgrenzung

- Mitzeichnungsstationen (parallel, Fristen, Eskalation, automatische Weiterleitung bei
  Abwesenheit) behandelt #151; es kann auf dem Vertretungsmodell aufbauen.
- Fristen-Erinnerungen gehen weiterhin an alle mit dem jeweiligen Recht, nicht zusätzlich an
  Vertretungen.
