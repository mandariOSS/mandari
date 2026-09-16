# Skalierung und Verfügbarkeit

Wie mandari heute betrieben wird, wo die Ausfallpunkte liegen und in welchen
Stufen sich das sinnvoll auflösen lässt.

Bezug: BSI IT-Grundschutz **CON.3** (Datensicherung), **DER.4** (Notfallmanagement),
**APP.3.1** (Betrieb der Webanwendung). Verzahnt mit Issue #229.

Stand: 16.09.2026, erhoben am laufenden System.

---

## 1. Wie es heute aussieht

```
                    web01  (ein physischer Rechner, kein Cluster)
                      │
   ┌──────────────┬───┴────────────┬──────────────┐
hosting01      franzfon        mandari01      electionhub
 (24 GB)        (4 GB)          (16 GB)         (16 GB)
                                   │
                    ┌──────────────┼──────────────┬───────────────┐
                  Caddy      mandari (App)    PostgreSQL       Redis
                            ingestor, OCR    Elasticsearch    Portal, Website
```

Speicher: lokales Verzeichnis und LVM-Thin auf derselben Maschine. Keine
Replikation, kein zweiter Knoten, kein Quorum.

Die Sicherung läuft täglich per restic auf zwei Hetzner Storage Boxen
(Frankfurt und Helsinki) — das ist die einzige Ebene, die heute bereits
standortübergreifend ausgelegt ist.

## 2. Ausfallpunkte, nach Tragweite geordnet

| # | Ausfallpunkt | Folge | Heutige Absicherung |
|---|---|---|---|
| **1** | **web01 als Ganzes** | **Alles** ist weg: mandari, Portal, Website, franzfon, electionhub | Nur Wiederherstellung aus der Sicherung, auf neuer Hardware |
| 2 | Lokaler Speicher (LVM-Thin) | Datenverlust seit der letzten Sicherung | Tägliche Sicherung → bis zu 24 h Verlust |
| 3 | PostgreSQL | mandari komplett nicht nutzbar | Keine Replik, kein Failover |
| 4 | Die Anwendungs-Container | mandari nicht erreichbar | Compose startet neu; ein Fehler im Image hilft das nicht |
| 5 | Caddy / HAProxy-Ingress | Nichts ist erreichbar, auch wenn alles läuft | Keine zweite Instanz |
| 6 | Redis | WebSockets und Live-Kollaboration fallen aus | Sitzungen überleben (`cached_db` fällt auf die Datenbank zurück) |
| 7 | Elasticsearch | Suche fällt aus, übriges Portal läuft | Einzelknoten |
| 8 | Medienverzeichnis | Anlagen nicht abrufbar | Teil der Sicherung |

**Punkt 1 überragt alles andere.** Solange ein einziger Rechner alles trägt, ist
jede Verbesserung an den Punkten 3 bis 8 eine Verfeinerung an der zweiten
Nachkommastelle. Auch ein geplanter Neustart des Hosts — für ein Kernel-Update
etwa — ist heute ein vollständiger Ausfall aller Dienste.

Ehrlich benannt: **Was heute existiert, ist keine Hochverfügbarkeit, sondern
Wiederanlauffähigkeit.** Das ist für die Größe angemessen, sollte aber so heißen
und nicht anders — insbesondere in Vergabeunterlagen.

## 3. Was die Anwendung bereits kann

Hier ist die Lage deutlich besser als die Infrastruktur vermuten lässt. mandari
ist fast zustandsfrei:

| Baustein | Zustand | Mehrere Instanzen möglich? |
|---|---|---|
| Sitzungen | `cached_db` über Redis | **ja**, gemeinsam genutzt |
| Cache | Redis | **ja** |
| WebSockets / Live-Kollaboration | `channels_redis` | **ja**, über Redis verteilt |
| Statische Dateien | ins Image gebaut (WhiteNoise) | **ja** |
| Datenbank | PostgreSQL, extern | **ja** |
| **Hochgeladene Dateien** | **lokales Verzeichnis** | **nein — der eine echte Blocker** |

Ein Helm-Chart liegt unter `deploy/kubernetes/helm/mandari` und kennt die
Einschränkung bereits: „ReadWriteMany erlaubt mehrere Anwendungs-Repliken. Kann
die Speicherklasse das nicht, muss `app.replicas` auf 1 bleiben."

**Es fehlt also im Wesentlichen ein Objektspeicher-Backend für Medien.**
`django-storages` ist derzeit keine Abhängigkeit.

### Dienste, die bewusst nur einmal laufen dürfen

Nicht alles soll skalieren:

- **Der Ingestor** darf **nicht** mehrfach laufen. Er ruft Ratsinformationssysteme
  fremder Kommunen ab; doppelte Abrufe verdoppeln die Last bei Stellen, die uns
  nicht gebeten haben, sie zu belasten. Köln hat unsere Adresse bereits mit einem
  Ratenlimit belegt. Bei mehreren Instanzen wäre eine Sperre über die Datenbank
  (`pg_advisory_lock`) nötig — heute gibt es keine.
- **Der Protokoll-Orchestrator** ist eine Schleife mit Zustand in der Datenbank;
  auch hier gilt: einmal, oder mit Sperre.

## 4. Stufenkonzept

Die Stufen sind so geschnitten, dass jede für sich einen Nutzen hat und auf der
vorherigen aufbaut. Keine verlangt, die nächste sofort mitzumachen.

