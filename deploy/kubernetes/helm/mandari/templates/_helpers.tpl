{{/*
Namen und wiederkehrende Bausteine.
*/}}

{{- define "mandari.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mandari.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "mandari.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "mandari.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "mandari.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mandari.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "mandari.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "mandari.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "mandari.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "mandari.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Vorhandenes Secret lesen, damit erzeugte Passwörter bei "helm upgrade" stabil bleiben.
Ohne diesen Kniff bekäme jede Installation neue Schlüssel – verschlüsselte Daten wären
danach unlesbar und die Datenbank-Anmeldung schlüge fehl.
*/}}
{{- define "mandari.existingSecretData" -}}
{{- $name := printf "%s-secrets" (include "mandari.fullname" .) -}}
{{- $found := lookup "v1" "Secret" .Release.Namespace $name -}}
{{- if $found -}}
{{- toYaml $found.data -}}
{{- end -}}
{{- end -}}

{{/*
Einzelnen Secret-Wert bestimmen: gesetzter Wert, sonst vorhandener, sonst neu erzeugt.
Aufruf: include "mandari.secretValue" (dict "ctx" $ "key" "secret-key" "value" .Values.secrets.secretKey "length" 50)
*/}}
{{- define "mandari.secretValue" -}}
{{- $ctx := .ctx -}}
{{- if .value -}}
{{- .value | b64enc -}}
{{- else -}}
{{- $name := printf "%s-secrets" (include "mandari.fullname" $ctx) -}}
{{- $found := lookup "v1" "Secret" $ctx.Release.Namespace $name -}}
{{- if and $found (hasKey $found.data .key) -}}
{{- index $found.data .key -}}
{{- else -}}
{{- randAlphaNum (.length | int) | b64enc -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Hauptschlüssel der Feldverschlüsselung (Eintrag encryption-key, ENCRYPTION_MASTER_KEY):
Base64 von genau 32 zufälligen Bytes. Gesetzter Wert, sonst vorhandener, sonst neu erzeugt.
Nicht über mandari.secretValue: 32 Buchstaben und Ziffern ergeben dekodiert nur 24 Byte, damit
verschlüsselt die Anwendung nichts. Ein solcher Wert bricht die Installation deshalb ab.
*/}}
{{- define "mandari.encryptionKey" -}}
{{- $key := .Values.secrets.encryptionKey -}}
{{- if not $key -}}
{{- $name := printf "%s-secrets" (include "mandari.fullname" .) -}}
{{- $found := lookup "v1" "Secret" .Release.Namespace $name -}}
{{- if and $found (hasKey $found.data "encryption-key") -}}
{{- $key = index $found.data "encryption-key" | b64dec -}}
{{- else -}}
{{- $key = randBytes 32 -}}
{{- end -}}
{{- end -}}
{{- if ne (len (b64dec $key)) 32 -}}
{{- fail "encryption-key muss Base64 von genau 32 Byte sein (erzeugen: openssl rand -base64 32). Siehe deploy/kubernetes/README.md, Abschnitt „Zugangsdaten sichern“." -}}
{{- end -}}
{{- $key | b64enc -}}
{{- end -}}

{{- define "mandari.postgresHost" -}}
{{- printf "%s-postgres" (include "mandari.fullname" .) -}}
{{- end -}}

{{- define "mandari.redisHost" -}}
{{- printf "%s-redis" (include "mandari.fullname" .) -}}
{{- end -}}

{{- define "mandari.elasticsearchUrl" -}}
{{- if .Values.elasticsearch.enabled -}}
{{- printf "http://%s-elasticsearch:9200" (include "mandari.fullname" .) -}}
{{- else -}}
{{- .Values.externalElasticsearch.url -}}
{{- end -}}
{{- end -}}

{{/*
Umgebung, die Anwendung, Ingestor und Migrations-Job gemeinsam brauchen.
*/}}
{{- define "mandari.commonEnv" -}}
- name: DEBUG
  value: "false"
- name: ALLOWED_HOSTS
  value: {{ printf "%s,localhost,127.0.0.1" .Values.domain | quote }}
- name: CSRF_TRUSTED_ORIGINS
  value: {{ printf "https://%s" .Values.domain | quote }}
- name: SITE_URL
  value: {{ printf "https://%s" .Values.domain | quote }}
- name: TZ
  value: {{ .Values.timezone | quote }}
- name: LOG_FORMAT
  value: {{ .Values.logging.format | quote }}
