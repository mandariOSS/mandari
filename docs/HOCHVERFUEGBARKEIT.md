# Hochverfügbarkeit

Zielarchitektur für einen Betrieb ohne einzelne Ausfallpunkte, mit automatischem
Umschalten und ohne Datenverlust.

Abgrenzung: Dieses Dokument beschreibt **Hochverfügbarkeit**, nicht
Wiederanlauffähigkeit. Der Unterschied ist nicht sprachlich. Eine Replikation von
Maschinenabbildern im Minutentakt ist ein **warmer Klon**: Sie verliert Daten seit
der letzten Übertragung und braucht jemanden, der umschaltet. Hochverfügbarkeit
heißt, dass der Dienst weiterläuft, ohne dass jemand eingreift, und dass keine
bestätigte Schreiboperation verlorengeht.

Bezug: BSI **DER.4** (Notfallmanagement), **CON.3** (Datensicherung),
**APP.3.1.A11** (sichere Anbindung von Hintergrundsystemen). Ergänzt Issue #229.

---

## 1. Was Hochverfügbarkeit technisch verlangt

Drei Bedingungen, die alle erfüllt sein müssen. Fehlt eine, ist es kein HA.

### Mehrheitsentscheid statt Paarlauf

Zwei Knoten können nicht sicher entscheiden, wer nach einem Netzwerkfehler
weitermachen darf. Beide halten sich für den Überlebenden, beide schreiben — das
ist das Split-Brain, und es zerstört Daten zuverlässiger als jeder Ausfall.

**Daraus folgt: drei Knoten, nicht zwei.** Ein QDevice auf einem kleinen Gerät
verschafft zwar ein Quorum für den Cluster, aber keine dritte Datenkopie. Für
verteilten Speicher und für die Datenbank reicht das nicht.

### Zwangsabschaltung (Fencing)

Ein Knoten, der sein Quorum verliert, muss sich **selbst abschalten**, bevor seine
Dienste anderswo anlaufen. Proxmox löst das über einen Watchdog: Der Knoten setzt
einen Timer, und wenn er das Quorum verliert, hört er auf, den Timer
zurückzusetzen — und startet neu. Ohne dieses Verhalten kann dieselbe VM auf zwei
Knoten laufen und denselben Datenbestand beschreiben.

### Gemeinsamer, synchron replizierter Speicher

Eine VM kann nur dann anderswo anlaufen, wenn ihr Speicher dort ebenfalls und
**aktuell** verfügbar ist. Bei Proxmox ist Ceph der vorgesehene Weg. Ceph mit
drei Kopien braucht drei Knoten und ein eigenes Netz — 10 GbE ist die untere
Grenze, darunter wird die Replikation zum Engpass.

## 2. Zielarchitektur

```
                         Internet
                             │
                   Floating IP (VRRP)
                             │
              ┌──────────────┴──────────────┐
          Caddy-1                       Caddy-2          aktiv / passiv
              └──────────────┬──────────────┘
                             │
        ┌────────────────────┼────────────────────┐
     app-1                app-2                app-3     zustandsfrei, N≥2
        └────────────────────┼────────────────────┘
                             │
      ┌───────────┬──────────┼──────────┬───────────────┐
      │           │          │          │               │
  pgBouncer    Redis    Elasticsearch  Ceph RGW    Ingestor (genau einer)
      │        Sentinel    3 Knoten   (Objekt-      über pg_advisory_lock
   Patroni      3 Knoten               speicher)
  pg-1/2/3
  synchron
      │
    etcd  3 Knoten

  ────────────────────────────────────────────────────────────────
  Unterbau: 3 physische Knoten, Proxmox-Cluster mit HA-Manager,
            Ceph über eigenes 10-GbE-Netz, Watchdog-Fencing
```

### Datenbank: der anspruchsvollste Teil

**Patroni mit `synchronous_mode`** und einem etcd-Verbund aus drei Mitgliedern.
Der entscheidende Schalter ist `synchronous_mode`: Ohne ihn kann Patroni einen
Standby befördern, der nicht alle bestätigten Transaktionen hat. Mit ihm wird nur
befördert, wer nachweislich vollständig ist.

Der Preis dafür ist ehrlich zu nennen: Jeder Schreibvorgang wartet auf die
Bestätigung des synchronen Standby — typischerweise **zwei bis fünf Millisekunden
mehr** je Transaktion. Für eine Anwendung, die Sitzungsunterlagen verwaltet, ist
das nicht spürbar.

Davor gehört **pgBouncer**, damit die Anwendung den Wechsel der Schreibinstanz
nicht bemerkt.

### Objektspeicher statt gemeinsamem Verzeichnis

Mehrere Anwendungsinstanzen brauchen die hochgeladenen Dateien an einem Ort, den
alle erreichen. Da Ceph ohnehin steht, ist **Ceph RGW** der naheliegende Weg —
ein S3-kompatibler Zugang ohne zusätzliche Software. Auf Seite der Anwendung
kommt `django-storages` dazu.

Das ist die **einzige nennenswerte Codeänderung** im ganzen Vorhaben.

### Ingestor: genau einer, aber irgendwo

