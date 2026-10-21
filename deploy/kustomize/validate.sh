#!/usr/bin/env bash
# Build every overlay, plus one build with every optional component, and validate the
# output with kubeconform against the Kubernetes and CRD (ServiceMonitor, HTTPRoute) schemas.
#
#   deploy/kustomize/validate.sh                     # kustomize and kubeconform on PATH
#   KUSTOMIZE=/path/kustomize KUBECONFORM=/path/kubeconform deploy/kustomize/validate.sh
#
# KUBERNETES_VERSION selects the schema version (default below); the schemas are downloaded
# from GitHub, so the script needs network access.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
kustomize="${KUSTOMIZE:-kustomize}"
kubeconform="${KUBECONFORM:-kubeconform}"
k8s_version="${KUBERNETES_VERSION:-1.31.0}"
# CRD schemas (ServiceMonitor, HTTPRoute) from a fixed commit of the datree CRDs catalog.
crds_ref="${CRDS_CATALOG_REF:-501527ab9c40c827144c8f45945b25a023e344ce}"

# Inside this folder: kustomize refuses absolute paths to bases and components.
work="$(mktemp -d "$here/.validate.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# Every component on top of the base, so components that no overlay enables are checked
# too (ingress and gateway-api are alternatives, but both build together).
cat > "$work/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: autoair
resources:
  - ../base
images:
  - name: ghcr.io/ctolon/autoair
    newTag: "0.0.0-validate"
components:
  - ../components/postgresql
  - ../components/git-target
  - ../components/validator
  - ../components/ai-service
  - ../components/mcp-http
  - ../components/ingress
  - ../components/gateway-api
  - ../components/hpa
  - ../components/network-policies
  - ../components/service-monitor
EOF

builds=(overlays/dev overlays/production overlays/airflow-plugin base "$work")
for target in "${builds[@]}"; do
  dir="$target"
  [[ "$dir" = /* ]] || dir="$here/$target"
  out="$work/$(basename "$target").out.yaml"
  echo "== kustomize build $target"
  "$kustomize" build "$dir" > "$out"
  "$kubeconform" -strict -summary \
    -kubernetes-version "$k8s_version" \
    -schema-location default \
    -schema-location "https://raw.githubusercontent.com/datreeio/CRDs-catalog/$crds_ref/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" \
    "$out"
done

# One image tag for every build: the base and the overlays repeat it.
tags="$(grep -h -A1 'name: ghcr.io/ctolon/autoair$' "$here/base/kustomization.yaml" \
  "$here"/overlays/*/kustomization.yaml | grep -o 'newTag: .*' | sort -u)"
if [ "$(printf '%s\n' "$tags" | wc -l)" -ne 1 ]; then
  echo "ghcr.io/ctolon/autoair tags differ between base and overlays:" >&2
  printf '%s\n' "$tags" >&2
  exit 1
fi
echo "image tag: $tags"