- name: LOG_LEVEL
  value: {{ .Values.logging.level | quote }}
# Schalter der Ereignistechnik je Erzeuger (Ingestor, Anwendung, Worker lesen denselben Wert)
- name: INGESTOR_EVENTS_ENABLED
  value: {{ .Values.events.producers | toString | quote }}
- name: OPARL_CHANGES_ENABLED
  value: {{ .Values.events.changesFeed | toString | quote }}
# Aufbewahrung des Journals (manage.py events_purge, Zeitplan im Worker nur wenn eingeschaltet)
- name: EVENTS_JOURNAL_PURGE_ENABLED
  value: {{ .Values.events.journalPurge | default false | toString | quote }}
- name: EVENTS_JOURNAL_RETENTION_DAYS
  value: {{ .Values.events.journalRetentionDays | default 90 | toString | quote }}
{{- if .Values.events.workerPushUrl }}
- name: WORKER_PUSH_URL
  value: {{ .Values.events.workerPushUrl | quote }}
{{- end }}
# DSGVO: nach einem redact personenbezogene Nutzlasten im Journal leeren (Auftrag im Worker)
- name: EVENTS_REDACT_NEUTRALIZE
  value: {{ .Values.events.redactNeutralize | default false | toString | quote }}
# Mailversand: Absender und Domain für Message-ID und EHLO (leer = Domain des Absenders, sonst die von domain)
{{- with .Values.mail }}
{{- if .fromEmail }}
- name: DEFAULT_FROM_EMAIL
  value: {{ .fromEmail | quote }}
{{- end }}
{{- if .messageIdDomain }}
- name: EMAIL_MESSAGE_ID_DOMAIN
  value: {{ .messageIdDomain | quote }}
{{- end }}
{{- end }}
{{- if .Values.tracing.otlpEndpoint }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .Values.tracing.otlpEndpoint | quote }}
- name: OTEL_SERVICE_NAME
  value: mandari-web
{{- end }}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "mandari.secretName" . }}
      key: database-url
- name: REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "mandari.secretName" . }}
      key: redis-url
- name: SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "mandari.secretName" . }}
      key: secret-key
- name: ENCRYPTION_MASTER_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "mandari.secretName" . }}
      key: encryption-key
# Nur während eines Schlüsselwechsels gesetzt (docs/KRYPTOKONZEPT.md)
- name: ENCRYPTION_MASTER_KEY_PREVIOUS
  valueFrom:
    secretKeyRef:
      name: {{ include "mandari.secretName" . }}
      key: encryption-key-previous
      optional: true
{{- with (include "mandari.elasticsearchUrl" .) }}
- name: ELASTICSEARCH_URL
  value: {{ . | quote }}
- name: ELASTICSEARCH_AUTO_INDEX
  value: "True"
{{- end }}
- name: OPARL_FILES_ROOT
  value: /app/files
# Dokumentablage und Texterkennung (docs/FILE_CACHE.md), für Anwendung, Worker und Ingestor gleich
- name: FILE_STORE_LAYOUT
  value: {{ .Values.files.storeLayout | quote }}
- name: INGESTOR_STORES_FILES
  value: {{ .Values.files.ingestorStoresFiles | toString | quote }}
- name: FILE_CACHE_MIN_FREE_GB
  value: {{ .Values.files.minFreeGb | toString | quote }}
- name: TEXT_EXTRACTION_RUNNER
  value: {{ .Values.files.textExtractionRunner | quote }}
# Live-Übertragungen (docs/LIVE_UEBERTRAGUNG.md); aus = keine Anfragen an Streaming-Anbieter
- name: LIVE_UEBERTRAGUNG_AKTIV
  value: {{ .Values.liveStreams.enabled | toString | quote }}
{{- end -}}

{{- define "mandari.appImage" -}}
{{ .Values.image.registry }}/{{ .Values.image.repository }}/mandari:{{ .Values.image.tag }}
{{- end -}}

{{- define "mandari.websiteImage" -}}
{{ .Values.image.registry }}/{{ .Values.image.repository }}/website:{{ .Values.image.tag }}
{{- end -}}

