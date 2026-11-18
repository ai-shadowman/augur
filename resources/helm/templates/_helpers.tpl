{{- define "agent-mesh.imageStreamName" -}}
{{- $name := printf "%s-%s" .base (.root.Values.namespace | default .root.Release.Namespace) -}}
{{- if gt (len $name) 63 -}}
{{- printf "%s-%s" ($name | trunc 54 | trimSuffix "-") ($name | sha256sum | trunc 8) -}}
{{- else -}}
{{- $name -}}
{{- end -}}
{{- end -}}
