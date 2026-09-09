# mandari in Kubernetes

Für einen einzelnen Server ist Docker Compose einfacher (`./install.sh` im Wurzelverzeichnis).
Kubernetes lohnt sich, wenn Sie ohnehin einen Cluster betreiben, mehrere Repliken brauchen
oder Datenbank und Cache als verwaltete Dienste nutzen wollen.

## Voraussetzungen

- Kubernetes 1.27 oder neuer, `kubectl` mit Zugriff auf den Cluster
- Eine Speicherklasse (`kubectl get storageclass`)
- Für den Zugriff aus dem Internet: ein Ingress-Controller und cert-manager.
  Der Installer bietet an, beide zu installieren, falls sie fehlen.
- Helm 3 — wird bei Bedarf vom Installer nachinstalliert

Bedarf: etwa 4 GB Arbeitsspeicher mit Elasticsearch, rund 2 GB ohne.

## Schnellstart

```bash
git clone https://github.com/mandariOSS/mandari.git
cd mandari
./install-k8s.sh
```

Der Installer fragt Domain, Namespace, Speicherklasse und Admin-Konto ab, installiert das
Chart und wartet, bis die Anwendung läuft. Ohne Rückfragen:

```bash
./install-k8s.sh --unattended \
  --domain ris.meine-kommune.de \
  --namespace mandari \
  --tag latest
```

Weitere Schalter: `--minimal` (ohne Elasticsearch), `--with-website`, `--dry-run` (zeigt nur
die Manifeste), `--storage-class`, `--uninstall`.

## Mit Helm direkt

```bash
helm upgrade --install mandari deploy/kubernetes/helm/mandari \
  --namespace mandari --create-namespace \
  --set domain=ris.meine-kommune.de \
  --set adminUser.email=admin@meine-kommune.de \
  --set adminUser.password='EinLangesPasswort' \
  --wait
```

## Ohne Helm

```bash
kubectl create namespace mandari
# In deploy/kubernetes/manifests/mandari.yaml alle Werte "BITTE-ERSETZEN-*" und die
# Domain anpassen, dann:
kubectl kustomize deploy/kubernetes/manifests | kubectl apply -f -
kubectl -n mandari apply -f deploy/kubernetes/manifests/job-migrate.yaml
```

Die Manifeste sind aus dem Chart erzeugt. Für Updates ist Helm deutlich bequemer.

## Wichtige Werte

| Wert | Vorgabe | Bedeutung |
|------|---------|-----------|
| `domain` | – | Adresse der Installation, Pflichtangabe |
| `image.tag` | `latest` | Version aller drei Images; in Produktion fest setzen |
| `app.replicas` | `1` | Mehr als eine Replik braucht `persistence.accessMode: ReadWriteMany` |
| `postgres.enabled` | `true` | Auf `false` bei verwalteter Datenbank, dann `externalDatabase.url` setzen |
| `redis.enabled` | `true` | Analog mit `externalRedis.url` |
| `elasticsearch.enabled` | `true` | Aus: Suche läuft über die Datenbank, spart etwa 2 GB Arbeitsspeicher |
| `website.enabled` | `false` | Marketing-Website (Wagtail) mitinstallieren |
| `ingestor.syncInterval` | `15` | Minuten zwischen zwei OParl-Synchronisationen |
| `persistence.files.size` | `50Gi` | Heruntergeladene RIS-Dokumente – wächst mit der Zahl der Kommunen |
| `secrets.existingSecret` | `""` | Eigenes Secret statt erzeugter Schlüssel |
| `adminUser.email` / `.password` | `""` | Legt beim ersten Lauf ein Administrationskonto an |
| `logging.format` | `json` | `json` oder `text` |
| `tracing.otlpEndpoint` | `""` | Gesetzt: OpenTelemetry aktiv |
| `networkPolicy.enabled` | `false` | Schränkt den Zugriff auf Datenbank, Cache und Suchindex ein |

Vollständige Liste: `deploy/kubernetes/helm/mandari/values.yaml`. Vorlagen für kleine und
große Installationen: `values-minimal.yaml`, `values-production.yaml`.

## Zugangsdaten sichern

Beim ersten Installieren erzeugt das Chart Schlüssel und Passwörter. Sie bleiben bei
Upgrades erhalten, weil das Chart das vorhandene Secret ausliest.

