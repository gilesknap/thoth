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
Init container shared by every vault-mounting workload: clones the vault repo into
an empty vault PVC on first start (a no-op once the vault is a git repo, and when
config.vaultRepoUrl is unset). Same image/env/mounts as the main container so the
clone lands on the same vault path. Indented for `nindent 8` (deployment pod spec)
or `nindent 12` (cronjob pod spec) by the caller, like thoth.volumes.
*/}}
{{- define "thoth.initContainers" -}}
- name: vault-bootstrap
  image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  args: ["vault-bootstrap"]
  envFrom:
    {{- include "thoth.envFrom" . | nindent 4 }}
  volumeMounts:
    {{- include "thoth.volumeMounts" . | nindent 4 }}
  {{- with .Values.securityContext }}
  securityContext:
    {{- toYaml . | nindent 4 }}
  {{- end }}
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
