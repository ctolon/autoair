{{- define "autoair-validator.name" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "autoair-validator.selector" -}}
app.kubernetes.io/name: autoair-validator
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "autoair-validator.labels" -}}
{{ include "autoair-validator.selector" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/component: validator
app.kubernetes.io/part-of: autoair
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}
