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
{{- with (include "mandari.elasticsearchUrl" .) }}
- name: ELASTICSEARCH_URL
  value: {{ . | quote }}
- name: ELASTICSEARCH_AUTO_INDEX
  value: "True"
{{- end }}
- name: OPARL_FILES_ROOT
  value: /app/files
{{- end -}}
