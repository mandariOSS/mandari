# Sitzungsformat und Landesrecht (präsent, hybrid, digital)

> **Keine Rechtsberatung.** Diese Übersicht fasst die öffentlich zugängliche Rechtslage zu hybriden und
> digitalen Gremiensitzungen in den 16 Ländern zusammen. **Stand der Recherche: 30.09.2026.** Maßgeblich
> sind der amtliche Gesetzestext, die Hauptsatzung und die Geschäftsordnung der Kommune. „Ungeklärt“
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
- **Gesetzliche Ausschussart** am Gremium (Hauptausschuss, Finanzausschuss, Rechnungsprüfungsausschuss),
  weil einzelne Länder diese Ausschüsse ausnehmen (NRW: § 58a i. V. m. § 57 Abs. 2 GO NRW).
- **Sitzungsformat je Sitzung** (`SessionMeeting.format`) mit Begründung (Notlage, Beschluss), verschlüsselt
  gespeichertem Zugangsweg für Zugeschaltete und Hinweis für die Öffentlichkeit (Übertragung, Anmeldung).

### Prüfregeln (`apps/session/services/meeting_format_service.py`)

1. Präsenzsitzungen sind immer zulässig.
2. Hybride und digitale Sitzungen setzen ein Landesprofil voraus.
3. Maßgeblich ist die Regel des Landesprofils für den Gremientyp (Rat bzw. Ausschüsse). Ausgenommene
   Ausschussarten fallen auf die Regel für ausgenommene Ausschüsse zurück (NRW: nur in Notlagen nach
   § 47a GO NRW). Bei gemeinsamen Sitzungen gilt die strengste Regel der beteiligten Gremien.
4. „nicht vorgesehen“ verhindert das Format mit Begründung und Norm.
5. „nur in Notlagen“ und jede digitale Sitzung verlangen eine Begründung (Notlage, zugrunde liegender Beschluss).
6. „zulässig (Regelbetrieb)“ verlangt den vollständigen Nachweis der örtlichen Rechtsgrundlage, ebenso
   „ungeklärt“. Wo das Land einen Beschluss des Gremiums verlangt (Hamburg, Berlin), ist die Begründung Pflicht.
7. Fraktionen und Verwaltungseinheiten unterliegen nicht den Sitzungsregeln der Kommunalverfassung.

