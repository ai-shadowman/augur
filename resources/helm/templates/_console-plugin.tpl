{{- define "codeUnderstanding.consolePluginHref" -}}
{{- $consoleRoute := lookup "route.openshift.io/v1" "Route" "openshift-console" "console" -}}
{{- if $consoleRoute -}}
{{- printf "https://%s/code-understanding" $consoleRoute.spec.host -}}
{{- else if .Values.consolePlugin.consoleBaseUrl -}}
{{- printf "%s/code-understanding" .Values.consolePlugin.consoleBaseUrl -}}
{{- else -}}
{{- printf "https://console-openshift-console.apps.%s/code-understanding" (.Values.clusterDomain | default "cluster.local") -}}
{{- end -}}
{{- end -}}
