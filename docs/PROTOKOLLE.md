# Protokolle: Aufbewahrung, Ablage, Sicherung

Was mandari protokolliert, wie lange die Protokolle vorgehalten werden, wo sie liegen
und wie sie gesichert und überwacht werden. Grundlage: BSI IT-Grundschutz OPS.1.1.5
(Protokollierung), Anforderungen A6 (zentrale Protokollierung, Dimensionierung), A8
(Archivierung, gesetzliche Fristen), A10 (Zugriffsschutz) sowie CON.10.A17
(Überlauf überwachen). Issue #263.

Leitgedanke: **so lange wie nötig, nicht wie möglich.** Protokolle enthalten
IP-Adressen und Kennungen; jede Frist unten ist zugleich eine Löschfrist.

## 1. Protokollarten und Aufbewahrung

| Protokoll | Inhalt | Aufbewahrung | Wo | Begründung |
|---|---|---|---|---|
| **Anwendungs- und Container-Logs** | Strukturierte JSON-Zeilen der Anwendung (Anfrage-Kennung, keine personenbezogenen Inhalte), Ingestor, Postgres, Elasticsearch, Redis, Website | **90 Tage**, höchstens 2 GB | systemd-Journal des Hosts (`journalctl CONTAINER_NAME=…`) | Sicherheitsvorfälle werden oft erst Wochen später erkannt; 90 Tage decken die Frage „seit wann?“ ab, ohne Bewegungsprofile über Monate zu bilden |
| **Zugriffslogs des Reverse Proxy** | Caddy-Zugriffslog je Site (IP, Pfad, Status, Zeit) | **14 Tage** | Datei im Caddy-Datenvolume (`/data/*-access.log`), zeitlich rollierend | IP-Adressen; nötig für Störungsanalyse und Missbrauchserkennung (Art. 6 Abs. 1 lit. f DSGVO), danach ohne Zweck |
| **Fachliches Audit-Log** (Verwaltungs-RIS) | Wer hat wann was geändert; nicht änder- oder löschbar im Admin | **je Mandant** (`audit_years`, Einstellungen → Datenschutz), Empfehlung 5–10 Jahre | Datenbank, in jeder Sicherung | Nachweis für Gremienarbeit; Frist folgt Archiv- und Aufbewahrungsvorschriften der Kommune (`docs/DSGVO_LOESCHKONZEPT.md`) |
| **Logdateien der Cron-Jobs** | Ausgaben von Erinnerungen, Fotos, Löschläufen, Backup-Watchdog (`/var/log/mandari-*.log`) | **12 Wochen**, komprimiert | Host, `logrotate` | Nur Zahlen und Statusmeldungen; für die Nachvollziehbarkeit geplanter Läufe |
| **Backup-Statusdateien** | Erfolg, Dauer, Prüfergebnisse je Lauf | letzter Stand je Lauf | `status/` des Backup-Stacks | Grundlage der Watchdog-Mail (`docs/BACKUP.md`) |

Nicht protokolliert werden Inhalte von Dokumenten, Passwörter, Tokens oder Mailtexte.

## 2. Ablage: journald statt Docker-JSON-Dateien

Docker schreibt Container-Ausgaben standardmäßig in JSON-Dateien im
Container-Verzeichnis, begrenzt nach Größe (`max-size`, `max-file`). Zwei Schwächen:
Die Dateien verschwinden, sobald der Container beim Deploy neu erstellt wird, und je
nach Last ist die Grenze nach wenigen Tagen erreicht.

Deshalb laufen die Container-Logs auf Produktionshosts über den Treiber `journald`
(`deploy/logging/docker-compose.journald.yml`). Das Journal liegt auf dem Host, hält
nach **Zeit** vor (`deploy/logging/journald-mandari.conf`: 90 Tage, höchstens 2 GB,
komprimiert, persistent) und übersteht jede Neuerstellung eines Containers.