{{/*
Umgebung der Anwendung und der Worker: gemeinsame Umgebung, Texterkennung, app.extraEnv.
*/}}
{{- define "mandari.appEnv" -}}
{{ include "mandari.commonEnv" . }}
# Adressraum je Tesseract-Unterprozess (Import, Aufträge in ocr)
- name: OCR_MEMORY_LIMIT_MB
  value: {{ .Values.files.ocrMemoryLimitMb | toString | quote }}
{{- range $key, $value := .Values.app.extraEnv }}
- name: {{ $key }}
  value: {{ $value | quote }}
{{- end }}
{{- end -}}

{{/*
initContainer "wait-for-schema": wartet, bis alle Migrationen eingespielt sind, die vor dem Ausrollen
laufen dürfen (Installation: Job <release>-migrate; Upgrade: Hook <release>-migrate-pre mit
safemigrate). Migrationen, die django-safemigrate erst nach dem Ausrollen erlaubt
(Safe.after_deploy), spielt der Hook <release>-migrate-post ein; auf sie wartet niemand.
So starten Anwendung, Worker und Ingestor nie gegen ein zu altes Schema, auch nicht bei der
Erstinstallation mit "helm install --wait".
*/}}
{{- define "mandari.waitForSchema" -}}
{{- if .Values.migrationJob.enabled }}
- name: wait-for-schema
  image: {{ include "mandari.appImage" . | quote }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  securityContext:
    {{- toYaml .Values.securityContext | nindent 4 }}
  command: ["python", "manage.py", "shell", "-c"]
  args:
    - |
      import sys, time
      from django.db import connections
      from django.db.migrations.executor import MigrationExecutor
      from django_safemigrate.management.commands.safemigrate import Command

      frist = {{ .Values.migrationJob.waitTimeoutSeconds | int }}
      beginn = time.monotonic()
      letzte_meldung = None
      while True:
          try:
              executor = MigrationExecutor(connections["default"])
              plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
              pruefer = Command()
              erklaert = {migration: pruefer.safe(migration) for migration, _ in plan}
              bereit, verschoben, blockiert = pruefer.categorize(
                  pruefer.resolve(erklaert, pruefer.detected(erklaert))
              )
              if not bereit and not blockiert:
                  hinweis = f" ({len(verschoben)} Migrationen folgen nach dem Ausrollen)" if verschoben else ""
                  print("Datenbankschema bereit" + hinweis + ".", flush=True)
                  sys.exit(0)
              meldung = f"{len(bereit) + len(blockiert)} Migrationen stehen noch aus"
          except Exception as fehler:  # Datenbank noch nicht erreichbar, Tabellen fehlen
              meldung = f"Datenbank noch nicht bereit ({type(fehler).__name__})"
          connections.close_all()
          if meldung != letzte_meldung:
              print(f"Warte auf den Migrations-Job: {meldung}", flush=True)
              letzte_meldung = meldung
          if time.monotonic() - beginn > frist:
              print(f"Schema nach {frist} s nicht bereit. Migrations-Job prüfen: kubectl logs job/<release>-migrate", flush=True)
              sys.exit(1)
          time.sleep(5)
  env:
    {{- include "mandari.appEnv" . | nindent 4 }}
  {{- if .Values.app.extraEnvFromSecret }}
  envFrom:
    - secretRef:
        name: {{ .Values.app.extraEnvFromSecret }}
  {{- end }}
  resources:
    requests:
      cpu: 50m
      memory: 128Mi
    limits:
      memory: 512Mi
{{- end }}
{{- end -}}

{{/*
Umgebung der Marketing-Website (Image ghcr.io/mandarioss/website), auch für ihre Migrationen.
*/}}
{{- define "mandari.websiteEnv" -}}
{{- $host := default .Values.domain .Values.website.host -}}
- name: DEBUG
  value: "false"
- name: ALLOWED_HOSTS
  value: {{ printf "%s,localhost,127.0.0.1" $host | quote }}
- name: CSRF_TRUSTED_ORIGINS
  value: {{ printf "https://%s" $host | quote }}
- name: SITE_URL
  value: {{ printf "https://%s" $host | quote }}
- name: TZ
  value: {{ .Values.timezone | quote }}
# Das Image legt keinen Benutzer an; als UID ohne Eintrag in /etc/passwd fehlt sonst ein Heimatverzeichnis
- name: HOME
  value: /tmp
- name: MANDARI_API_URL
  value: {{ printf "http://%s:%v/api" (include "mandari.fullname" .) .Values.service.port | quote }}
- name: WEBSITE_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "mandari.secretName" . }}
      key: website-database-url
- name: WEBSITE_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "mandari.secretName" . }}
      key: website-secret-key
{{- end -}}
