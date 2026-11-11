# Antragslauf: von der Idee bis zur Beschlusskontrolle

Fachliche Beschreibung des Kernprozesses kommunalpolitischer Arbeit und Abgleich mit dem,
was mandari heute abbildet.

Das zugehörige Modell liegt als **BPMN 2.0** in [`antragslauf.bpmn`](antragslauf.bpmn).
Die Datei lässt sich in jedem BPMN-Werkzeug öffnen (Camunda Modeler, [bpmn.io](https://demo.bpmn.io),
Signavio, Visual Paradigm) und enthält Diagram Interchange, wird also direkt gezeichnet.

**Leitfall ist die Gemeindeordnung Nordrhein-Westfalen (GO NRW).** Abweichungen anderer
Länder sind unten benannt. Verbindlich ist immer die örtliche Geschäftsordnung — sie
konkretisiert die Gemeindeordnung und weicht von Kommune zu Kommune ab.

---

## 1. Akteure und was sie tun müssen

### Fraktion

| Rolle | Aufgabe im Prozess | Rechtliche Grundlage |
|---|---|---|
| **Mitglied** | Antragsidee einbringen, Entwurf erstellen, fraktionsintern abstimmen | § 56 GO NRW (Fraktionen), Geschäftsordnung |
| **Fraktionsvorsitz / Geschäftsführung** | Fraktionsbeschluss herbeiführen, Koalitionsabstimmung führen, Antrag freigeben und einreichen | § 56 GO NRW |

Das Antragsrecht steht der Fraktion als Ganzes zu, nicht dem einzelnen Mitglied. Der
**Fraktionsbeschluss ist damit kein optionaler Zwischenschritt, sondern die Legitimation**
des Antrags. Ein Werkzeug, das den Antrag ohne diesen Schritt einreichbar macht, bildet
den Prozess verkürzt ab.

### Verwaltung

| Rolle | Aufgabe im Prozess | Rechtliche Grundlage |
|---|---|---|
| **Ratsbüro / Schriftführung** | Eingang erfassen, Aktenzeichen vergeben, Zulässigkeit prüfen, Tagesordnung aufstellen, Niederschrift führen | § 47, § 52 GO NRW |
| **Fachverwaltung** | Stellungnahme erstellen, fachliche und finanzielle Auswirkungen darstellen | Geschäftsordnung |
| **Bürgermeister:in** | Rat einberufen, laden, Tagesordnung bekanntgeben | § 47 GO NRW |

Die **Ladungsfrist beträgt mindestens sieben Tage**; in besonders dringenden Fällen darf
sie abgekürzt werden (§ 47 GO NRW). Ort, Zeit und Tagesordnung werden in der Einberufung
bekanntgegeben. Der Rat soll mindestens alle zwei Monate einberufen werden.

Die **Einstufung öffentlich / nicht-öffentlich** ist keine Ermessensfrage im Alltag,
sondern folgt § 48 GO NRW: **Öffentlichkeit ist der Regelfall**, der Ausschluss die
begründungsbedürftige Ausnahme. Im Modell ist dieser Schritt deshalb bewusst als
Geschäftsregel-Aufgabe (`businessRuleTask`) und nicht als freie Entscheidung dargestellt.

### Gremium (Ausschuss und Rat)

| Rolle | Aufgabe im Prozess |
|---|---|
| **Vorsitz** | Tagesordnungspunkt aufrufen, Beratung leiten, Abstimmung durchführen |
| **Mitglieder** | Beraten, Änderungsanträge stellen, abstimmen |

Ein Antrag endet **nicht zwingend** in einem Beschluss. Die praktisch häufigen Ausgänge
sind: Abstimmung, **Vertagung**, **Verweisung in einen Fachausschuss** und **Rücknahme
durch die Antragstellerin**. Alle vier sind im Modell abgebildet; wie mandari sie abbildet,
steht in Abschnitt 3.

Über den wesentlichen Verhandlungsverlauf ist eine **Niederschrift** zu führen (§ 52 GO NRW).

### Öffentlichkeit

Bürger:innen sind kein handelnder Akteur im Antragslauf, aber Adressat: Der öffentliche
Teil von Tagesordnung, Vorlagen, Beschlüssen und Niederschriften ist zugänglich zu machen.
In mandari ist das das Bürger-Portal und die OParl-Schnittstelle.

---

## 2. Der Prozess in Kurzform

1. **Idee → Entwurf → interne Abstimmung** (Fraktion, Schleife bis zur Einigung)
2. **Fraktionsbeschluss** — Legitimation des Antrags
3. Optional: **Koalitionsabstimmung**
4. **Freigabe und Einreichung** bei der Verwaltung
5. **Eingang, Aktenzeichen, Zulässigkeitsprüfung** — bei Mängeln Rückmeldung an die Fraktion
6. **Stellungnahme der Fachverwaltung**
7. **Einstufung öffentlich / nicht-öffentlich** (§ 48)
8. **Beratungsfolge festlegen** — welche Gremien in welcher Reihenfolge
9. **Tagesordnung aufstellen**, **Einberufung und Ladung** (§ 47, ≥ 7 Tage)
10. **Beratung im Gremium** → Abstimmung | Vertagung | Verweisung | Rücknahme
11. **Niederschrift** (§ 52), Freigabe, **Veröffentlichung des öffentlichen Teils**
12. **Beschlusskontrolle** — Umsetzung verfolgen

Schritt 8 ist der, der am häufigsten unterschätzt wird: Ein Antrag durchläuft
typischerweise **mehrere Gremien nacheinander** (Fachausschuss → Hauptausschuss → Rat).
Er ist also nicht „auf der Tagesordnung", sondern an einer bestimmten Station einer
Beratungsfolge.

---

## 3. Abgleich mit mandari

### Arbeitsbereich der Fraktion (Work)

Der Antragslauf ist als **Übergangsmatrix** implementiert (`apps/work/motions/models.py`,
`Motion.VALID_TRANSITIONS`): kein freies Statusfeld, sondern ein Zustandsmodell mit erlaubten
Übergängen und bewusst zugelassenen Rücksprüngen. Die geführte Reihenfolge (`PIPELINE_ORDER`):

```
draft → internal_review → external_review → approved → submitted → at_admin → on_agenda → completed
```

- `external_review` (Abstimmung mit Partnern, Schritt 3) ist optional.
- `EDITABLE_STATUSES` friert die Bearbeitung ab der Freigabe ein.
- Ab `on_agenda` stehen die Ausgänge der Beratung zur Wahl: **Beschlossen** (`adopted`),
  **Erledigt** (`completed`), **Abgelehnt** (`rejected`) und **Zurückgezogen** (`withdrawn`).
  Zurückziehen geht schon ab `submitted` und `at_admin`. Korrigiert die Verwaltung ein erfasstes
  Ergebnis, führt der Weg zurück auf `on_agenda`.

Digital eingereichte Anträge sind mit dem Vorgang der Verwaltung verknüpft
(`Motion.session_application`, Issues #40, #316). Work zeigt Eingangsnummer, Drucksachennummer,
die Stationen der Beratungsfolge mit Ergebnis (nicht-öffentliche nur als „nicht-öffentlich
beraten“) und den Beschluss; Statuswechsel der Verwaltung kommen als Rückmeldung zurück. Damit
bildet Work die Schritte 1, 3 und 4, die Beratungsfolge (Schritt 8) sowie Abstimmung und
Rücknahme aus Schritt 10 ab. Einzelheiten: `docs/WORK.md`, Abschnitt 2.

### Verwaltung (Session)

Die Verwaltungsseite kennt die **Beratungsfolge** (`SessionConsultation`, Migration
`0011_beratungsfolge`) sowie die Zustände **Vertagt** (`deferred`) und **Zurückgezogen**
(`withdrawn`). Die Ö/NÖ-Trennung zieht sich durch das ganze Rechtemodell
(`can_view_non_public_papers`, `can_view_non_public_meetings`).

---

## 4. Abweichungen anderer Länder

| Land | Abweichung gegenüber NRW |
|---|---|
| **Hessen (HGO)** | Andere Begriffe (Gemeindevertretung statt Rat, Vorsitz durch Gemeindevertretung selbst, nicht durch den Bürgermeister). Einberufung und Ladungsfrist in § 58 HGO. |
| **Niedersachsen (NKomVG)** | Hauptverwaltungsbeamtin bzw. -beamter (HVB) lädt und stellt die Tagesordnung im Benehmen mit dem Vorsitz auf; Hauptausschuss stets nichtöffentlich; Fristen fast nur in der Geschäftsordnung. Einzelheiten unten. |
| **Bayern (GO)** | Stärkere Stellung des ersten Bürgermeisters bei der Tagesordnung. |

### Niedersachsen im Einzelnen

Grundlage ist das NKomVG in der Fassung zum Sitzungsdatum (ab 07.05.2026 bzw. ab 01.11.2026, siehe
`docs/SESSION_SITZUNGSFORMAT_LANDESRECHT.md`). Gegenüber dem Ablauf oben ändert sich:

- **Antrag und Anfrage:** Jedes Mitglied der Vertretung kann Anträge stellen und von der bzw. dem HVB Auskunft
  verlangen; Akteneinsicht auf Verlangen eines Viertels der Mitglieder oder einer Fraktion bzw. Gruppe (§ 56,
  § 58 Abs. 4). Antrags- und Anfragefristen stehen nur in der Geschäftsordnung (Praxis: Anträge 10 bis 14 Tage,
  Anfragen 5 Tage vor der Sitzung) – in mandari im Ortsrecht der Körperschaft.
- **Tagesordnung:** Die bzw. der HVB stellt sie im Benehmen mit dem Vorsitz auf; der Vorsitz kann Punkte
  verlangen; in dringenden Fällen Erweiterung mit Zweidrittelmehrheit (§ 59 Abs. 3). In Ausschüssen lädt die
  bzw. der HVB im Einvernehmen, die Tagesordnung entsteht im Benehmen mit dem Ausschussvorsitz (§ 72).
- **Ladung:** Ladungsfrist, Eilfrist und Fristbeginn (Absendung oder Bereitstellung im Ratsinformationssystem)
  regelt die Geschäftsordnung; die erste Sitzung der Wahlperiode hat eine Woche Frist (§ 59).
- **Beratungsfolge:** Der Hauptausschuss (Verwaltungs-, Samtgemeinde-, Kreis- bzw. Regionsausschuss) bereitet
  Beschlüsse der Vertretung vor und tagt stets nichtöffentlich (§ 78 Abs. 2); alle Abgeordneten dürfen zuhören.
  Ortsräte sind zu allen wichtigen Fragen ihrer Ortschaft anzuhören (§ 94).
- **Beschluss:** Mehrheit der Ja- und Nein-Stimmen, Enthaltungen zählen nicht, Stimmengleichheit = abgelehnt
  (§ 66 Abs. 1) – die Abstimmungserfassung prüft das.
- **Samtgemeinde:** Die Verwaltung der Samtgemeinde führt in der Praxis auch den Sitzungsdienst der
  Mitgliedsgemeinden; Anträge, Ladung und Niederschrift tragen die Körperschaft, deren Gremium berät
  (Körperschaften im Mandanten).

Das Modell ist so geschnitten, dass **Fristen und Zuständigkeiten Parameter sind**, nicht
fest verdrahtete Schritte. Der Ablauf selbst — Idee, Legitimation, Einreichung, Prüfung,
Beratungsfolge, Beschluss, Niederschrift, Kontrolle — ist länderübergreifend gleich.

---

## 5. Pflege dieses Modells

Die Datei wird in einem BPMN-Werkzeug gepflegt (Camunda Modeler oder bpmn.io), nicht
von Hand im XML. Das Anfangslayout ist erzeugt; ab jetzt gilt das Werkzeug als Quelle.

Beim Ändern lohnt ein Blick auf vier Dinge, die beim ersten Entwurf schiefgingen:
Überlappen sich Elemente? Laufen Kanten durch fremde Elemente hindurch? Liegen
Beschriftungen auf Elementen? Sehen Alternativen wie eine Abfolge aus — der Fehler, der
Vertagung, Verweisung und Rücknahme zunächst als Kette erscheinen ließ, obwohl sie sich
gegenseitig ausschließen.

Quellen: [GO NRW auf recht.nrw.de](https://recht.nrw.de/lmi/owa/br_bes_text?anw_nr=2&gld_nr=2&ugl_nr=2023&bes_id=6784),
insbesondere § 47 (Einberufung), § 48 (Öffentlichkeit), § 52 (Niederschrift), § 56 (Fraktionen).