```bash
journalctl CONTAINER_NAME=mandari -S -1h          # letzte Stunde der Anwendung
journalctl CONTAINER_NAME=ingestor -p warning     # Warnungen und Fehler des Ingestors
journalctl CONTAINER_NAME=mandari -o cat | grep '"trace":"abc123"'   # eine Anfrage verfolgen
journalctl --disk-usage                           # Belegung
docker logs --since 10m mandari                   # funktioniert weiterhin
```

`docker logs` und `docker compose logs` arbeiten mit journald unverändert. Der
Treiber ist nur auf Linux-Hosts mit systemd verfügbar; für lokale Entwicklung bleibt
`json-file` aus der Basis-Compose.

## 3. Sicherung

`/var/log/journal` wird als `/source/journal` in die tägliche restic-Sicherung
eingehängt (`BACKUP_SRC_JOURNAL`, `docs/BACKUP.md`); das Caddy-Datenvolume mit den
Zugriffslogs war bereits Teil der Sicherung. Damit liegen Protokolle verschlüsselt an
zwei Standorten und überstehen einen Serververlust. Die Aufbewahrung in der Sicherung
(30 Tage) ist kürzer als die Vorhaltezeit auf dem Host; die Sicherung dient dem
Wiederanlauf, nicht der Archivierung.

## 4. Zugriffsschutz (A10)

- **Systemprotokolle:** Das Journal ist nur für `root` und die Gruppe
  `systemd-journal` lesbar. Eine nachträgliche unbemerkte Veränderung setzt
  Root-Rechte auf dem Host voraus; wer sie hat, kontrolliert ohnehin alles.
  Entscheidung: **keine kryptografische Versiegelung der Systemprotokolle.** Der
  Schutz besteht aus Betriebssystemrechten plus der täglichen, verschlüsselten
  Offsite-Kopie, gegen die sich ein manipulierter Stand vergleichen lässt. Wer mehr
  braucht, kann journald-Versiegelung (Forward Secure Sealing, `journalctl --setup-keys`,
  `Seal=yes`) aktivieren; das Verifikationsgeheimnis gehört dann außerhalb des Hosts
  verwahrt.
- **Fachliches Audit-Log:** unveränderlich im Admin; Prüfexport und
  Manipulationssicherheit sind Gegenstand von #221.
- **Zugriffslogs:** liegen im Caddy-Volume (root), keine Weitergabe an Dritte.

## 5. Überwachung des Überlaufs (CON.10.A17)

`deploy/logging/journal-alert.sh` läuft stündlich per Cron und schickt eine Mail (über
den mandari-Container), sobald das Journal 90 % seiner Obergrenze belegt. Ab der
Obergrenze kürzt journald nach Größe statt nach Zeit, die zugesagten 90 Tage gelten
dann nicht mehr. Die Mail nennt Belegung, Obergrenze und den ältesten Eintrag; höchstens
eine Mail je sechs Stunden. Die Root-Platte überwacht daneben der vorhandene
Disk-Alarm (`docs/MONITORING.md`).

## 6. Einrichtung

```bash
sudo sh deploy/logging/install.sh mandari admin@example.org   # journald, logrotate, Cron
docker compose -f docker-compose.yml -f deploy/logging/docker-compose.journald.yml up -d
```

Caddy: in jedem `log { output file … }`-Block `roll_keep_for 336h` (14 Tage) setzen,
danach `docker exec caddy caddy reload --config /etc/caddy/Caddyfile`. Bei einer von Hand
gepflegten Compose (andere Dienstnamen) den Override entsprechend anpassen.

## 7. Zusammenfassung des Abgleichs

| Anforderung | Stand |
|---|---|
| A6 zentrale Protokollierung, ausreichend dimensioniert | journald auf dem Host, 90 Tage / 2 GB, Zugriffslogs 14 Tage |
| A8 Archivierung nach Fristen | Fristen je Protokollart festgelegt und begründet (Abschnitt 1) |
| A10 Zugriffsschutz | Betriebssystemrechte, Offsite-Kopie; Versiegelung optional dokumentiert |
| CON.10.A17 Überlauf | stündliche Warnung ab 90 % der Obergrenze |
| Sicherung | Journal und Caddy-Volume in der täglichen restic-Sicherung |
