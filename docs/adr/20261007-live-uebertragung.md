# Live-Übertragungen: Anbieter-Adapter, Einblendungsprofile als Daten, keine Bild- und Tonspeicherung

- Status: angenommen
- Datum: 2026-10-07
- Issue: #915 (Elternissue #47), Folgearbeit Fraktionszuordnung #916
- Bezug: [A1 Schichtenmodell](20260929-schichtenmodell.md), [A4 Aufträge und Zeitpläne](20260929-auftraege-und-zeitplaene.md),
  [A5 Ereignisverträge](20260929-ereignisvertraege.md), [A8 Fremdschlüssel auf den RIS-Bestand](20260929-fremdschluessel-ris-bestand.md)

## Kontext

Viele Kommunen übertragen Rats- und Ausschusssitzungen live über einen Streaming-Anbieter. Die Übertragung
zeigt meist eine Einblendung der Kommune mit der Nummer und dem Titel des Tagesordnungspunkts und dem Namen und
der Fraktion bzw. Funktion der Person, die gerade spricht. Insight kennt die Tagesordnung aus OParl, aber nicht,
welcher Punkt gerade läuft. Wer zuschaut, will wissen: Was wird gerade beraten, wer spricht, wo steht die
Vorlage?

Rahmen: Bild und Ton dürfen nicht gespeichert werden (Urheber- und Persönlichkeitsrechte, Satzungen mancher
Kommunen untersagen Mitschnitte), Redezeiten werden nicht erfasst. Anbieter und Gestaltung der Einblendung
unterscheiden sich je Kommune. Ein lokaler Prototyp hat gegen eine Aufzeichnung gezeigt, dass Texterkennung auf
einem Einzelbild je rund 10 Sekunden TOP, Name und Fraktion zuverlässig liest (etwa 1 bis 2 Sekunden je Bild).

## Entscheidung

- **Eigenes Paket der Drehscheibe** `hub.live` (App-Label `hub_live`, Schicht `hub`): Modelle für Quelle,
  Übertragung, TOP-Abschnitt, Wortmeldung und Protokoll. Verweise auf den RIS-Bestand nach A8 (`PROTECT` für
  Kommune, Gremium und Sitzung, `SET_NULL` für Tagesordnungspunkt und Person), nie `CASCADE` auf RIS-Tabellen.
  Insight liest nur über `hub.live.selectors`.
