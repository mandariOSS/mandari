# Sitzungsformat und Landesrecht (präsent, hybrid, digital)

> **Keine Rechtsberatung.** Diese Übersicht fasst die öffentlich zugängliche Rechtslage zu hybriden und
> digitalen Gremiensitzungen in den 16 Ländern zusammen. **Stand der Recherche: 30.09.2026.** Maßgeblich
> sind der amtliche Gesetzestext, die Hauptsatzung und die Geschäftsordnung der Kommune. Am Wortlaut nachgeprüft
> am 02.10.2026 (Issue #754): Wahlen und geheime Abstimmungen in Brandenburg, Hessen, Niedersachsen,
> Rheinland-Pfalz, Saarland und Sachsen-Anhalt. „Ungeklärt“
> heißt: Die Recherche hat keine gesicherte Aussage ergeben – nicht, dass das Format unzulässig ist.

Issue: #138 (Grundlage für #139 Teilnahmeart, #140 Cockpit, #141 Selbst-Abstimmung, #142 Video-Adapter,
#143 Zertifizierung).

## Begriffe

| Format | Bedeutung in mandari |
|--------|----------------------|
| Präsenzsitzung | Alle Mitglieder im Sitzungsraum. Immer zulässig. |
| Hybride Sitzung | Sitzung im Sitzungsraum; einzelne Mitglieder per zeitgleicher Bild-Ton-Übertragung zugeschaltet. |
| Digitale Sitzung | Alle Mitglieder zugeschaltet (Videokonferenz); in fast allen Ländern nur in Notlagen. |

## Umsetzung in mandari Session

- **Landesprofil je Mandant** (`SessionStateProfile`, Einstellungen → Sitzungsformate): Regeln des Landes für
  Rat/Vertretung und Ausschüsse, Voraussetzung (Hauptsatzung, Geschäftsordnung oder Beschluss), Vorsitz,
  Wahlen und geheime Abstimmungen für Zugeschaltete, ausgenommene Ausschussarten, Öffentlichkeit, Normen,
  Quellen und Stand. Die Profile stehen in `mandari/apps/session/presets/landesprofile.json`.
- **Nachweis der örtlichen Rechtsgrundlage** am Mandanten: Art (Hauptsatzung, Geschäftsordnung, Beschluss),
  Datum und Fundstelle. Ohne vollständigen Nachweis sind hybride Sitzungen im Regelbetrieb gesperrt.
- **Gesetzliche Ausschussart** am Gremium (Hauptausschuss, Finanzausschuss, Rechnungsprüfungsausschuss oder
  „anderer Ausschuss“), weil einzelne Länder diese Ausschüsse ausnehmen (NRW: § 58a i. V. m. § 57 Abs. 2
  GO NRW). Leer heißt „nicht eingeordnet“. Die Einstellungsseite listet bei solchen Landesprofilen die
  aktiven Ausschüsse mit ihrer Einordnung und warnt, solange keinem Gremium eine der Arten zugeordnet ist.
- **Sitzungsformat je Sitzung** (`SessionMeeting.format`) mit Begründung (Notlage, Beschluss), verschlüsselt
  gespeichertem Zugangsweg für Zugeschaltete und Hinweis für die Öffentlichkeit (Übertragung, Anmeldung).

### Prüfregeln (`apps/session/services/meeting_format_service.py`)

1. Präsenzsitzungen sind immer zulässig.
2. Hybride und digitale Sitzungen setzen ein Landesprofil voraus.
3. Maßgeblich ist die Regel des Landesprofils für den Gremientyp (Rat bzw. Ausschüsse). Ausgenommene
   Ausschussarten fallen auf die Regel für ausgenommene Ausschüsse zurück (NRW: nur in Notlagen nach
   § 47a GO NRW). Bei gemeinsamen Sitzungen gilt die strengste Regel der beteiligten Gremien.
   Nimmt das Landesprofil Ausschussarten aus, ist ein nicht eingeordnetes Gremium, dessen Name oder Kurzname
   auf eine dieser Arten hindeutet (z. B. „Haupt- und Finanzausschuss“, „HFA“, „Rechnungsprüfungsausschuss“),
   bis zur Einordnung für hybride und digitale Sitzungen gesperrt; ein nicht eingeordneter Ausschuss ohne
   solchen Namen erzeugt beim Speichern eine Warnung.
4. „nicht vorgesehen“ verhindert das Format mit Begründung und Norm.
5. „nur in Notlagen“ und jede digitale Sitzung verlangen eine Begründung (Notlage, zugrunde liegender Beschluss).
   Wo das Land auch für die Notlage eine örtliche Regelung verlangt (`emergency_needs_local_basis`:
   Baden-Württemberg, Mecklenburg-Vorpommern, Schleswig-Holstein, Thüringen), zusätzlich deren Nachweis.
6. „zulässig (Regelbetrieb)“ verlangt den vollständigen Nachweis der örtlichen Rechtsgrundlage, ebenso
   „ungeklärt“. Wo das Land einen Beschluss des Gremiums verlangt (Hamburg, Berlin), ist die Begründung Pflicht.
7. Fraktionen und Verwaltungseinheiten unterliegen nicht den Sitzungsregeln der Kommunalverfassung.

8. Schließt die Hauptsatzung die Zuschaltung für ein Gremium aus (örtliche Regel am Gremium, z. B. § 64 Abs. 8
   NKomVG), ist die hybride Sitzung für dieses Gremium nicht zulässig. Videositzungen in einer Notlage (§ 182
   NKomVG) beruhen auf einer eigenen Grundlage; für sie gilt weiter die Regel des Landesprofils.
9. Beschränkt die Hauptsatzung die Zuschaltung auf öffentliche Sitzungen (Ortsrecht der Körperschaft, § 64
   Abs. 3 Satz 3 NKomVG), sind nichtöffentliche Sitzungen nur in Präsenz möglich.
10. Verlangt das Landesrecht für die Notlage einen Beschluss der Vertretung mit Ablauf (Niedersachsen: § 182
    Abs. 1 Satz 2 NKomVG, höchstens drei Monate), muss der Beschluss im Ortsrecht der Körperschaft stehen und
    den Sitzungstag abdecken.

Maßgeblich ist das Landesprofil in der **Fassung zum Sitzungsdatum** (nächster Abschnitt). Beim Bearbeiten einer
gespeicherten Sitzung prüft das Formular nur, wenn sich Format, Begründung, Öffentlichkeit, die beteiligten
Gremien oder der Sitzungstag ändern; so bleibt eine Sitzung auch nach einem Wechsel des Landesprofils absag- und
bearbeitbar; der Versand prüft das Format erneut (siehe unten).

## Landesrecht als Sitzungsrecht (Issue #757)

Das Landesprofil beschreibt nicht nur hybride und digitale Sitzungen, sondern das Sitzungsrecht des Landes:
Körperschaften und Gremienbezeichnungen, Öffentlichkeit, Einberufung und Tagesordnung, Beschlussfähigkeit,
Abstimmungen und Wahlen, Mitwirkungsverbot, Niederschrift, Antragsrecht, Eilentscheidung und Einspruch,
Wahlperiode, Ausschussbesetzung, Ämter, Aufnahmen und Übertragung. Bisher ist Niedersachsen erfasst; die übrigen
Länder folgen bei Bedarf.

### Fassungen mit Stichtag

- Kommunalverfassungen ändern sich zu Stichtagen. Je Land gibt es **Fassungen** (`SessionStateProfileVersion`) mit
  „gültig ab“. Maßgeblich ist die Fassung zum **Sitzungsdatum**: die jüngste mit Stichtag am oder vor dem Tag.
  Spätere Fassungen erben die Einträge früherer und überschreiben nur, was sich ändert; eine Fassung kann auch
  Felder des Landesprofils ab dem Stichtag ändern (`overrides`). Vor der ersten Fassung gilt das Landesprofil
  ohne Sitzungsrecht.
- Niedersachsen: Fassung ab **07.05.2026** (zuletzt geändert durch Art. 3 des Gesetzes vom 28.04.2026, Nds. GVBl.
  2026 Nr. 30) und ab **01.11.2026** (Art. 1 des Gesetzes vom 09.09.2026, Nds. GVBl. 2026 Nr. 77): Ausschusssitze
  nach Sainte-Laguë/Schepers statt d’Hondt, Abberufung von Ausschussvorsitzen ohne erneute Benennung,
  „Sitzungsleitung“ statt „Vorsitz“ im Sitzungsraum, Öffentlichkeit per Video laut Hauptsatzung (§ 64 Abs. 9
  neu), Einwohnerfragen nur von Anwesenden, bis zu fünf ehrenamtliche Stellvertretungen der bzw. des HVB,
  Jugendbeteiligungsgremium, einzelne Ämter erst ab 18. Eine Sitzung am 31.10.2026 folgt der ersten, eine am
  01.11.2026 der zweiten Fassung.
- **Jeder Eintrag nennt Norm, Quelle und Stand**; „ungeklärt“, wo die Quelle nichts sicher belegt. Werte mit
  fester Bedeutung (z. B. Stimmengleichheit, Verteilungsverfahren, Sitzungsleitung, Öffentlichkeit per Video)
  prüft der Code (`apps/session/services/state_law_service.py`, Katalog `LAW_FIELDS`), Erläuterungen zeigt die
  Rechtsübersicht unter Einstellungen → Sitzungsformate, wahlweise zu einem Stichtag (`?stichtag=JJJJ-MM-TT`).

### Ortsrecht je Körperschaft

Hauptsatzung und Geschäftsordnung gelten je Körperschaft (Einstellungen → Sitzungsformate → Ortsrecht,
`SessionBody.local_rules`); jede Änderung steht im Prüfprotokoll:

- **Hauptsatzung:** Zuschaltung je Ladung zulassen (Vermerk in der Ladung), Zuschaltung nur in öffentlichen
  Sitzungen, Nachweise (Datum, Fundstelle) für Bild- und Tonaufnahmen und für die Öffentlichkeit per Video,
  Notlagenbeschluss mit Datum, Ablauf und Fundstelle (Warnung zwei Wochen vor dem Ablauf).
- **Geschäftsordnung:** Datum und Fundstelle, Ladungsfrist mit Zählweise (Kalender- oder Arbeitstage) und
  Fristbeginn (Absendung oder Bereitstellung), Eilfrist mit Pflichthinweis, Antrags- und Anfragefristen,
  Unterzeichnende der Niederschrift, Abstimmungsarten, Öffentlichkeit der Ausschüsse, Zeitrahmen der
  Einwohnerfragestunde. Fristberechnung und Abläufe nutzen diese Werte schrittweise (Fristen, Ablauf-Regelwerk).

### Was mandari daraus prüft und anzeigt

- **Ergebnisregel:** Kennt das Landesprofil „Stimmengleichheit = abgelehnt“ (Niedersachsen: § 66 Abs. 1 NKomVG),
  lehnt die Abstimmungserfassung „angenommen“ mit höchstens so vielen Ja- wie Nein-Stimmen ab; Enthaltungen
  zählen nicht mit.
- **Sitzungsleitung:** Ab der Fassung mit „Sitzungsleitung“ weist die Anwesenheit auch auf eine zugeschaltete
  Stellvertretung hin, wenn der Vorsitz nicht im Raum ist und sie die Sitzung leitet.
- **Ladung:** Vermerk „Zuschaltung mit dieser Ladung zugelassen“ (§ 64 Abs. 3 Satz 2 NKomVG) und Pflichthinweis
  an Zugeschaltete, dass Dritte den nichtöffentlichen Teil nicht mitverfolgen (§ 64 Abs. 6 NKomVG).
- **Störungen:** Im Störungsprotokoll lässt sich der Verantwortungsbereich festhalten; liegt eine andauernde
  Störung im Verantwortungsbereich der Kommune, fordert das Sitzungscockpit zur Unterbrechung auf (§ 64 Abs. 5
  NKomVG). Sonstige Störungen sind für die Sitzung unbeachtlich; die Person zählt nur für ihre Dauer nicht mit.
- **Übertragung** (Adresse für die Öffentlichkeit an der Sitzung): Die Detailseite weist intern auf einen
  fehlenden Nachweis in der Hauptsatzung hin – bis 31.10.2026 für Bild- und Tonaufnahmen (§ 64 Abs. 2), ab
  01.11.2026 für die Öffentlichkeit per Video (§ 64 Abs. 9) – und nennt Mitglieder, die Aufnahmen ihrer Person
  widersprochen haben (Kennzeichen an der Person). Livestream und Aufzeichnung selbst: #146.
- **Notlage:** Digitale Sitzungen nach § 182 NKomVG sind „nur in Notlagen“ zulässig und brauchen den Beschluss
  im Ortsrecht (siehe Prüfregeln).

### Gremientypen, Funktionen und Vorlagen

- **Gremientypen:** Ortsrat bzw. Stadtbezirksrat, Gruppe (wie eine Fraktion, ohne Sitzungsregeln) und
  Jugendbeteiligungsgremium; gesetzliche Ausschussart „Ausschuss nach besonderen Rechtsvorschriften“ (§ 73 NKomVG).
  Der Hauptausschuss ist ein Ausschuss mit der Ausschussart „Hauptausschuss“; seine gesetzliche Bezeichnung nach
  dem Körperschaftstyp (Verwaltungs-, Samtgemeinde-, Kreis-, Regionsausschuss) zeigt die Gremienseite.
- **Funktionen:** Hauptverwaltungsbeamtin bzw. -beamter kraft Amtes, ehrenamtliche Stellvertretung des HVB,
  Grundmandat (beratend) und Hinzugewählte (immer ohne Stimmrecht), Ortsvorsteherin bzw. Ortsvorsteher,
  Gemeindedirektorin bzw. Gemeindedirektor.
- **Ämter erst ab 18** (Fassung ab 01.11.2026): Ist an der Person „volljährig ab“ eingetragen, lehnt die Besetzung
  Vorsitz und Stellvertretung im Ortsrat, Ortsvorsteher, Ratsvorsitz einer Mitgliedsgemeinde, Gemeindedirektor und
  HVB vor diesem Tag ab.
- **Abberufung:** Das Ende einer Besetzung als Ausschussvorsitz lässt sich als „abberufen“ vermerken; ab der
  Fassung vom 01.11.2026 lehnt mandari dann eine erneute Benennung derselben Person in diesem Ausschuss und dieser
  Wahlperiode ab (§ 71 Abs. 8 NKomVG).
- **Vorlagen je Körperschaftstyp** und **konstituierende Sitzung:** siehe `docs/SESSION_MANDANT_ANLEGEN.md`.
- **TOP-Art Einwohnerfragestunde:** nur im öffentlichen Teil; Zeitrahmen laut Geschäftsordnung (Ortsrecht).

### Ladung, Tagesordnung und Öffentlichkeit

- **Ladung und Ladungs-PDF** (auch Nachtrag, Sitzungsmappe, Abruf in mandari Work) nennen bei hybriden und
  digitalen Sitzungen Format, Rechtsgrundlage (Norm i. V. m. örtlichem Nachweis bzw. Norm der Notlage) und
  Begründung. Die öffentliche Fassung und der Kalendereintrag (ICS) nennen nur das Format.
- **Zugangsweg für Zugeschaltete** (verschlüsselt gespeichert):
  - Ladungsmail und Ladungs-PDF an die Empfänger der vollständigen Ladung (Gremienmitglieder mit
    nichtöffentlichem Teil), auch beim Abruf der eigenen Ladung in mandari Work.
  - Gäste erhalten die öffentliche Fassung und damit keinen Zugangsweg. Das ist bewusst so: Die
    Zuschaltung nach Landesrecht gilt für Mitglieder; ob ein Gast zugeschaltet wird, entscheidet die
    Sitzungsleitung im Einzelfall und teilt den Zugang dann selbst mit.
  - Im Sitzungsdienst nur mit dem Recht „Sitzungen bearbeiten“ – auf der Detailseite wie im PDF-Abruf;
    der Abruf einer PDF mit Zugangsweg wird im Audit-Log protokolliert.
  - Nie in der Sitzungsmappe (gespeicherte Datei für alle Berechtigten), in öffentlichen Dokumenten, in der
    OParl-API oder im Kalendereintrag.
- **Versand gesperrt**, solange das gespeicherte Format nach dem aktuellen Landesprofil nicht zulässig ist
  (z. B. nach Wechsel des Landesprofils); die Detailseite der Sitzung zeigt den Grund.
- **Öffentlichkeit:** Hinweis auf Übertragung bzw. geschützten Zugang mit Anmeldefrist (NRW: § 3 DigiSiVO)
  in Ladungs-PDF, OParl-API (`mandari:meetingFormat`, `mandari:publicAccess`, siehe `SESSION_OPARL_API.md`)
  und auf der Sitzungsseite im Bürgerportal.

Wahlen, geheime Abstimmungen, konstituierende Sitzungen und Satzungsbeschlüsse betreffen einzelne
Tagesordnungspunkte bzw. Teilnehmende. Das Landesprofil hält sie fest; durchgesetzt werden Wahlen und
geheime Abstimmungen mit der Teilnahmeart (#139, nächster Abschnitt), die Stimmabgabe am eigenen Gerät
folgt mit der Selbst-Abstimmung (#141).

### Teilnahmeart in der Anwesenheit (Issue #139)

Zugeschaltete gelten als anwesend, solange sie per Bild und Ton teilnehmen. Die Anwesenheitsliste der
Sitzung (`SessionAttendance`, `apps/session/services/participation_service.py`) hält dafür fest:

- **Teilnahmeart** „vor Ort“ oder „zugeschaltet“ (`participation_mode`). Zugeschaltet werden kann nur in
  hybriden und digitalen Sitzungen; in digitalen Sitzungen ist es die Vorgabe beim Erzeugen der Liste. Bei
  Zugeschalteten sind Ankunft und Abgang die Zeiten der **Zuschaltung und Trennung**. Steht in einer
  Präsenzsitzung jemand als zugeschaltet (etwa nach Änderung des Formats), weist die Sitzungsseite darauf hin
  und zeigt die Spalte „Teilnahme“, damit sich die Angabe in der Zeile korrigieren lässt. Bis dahin zählt die
  Person zur Beschlussfähigkeit – bewusst, damit sie nicht stillschweigend kippt.
- **Störungen** (`SessionAttendanceDisruption`) mit Beginn, Ende und Ursache (Verbindung abgebrochen, Ton
  gestört, Bild gestört, sonstige) und einem internen Vermerk. In der Sitzung genügt ein Klick in der Zeile
  („Störung ab jetzt“) bzw. „Jetzt beenden“; Zeiten lassen sich nachtragen und korrigieren. Ein Ende vor
  dem Beginn gilt als Störung über Mitternacht und ist nur in einer Sitzung möglich, die bis in den Folgetag
  reicht (tatsächliches bzw. geplantes Sitzungsende; „Jetzt beenden“ nach Mitternacht geht immer). Sonst und
  bei einer nicht lesbaren Uhrzeit bleibt der bisherige Stand mit einer Fehlermeldung unverändert.
- **Beschlussfähigkeit:** Zugeschaltete zählen wie Anwesende im Raum, während einer andauernden Störung aber
  nicht („nicht erreichbar“); nach dem Ende zählen sie wieder. Schließt das Landesprofil nur die
  Zugeschalteten von einer Abstimmung aus („für Zugeschaltete ausgeschlossen“, z. B. Bayern: „anwesend und
  stimmberechtigt“, Art. 47 Abs. 2 GO), zählen sie für diesen TOP nicht. Ist der Vorgang in der ganzen Sitzung
  unzulässig („in der Sitzung unzulässig“, z. B. Niedersachsen), zählen sie weiter mit – sie gelten als
  anwesend (§ 64 Abs. 3 Satz 5 NKomVG). Die Anzeige nennt, wie viele zugeschaltet sind und wer wegen einer
  Störung bzw. des Landesprofils nicht mitgezählt wird. Gäste und Protokollführung zählen nie – auch nicht mit
  gesetztem Stimmrecht (wie bei der Stimmabgabe).
- **Wahlen, geheime Abstimmungen, geheimhaltungspflichtige Angelegenheiten** (Issue #754): Die
  Abstimmungserfassung kennt die Angabe „Wahl“ je TOP, die TOP-Bearbeitung das Merkmal
  „geheimhaltungspflichtig“ (Geheimhaltung gesetzlich vorgeschrieben oder behördlich angeordnet, z. B. § 6
  Abs. 3 Satz 1 NKomVG – nicht dasselbe wie nichtöffentlich, aber nur im nichtöffentlichen Teil möglich; ein
  TOP mit geheimhaltungspflichtigem Unterpunkt bleibt ebenfalls nichtöffentlich). Das Merkmal lässt sich beim
  Anlegen und Bearbeiten eines TOP setzen. Das Landesprofil regelt je Vorgang
  (`remote_elections`, `remote_secret_votes`, `remote_secrecy_matters`):
  - **für Zugeschaltete ausgeschlossen** (z. B. Bayern, Hessen, Brandenburg bei geheimen Wahlen):
    Zugeschaltete stimmen bei dieser Abstimmung nicht ab und zählen für diesen TOP nicht zur
    Beschlussfähigkeit, die Summen dürfen die Zahl der Stimmberechtigten im Raum nicht übersteigen;
  - **in der Sitzung unzulässig, sobald jemand zugeschaltet ist** (Niedersachsen, Sachsen-Anhalt, Saarland):
    Nimmt ein Mitglied des Gremiums (Mitglied, Vorsitz, stellvertretender Vorsitz) zugeschaltet teil oder hat
    teilgenommen (auch vorzeitig getrennt), lassen sich geheime Wahlen bzw. Abstimmungen weder in der
    Abstimmungserfassung noch im Sitzungscockpit erfassen und geheimhaltungspflichtige TOPs nicht aufrufen.
    Zugeschaltete Gäste, Sachverständige und Protokollführung sperren nicht – etwa eine Anhörung per Video
    (§ 64 Abs. 7 NKomVG); das Gesetz spricht von Abgeordneten (NI) bzw. Mitgliedern (ST). Die Oberfläche
    nennt Grund und Norm und bietet „TOP vertagen“ an (nur ohne festgestelltes Ergebnis) – die Zugeschalteten
    abzuschalten genügt nicht. Die Sperre gilt ab der ersten Zuschaltung bis zum Sitzungsende; maßgeblich ist,
    ob ein Mitglied zugeschaltet teilnimmt, nicht das Format: weder „hybrid“ allein noch das nachträgliche
    Umstellen auf „Präsenz“ – erfasste Zuschaltungen sperren weiter, bis die Teilnahmeart berichtigt ist. Eine
    Abstimmung, die laut Sitzungscockpit vor der ersten Zuschaltung abgeschlossen war, bleibt erfassbar; ohne
    Zuschaltzeit gilt die Sperre ab Sitzungsbeginn. Wird während einer offenen geheimen Abstimmung jemand
    zugeschaltet, lässt sie sich nicht mehr schließen, nur abbrechen. Dasselbe gilt in digitalen Sitzungen
    (Niedersachsen: § 182 Abs. 2 Satz 6). Präsenzsitzungen ohne Zuschaltung bleiben unverändert;
  - „nur unter Bedingungen“ und „ungeklärt“ ergeben einen Hinweis.

  Gilt die Regel für Wahlen nur für geheime Wahlen (`remote_elections_scope`, z. B. Niedersachsen: § 67
  Satz 2 NKomVG), erfassen offene Wahlen die Stimmen der Zugeschalteten. Während einer Störung ist keine
  Stimmabgabe möglich. Mit der Genehmigung der Niederschrift sind die Angaben „Wahl“ und
  „geheimhaltungspflichtig“ gesperrt wie Ergebnis und Abstimmungsart (`protocol_lock`); eine Berichtigung
  wertet Stimmen nach der gespeicherten Angabe. Die geheime Abstimmung Zugeschalteter am eigenen Gerät (#149)
  bleibt für Niedersachsen ausgeschaltet.
- **Sitzungsleitung:** Verlangt das Landesprofil die Sitzungsleitung im Sitzungsraum (`chair_present`),
  weist die Anwesenheit in hybriden Sitzungen auf einen zugeschalteten Vorsitz hin (ohne zu sperren – die
  Leitung kann an die Stellvertretung übergehen).
- **Niederschrift, Teilnehmerverzeichnis, Protokoll-PDF** (interne und öffentliche Fassung) nennen in
  hybriden und digitalen Sitzungen die Teilnahmeart je Person, bei Zugeschalteten mit Zeiten und Störungen,
  z. B. „zugeschaltet 18:03–19:10 Uhr; Störung 18:40–18:44 Uhr (Verbindung abgebrochen)“. Der interne
  Vermerk einer Störung erscheint dort nie.
- **Datenschutz:** Die Auskunft an eine Person enthält Teilnahmeart und Störungen; die Anonymisierung leert
  die Vermerke, Zeiten und Ursache bleiben als Nachweis der Beschlussfassung.

### Pflege

Ändert sich die Rechtslage: `landesprofile.json` anpassen (`as_of` des Landes bzw. `stand` der Datei, Quellen;
eine neue Rechtslage zu einem Stichtag als neue Fassung unter `versions`), diese Übersicht nachziehen und nach
dem Deploy `python manage.py session_state_profiles --sync` ausführen. `--check` meldet Abweichungen zwischen
Datenbank und Datei, auch bei den Fassungen. Die Datenmigration `session.0045` übernimmt die Profile beim ersten
Deploy automatisch, `session.0055` die Hybridregel aus Issue #754, `session.0063` die Fassungen aus Issue #757
(alle idempotent).

## Übersicht

Spalten: **Hybrid Rat / Ausschüsse** und **Digital** (Rat): R = zulässig im Regelbetrieb, N = nur in
Notlagen, – = nicht vorgesehen, ? = ungeklärt. **Vorsitz**: P = muss im Sitzungsraum anwesend sein.
**Wahlen / geheim**: Teilnahme Zugeschalteter an Wahlen bzw. geheimen Abstimmungen (✗ = für Zugeschaltete
ausgeschlossen, S = in der ganzen Sitzung unzulässig, sobald jemand zugeschaltet ist, B = nur unter Bedingungen,
? = ungeklärt; „g“ = Regel nur für geheime Wahlen). **Prüftiefe**: W = Wortlaut eingesehen, T = teilweise,
S = nur Sekundärquellen.

| Land | Kernnorm | Hybrid Rat | Hybrid Ausschüsse | Digital | Voraussetzung | Vorsitz | Wahlen / geheim | Prüftiefe |
|------|----------|:---:|:---:|:---:|---|:---:|:---:|:---:|
| Baden-Württemberg | § 37a GemO | R | R | N | Hauptsatzung | P | ✗ / ? | W |
| Bayern | Art. 47a GO | R | ? | – | Geschäftsordnung | ? | ✗ / ? | W |
| Berlin | § 8a BezVG | ? | ? | N | Beschluss (BVV-Vorstand) | ? | B / B | S |
| Brandenburg | §§ 34, 43 BbgKVerf | R | R | N | Geschäftsordnung | P | ✗g / ? | T |
| Bremen (Bremerhaven) | § 38 Verfassung Bremerhaven | ? | ? | ? (Ausschüsse N) | ungeklärt | ? | ? / ? | S |
| Hamburg | § 13 BezVG | N | R | N (Ausschüsse R) | Beschluss | ? | ✗ / ? | S |
| Hessen | § 52a HGO | R | ? | – | Hauptsatzung | P | ✗ / ? | W |
| Mecklenburg-Vorpommern | § 29a KV M-V | R | R | N | Hauptsatzung | P | ? / ✗ | T |
| Niedersachsen | § 64 NKomVG | R | R | N | Hauptsatzung | P | Sg / S | T |
| Nordrhein-Westfalen | §§ 47a, 58a GO NRW | N | R (außer HA, FA, RPA) | N | Hauptsatzung | P | B / B | W |
| Rheinland-Pfalz | § 35a GemO | R | ? | N | Geschäftsordnung | P | ✗ / ✗ | T |
| Saarland | §§ 48, 51a KSVG | N | R | N | Geschäftsordnung | ? | S / S | T |
| Sachsen | § 36a SächsGemO | N | ? | N | ungeklärt | ? | ? / ? | T |
| Sachsen-Anhalt | §§ 56a, 56b KVG LSA | R | R | N | Hauptsatzung | P | Sg / ? | T |
| Schleswig-Holstein | §§ 34a, 35a GO | R | R | N | Hauptsatzung | P | B / ? | T |
| Thüringen | § 36a ThürKO | N | ? | N | Hauptsatzung | ? | ? / ? | T |

## Länder im Einzelnen

### Baden-Württemberg

- **Norm:** § 37a GemO in der Neufassung durch das Gesetz zur Änderung kommunalrechtlicher Vorschriften vom
  22.07.2025, in Kraft seit 01.09.2025.
- **Regelbetrieb:** Die Hauptsatzung kann bestimmen, dass Mitglieder des Gemeinderats **mit Ausnahme des
  Vorsitzenden** mit ihrer Zustimmung per Ton- und Bildübertragung zugeschaltet werden (§ 37a Abs. 1).
  Zugeschaltete gelten als anwesend, sind aber **bei Wahlen nicht stimmberechtigt**.
- **Notlage:** Per Hauptsatzung kann auch der Vorsitzende zugeschaltet werden, wenn die Sitzung sonst aus
  schwerwiegenden Gründen nicht ordnungsgemäß durchgeführt werden könnte (§ 37a Abs. 4); öffentliche
  Sitzungen dann mit Übertragung in einen öffentlich zugänglichen Raum.
- **Ausschlüsse:** Erste Sitzung nach § 32 Abs. 1 Satz 2 GemO; weitere begründete Einzelfälle per
  Hauptsatzung (§ 37a Abs. 3). Geheime Abstimmungen: nicht ausdrücklich geregelt (ungeklärt).
- **Ausschüsse:** Verweise in §§ 39, 41 GemO; Reichweite im Einzelfall prüfen.
- **Quellen:** <https://www.landesrecht-bw.de/bsbw/document/jlr-GemOBWV27P37a>,
  <https://dejure.org/gesetze/GemO/37a.html> (abgerufen 30.09.2026).

### Bayern

- **Norm:** Art. 47a GO (Sitzungsteilnahme durch Ton-Bild-Übertragung).
- **Regelbetrieb:** Zuschaltung, soweit der Gemeinderat sie in der **Geschäftsordnung** zugelassen hat; die
  Gemeinde kann Zahl und Voraussetzungen begrenzen. Zugeschaltete gelten als anwesend.
- **Wahlen:** „Bei einer Zuschaltung mittels Ton-Bild-Übertragung ist eine Teilnahme an Wahlen nicht möglich.“
- **Ausschlüsse:** Keine Zuschaltung, soweit Sitzung oder Beratungsgegenstände nach Art. 56a GO geheim zu
  halten sind (Art. 47a Abs. 2).
- **Öffentlichkeit:** Zugeschaltete müssen für die im Saal anwesende Öffentlichkeit wahrnehmbar sein.
- **Ungeklärt:** Mehrheitserfordernis für den Geschäftsordnungsbeschluss (gesetze-bayern.de: Zweidrittelmehrheit;
  der Gesetzentwurf Drs. 19/7893 vom 14.08.2025 sah nach der Kommunalwahl 2026 die einfache Mehrheit vor);
  Anwendung auf Ausschüsse; ob der Vorsitz zugeschaltet sein darf. Rein digitale Sitzungen sieht die GO nicht vor.
- **Quellen:** <https://www.gesetze-bayern.de/Content/Document/BayGO-47a>,
  <https://www.bayern.landtag.de/www/ElanTextAblage_WP19/Drucksachen/Basisdrucksachen/0000006500/0000006614.pdf>.

### Berlin

- **Norm:** § 8a Bezirksverwaltungsgesetz (Videositzung der Bezirksverordnetenversammlung).
- **Notlage:** Videositzung zur Abwehr außergewöhnlicher Gefahren für Leben, Gesundheit oder Wohlergehen der
  Bezirksverordneten oder bei vergleichbar schwerwiegenden allgemeinen Notlagen; Entscheidung des BVV-Vorstands
  im Einvernehmen mit dem Ältestenrat (§ 8a Abs. 3). Geheime Abstimmungen im schriftlichen Verfahren.
- **Öffentlichkeit:** zeitgleiche Übertragung an einen öffentlich zugänglichen Ort, über das Internet oder den Rundfunk.
- **Ungeklärt:** Zuschaltung einzelner Bezirksverordneter im Regelbetrieb (Praxisberichte über hybride Sitzungen
  ohne Stimmrecht der Zugeschalteten), Ausschüsse.
- **Quelle (Sekundärquelle, Wortlaut vor Einsatz prüfen):**
  <https://www.berlin.de/ba-lichtenberg/politik-und-verwaltung/bezirksverordnetenversammlung/wissenswertes/bezirksverwaltungsgesetz-berlin.pdf>.

### Brandenburg

- **Norm:** § 34 Abs. 2 BbgKVerf (Videoteilnahme), § 43 BbgKVerf (Entscheidungsfähigkeit in außergewöhnlichen
  Notlagen), Fassung des Gesetzes zur Modernisierung des Kommunalrechts vom 05.03.2024.
- **Regelbetrieb:** Videoteilnahme auf **begründeten Antrag**; die Geschäftsordnung muss Gründe und Antragsverfahren
  regeln (§ 34 Abs. 2 Satz 4). Die Sitzungsleitung erfolgt immer in Präsenz; Hauptverwaltungsbeamte nur ausnahmsweise
  per Video (§ 34 Abs. 2 Satz 6).
- **Ausschlüsse:** keine Videoteilnahme an der konstituierenden Sitzung und an Tagesordnungspunkten, „in denen
  geheime Wahlen durchzuführen sind“ (§ 34 Abs. 2 Satz 2) – ausgeschlossen ist die Teilnahme der Zugeschalteten
  an diesem Punkt, nicht die Wahl in der Sitzung. Offene Wahlen und geheime Abstimmungen nennt die Norm nicht.
  Wortlaut geprüft am 02.10.2026 (Fassung zuletzt geändert durch Art. 3 des Gesetzes vom 18.12.2025).
- **Notlage:** Video- oder Audiositzung nach § 43 Abs. 2.
- **Ausschüsse:** entsprechend (§ 44 Abs. 9 Satz 1, Hauptausschuss § 50 Abs. 4).
- **Quellen:** Rundschreiben des MIK vom 13.06.2024
  <https://mik.brandenburg.de/sixcms/media.php/9/20240613_Rundschreiben_zum_Gesetz_zur_Modernisierung_des_Kommunalrechts_vom_5_Maerz_2024.pdf>,
  <https://bravors.brandenburg.de/gesetze/bbgkverf>.

### Bremen

- Bremen kennt keine Gemeindeordnung: Die Stadtbürgerschaft der Stadtgemeinde Bremen tagt nach dem Recht der
  Bürgerschaft, die Beiräte nach dem Ortsgesetz über Beiräte und Ortsämter (**nicht geprüft, ungeklärt**).
- **Bremerhaven:** § 38 Verfassung für die Stadt Bremerhaven erlaubt Ausschusssitzungen als Videokonferenz, soweit
  technisch möglich, wenn eine außergewöhnliche Notsituation die Präsenzsitzung erheblich erschwert, verhindert oder
  unzumutbar macht (Sekundärquelle, Wortlaut vor Einsatz prüfen).
- **Quellen:** <https://www.bremische-buergerschaft.de/fileadmin/user_upload/Informationsmaterial/Stadtverfassung_Bremerhaven_2018-11_web.pdf>,
  <https://www.transparenz.bremen.de/metainformationen/geschaeftsordnung-der-stadtverordnetenversammlung-der-stadt-bremerhaven-gostvv-175437?asl=bremen203_tpgesetz.c.55340.de&template=20_gp_ifg_meta_detail_d>.

### Hamburg

- **Norm:** § 13 Abs. 3 und 4 Bezirksverwaltungsgesetz, Fassung des Gesetzes vom 27.04.2022.
- **Notlage:** Ist die Durchführung an einem Ort aufgrund äußerer, nicht kontrollierbarer Umstände erheblich
  erschwert, können Bezirksversammlung und Ausschüsse per Telefon- oder Videokonferenz bzw. hybrid tagen (Abs. 3).
- **Regelbetrieb:** Ausschüsse können einzelne Sitzungen auch außerhalb von Notlagen digital durchführen (Abs. 4);
  Einzelheiten regelt die Geschäftsordnung der Bezirksversammlung.
- **Ausschlüsse:** Wahlen und die konstituierende Sitzung.
- **Öffentlichkeit:** Die Teilnahme der Öffentlichkeit an öffentlichen Sitzungen ist zu gewährleisten.
- **Quelle (Sekundärquelle):** Drucksache 22-3691 der Bezirksversammlung Hamburg-Mitte vom 20.04.2023
  <https://bv-hh.de/hamburg-mitte/documents/anpassung-der-geschaeftsordnung-der-bezirksversammlung-fuer-digitale-hybride-sitzungsmodelle-138479>.

### Hessen

- **Norm:** § 52a HGO (Digitale Sitzungsteilnahme), eingefügt durch das Gesetz zur Verbesserung der
  Funktionsfähigkeit der kommunalen Vertretungskörperschaften, GVBl. 2025 Nr. 24 vom 04.04.2025
  (in Kraft am Tag nach der Verkündung).
- **Regelbetrieb:** Zuschaltung, soweit die **Hauptsatzung** es bestimmt; nicht für den Vorsitzenden der
  Gemeindevertretung (§ 52a Abs. 1).
- **Ausschlüsse:** Teilnahme per Bild-Ton-Übertragung bei Wahlen nach § 55 (alle Wahlen, auch offene),
  Beschlüssen nach § 39a Abs. 3 Satz 2, § 57 Abs. 2, § 76 Abs. 1 und Abs. 4 Satz 3, § 76a sowie in der ersten
  Sitzung der Gemeindevertretung; weitere Fälle per Hauptsatzung (§ 52a Abs. 2). Ausgeschlossen ist die
  Teilnahme der Zugeschalteten, nicht der Vorgang; sie gelten als anwesend im Sinne von § 53 Abs. 1 Satz 1.
  Geheime Abstimmungen nennt § 52a nicht (Wortlaut geprüft am 02.10.2026).
- **Öffentlichkeit:** Zugeschaltete müssen für die Saalöffentlichkeit in Bild und Ton wahrnehmbar sein
  (§ 52a Abs. 3); Echtzeitübertragung im Internet per Hauptsatzung zulässig (§ 52 Abs. 3).
- **Weiteres:** Gemeindevorstand per Geschäftsordnung (§ 67); Ausländerbeirat und Integrations-Kommission
  entsprechend (§ 52a Abs. 5). Ausschüsse der Gemeindevertretung: ungeklärt. Rein digitale Sitzungen der
  Gemeindevertretung sieht die HGO nicht vor.
- **Quelle:** <https://starweb.hessen.de/cache/GVBL/2025/00024.pdf>.

### Mecklenburg-Vorpommern

- **Norm:** § 29a KV M-V, eingefügt durch das Gesetz zur Modernisierung des Kommunalverfassungsrechts vom
  14.05.2024 (GVOBl. M-V 2024 S. 154).
- **Regelbetrieb:** Sitzungen finden grundsätzlich in Präsenz statt; Mitglieder können per Bild- und
  Tonübertragung teilnehmen, soweit die **Hauptsatzung** es bestimmt. Nicht für die konstituierende Sitzung und
  nicht für den Vorsitz.
- **Geheime Abstimmungen:** „An einer geheimen Abstimmung darf mittels Bild- und Tonübertragung nicht
  teilgenommen werden“ (§ 29a Abs. 3). Offene Wahlen: ungeklärt.
- **Notlage:** Per Hauptsatzung ausschließlich per Bild- und Tonübertragung bei Katastrophe, epidemischer Lage
  oder vergleichbarer Notsituation (§ 29a Abs. 5).
- **Ausschüsse:** Hauptausschuss und beratende Ausschüsse entsprechend (§ 35 Abs. 5, § 36 Abs. 7).
- **Quelle:** <https://www.umwelt-online.de/regelwerk/cgi-bin/suchausgabe.cgi?pfad=%2Fallgemei%2Flaender%2Fmv%2Fz24_0154.htm&such=Kommunale>.

### Niedersachsen

- **Norm:** § 64 Abs. 3 ff. NKomVG (gültig ab 30.03.2022).
- **Regelbetrieb:** Abgeordnete **mit Ausnahme der oder des Vorsitzenden** können per Videokonferenztechnik
  teilnehmen, soweit die **Hauptsatzung** es zulässt; sie gelten als anwesend. Die Öffentlichkeit muss sie sehen
  und hören können.
- **Ausschlüsse:** In einer Sitzung, an der Abgeordnete zugeschaltet teilnehmen, dürfen geheime Wahlen (§ 67
  Satz 2), nach § 66 Abs. 2 vorgesehene geheime Abstimmungen und Beratungen von Angelegenheiten, zu deren
  Geheimhaltung die Kommune nach § 6 Abs. 3 Satz 1 verpflichtet ist, **in der ganzen Sitzung** nicht
  durchgeführt werden (§ 64 Abs. 3 Satz 6); für Videositzungen nach § 182 ebenso (§ 182 Abs. 2 Satz 6).
  Zugeschaltete gelten als anwesend (Satz 5) und nehmen an offenen Wahlen und Abstimmungen teil. Die
  Zugeschalteten für diese Zeit abzuschalten, ist nach der Arbeitshilfe des NLT (Stand 05.07.2023, S. 8 f.)
  unzulässig; ergibt sich eine geheime Wahl erst in der Sitzung, folgt sie in einer späteren Präsenzsitzung.
  Das Muster einer Geschäftsordnung des NLT sieht eine geheime Abstimmung nur auf Mehrheitsbeschluss vor.
- **Ausschüsse:** Hauptausschuss und Ausschüsse entsprechend, soweit die Hauptsatzung nichts anderes bestimmt
  (§ 64 Abs. 8).
- **Notlage (§ 182):** Bei örtlich relevantem Infektionsgeschehen oder außergewöhnlicher Notlage beschließt die
  Vertretung auf Vorschlag der bzw. des HVB mit Zweidrittelmehrheit die Erleichterungen für höchstens drei
  Monate (Abs. 1 Satz 2); danach kann die Ladung eine Videositzung anordnen, geheime Wahlen und Abstimmungen
  sind dann unzulässig (Abs. 2 Satz 6). Nach der Arbeitshilfe des NLT seit dem Gesetz vom 07.12.2021 in Kraft;
  das Profil sagt deshalb „nur in Notlagen“ (bisher „ungeklärt“).
- **Fassungen des Sitzungsrechts:** ab 07.05.2026
  <https://voris.wolterskluwer-online.de/browse/document/ef43f33c-e3d3-3ffd-99ad-c8b008cdee38> (Nds. GVBl. 2026
  Nr. 30 <https://www.verkuendung-niedersachsen.de/api/ndsgvbl/2026/30/0/nds-gvbl-2026-30.pdf>) und ab 01.11.2026
  <https://voris.wolterskluwer-online.de/browse/document/f0e5a51c-8827-33a6-907e-3994202d691b> (Nds. GVBl. 2026
  Nr. 77 <https://www.verkuendung-niedersachsen.de/api/ndsgvbl/2026/77/0/nds-gvbl-2026-77.pdf>), siehe oben.
- **Quellen:** <https://voris.wolterskluwer-online.de/browse/document/4fc126d2-a078-3b04-bdec-77b9525778e6>,
  § 182 <https://voris.wolterskluwer-online.de/browse/document/90d143bf-4b8a-3fd4-803d-94a4742256f0>,
  Arbeitshilfe des NLT <https://www.nlt.de/wp-content/uploads/2023/07/Arbeitshilfe-des-NLT-zur-optionalen-Einfuehrung-von-Hybridsitzungen-nach-%C2%A7-64-NKomVG-Gesamtdok.pdf>,
  Muster einer Geschäftsordnung des NLT <https://www.nlt.de/wp-content/uploads/2021/10/Arbeitshilfe-Muster-einer-Geschaeftsordnung-Stand-13.10.2021.pdf>
  (abgerufen 02.10.2026).

### Nordrhein-Westfalen

- **Normen:** § 47a GO NRW (digitale und hybride Sitzungen in Ausnahmefällen), § 58a GO NRW (hybride Sitzungen
  der Ausschüsse), eingefügt durch das Gesetz zur Einführung digitaler Sitzungen für kommunale Gremien vom
  13.04.2022 (GV. NRW. 2022 S. 490); Digitalsitzungsverordnung (DigiSiVO) vom 13.05.2022. Kreise: §§ 32a, 41a KrO NRW.
- **Rat:** digital oder hybrid **nur in Ausnahmefällen** (Katastrophe, epidemische Lage, andere außergewöhnliche
  Notsituation); Ratsbeschluss mit Zweidrittelmehrheit für höchstens zwei Monate, Verlängerung möglich (§ 47a Abs. 1, 3).
- **Ausschüsse:** hybrid auch **im Regelbetrieb**, wenn die **Hauptsatzung** es bestimmt; der Ausschuss beschließt
  mit einfacher Mehrheit (§ 58a Satz 1). **Ausgenommen** sind Hauptausschuss, Finanzausschuss und
  Rechnungsprüfungsausschuss (§ 57 Abs. 2 GO NRW) – für sie gilt nur § 47a.
- **Vorsitz:** In der hybriden Sitzung ist die Sitzungsleitung am Sitzungsort anwesend (§ 47a Abs. 2).
- **Technik:** Nur von der gpaNRW zugelassene Anwendungen (§ 47a Abs. 4).
- **Wahlen und geheime Abstimmungen:** nur mit einem zugelassenen Abstimmungssystem, das Zugeschaltete und
  Anwesende einheitlich nutzen (§ 5 Abs. 2 DigiSiVO); sonst Briefwahl im Nachgang (§ 4 Abs. 2 Satz 3 DigiSiVO).
- **Öffentlichkeit digitaler Sitzungen:** geschützter Zugang nach vorheriger Anmeldung, Frist nach
  Geschäftsordnung (§ 3 Abs. 1 DigiSiVO); Hinweis, dass Aufzeichnung und Weiterverbreitung untersagt sind.
- **Quellen:** <https://recht.nrw.de/gvnrw/2022-s490/>,
  <https://recht.nrw.de/lmi/owa/br_vbl_detail_text?anw_nr=6&vd_id=20434&vd_back=N712&sg=0&menu=0>,
  Handreichung des Landes (Stand September 2023) <https://www.land.nrw/media/30398/download>,
  <https://gpanrw.de/prufung/digitale-gremienarbeit/digitale-gremienarbeit>.

### Rheinland-Pfalz

- **Norm:** § 35a GemO (Digitale Sitzungsteilnahme, seit 2023), § 35 Abs. 3 GemO (Notlage).
- **Regelbetrieb:** Ratsmitglieder können mit ihrer Zustimmung zugeschaltet werden, soweit der Gemeinderat es in der
  **Geschäftsordnung** zugelassen hat; nicht für den Vorsitzenden.
- **Ausschlüsse:** „Die Teilnahme mittels Ton- und Bildübertragung darf nicht zugelassen werden bei
  konstituierenden Sitzungen, Satzungsbeschlüssen sowie bei geheimen Abstimmungen und Wahlen.“ (§ 35a Abs. 1)
  Ausgeschlossen ist die Teilnahme der Zugeschalteten, nicht der Vorgang in der Sitzung. Ob „geheimen“ auch
  die Wahlen erfasst, ist ungeklärt; vorsorglich gilt der Ausschluss für alle Wahlen (Wortlaut geprüft am
  02.10.2026 nach Auszug).
- **Notlage:** Beschlüsse per Video- oder Telefonkonferenz, wenn zwei Drittel der gesetzlichen Zahl der
  Ratsmitglieder zustimmen (§ 35 Abs. 3).
- **Ungeklärt:** Anwendung auf Ausschüsse.
- **Quellen:** <https://www.haufe.de/id/norm/gemeindeordnung-rheinland-pfalz-35a-digitale-sitzungsteilnahme-HI1521321_p35a.html>,
  <https://www.landesrecht.rlp.de/bsrp/document/jlr-GemORPV37P35>.

### Saarland

- **Normen:** § 51a KSVG (Videokonferenzen und Hybridsitzungen in außerordentlichen Notlagen), § 48 Abs. 6 KSVG
  (Ausschüsse), Änderung vom 12.11.2025 (Amtsblatt des Saarlandes, Dezember 2025).
- **Gemeinderat:** Videokonferenz oder hybrid **nur in außerordentlichen Notlagen**, wenn zwei Drittel der gesetzlichen
  Mitgliederzahl zustimmen.
- **Ausschüsse:** hybrid auch **im Regelbetrieb**, wenn der Gemeinderat es mit Zweidrittelmehrheit in der
  Geschäftsordnung festlegt.
- **Ausschlüsse:** „Die Durchführung von Wahlen und geheimen Abstimmungen in Videokonferenzen und
  Hybridsitzungen ist unzulässig.“ (§ 51a Abs. 4) – in der ganzen Sitzung, sobald jemand zugeschaltet ist, nicht
  nur für die Zugeschalteten. Fassung des Gesetzes Nr. 2187 vom 12.11.2025 (Amtsbl. I S. 285), Wortlaut nach nicht
  amtlicher Textsammlung, geprüft am 02.10.2026.
- **Quellen (teils Sekundärquellen):**
  <https://netzwerk-kommunalpolitik.de/2026/01/07/hybridsitzungen-im-saarlaendischen-kommunalrecht/>,
  <https://www.saarheim.de/Gesetze/ksvg.htm>.

### Sachsen

- **Norm:** § 36a SächsGemO (Sitzungen ohne persönliche Anwesenheit im Sitzungsraum).
- **Notlage:** in Ausnahmefällen durch Naturkatastrophen, Infektionsschutz oder sonstige außergewöhnliche
  Notsituationen; öffentliche Sitzungen mit unmittelbarer Übertragung an einen öffentlich zugänglichen Ort.
- **Regelbetrieb:** nach Stand der Recherche **nicht vorgesehen**. Die SächsGemO wurde zuletzt am 27.06.2025
  geändert; Auswirkungen auf § 36a nicht geprüft (ungeklärt).
- **Quellen:** <https://www.revosax.sachsen.de/vorschrift/2754-Saechsische-Gemeindeordnung>,
  <https://www.haufe.de/id/norm/saechsische-gemeindeordnung-36a-durchfuehrung-von-sitzungen-ohne-persoenliche-anwesenheit-im-sitzungsraum-HI1989031_p36a.html>.

### Sachsen-Anhalt

- **Norm:** § 56a KVG LSA in der Fassung des Gesetzes zur Fortentwicklung des Kommunalverfassungsrechts
  (verkündet am 31.05.2024).
- **Regelbetrieb:** Hybridsitzungen auch außerhalb außergewöhnlicher Notsituationen, soweit die **Hauptsatzung**
  es zulässt (§ 56b Abs. 1); nicht zugeschaltet werden dürfen der Vorsitzende der Sitzung und der
  Hauptverwaltungsbeamte (Satz 3). Zugeschaltete gelten als anwesend (Satz 5).
- **Ausschlüsse:** „In einer Sitzung, an der Mitglieder durch Zuschaltung mittels Ton- und Bildübertragung
  teilnehmen, dürfen geheime Wahlen nicht durchgeführt werden.“ (§ 56b Abs. 1 Satz 8) – in der ganzen Sitzung;
  offene Wahlen (§ 56 Abs. 3: wenn kein Mitglied widerspricht) bleiben möglich. In Videokonferenzsitzungen nach
  § 56a dürfen Wahlen im Sinne von § 56 Abs. 3 nicht durchgeführt werden. Wortlaut nach dem Gesetzentwurf
  Drs. 8/3424 (geprüft am 02.10.2026); die verkündete Fassung (Gesetz vom 16.05.2024, GVBl. LSA S. 128) stimmt
  darin nach nicht amtlicher Textsammlung überein.
- **Notlage:** Videokonferenz bei Naturkatastrophe, epidemischer oder pandemischer Lage oder sonstiger
  außergewöhnlicher Notsituation (§ 56a Abs. 1).
- **Ungeklärt:** Vorgaben zur Öffentlichkeit in der verkündeten Fassung.
- **Quellen (teils Sekundärquellen):** <https://presse.sachsen-anhalt.de/staatskanzlei/2023/12/05/sachsen-anhalt-bekommt-modernes-kommunalrecht>,
  <https://www.landesrecht.sachsen-anhalt.de/bsst/document/jlr-KomVerfGST2014V10P56a>,
  Gesetzentwurf (Parlamentsdokumentation des Landtags) <https://padoka.landtag.sachsen-anhalt.de/files/drs/wp8/drs/d3424lge.pdf>,
  Beispiel <https://www.raguhn-jessnitz.de/de/datei/anzeigen/id/41329,1203/hauptsatzung_r-j_11.07.2024.pdf>.

### Schleswig-Holstein

- **Normen:** § 34a GO (Sitzungsteilnahme durch Ton-Bild-Übertragung), § 35a GO (Sitzungen in Fällen höherer
  Gewalt), Gesetz zur Änderung kommunalrechtlicher Vorschriften (Landtag, Januar 2025).
- **Regelbetrieb:** Per **Hauptsatzung** können Gemeindevertreter zugeschaltet werden, wenn eine Teilnahme im
  Sitzungsraum nicht möglich ist; nicht für die konstituierende Sitzung; der Vorsitz muss im Sitzungsraum sein;
  Erklärung spätestens zwei Tage vor der Sitzung. Ausschüsse, Ortsbeiräte und Beiräte per Hauptsatzung (§ 34a Abs. 9).
  Laut Landtag ab 01.01.2027 verpflichtend, wenn ein Mitglied es wünscht und es technisch möglich ist.
- **Wahlen:** Zugeschaltete nehmen nicht teil, wenn nach § 40 Abs. 2 GO widersprochen wird (geheime Wahl).
- **Notlage:** Videokonferenz per Hauptsatzung; Öffentlichkeit durch Übertragung in einen öffentlich zugänglichen
  Raum und über das Internet (§ 35a Abs. 5).
- **Hinweis:** Angaben nach dem Gesetzentwurf; die beschlossene Fassung vor Einsatz prüfen.
- **Quellen:** <https://www.landtag.ltsh.de/infothek/wahl20/drucks/02500/drucksache-20-02574.pdf>,
  <https://www.landtag.ltsh.de/nachrichten/25_01_28_kommunalrecht_vorschriften/>.

### Thüringen

- **Norm:** § 36a ThürKO (Sitzungen und Entscheidungen in Notlagen), eingefügt durch das Sechste Gesetz zur Änderung
  der ThürKO vom 11.03.2021, in Kraft seit 01.04.2021.
- **Notlage:** per **Hauptsatzung** Videokonferenzen, wenn eine persönliche Teilnahme wegen einer außergewöhnlichen
  Situation (insbesondere Katastrophenfall, Pandemie, Epidemie) nicht möglich ist; der Bürgermeister stellt die
  Notlage fest, der Gemeinderat beschließt in der nächsten Sitzung über ihren Fortbestand.
- **Öffentlichkeit:** zeitgleiche Übertragung in einen öffentlich zugänglichen Raum (§ 40 Abs. 1 ThürKO).
- **Regelbetrieb:** nach Stand der Recherche **nicht vorgesehen**.
- **Quellen:** <https://landesrecht.thueringen.de/bsth/document/jlr-KomOTH2003V28P36a>,
  <https://ris.eisenach.de/bi/vo0050.php?__kvonr=7208>.

## Offene Punkte der Recherche

- Wortlaut prüfen, wo nur Sekundärquellen vorlagen: Berlin, Bremen/Bremerhaven, Hamburg; Saarland und
  Sachsen-Anhalt nur für Wahlen und geheime Abstimmungen geprüft (nicht amtliche Textsammlung bzw. Gesetzentwurf).
- Sachsen-Anhalt: In Videokonferenzsitzungen nach § 56a sind alle Wahlen ausgeschlossen, in Hybridsitzungen nur
  geheime; das Profil kennt eine Regel je Land und sperrt offene Wahlen in digitalen Sitzungen bisher nicht.
- Hessen: Zugeschaltete gelten als anwesend im Sinne von § 53 Abs. 1 Satz 1 HGO (Beschlussfähigkeit,
  § 52a Abs. 1); mandari nimmt sie bei Wahlen („für Zugeschaltete ausgeschlossen“) wie in Bayern,
  Baden-Württemberg, Brandenburg und Rheinland-Pfalz für diesen TOP aus der Beschlussfähigkeit. Eine eigene
  Profileinstellung für die Beschlussfähigkeit fehlt noch.
- Beschlossene Fassung des § 34a GO Schleswig-Holstein (Pflicht zur Zuschaltung ab 2027).
- Bayern: Mehrheitserfordernis nach der Kommunalwahl 2026, Anwendung auf Ausschüsse.
- Ausschüsse in Hessen, Rheinland-Pfalz, Sachsen, Thüringen.
- Sitzungsrecht (Fassungen) der übrigen Länder; Niedersachsen: Spezialregeln der Ausschüsse nach § 73 NKomVG
  und die Anwendungshinweise der Spitzenverbände zu den Änderungen ab 01.11.2026 nicht geprüft.
- Landkreisordnungen sind nicht Gegenstand dieser Übersicht (in den meisten Ländern parallel geregelt).