```bash
kubectl -n mandari get secret mandari-secrets -o yaml > mandari-secrets-backup.yaml
```

Der Eintrag `encryption-key` verschlüsselt Fachdaten (Protokolle, Anträge, personenbezogene
Felder). Geht er verloren, sind diese Daten unwiederbringlich unlesbar. Die Sicherung gehört
in einen Passwortspeicher, nicht in die Versionsverwaltung.

Eigene Schlüssel vorgeben statt erzeugen lassen:

```bash
kubectl -n mandari create secret generic mandari-eigene-schluessel \
  --from-literal=secret-key="$(openssl rand -base64 48)" \
  --from-literal=encryption-key="$(openssl rand -base64 32)" \
  --from-literal=website-secret-key="$(openssl rand -base64 48)" \
  --from-literal=postgres-password="$(openssl rand -base64 24)" \
  --from-literal=redis-password="$(openssl rand -base64 24)" \
  --from-literal=database-url="postgresql://..." \
  --from-literal=website-database-url="postgresql://..." \
  --from-literal=redis-url="redis://..."
helm upgrade --install mandari deploy/kubernetes/helm/mandari -n mandari \
  --set secrets.existingSecret=mandari-eigene-schluessel --set domain=…
```

## Betrieb

**Aktualisieren.** Migrationen laufen automatisch als Job, bevor die neuen Pods starten:

```bash
helm upgrade mandari deploy/kubernetes/helm/mandari -n mandari \
  --reuse-values --set image.tag=v1.2.3 --wait
```

**Datenbank sichern.**

```bash
kubectl -n mandari exec statefulset/mandari-postgres -- \
  pg_dump -U mandari mandari | gzip > mandari-$(date +%F).sql.gz
```

**Dateien sichern.** Die PersistentVolumeClaims `mandari-media` und `mandari-files` über die
Sicherungslösung des Clusters einbeziehen (z. B. Velero) oder aus einem Pod heraus kopieren:

```bash
kubectl -n mandari cp mandari-<pod>:/app/media ./media-backup
```

**Logs.**

```bash
kubectl -n mandari logs -f deploy/mandari
kubectl -n mandari logs -f deploy/mandari-ingestor
kubectl -n mandari logs job/mandari-migrate
```

**Verwaltungsbefehle.**

```bash
kubectl -n mandari exec -it deploy/mandari -- python manage.py createsuperuser
kubectl -n mandari exec -it deploy/mandari -- python manage.py sync_oparl --full
```

**Skalieren.** Mehr Repliken der Anwendung brauchen eine Speicherklasse mit `ReadWriteMany`
(NFS, CephFS, Longhorn, EFS). Der Ingestor bleibt bewusst bei einer Instanz, damit Quellen
nicht doppelt abgefragt werden.

## Deinstallieren

```bash
./install-k8s.sh --uninstall --namespace mandari
```

Daten (PersistentVolumeClaims) und das Secret bleiben absichtlich erhalten. Vollständig
entfernen — nicht umkehrbar:

```bash
kubectl delete namespace mandari
```

## Fehlersuche

| Beobachtung | Ursache und Abhilfe |
|---|---|
| Pods bleiben `Pending` | Keine passende Speicherklasse oder zu wenig Ressourcen: `kubectl -n mandari describe pod <name>` |
| Migrations-Job schlägt fehl | Datenbank nicht erreichbar oder Zugangsdaten passen nicht: `kubectl -n mandari logs job/mandari-migrate` |
| Anwendung startet nicht (`CrashLoopBackOff`) | Meist die Datenbankverbindung: `kubectl -n mandari logs deploy/mandari` |
| Kein Zertifikat | cert-manager oder ClusterIssuer fehlt: `kubectl describe certificate -n mandari` |
| 502 vom Ingress | Anwendung noch nicht bereit: `kubectl -n mandari get pods` |
| Elasticsearch startet nicht | Zu wenig Arbeitsspeicher; `elasticsearch.javaOpts` senken oder `elasticsearch.enabled=false` |

Ohne Ingress lässt sich die Installation direkt erreichen:

```bash
kubectl -n mandari port-forward svc/mandari 8080:80
```

Weitere Dokumentation: <https://docs.mandari.de>