### Stufe 1 — Wiederanlauf messen statt vermuten *(Tage, keine Kosten)*

Bevor irgendetwas gebaut wird: Wie lange dauert eine vollständige
Wiederherstellung auf frischer Hardware tatsächlich? Diese Zahl kennt heute
niemand.

- Wiederherstellung **üben**, mit Uhr: Datenbank, Medien, Konfiguration, Schlüssel
- **RTO und RPO** daraus ableiten und aufschreiben (RPO liegt heute bei bis zu 24 h)
- Prüfen, ob der Hauptschlüssel zur gesicherten Datenbank passt — ohne ihn ist die
  Sicherung wertlos (siehe [Kryptokonzept](KRYPTOKONZEPT.md))

Das ist Issue #229 und die Voraussetzung für jede belastbare Aussage gegenüber
Kommunen.

### Stufe 2 — Medien in einen Objektspeicher *(überschaubar, ermöglicht alles Weitere)*

Der eine Code-Schritt, der horizontale Skalierung überhaupt erst möglich macht.

- `django-storages` mit S3-kompatiblem Backend; als Gegenstelle genügt MinIO oder
  Garage auf eigener Hardware, es braucht keinen Hyperscaler
- `serve_media` und die geschützten Download-Views auf das Backend umstellen,
  die Zugriffsprüfung bleibt wie sie ist
- Bestehende Dateien einmalig übertragen
- Für Selbst-Hoster muss der lokale Pfad weiterhin funktionieren — eine Kommune
  mit einer Installation braucht keinen Objektspeicher

Nutzen auch ohne weitere Stufen: Die Sicherung der Medien wird vom Server
entkoppelt.

### Stufe 3 — Zweiter physischer Knoten *(die größte Risikominderung)*

Der Schritt, der Ausfallpunkt 1 auflöst.

- Zweiter Rechner, Proxmox-Cluster. Für ein Quorum bei zwei Knoten genügt ein
  **QDevice** auf einem kleinen dritten Gerät — kein dritter Server nötig
- **ZFS-Replikation** der VMs in kurzen Abständen (Minuten). Das ist deutlich
  einfacher zu betreiben als Ceph und für zwei Knoten angemessen
- Damit wird möglich: Live-Migration bei geplanten Arbeiten, Neustart eines Hosts
  ohne Ausfall, und im Störfall ein Anlauf auf dem zweiten Knoten mit wenigen
  Minuten Datenverlust statt Stunden

Das ist der Punkt mit dem besten Verhältnis von Aufwand zu Wirkung. Ein zweiter
Knoten kostet weniger als der Ausfall eines Sitzungstags bei einem Kunden.

### Stufe 4 — Anwendung mehrfach *(klein, sobald Stufe 2 steht)*

- `app.replicas` über 1, verteilt auf beide Knoten
- Caddy zweimal, davor der bestehende HAProxy-Ingress
- Ingestor und Orchestrator bleiben einfach — mit `pg_advisory_lock` abgesichert,
  damit ein versehentlicher Doppelstart nichts anrichtet

### Stufe 5 — Zustandsdienste ausfallsicher *(aufwendig, zuletzt)*

- **PostgreSQL:** Bei zwei Knoten ist **repmgr** oder **pg_auto_failover** die
  passende Wahl; Patroni braucht ein etcd-Quorum und lohnt erst in größeren
  Umgebungen. Dazu pgBouncer, damit die Anwendung den Wechsel nicht merkt
- **Redis:** Sentinel mit einem Beobachter auf dem QDevice
- **Elasticsearch:** zweiter Knoten. Geringste Dringlichkeit — fällt die Suche
  aus, bleibt das Portal nutzbar

## 5. Empfehlung

In dieser Reihenfolge:

1. **Stufe 1 jetzt.** Ohne gemessene Wiederherstellungszeit ist jede Zusage geraten.
2. **Stufe 3 als nächstes größeres Vorhaben.** Ein zweiter Knoten beseitigt den
   Ausfallpunkt, der alle anderen dominiert. Alles davor ist Kosmetik.
3. **Stufe 2 parallel**, wenn Entwicklungszeit frei ist — sie ist die Voraussetzung
   für Stufe 4 und nützt schon für sich.
4. **Stufe 5 erst, wenn es mehr Kunden gibt.** Datenbank-Failover ist aufwendig im
   Betrieb und bringt bei einem Ausfallpunkt, der ohnehin dahinter liegt, wenig.

Was ausdrücklich **nicht** empfohlen wird: mehrere Instanzen der Anwendung, solange
alles auf einer Maschine läuft. Das erhöht die Zahl der beweglichen Teile, ohne den
Ausfallpunkt zu beseitigen — und kann durch geteilte Medienverzeichnisse neue
Fehler schaffen.

## 6. Was das für Kunden bedeutet

Heutige, belegbare Aussage: tägliche Sicherung an zwei Standorten, Wiederanlauf
auf neuer Hardware möglich, Wiederherstellungszeit nach Stufe 1 beziffert.

Nach Stufe 3: geplante Arbeiten ohne Ausfall, ungeplanter Ausfall mit wenigen
Minuten statt Stunden.

Was heute **nicht** zugesagt werden kann und auch nicht sollte: unterbrechungsfreier
Betrieb, Verfügbarkeitsgarantien im Bereich 99,9 % oder darüber.
