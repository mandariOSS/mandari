# Kein Scraping für gehostetes Sternberg RIM, regisafe und komuna: Kooperation statt Adapter

Status: angenommen · Datum: 2026-09-17 · Wiedervorlage: 2027-09 · Bezug: Epic #125, Issue #124

## Kontext

Das Bürgerportal bindet Ratsinformationssysteme (RIS) bevorzugt über OParl an. Wo
eine Kommune kein OParl anbietet, ist ein Adapter, der die öffentliche Weboberfläche
liest („Scraping“), die naheliegende Frage. Für drei verbreitete Systeme kehrt sie
regelmäßig wieder. Damit sie nicht jedes Mal neu bewertet wird, halten wir die
Entscheidung hier fest.

Unsere Regeln für alle Scraper-Quellen (`docs/SCRAPER_SOURCES.md`, Abschnitt 3):
identifizierbarer User-Agent mit Kontaktadresse, robots.txt nach RFC 9309, höchstens
eine Anfrage je zwei Sekunden je Host, **keine Umgehung** von Logins, CAPTCHAs,
Proof-of-Work-Gates oder Session-Schranken.

Dazu kommt das Recht: Nach **§ 44b UrhG** ist ein maschinenlesbarer Nutzungsvorbehalt
zu beachten, wenn Inhalte für Text- und Data-Mining vervielfältigt werden. Eine
robots.txt mit `Disallow` ist genau ein solcher Vorbehalt. Unser robots-Respekt ist
damit keine Höflichkeit, sondern Rechtsgrundlage unseres Handelns.

## Befund je System (Stand September 2026)

| System | Befund | Folge für Scraping |
|---|---|---|
| **Sternberg RIM (Rechenzentrums-Hosting)** | Vor den gehosteten Instanzen liegt eine Web Application Firewall, die Browser per JavaScript verifiziert; die Oberfläche nutzt opake, nicht stabile Kennungen für Vorgänge und Dokumente | Zugriff nur mit Umgehung der WAF möglich, Änderungserkennung ohne stabile Kennungen unzuverlässig. **Ausgeschlossen.** Das OParl-Modul ist im Hosting vorhanden und kann von der Kommune aktiviert werden. |
| **regisafe** | `robots.txt` mit `Disallow: /`; Oberfläche als Single-Page-App, Inhalte werden erst durch Skripte geladen | Nutzungsvorbehalt nach § 44b UrhG; ein Adapter müsste einen Browser fernsteuern. **Ausgeschlossen.** |
| **komuna** | `robots.txt` mit `Disallow: /`; Single-Page-App | wie regisafe. **Ausgeschlossen.** |

Zum Vergleich lohnt Scraping dort, wo Server-HTML mit stabilen Kennungen und
permissiver robots.txt vorliegt: selbst gehostetes SessionNet (Adapter vorhanden),
selbst gehostetes ALLRIS 4 über Bridge oder App-API, Restbestand ALLRIS 3.

## Optionen

1. **Adapter mit Umgehung** (Browser-Automatisierung, WAF-Token, robots ignorieren):
   technisch möglich, verstößt gegen unsere Regeln und § 44b UrhG, bricht bei jeder
   Änderung der Gegenseite, gefährdet die Reputation des Projekts bei genau den
   Verwaltungen, die wir als Kunden gewinnen wollen.
2. **Warten**: keine Kosten, aber die Kommunen fehlen weiter im Portal.
3. **Kooperationspfad**: Die Kommune erlaubt unseren User-Agent ausdrücklich per
   robots.txt oder aktiviert OParl. Beides ist für sie ein kleiner Schritt; OParl
   ist bei allen drei Herstellern verfügbar oder lizenzierbar.

## Entscheidung

Wir bauen **keinen Adapter** für gehostetes Sternberg RIM, regisafe und komuna und
umgehen keine der genannten Schranken. Stattdessen bieten wir betroffenen Kommunen
den Kooperationspfad an (Vorlage unten). Der Zensus mit `probe_ris` (#114) erfasst
je Kommune Hersteller, robots-Status, Gate und OParl-Autodiscovery, damit das
Angebot gezielt und mit Beleg ausgesprochen werden kann.

**Wiedervorlage im September 2027** oder früher, wenn ein Hersteller robots.txt
öffnet, die WAF entfällt oder OParl standardmäßig aktiv wird.

## Folgen

- Kommunen mit diesen Systemen erscheinen im Portal erst nach Kooperation. Wir
  sagen das auf der Crawler-Infoseite und im Portal transparent.
- Der Aufwand verlagert sich von Entwicklung zu Ansprache; die Erfolgsquote wird im
  Epic #125 gemessen (Ziel: OParl-Aktivierungen).
- Wer trotzdem eine Anbindung baut, dokumentiert hier, was sich an den drei Befunden
  geändert hat.

## Anhang: Kooperationsangebot (Textvorlage)

> Betreff: Ratsinformationen Ihrer Kommune im Bürgerportal mandari
>
> Sehr geehrte Damen und Herren,
>
> mandari ist ein quelloffenes Bürgerportal, das Ratsinformationen deutscher Kommunen
> durchsuchbar macht und über die standardisierte OParl-Schnittstelle bezieht. Für
> [Kommune] können wir Ihre Sitzungen, Vorlagen und Beschlüsse derzeit nicht
> übernehmen, weil Ihr Ratsinformationssystem [Sternberg RIM / regisafe / komuna]
> keine OParl-Schnittstelle freigeschaltet hat und die Weboberfläche automatisierte
> Zugriffe untersagt. Diesen Nutzungsvorbehalt respektieren wir, auch aus § 44b UrhG.
>
> Zwei Wege führen mit wenig Aufwand zum Ziel:
>
> 1. **OParl aktivieren.** Ihr Hersteller bietet das OParl-Modul an [Hinweis je
>    Hersteller: bei Sternberg im Rechenzentrums-Hosting vorhanden; bei regisafe und
>    komuna beim Hersteller erfragen]. Nach Aktivierung binden wir die Schnittstelle
>    innerhalb weniger Tage an und stimmen Abrufintervall und Last mit Ihnen ab.
> 2. **Zugriff ausdrücklich erlauben.** Alternativ ergänzen Sie in der robots.txt
>    Ihres Ratsinformationssystems einen Eintrag für unseren Crawler:
>
>    ```
>    User-agent: mandari-ingestor
>    Allow: /
>    ```
>
>    Unser Crawler identifiziert sich als `mandari-ingestor (+https://mandari.de/crawler)`,
>    stellt höchstens eine Anfrage alle zwei Sekunden und liest ausschließlich
>    öffentliche Bereiche. Bei Systemen mit Bot-Schutz oder rein skriptgesteuerter
>    Oberfläche ist Weg 1 der einzige belastbare.
>
> Ihre Daten bleiben bei Ihnen; das Portal verlinkt auf Ihr System als Quelle. Über
> Rückmeldung oder ein kurzes Gespräch freuen wir uns: [Kontakt].
>
> Mit freundlichen Grüßen
> [Name], mandari
