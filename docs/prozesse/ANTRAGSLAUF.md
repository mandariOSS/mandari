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
durch die Antragstellerin**. Alle vier sind im Modell abgebildet — das ist der Punkt, an
dem die Software heute am deutlichsten hinter dem Prozess zurückbleibt (siehe Abschnitt 3).

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

### Was gut abgebildet ist

Der Antragslauf im Arbeitsbereich ist als **ausformulierte Übergangsmatrix** implementiert
(`apps/work/motions/models.py`, `VALID_TRANSITIONS`) — kein freies Statusfeld, sondern ein
Zustandsmodell mit erlaubten Übergängen und bewusst zugelassenen Rücksprüngen:

```
draft → internal_review → external_review → approved → submitted → at_admin → on_agenda → completed
```

Dazu passend friert `EDITABLE_STATUSES` die Bearbeitung ab der Freigabe ein. Das ist eine
saubere Umsetzung der Schritte 1 bis 4.

Die Verwaltungsseite kennt **Beratungsfolge** (`SessionConsultation`, Migration
`0011_beratungsfolge`) sowie die Zustände **Vertagt** (`deferred`) und **Zurückgezogen**
(`withdrawn`). Die Ö/NÖ-Trennung zieht sich durch das ganze Rechtemodell
(`can_view_non_public_papers`, `can_view_non_public_meetings`).

### Wo der Prozess weiter ist als die Software

**a) Der Fraktionsseite fehlen Vertagung, Verweisung und Rücknahme.**
`on_agenda` erlaubt nur `completed`, `rejected` oder zurück zu `at_admin`. Wird ein Antrag
im Rat vertagt oder in einen Ausschuss verwiesen, gibt es dafür keinen Zustand. In der
Praxis bleibt er entweder auf `on_agenda` stehen oder wandert zurück nach `at_admin` —
in beiden Fällen geht die Information verloren, dass bereits beraten wurde. Ein
zurückgezogener Antrag müsste heute als `rejected` geführt werden, was politisch das
Gegenteil aussagt.

Bemerkenswert: Die Verwaltungsseite hat diese Zustände. Die beiden Hälften des Produkts
modellieren denselben Vorgang unterschiedlich tief.

**b) Keine Beratungsfolge im Arbeitsbereich.**
`on_agenda` ist ein einzelner Zustand. Eine Fraktion kann nicht erkennen, an welcher
Station der Beratungsfolge ihr Antrag steht — obwohl genau das die Frage ist, die vor
jeder Sitzung gestellt wird.

**c) Keine Verknüpfung zwischen Antrag und Verwaltungsvorgang.**
Ein `Motion` trägt kein Feld, das auf den Vorgang, das Aktenzeichen oder die Vorlage in
der Verwaltung verweist. Nach der Einreichung (`motions.submit_to_ris`) reißt die Spur ab;
der weitere Verlauf wird von Hand nachgehalten. Damit ist Schritt 12 — Beschlusskontrolle —
nur so gut wie die Disziplin der Person, die den Status pflegt.

**d) Der Fraktionsbeschluss ist kein eigener Schritt.**
`internal_review → approved` bildet die Abstimmung ab, nicht den Beschluss. Für die
Legitimation des Antrags (§ 56 GO NRW) wäre ein nachvollziehbarer Fraktionsbeschluss mit
Datum und Ergebnis der sauberere Anker — das Modul für Fraktionssitzungen bringt die
Bausteine dafür bereits mit.

### Bewertung

Keiner dieser Punkte ist ein Fehler. Es sind Stellen, an denen die Software einen
einfacheren Prozess annimmt, als die Praxis ihn hat. Am folgenreichsten ist **(c)**: Ohne
die Verknüpfung bleibt der Kreis zwischen Antrag und Beschlusskontrolle offen, und genau
dieser geschlossene Kreis ist das Versprechen des Produkts.

---

## 4. Abweichungen anderer Länder

| Land | Abweichung gegenüber NRW |
|---|---|
| **Hessen (HGO)** | Andere Begriffe (Gemeindevertretung statt Rat, Vorsitz durch Gemeindevertretung selbst, nicht durch den Bürgermeister). Einberufung und Ladungsfrist in § 58 HGO. Für Darmstadt maßgeblich. |
| **Niedersachsen (NKomVG)** | Ladungsfristen und Ausschussrechte abweichend geregelt. |
| **Bayern (GO)** | Stärkere Stellung des ersten Bürgermeisters bei der Tagesordnung. |

Das Modell ist so geschnitten, dass **Fristen und Zuständigkeiten Parameter sind**, nicht
fest verdrahtete Schritte. Der Ablauf selbst — Idee, Legitimation, Einreichung, Prüfung,
Beratungsfolge, Beschluss, Niederschrift, Kontrolle — ist länderübergreifend gleich.

---

## 5. Pflege dieses Modells

Die BPMN-Datei wird aus `scripts/` nicht generiert, sondern direkt gepflegt. Wer sie
ändert, sollte anschließend prüfen, dass Referenzen und Diagrammangaben zusammenpassen
(jeder Fluss verbindet Knoten desselben Prozesses, jeder Knoten hat eine Form).

Quellen: [GO NRW auf recht.nrw.de](https://recht.nrw.de/lmi/owa/br_bes_text?anw_nr=2&gld_nr=2&ugl_nr=2023&bes_id=6784),
insbesondere § 47 (Einberufung), § 48 (Öffentlichkeit), § 52 (Niederschrift), § 56 (Fraktionen).