- **Anbieter-Adapter** mit kleiner Schnittstelle und Register nach Code: `aufloesen(kennung) → StreamInfo`,
  `status(info) → StreamStatus` (online, Phase vorher/live/nachher/unbekannt, Tafeltext, Zuschauerzahl,
  Rohdaten ohne Bild-Adressen), `einbettung_url(kennung)`. Zum Start `3q` (Player-Konfiguration und
  Streamstatus) und `hls` (allgemein: Playlist erreichbar und ohne `#EXT-X-ENDLIST`). Alle Abrufe nur über https,
  mit Zeit- und Größengrenzen und dem User-Agent „mandari (+https://mandari.de)“.
- **Zeitfenster aus OParl**: Abgefragt wird nur rund um die Sitzungen des Gremiums einer aktiven Quelle (Beginn
  − 45 Minuten bis Beginn + 10 Stunden), einmal je Minute (Zeitplan `live_status_abfragen`, verpasste Termine
  ausgelassen). Zustände: geplant → live → beendet, bzw. nicht übertragen, wenn bis 3 Stunden nach Beginn nichts
  lief. Ein Standbild nach dem Ende gilt nicht als Sitzung: Maßgeblich ist die Phase des Anbieters (bei 3Q
  `playoutState`, ersatzweise die Tafel), nicht „online“. Aussetzer beenden erst nach 15 Minuten ohne Signal.
- **Einblendungsprofile als Daten** (JSON an der Quelle, versioniert und geprüft): Balkenerkennung (Ausschnitt,
  Farbregeln, Mindestanteil), Felder `top`, `name`, `fraktion`, `titel` mit relativen Ausschnitten und
  Tesseract-Parametern, TOP-Muster, Funktionsbezeichnungen, bekannte Fraktionen, Zahl der Bestätigungen.
  Felder für Uhren oder Redezeiten gibt es bewusst nicht. Eine allgemeine Vorlage („Balken unten, dreizeilig“)
  liefert die am Prototyp geprüften Werte. Neue Kommunen brauchen ein Profil, keinen Code.
- **Keine Bild- und Tonspeicherung**: Ein Leseauftrag holt das letzte Segment der HLS-Playlist in den Speicher,
  dekodiert das erste Bild mit PyAV (FFmpeg als Bibliothek, kein Programm im Image), liest die Felder mit
  Tesseract (Unterprozess über stdin/stdout, ein Thread, Speicher- und Zeitgrenze) und verwirft Bild und
  Segment. Ins Protokoll gehen nur die gelesenen Texte, Zeiten und die Bildgröße.
- **Entprellung und Zuordnung**: Ein Wechsel von TOP oder Person gilt erst nach zwei gleichen Lesungen in Folge.
  Die TOP-Reihenfolge darf springen (vertagte Punkte). Die TOP-Nummer wird dem Tagesordnungspunkt der Sitzung
  zugeordnet, die Titelähnlichkeit zur Kontrolle gespeichert. Namen werden zuerst unter den Mitgliedern des
  Gremiums am Sitzungstag, dann in der Kommune gesucht; „eindeutig“ nur bei genau einem Treffer.
  Funktionsbezeichnungen (Oberbürgermeisterin, Kämmerer …) werden unscharf erkannt und gelten nicht als
  Fraktion. Gelesene Fraktionen werden unscharf auf die bekannten Bezeichnungen der Kommune abgebildet
  (Umlautfehler, abgeschnittene lange Namen), Kürzel müssen genau gleich sein.
- **Ereignisse** `ris.broadcast.started`, `.agenda_item_started`, `.speaker_changed`, `.ended` (v1) mit eigenem
  Objekttyp `Broadcast`, nur Kennungen und Codes, in derselben Transaktion wie der Zustand (Nachtrag in A5).
  Die Fraktionszuordnung (#916) ist Abonnent von `ris.broadcast.speaker_changed` (`insight.fraktionen_live`,
  idempotent je Wortmeldung), statt dass `hub.live` sie direkt aufruft.
- **Eigener Worker** `worker-live` (Warteschlange `live`, Parallelität 2, 512 MB): Statusabfrage und
  Leseaufträge (je rund 50 Sekunden, höchstens einer je Übertragung, Sperre per Lease) warten nie hinter
  Texterkennung von Dokumenten oder Mailversand.
- **Schalter** `LIVE_UEBERTRAGUNG_AKTIV` (Standard aus): aus = keine Anfragen nach außen, kein Zeitplan, keine
  Aufträge. Zusätzlich hat jede Quelle ihren eigenen Schalter.
- **Öffentliche Live-Seite** `/insight/termine/<uuid>/live/`, ohne Link aus Menü oder Sitzungsseite,
  `noindex`, Player erst nach Klick (Zwei-Klick-Lösung), Aktualisierung des Live-Teils alle 15 Sekunden.

## Alternativen

- **ffmpeg als Programm** im Image: größeres Image, ein weiterer Unterprozess je Bild; PyAV liefert dieselben
  Bibliotheken als Wheel.
- **Koordinaten im Code** je Kommune: jede neue Kommune bräuchte ein Release. Verworfen zugunsten von Profilen.
- **Ton-Transkription** statt Einblendung: liefert mehr, verarbeitet aber Ton (Rechtslage, Rechenaufwand) und
  erkennt Sprecher nicht sicher. Als spätere Option offen.
- **Leseaufträge im allgemeinen Worker**: Ein OCR-Auftrag eines Dokuments kann 30 Minuten dauern, die Lesungen
  stünden so lange still.

## Folgen

- Je Übertragung entstehen rund 5 Lesungen je Minute; das Protokoll wächst entsprechend und wird nach
  `LIVE_PROTOKOLL_TAGE` (Standard 90) vom Zeitplan `live_protokoll_aufraeumen` gelöscht.
- Lesefehler sind möglich: Die Seite sagt das, die Verwaltung kann Personen und TOPs korrigieren.
- Der Anbieter sieht die Abrufe der Statusschnittstelle und der Segmente vom Server aus; Besucherinnen und
  Besucher der Live-Seite erst, wenn sie den Player laden.

## Erweiterung

- **Weitere Kommunen** mit demselben Anbieter: Quelle anlegen (`live_quelle_einrichten`), Profil mit
  `live_profil_testen` an einem Bild der Übertragung kalibrieren, Quelle einschalten.
- **Weitere Anbieter** (YouTube, Vimeo, eigene Player): ein Modul unter `hub/live/anbieter/`, das sich mit
  `registrieren` einträgt. Wo es keine Statusschnittstelle gibt, genügt der allgemeine Adapter `hls`.
- **Ton-Transkription** oder Live-Untertitel wären ein weiterer Leser neben `lesung`, mit eigener Entscheidung
  zu Rechtslage und Speicherung.

## Bezug

- Betrieb: [docs/LIVE_UEBERTRAGUNG.md](../LIVE_UEBERTRAGUNG.md)
- PyAV: https://pyav.basswood-io.com/
- Tesseract: https://tesseract-ocr.github.io/