Der Ingestor darf nicht vervielfacht werden — doppelte Abrufe belasten fremde
Ratsinformationssysteme doppelt, und Köln hat unsere Adresse bereits mit einem
Ratenlimit belegt. In einem HA-Aufbau ist er trotzdem kein Ausfallpunkt: Er läuft
auf beliebigen Knoten, und eine **Sperre über `pg_advisory_lock`** sorgt dafür,
dass immer nur einer arbeitet. Fällt sein Knoten aus, greift der nächste zu.

Dasselbe gilt für den Protokoll-Orchestrator.

## 3. Was Hochverfügbarkeit nicht leistet

Das gehört vor die Investitionsentscheidung, nicht danach.

**Es ist nicht unterbrechungsfrei.** Ein Datenbank-Failover mit Patroni dauert bei
üblicher Einstellung **30 bis 45 Sekunden**: Die Sperre muss ablaufen, ein Standby
gewinnt die Beförderung, der Verteiler findet das Ziel. Wer in diesem Moment
speichert, sieht einen Fehler. Die ehrliche Zusage lautet „Sekunden statt Stunden",
nicht „kein Ausfall".

**Ein Standort bleibt ein Ausfallpunkt.** Drei Knoten im selben Rack hängen an
einer Stromeinspeisung und einer Anbindung. Gegen Feuer, Wasser oder einen
Baggerbiss hilft nur ein zweiter Brandabschnitt — besser ein zweiter Standort.
Ceph über zwei Standorte verlangt allerdings sehr kurze Laufzeiten; realistisch
ist dann ein asynchron gespiegelter Zweitstandort für den Katastrophenfall.

**Die Komplexität wird selbst zum Risiko.** Ceph, Patroni, etcd, Sentinel und
keepalived sind fünf zusätzliche Systeme, die ausfallen, falsch konfiguriert sein
oder nach einem Update anders reagieren können. Bei einem sehr kleinen Team ist
die häufigste Ursache eines Ausfalls in solchen Aufbauten **der HA-Mechanismus
selbst**. Das ist kein Argument dagegen — aber es bedeutet, dass ein Failover
regelmäßig geübt gehört, nicht nur eingerichtet.

**Es ersetzt keine Sicherung.** Ein versehentlich gelöschter Datensatz wird
synchron auf alle Knoten repliziert. Die restic-Sicherung bleibt unverändert nötig.

## 4. Weg dorthin

Die Reihenfolge ist so gewählt, dass jeder Schritt für sich einen Nutzen bringt
und der teuerste zuletzt kommt.

| Schritt | Inhalt | Nutzen einzeln |
|---|---|---|
| **1** | Medien in Objektspeicher (`django-storages`), Ingestor-Sperre über `pg_advisory_lock` | Entkoppelt Dateien vom Server; Voraussetzung für alles Weitere |
| **2** | Wiederherstellung einmal mit der Uhr üben (#229) | Beziffert, was heute gilt — und was Stufe 3 einspart |
| **3** | Drei physische Knoten, Proxmox-Cluster, Ceph über eigenes 10-GbE-Netz, Watchdog-Fencing | Löst den Ausfallpunkt, der alle anderen dominiert |
| **4** | Anwendung mehrfach, Caddy doppelt mit Floating IP | Rollierende Aktualisierung ohne Ausfall |
| **5** | Patroni mit `synchronous_mode` + etcd + pgBouncer | Datenbank ohne Datenverlust, Umschalten in Sekunden |
| **6** | Redis Sentinel, Elasticsearch auf drei Knoten | Live-Kollaboration und Suche überstehen einen Knotenausfall |
| **7** | Zweiter Standort, asynchron | Schutz gegen den Verlust eines ganzen Standorts |

Schritt 1 und 2 sind reine Arbeitszeit. Schritt 3 ist die eigentliche Investition —
davor lohnt sich Schritt 4 bis 6 nicht, weil alles auf einer Maschine läge.

## 5. Zu klärende Entscheidungen

Diese Fragen bestimmen die Architektur und lassen sich nicht technisch beantworten:

1. **Eigenbetrieb oder betreute Bausteine?** Die Datenbank ist der schwierigste
   Teil. Ein betreuter PostgreSQL-Dienst mit Hochverfügbarkeit nimmt genau die
   Komplexität heraus, die am ehesten selbst zum Ausfall führt. Für ein Produkt im
   Verwaltungsumfeld muss der Anbieter in Deutschland sitzen und auftragsverarbeiten.
2. **Welche Verfügbarkeit soll vertraglich zugesagt werden?** 99,9 % sind rund
   neun Stunden Ausfall im Jahr und mit dieser Architektur erreichbar. 99,99 % sind
   52 Minuten und verlangen einen zweiten Standort samt Bereitschaft.
3. **Ein Standort oder zwei?** Bestimmt, ob Ceph gestreckt wird oder ein zweiter
   Standort asynchron folgt.
4. **Wer ist nachts erreichbar?** Automatisches Umschalten deckt den Ausfall eines
   Knotens. Es deckt nicht den Fall, dass der Umschaltmechanismus selbst klemmt.

---

**Quellen:** Proxmox-Cluster benötigen für produktive Hochverfügbarkeit mindestens
drei Knoten; Fencing erfolgt über Watchdog-Selbstabschaltung; Ceph sollte über ein
eigenes 10-GbE-Netz laufen. Patronis `synchronous_mode` verhindert die Beförderung
unvollständiger Standbys; ein etcd-Verbund braucht drei Mitglieder; ein Failover
dauert bei Standardwerten 30 bis 45 Sekunden.
