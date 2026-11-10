{{- define "codeUnderstanding.pluginApiRouteHost" -}}
{{- if .Values.consolePlugin.apiRouteHost -}}
{{- .Values.consolePlugin.apiRouteHost | trimPrefix "https://" | trimPrefix "http://" -}}
{{- else -}}
{{- $route := lookup "route.openshift.io/v1" "Route" .Values.namespace "code-understanding-plugin-api" -}}
{{- if $route -}}
{{- $route.spec.host -}}
{{- else -}}
{{- $domain := .Values.clusterDomain | default "cluster.local" -}}
{{- if hasPrefix "apps." $domain -}}
{{- printf "code-understanding-plugin-api-%s.%s" .Values.namespace $domain -}}
{{- else -}}
{{- printf "code-understanding-plugin-api-%s.apps.%s" .Values.namespace $domain -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

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