Wahlen, geheime Abstimmungen, konstituierende Sitzungen und Satzungsbeschlüsse betreffen einzelne
Tagesordnungspunkte bzw. Teilnehmende. Das Landesprofil hält sie fest; durchgesetzt werden sie mit der
Teilnahmeart (#139) und der Selbst-Abstimmung (#141).

### Pflege

Ändert sich die Rechtslage: `landesprofile.json` anpassen (`as_of` des Landes bzw. `stand` der Datei, Quellen),
diese Übersicht nachziehen und nach dem Deploy `python manage.py session_state_profiles --sync` ausführen.
`--check` meldet Abweichungen zwischen Datenbank und Datei. Die Datenmigration `session.0045` übernimmt die
Profile beim ersten Deploy automatisch.

## Übersicht

Spalten: **Hybrid Rat / Ausschüsse** und **Digital** (Rat): R = zulässig im Regelbetrieb, N = nur in
Notlagen, – = nicht vorgesehen, ? = ungeklärt. **Vorsitz**: P = muss im Sitzungsraum anwesend sein.
**Wahlen / geheim**: Teilnahme Zugeschalteter an Wahlen bzw. geheimen Abstimmungen (✗ = ausgeschlossen,
B = nur unter Bedingungen, ? = ungeklärt). **Prüftiefe**: W = Wortlaut eingesehen, T = teilweise,
S = nur Sekundärquellen.

| Land | Kernnorm | Hybrid Rat | Hybrid Ausschüsse | Digital | Voraussetzung | Vorsitz | Wahlen / geheim | Prüftiefe |
|------|----------|:---:|:---:|:---:|---|:---:|:---:|:---:|
| Baden-Württemberg | § 37a GemO | R | R | N | Hauptsatzung | P | ✗ / ? | W |
| Bayern | Art. 47a GO | R | ? | – | Geschäftsordnung | ? | ✗ / ? | W |
| Berlin | § 8a BezVG | ? | ? | N | Beschluss (BVV-Vorstand) | ? | B / B | S |
| Brandenburg | §§ 34, 43 BbgKVerf | R | R | N | Geschäftsordnung | P | ✗ / ? | T |
| Bremen (Bremerhaven) | § 38 Verfassung Bremerhaven | ? | ? | ? (Ausschüsse N) | ungeklärt | ? | ? / ? | S |
| Hamburg | § 13 BezVG | N | R | N (Ausschüsse R) | Beschluss | ? | ✗ / ? | S |
| Hessen | § 52a HGO | R | ? | – | Hauptsatzung | P | ✗ / ? | W |
| Mecklenburg-Vorpommern | § 29a KV M-V | R | R | N | Hauptsatzung | P | ? / ✗ | T |
| Niedersachsen | § 64 NKomVG | R | R | ? | Hauptsatzung | P | ✗ / ✗ | T |
| Nordrhein-Westfalen | §§ 47a, 58a GO NRW | N | R (außer HA, FA, RPA) | N | Hauptsatzung | P | B / B | W |
| Rheinland-Pfalz | § 35a GemO | R | ? | N | Geschäftsordnung | P | ✗ / ✗ | T |
| Saarland | §§ 48, 51a KSVG | N | R | N | Geschäftsordnung | ? | ✗ / ✗ | S |
| Sachsen | § 36a SächsGemO | N | ? | N | ungeklärt | ? | ? / ? | T |
| Sachsen-Anhalt | § 56a KVG LSA | R | R | N | Hauptsatzung | ? | ✗ / ? | S |
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
- **Ausschlüsse:** konstituierende Sitzung und Tagesordnungspunkte mit **geheimen Wahlentscheidungen** (§ 34 Abs. 2 Satz 2).
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
- **Ausschlüsse:** Wahlen nach § 55, Beschlüsse nach § 39a Abs. 3 Satz 2, § 57 Abs. 2, § 76 Abs. 1 und Abs. 4
  Satz 3, § 76a sowie die erste Sitzung der Gemeindevertretung; weitere Fälle per Hauptsatzung (§ 52a Abs. 2).
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
- **Ausschlüsse:** In Hybridsitzungen dürfen geheime Wahlen (§ 67 Satz 2) und geheime Abstimmungen (§ 66 Abs. 2)
  **insgesamt** nicht durchgeführt werden; keine Beratung geheim zu haltender Angelegenheiten.
- **Ausschüsse:** Hauptausschuss und Ausschüsse entsprechend (§ 64 Abs. 8).
- **Ungeklärt:** Fortgeltung der Sonderregelung § 182 NKomVG (epidemische Lagen, Notsituationen) für rein
  digitale Sitzungen.
- **Quellen:** <https://voris.wolterskluwer-online.de/browse/document/4fc126d2-a078-3b04-bdec-77b9525778e6>,
  Arbeitshilfe des NLT <https://www.nlt.de/wp-content/uploads/2023/07/Arbeitshilfe-des-NLT-zur-optionalen-Einfuehrung-von-Hybridsitzungen-nach-%C2%A7-64-NKomVG-Gesamtdok.pdf>.

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
- **Ausschlüsse:** konstituierende Sitzungen, **Satzungsbeschlüsse**, geheime Abstimmungen und Wahlen.
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
- **Ausschlüsse:** Wahlen und geheime Abstimmungen nicht digital oder hybrid.
- **Quellen (Sekundärquellen, Wortlaut vor Einsatz prüfen):**
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
- **Regelbetrieb:** Hybridsitzungen auch außerhalb außergewöhnlicher Notsituationen; Einzelheiten in der
  **Hauptsatzung**.
- **Ausschlüsse:** geheime Wahlen (§ 56 Abs. 3 KVG LSA) in Hybridsitzungen unzulässig.
- **Notlage:** Videokonferenz bei Naturkatastrophe, epidemischer oder pandemischer Lage oder sonstiger
  außergewöhnlicher Notsituation (§ 56a Abs. 1).
- **Ungeklärt:** genauer Absatz der Regelbetriebsnorm, Vorgaben zum Vorsitz und zur Öffentlichkeit (Muster-Hauptsatzungen
  nehmen den Vorsitz von der Zuschaltung aus).
- **Quellen (teils Sekundärquellen):** <https://presse.sachsen-anhalt.de/staatskanzlei/2023/12/05/sachsen-anhalt-bekommt-modernes-kommunalrecht>,
  <https://www.landesrecht.sachsen-anhalt.de/bsst/document/jlr-KomVerfGST2014V10P56a>,
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

- Wortlaut prüfen, wo nur Sekundärquellen vorlagen: Berlin, Bremen/Bremerhaven, Hamburg, Saarland, Sachsen-Anhalt.
- Beschlossene Fassung des § 34a GO Schleswig-Holstein (Pflicht zur Zuschaltung ab 2027).
- Bayern: Mehrheitserfordernis nach der Kommunalwahl 2026, Anwendung auf Ausschüsse.
- Ausschüsse in Hessen, Rheinland-Pfalz, Sachsen, Thüringen; Fortgeltung § 182 NKomVG.
- Landkreisordnungen sind nicht Gegenstand dieser Übersicht (in den meisten Ländern parallel geregelt).
