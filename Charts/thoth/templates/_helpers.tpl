{{/*
Expand the name of the chart.
*/}}
{{- define "thoth.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this
(by the DNS naming spec). If the release name contains the chart name it is used
as the full name.
*/}}
{{- define "thoth.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "thoth.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "thoth.labels" -}}
helm.sh/chart: {{ include "thoth.chart" . }}
{{ include "thoth.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels (stable across template changes — used by every workload selector).
*/}}
{{- define "thoth.selectorLabels" -}}
app.kubernetes.io/name: {{ include "thoth.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Names of the chart-managed resources.
*/}}
{{- define "thoth.configmapName" -}}
{{- printf "%s-config" (include "thoth.fullname" .) }}
{{- end }}

{{- define "thoth.vaultPvcName" -}}
{{- printf "%s-vault" (include "thoth.fullname" .) }}
{{- end }}

{{- define "thoth.thothHomePvcName" -}}
{{- printf "%s-thoth-home" (include "thoth.fullname" .) }}
{{- end }}

{{- define "thoth.pg0PvcName" -}}
{{- printf "%s-hindsight-pg0" (include "thoth.fullname" .) }}
{{- end }}

{{- define "thoth.hindsightServiceName" -}}
{{- printf "%s-hindsight" (include "thoth.fullname" .) }}
{{- end }}

{{- define "thoth.mcpServiceName" -}}
{{- printf "%s-mcp" (include "thoth.fullname" .) }}
{{- end }}

{{/*
envFrom block shared by the thoth (non-hindsight) workloads: the non-secret
ConfigMap plus the user-provided Secret. Indented with `nindent 12` (containers
spec) by the caller.
*/}}
{{- define "thoth.envFrom" -}}
- configMapRef:
    name: {{ include "thoth.configmapName" . }}
- secretRef:
    name: {{ .Values.secretName }}
{{- end }}

{{/*
Volumes shared by the thoth (non-hindsight) workloads: the vault and the
thoth-home PVCs.
*/}}
{{- define "thoth.volumes" -}}
- name: vault
  persistentVolumeClaim:
    claimName: {{ include "thoth.vaultPvcName" . }}
- name: thoth-home
  persistentVolumeClaim:
    claimName: {{ include "thoth.thothHomePvcName" . }}
{{- end }}

{{/*
Volume mounts matching thoth.volumes; mount points come from the non-secret config.
*/}}
{{- define "thoth.volumeMounts" -}}
- name: vault
  mountPath: {{ .Values.config.pkmVault | quote }}
- name: thoth-home
  mountPath: {{ .Values.config.thothHome | quote }}
{{- end }}
